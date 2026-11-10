"""Tests for Neon/Vercel PostgreSQL persistence adapter, URL sanitization, and GTFS city enrichment subtask."""

import io
import sqlite3
import zipfile
from datetime import datetime
from types import SimpleNamespace
import pytest

import circumorari_bot.data.db as db_mod
from circumorari_bot.config import sanitize_postgres_url, settings
from circumorari_bot.data.city_resolver import enrich_stops_batch, resolve_stop_city
from circumorari_bot.data.db import (
    _PgCompatConnection,
    _PgCompatRow,
    _convert_qmark_to_pyformat,
    get_db,
    get_persistence_status,
)
from circumorari_bot.data.gtfs_loader import load_gtfs_from_zip
from circumorari_bot.data.users_repo import UsersRepository
from circumorari_bot.core.models import NewsItem
from circumorari_bot.services.news_service import NewsService


def test_sanitize_postgres_url_vercel_and_neon():
    """Verify Prisma/Vercel query params are stripped and SSL is enforced on remote Neon hosts."""
    vercel_prisma_url = (
        "postgres://neondb_owner:secret123@ep-cool-cloud-pooler.eu-central-1.aws.neon.tech/neondb"
        "?pgbouncer=true&connection_limit=15&sslmode=require"
    )
    cleaned = sanitize_postgres_url(vercel_prisma_url)
    assert cleaned == (
        "postgresql://neondb_owner:secret123@ep-cool-cloud-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    )

    # Remote host without explicit sslmode gets sslmode=require automatically
    no_ssl_url = "postgres://u:p@ep-test.neon.tech/dbname?supa=base-pooler.x"
    cleaned2 = sanitize_postgres_url(no_ssl_url)
    assert cleaned2 == "postgresql://u:p@ep-test.neon.tech/dbname?sslmode=require"

    # Localhost does not force sslmode=require
    local_url = "postgresql://postgres:postgres@localhost:5432/circumorari_test"
    assert sanitize_postgres_url(local_url) == local_url

    assert sanitize_postgres_url("") is None
    assert sanitize_postgres_url("sqlite:///circumorari_lines.db") is None


def test_qmark_to_pyformat_conversion():
    """Verify SQLite '?' placeholders convert to '%s' without corrupting string literals or '%'."""
    sql = "SELECT * FROM users WHERE username LIKE ? AND first_name != 'Why? ''test''' AND chat_id = ?"
    converted = _convert_qmark_to_pyformat(sql)
    assert converted == (
        "SELECT * FROM users WHERE username LIKE %s AND first_name != 'Why? ''test''' AND chat_id = %s"
    )

    sql_pct = "SELECT * FROM stops WHERE normalized_name LIKE '%SANTA%' AND city = ?"
    assert _convert_qmark_to_pyformat(sql_pct) == (
        "SELECT * FROM stops WHERE normalized_name LIKE '%SANTA%' AND city = %s"
    )


def test_pg_compat_row_dict_and_positional_access():
    """Verify _PgCompatRow behaves like sqlite3.Row AND dict simultaneously."""
    dt = datetime(2026, 11, 8, 15, 30, 0)
    row = _PgCompatRow(["chat_id", "username", "last_active"], ("12345", "leo", dt))
    assert row[0] == "12345"
    assert row["chat_id"] == "12345"
    assert row.get("username") == "leo"
    assert row["last_active"] == "2026-11-08 15:30:00"
    assert dict(row) == {
        "chat_id": "12345",
        "username": "leo",
        "last_active": "2026-11-08 15:30:00",
    }


class _FakePsycopgCursor:
    """Simulates a psycopg v3 cursor backed by a shared persistent SQLite file."""

    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._cur = sqlite_conn.cursor()
        self.description = None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, pg_sql: str, params=None):
        norm_sql = pg_sql.strip()
        # Emulate PostgreSQL 'ADD COLUMN IF NOT EXISTS' on SQLite
        if "ADD COLUMN IF NOT EXISTS" in norm_sql.upper():
            sql_try = norm_sql.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
            try:
                self._cur.execute(sql_try)
            except sqlite3.OperationalError:
                pass
            self.description = None
            return self

        # Verify that _PgCompatCursor properly translated '?' -> '%s' and escaped '%' -> '%%'
        assert "?" not in norm_sql.replace("'Why?'", "")
        sqlite_sql = norm_sql.replace("%s", "?").replace("%%", "%")
        if params:
            self._cur.execute(sqlite_sql, params)
        else:
            self._cur.execute(sqlite_sql)
        if self._cur.description:
            self.description = [SimpleNamespace(name=d[0]) for d in self._cur.description]
        else:
            self.description = None
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        self._cur.close()


