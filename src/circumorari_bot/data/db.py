"""Database initialization and connection management (SQLite GTFS + Neon/Vercel Postgres persistence)."""

import logging
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Iterable, List, Optional

from circumorari_bot.config import settings

logger = logging.getLogger(__name__)

# Track whether PostgreSQL / SQLite persistent tables have been initialized in this process
_PG_SCHEMA_INITIALIZED_URL: Optional[str] = None
_SQLITE_PERSISTENT_INITIALIZED_PATHS: set[str] = set()


def normalize_stop_name(s: str) -> str:
    """Normalize station and city names for robust fuzzy and exact matching."""
    if not s:
        return ""
    s = s.upper()
    s = (
        s.replace("P.ZZA", "PIAZZA")
        .replace("Ù", "U")
        .replace("Ò", "O")
        .replace("È", "E")
        .replace("É", "E")
        .replace("À", "A")
        .replace("Ì", "I")
    )
    s = s.replace("’", "").replace("'", "")
    # Remove punctuation and parentheses
    s = re.sub(r"[().,\-–_/\\]", " ", s)
    # Remove extra spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_serverless_env() -> bool:
    return bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def get_db_path() -> Path:
    """Return GTFS SQLite database path, ensuring writable copy in /tmp on serverless environments."""
    if _is_serverless_env():
        tmp_db = Path("/tmp/circumorari_lines.db")
        if not tmp_db.exists():
            orig_db = settings.DATABASE_PATH
            if orig_db.exists():
                try:
                    shutil.copy2(orig_db, tmp_db)
                except Exception:
                    pass
        if tmp_db.exists():
            return tmp_db

    path = settings.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_persistence_status() -> dict:
    """Report active database persistence mode for users and news_cache."""
    pg_url = settings.postgres_url
    if pg_url:
        return {
            "backend": "postgresql",
            "permanent": True,
            "label": "🟢 PostgreSQL Permanente (Neon / Vercel)",
        }
    if _is_serverless_env():
        return {
            "backend": "sqlite_ephemeral",
            "permanent": False,
            "label": "⚠️ SQLite Temporaneo (/tmp) — configura Neon Postgres su Vercel Storage per salvataggio permanente",
        }
    return {
        "backend": "sqlite_local",
        "permanent": True,
        "label": "🟢 SQLite Locale Permanente",
    }


@contextmanager
def get_db(path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional SQLite connection for GTFS transit tables (and local fallback)."""
    target_path = path or get_db_path()
    conn = sqlite3.connect(target_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==========================================
# PostgreSQL Compatibility Wrapper (Neon / Vercel Postgres)
# ==========================================

def _convert_qmark_to_pyformat(sql: str) -> str:
    """Translate SQLite '?' parameter placeholders to psycopg '%s' outside string literals."""
    out: List[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "%" and not in_single and not in_double:
            out.append("%%")
        elif ch == "'" and not in_double:
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class _PgCompatRow:
    """Row wrapper supporting both dict key lookup (`row['chat_id']`, `dict(row)`) and positional `row[0]`."""

    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols: List[str], vals: tuple):
        normalized_vals = tuple(
            v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
            for v in vals
        )
        self._cols = cols
        self._vals = normalized_vals
        self._map = dict(zip(cols, normalized_vals))

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._vals[key]
        return self._map[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._map.get(key, default)

    def keys(self) -> List[str]:
        return list(self._cols)

    def items(self):
        return self._map.items()

    def values(self):
        return self._map.values()

    def __iter__(self):
        return iter(self._cols)

    def __len__(self) -> int:
        return len(self._vals)

    def __contains__(self, item: object) -> bool:
        return item in self._map


class _PgCompatCursor:
    """DB-API cursor adapter unifying sqlite3 and psycopg v3 cursor ergonomics."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PgCompatCursor":
        pg_sql = _convert_qmark_to_pyformat(sql)
        param_tuple = tuple(params) if params else None
        if param_tuple:
            self._cur.execute(pg_sql, param_tuple)
        else:
            self._cur.execute(pg_sql)
        return self

    def fetchone(self) -> Optional[_PgCompatRow]:
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [col.name for col in (self._cur.description or ())]
        return _PgCompatRow(cols, row)

    def fetchall(self) -> List[_PgCompatRow]:
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [col.name for col in (self._cur.description or ())]
        return [_PgCompatRow(cols, r) for r in rows]

    def close(self) -> None:
        self._cur.close()


class _PgCompatConnection:
    """Connection wrapper matching sqlite3.Connection's `.cursor()` and `.execute()` API."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self) -> _PgCompatCursor:
        return _PgCompatCursor(self._conn.cursor())

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _PgCompatCursor:
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _ensure_postgres_schema(compat_conn: _PgCompatConnection) -> None:
    """Ensure `users`, `news_cache`, and `bot_settings` tables and indexes exist in PostgreSQL."""
    cur = compat_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_subscribed INTEGER DEFAULT 0,
            ai_allowed INTEGER DEFAULT 0,
            ai_expires_at TIMESTAMP NULL,
            trial_used INTEGER DEFAULT 0
        )
    """)
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_allowed INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_subscribed INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_expires_at TIMESTAMP NULL")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_used INTEGER DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_sub ON users(is_subscribed)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_ai_exp ON users(ai_expires_at)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            guid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            pub_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    compat_conn.commit()


