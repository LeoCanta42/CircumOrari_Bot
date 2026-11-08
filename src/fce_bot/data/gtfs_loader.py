"""Loader and parser for Ferrovia Circumetnea official GTFS feeds."""

import io
import csv
import zipfile
import logging
import requests
from pathlib import Path
from typing import Optional, Tuple
from fce_bot.config import settings
from fce_bot.core.models import TransportType
from fce_bot.data.db import get_db, init_db, normalize_stop_name
from fce_bot.data.city_resolver import enrich_stops_batch, resolve_stop_city

logger = logging.getLogger(__name__)


def classify_route_transport(route_id: str, short_name: str, long_name: str) -> TransportType:
    """Classify FCE route into METRO, TRENO (superficie / littorina), or BUS."""
    s_name = (short_name or "").upper()
    l_name = (long_name or "").upper()

    if route_id == "53" or "METROPOLITANA" in l_name:
        return TransportType.METRO
    if route_id in ("52", "47", "48") or "SUPERFICIE" in l_name or "TRENI" in l_name:
        return TransportType.TRENO
    return TransportType.BUS


def load_gtfs_from_zip(zip_bytes: bytes, db_path: Optional[Path] = None) -> Tuple[int, int, int]:
    """Parse GTFS zip bytes and load all entities into SQLite."""
    init_db(db_path)

    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    file_list = z.namelist()

    required = ["routes.txt", "trips.txt", "stops.txt", "stop_times.txt"]
    for req in required:
        if req not in file_list:
            raise ValueError(f"Invalid GTFS feed: missing {req}")

    # Read and classify routes
    routes_data = []
    route_type_map = {}
    with z.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            rid = row["route_id"]
            sname = row.get("route_short_name", "")
            lname = row.get("route_long_name", "")
            rtype = int(row.get("route_type", 3) or 3)
            ttype = classify_route_transport(rid, sname, lname)
            route_type_map[rid] = ttype
            routes_data.append((rid, sname, lname, rtype, ttype.value))

    # Read trips
    trips_data = []
    trip_route_map = {}
    with z.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            tid = row["trip_id"]
            rid = row["route_id"]
            trip_route_map[tid] = rid
            trips_data.append((
                tid,
                rid,
                row.get("service_id", ""),
                row.get("trip_headsign", ""),
                row.get("trip_short_name", ""),
                int(row.get("direction_id", 0) or 0)
            ))

    # Read stop_times
    stop_times_data = []
    stop_transport_usage = {}  # stop_id -> set(TransportType)
    with z.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            tid = row["trip_id"]
            sid = row["stop_id"]
            arr = row["arrival_time"][:5]  # format HH:MM
            dep = row["departure_time"][:5]
            seq = int(row["stop_sequence"])
            stop_times_data.append((tid, arr, dep, sid, seq))

            # Track transport usage for stop
            rid = trip_route_map.get(tid)
            if rid and rid in route_type_map:
                tt = route_type_map[rid]
                if sid not in stop_transport_usage:
                    stop_transport_usage[sid] = set()
                stop_transport_usage[sid].add(tt)

    # Read stops
    raw_stops_data = []
    with z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            sid = row["stop_id"]
            sname = row["stop_name"].strip()
            norm = normalize_stop_name(sname)
            lat = float(row.get("stop_lat", 0) or 0)
            lon = float(row.get("stop_lon", 0) or 0)

            usages = stop_transport_usage.get(sid, set())
            has_bus = 1 if TransportType.BUS in usages else 0
            has_train = 1 if TransportType.TRENO in usages else 0
            has_metro = 1 if TransportType.METRO in usages else 0

            # Fallback if no trip associated yet
            if not usages:
                has_bus = 1

            raw_stops_data.append((sid, sname, norm, lat, lon, has_bus, has_train, has_metro))

    # Subtask: enrich stops with their municipality (Comune / City)
    stops_data = enrich_stops_batch(raw_stops_data, allow_network=True)

    # Atomic write to DB
    with get_db(db_path) as conn:
        cur = conn.cursor()

        # Clear old transit tables
        cur.execute("DELETE FROM stop_times")
        cur.execute("DELETE FROM trips")
        cur.execute("DELETE FROM routes")
        cur.execute("DELETE FROM stops")

        # Insert batch
        cur.executemany("INSERT INTO routes VALUES (?, ?, ?, ?, ?)", routes_data)
        cur.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", trips_data)
        cur.executemany(
            "INSERT INTO stops (stop_id, stop_name, normalized_name, city, normalized_city, lat, lon, has_bus, has_train, has_metro) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            stops_data
        )
        cur.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)", stop_times_data)

    cities_found = len({row[3] for row in stops_data if row[3]})
    logger.info(
        "Loaded GTFS successfully: %d stops (%d cities), %d trips, %d stop_times",
        len(stops_data), cities_found, len(trips_data), len(stop_times_data)
    )
    return len(stops_data), len(trips_data), len(stop_times_data)


def enrich_existing_stops_cities(db_path: Optional[Path] = None, allow_network: bool = True) -> int:
    """Subtask to backfill `city` and `normalized_city` on existing stops in SQLite."""
    init_db(db_path)
    updated = 0
    with get_db(db_path) as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT stop_id, stop_name, lat, lon FROM stops").fetchall()
        updates = []
        for r in rows:
            city = resolve_stop_city(r["stop_name"], r["lat"] or 0.0, r["lon"] or 0.0, allow_network=allow_network)
            norm_city = normalize_stop_name(city)
            updates.append((city, norm_city, r["stop_id"]))
        if updates:
            cur.executemany(
                "UPDATE stops SET city = ?, normalized_city = ? WHERE stop_id = ?",
                updates
            )
            updated = len(updates)
    logger.info("Enriched city metadata for %d stops", updated)
    return updated


def download_and_sync_gtfs(feed_url: Optional[str] = None, db_path: Optional[Path] = None) -> Tuple[int, int, int]:
    """Download latest official GTFS feed from circumetnea.it and sync SQLite."""
    url = feed_url or settings.CIRCUMETNEA_GTFS_URL
    logger.info("Downloading GTFS feed from %s...", url)
    headers = {"User-Agent": "FCE-Bot/2.0 (+https://github.com/Leonardo0101/FCE_bot)"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()

    return load_gtfs_from_zip(resp.content, db_path)