class _FakePsycopgConnection:
    def __init__(self, backing_path):
        self._conn = sqlite3.connect(backing_path)

    def cursor(self):
        return _FakePsycopgCursor(self._conn)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def test_postgres_compat_layer_survives_serverless_restarts(tmp_path, monkeypatch):
    """Simulate Vercel Serverless cold starts with Neon Postgres and verify users & news never reset."""
    remote_pg_backing = tmp_path / "simulated_neon_postgres.sqlite"

    import psycopg

    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda url, **kwargs: _FakePsycopgConnection(remote_pg_backing),
    )
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgres://user:pass@ep-circumorari.eu-central-1.aws.neon.tech/neondb?pgbouncer=true",
    )
    monkeypatch.setenv("VERCEL", "1")
    db_mod._PG_SCHEMA_INITIALIZED_URL = None

    # Status should report permanent PostgreSQL storage even on Vercel
    p_status = get_persistence_status()
    assert p_status["backend"] == "postgresql"
    assert p_status["permanent"] is True

    # Container #1 (Day 1): Two users interact with bot + news cron caches an article
    repo1 = UsersRepository()
    repo1.register_or_update(chat_id=1001, username="admin_leo", first_name="Leo")
    repo1.register_or_update(chat_id=2002, username="viaggiatore_ct", first_name="Marco")
    repo1.subscribe_user(chat_id=2002, username="viaggiatore_ct")
    repo1.set_ai_allowed(chat_id=2002, allowed=True)

    news_svc = NewsService()
    seed_item = NewsItem(
        guid="https://www.circumetnea.it/news/avviso-seed/",
        title="Avviso Iniziale",
        link="https://www.circumetnea.it/news/avviso-seed/",
        published_date="2026-11-07",
    )
    new_item = NewsItem(
        guid="https://www.circumetnea.it/news/avviso-test-1/",
        title="Avviso Test Metro",
        link="https://www.circumetnea.it/news/avviso-test-1/",
        published_date="2026-11-08",
    )
    # Initial seed on empty news_cache
    monkeypatch.setattr(news_svc, "fetch_news", lambda limit=10: [seed_item])
    assert news_svc.check_for_new_articles() == []

    # New announcement arrives on Day 1 -> detected & stored in Postgres
    monkeypatch.setattr(news_svc, "fetch_news", lambda limit=10: [new_item, seed_item])
    unseen_day1 = news_svc.check_for_new_articles()
    assert len(unseen_day1) == 1
    assert unseen_day1[0].guid == new_item.guid

    # Simulate overnight Vercel Serverless scale-to-zero / cold start:
    # reset in-process schema cache completely!
    db_mod._PG_SCHEMA_INITIALIZED_URL = None

    # Container #2 (Day 2): Both users and news_cache MUST still be intact!
    repo2 = UsersRepository()
    stats = repo2.get_stats()
    assert stats["total_users"] == 2
    assert stats["subscribed_users"] == 1

    u2 = repo2.get_user("viaggiatore_ct")
    assert u2 is not None
    assert u2["chat_id"] == "2002"
    assert u2["is_subscribed"] == 1
    assert u2["ai_allowed"] == 1

    # Cron on Day 2 sees 0 unseen articles (news_cache persisted across cold start!)
    assert news_svc.check_for_new_articles() == []

    # Verify prune_inactive preserves subscribed & AI-whitelisted users even if >90 days old
    with db_mod.get_persistent_db() as conn:
        conn.execute("UPDATE users SET last_active = '2020-01-01 00:00:00'")
    pruned = repo2.prune_inactive(days=90)
    # User 1001 (unsubscribed & ai_allowed=0) is pruned; User 2002 (subscribed & ai_allowed=1) is preserved!
    assert pruned == 1
    assert repo2.get_user(2002) is not None
    assert repo2.get_user(1001) is None


def test_gtfs_city_enrichment_subtask(tmp_path):
    """Verify GTFS sync runs the municipality enrichment subtask and populates stops.city."""
    # Direct unit check of coordinate + name resolver
    assert resolve_stop_city("Parcheggio Santa Sofia", 37.5293, 15.0688) == "Catania"
    assert resolve_stop_city("Piano Tavola", 37.5515, 14.9785) == "Belpasso"

    # Build minimal GTFS archive and load into fresh temp SQLite DB
    gtfs_zip = tmp_path / "sample_gtfs.zip"
    test_db = tmp_path / "sample_circumorari.db"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "METRO,METRO,Metropolitana di Catania,1\n"
            "T1,FCE,Catania - Randazzo,2\n",
        )
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "S1,Parcheggio Santa Sofia,37.5293,15.0688\n"
            "S2,STESICORO,37.5076,15.0836\n"
            "S3,RANDAZZO,37.8794,14.9479\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id\n"
            "METRO,WKD,TR1,Stesicoro,M1,0\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "TR1,08:00:00,08:00:00,S1,1\n"
            "TR1,08:10:00,08:10:00,S2,2\n",
        )
    gtfs_zip.write_bytes(buf.getvalue())

    s_cnt, t_cnt, st_cnt = load_gtfs_from_zip(buf.getvalue(), db_path=test_db)
    assert (s_cnt, t_cnt, st_cnt) == (3, 1, 2)

    with get_db(test_db) as conn:
        rows = conn.execute("SELECT stop_id, city, normalized_city FROM stops ORDER BY stop_id").fetchall()
        by_id = {r["stop_id"]: (r["city"], r["normalized_city"]) for r in rows}

    assert by_id["S1"] == ("Catania", "CATANIA")
    assert by_id["S2"] == ("Catania", "CATANIA")
    assert by_id["S3"] == ("Randazzo", "RANDAZZO")


def test_all_production_stops_have_city_enriched():
    """Verify bundled circumorari_lines.db has municipality enriched for every stop."""
    with get_db() as conn:
        total = conn.execute("SELECT count(*) FROM stops").fetchone()[0]
        missing = conn.execute("SELECT count(*) FROM stops WHERE city IS NULL OR city = ''").fetchone()[0]
        distinct_cities = conn.execute("SELECT count(DISTINCT city) FROM stops WHERE city != ''").fetchone()[0]

    assert total >= 580
    assert missing == 0
    assert distinct_cities >= 20