def _ensure_sqlite_persistent_schema(conn: sqlite3.Connection) -> None:
    """Ensure `users`, `news_cache`, and `bot_settings` tables exist on a SQLite connection."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_subscribed INTEGER DEFAULT 0,
            ai_allowed INTEGER DEFAULT 0,
            ai_expires_at TIMESTAMP NULL,
            trial_used INTEGER DEFAULT 0
        )
    """)
    for col_def in (
        "ai_allowed INTEGER DEFAULT 0",
        "is_subscribed INTEGER DEFAULT 0",
        "ai_expires_at TIMESTAMP NULL",
        "trial_used INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_ai_exp ON users(ai_expires_at)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            guid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            pub_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


@contextmanager
def get_persistent_db(db_path: Optional[Path] = None):
    """Provide a permanent transactional DB connection for mutable state (`users`, `news_cache`).

    - Uses Neon / Vercel PostgreSQL whenever `POSTGRES_URL` / `DATABASE_URL` is configured
      (and no explicit local SQLite `db_path` override was passed).
    - Automatically falls back to SQLite for local development, Docker volumes, or unit tests.
    """
    global _PG_SCHEMA_INITIALIZED_URL

    pg_url = None if db_path is not None else settings.postgres_url
    if pg_url:
        import psycopg

        # Serverless-safe connection: prepare_threshold=None ensures 100% compatibility
        # with Neon's transaction pooler (-pooler.*.neon.tech / PgBouncer).
        raw_conn = psycopg.connect(pg_url, prepare_threshold=None, connect_timeout=10)
        compat = _PgCompatConnection(raw_conn)
        try:
            if _PG_SCHEMA_INITIALIZED_URL != pg_url:
                _ensure_postgres_schema(compat)
                _PG_SCHEMA_INITIALIZED_URL = pg_url
            yield compat
            compat.commit()
        except Exception:
            _PG_SCHEMA_INITIALIZED_URL = None
            compat.rollback()
            raise
        finally:
            compat.close()
        return

    target_path = db_path or get_db_path()
    key = str(target_path)
    with get_db(target_path) as conn:
        if key not in _SQLITE_PERSISTENT_INITIALIZED_PATHS:
            _ensure_sqlite_persistent_schema(conn)
            _SQLITE_PERSISTENT_INITIALIZED_PATHS.add(key)
        yield conn


def init_db(path: Optional[Path] = None) -> None:
    """Initialize SQLite GTFS transit tables, persistent tables, migrations, and indexes."""
    with get_db(path) as conn:
        cursor = conn.cursor()

        # Transit tables (GTFS backed)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stops (
                stop_id TEXT PRIMARY KEY,
                stop_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                city TEXT DEFAULT '',
                normalized_city TEXT DEFAULT '',
                lat REAL DEFAULT 0.0,
                lon REAL DEFAULT 0.0,
                has_bus INTEGER DEFAULT 0,
                has_train INTEGER DEFAULT 0,
                has_metro INTEGER DEFAULT 0
            )
        """)

        # Migrations: ensure city & normalized_city columns exist on existing databases
        for col_def in ("city TEXT DEFAULT ''", "normalized_city TEXT DEFAULT ''"):
            try:
                cursor.execute(f"ALTER TABLE stops ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                route_id TEXT PRIMARY KEY,
                route_short_name TEXT,
                route_long_name TEXT,
                route_type INTEGER,
                transport_type TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                trip_id TEXT PRIMARY KEY,
                route_id TEXT NOT NULL,
                service_id TEXT,
                trip_headsign TEXT,
                trip_short_name TEXT,
                direction_id INTEGER DEFAULT 0,
                FOREIGN KEY (route_id) REFERENCES routes(route_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stop_times (
                trip_id TEXT NOT NULL,
                arrival_time TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                stop_sequence INTEGER NOT NULL,
                PRIMARY KEY (trip_id, stop_sequence),
                FOREIGN KEY (trip_id) REFERENCES trips(trip_id),
                FOREIGN KEY (stop_id) REFERENCES stops(stop_id)
            )
        """)

        _ensure_sqlite_persistent_schema(conn)

        # Performance indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stops_norm ON stops(normalized_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stops_name ON stops(stop_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stops_city ON stops(normalized_city)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_st_stop_dep ON stop_times(stop_id, departure_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_st_trip_seq ON stop_times(trip_id, stop_sequence)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trips_route ON trips(route_id)")
