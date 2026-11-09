"""Transit query service for Ferrovia Circumetnea schedules, fuzzy stop matching, proximity lookup, and path tracing."""

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from fce_bot.core.models import FilterType, Itinerary, Stop, TransportType, TripSegment
from fce_bot.core.time_utils import get_current_time_str
from fce_bot.data.city_resolver import geocode_place_in_etna_area, haversine_meters
from fce_bot.data.db import get_db, normalize_stop_name

logger = logging.getLogger(__name__)

# Generic structural words stripped when comparing core station tokens
_GENERIC_STOP_TOKENS: Set[str] = {
    "STAZIONE",
    "FERMATA",
    "PARCHEGGIO",
    "METRO",
    "METROPOLITANA",
    "FCE",
    "CIRCUMETNEA",
    "BIVIO",
    "PIAZZA",
    "PIAZZALE",
    "VIA",
    "VIALE",
    "CORSO",
    "CONTRADA",
    "LARGO",
    "PONTE",
    "ROTONDA",
    "CIRCONVALLAZIONE",
    "INCROCIO",
    "NORD",
    "SUD",
    "EST",
    "OVEST",
    "ALTA",
    "BASSA",
    "DI",
    "DEL",
    "DELLA",
    "DELLO",
    "DEI",
    "DEGLI",
    "DELLE",
    "DA",
    "IN",
    "AL",
    "ALLA",
    "ALLE",
}


def _hhmm_to_minutes(hhmm: str) -> int:
    try:
        p = hhmm.split(":")
        return int(p[0]) * 60 + int(p[1])
    except Exception:
        return 0


@dataclass
class LocationResolution:
    """Detailed resolution result for an origin or destination query."""

    raw_query: str
    stop_ids: List[str] = field(default_factory=list)
    matched_stops: List[Stop] = field(default_factory=list)
    match_kind: str = "none"  # "exact" | "city" | "similar" | "nearest_place" | "none"
    resolved_label: str = ""
    nearest_distance_m: Optional[int] = None
    place_coords: Optional[Tuple[float, float]] = None


class TransitService:
    """Provides timetable lookups, city/fuzzy station search, nearest-stop geocoding, and route path tracing."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self._hub_neighbors_cache: Dict[int, Dict[str, List[Tuple[str, float]]]] = {}

    @staticmethod
    def _row_to_stop(r, distance_meters: Optional[int] = None) -> Stop:
        keys = r.keys()
        return Stop(
            stop_id=str(r["stop_id"]),
            stop_name=r["stop_name"],
            normalized_name=r["normalized_name"],
            lat=float(r["lat"] or 0.0),
            lon=float(r["lon"] or 0.0),
            has_bus=bool(r["has_bus"]),
            has_train=bool(r["has_train"]),
            has_metro=bool(r["has_metro"]),
            city=(r["city"] or "") if "city" in keys else "",
            distance_meters=distance_meters,
        )

    @staticmethod
    def _transport_clause(transport_type: Optional[TransportType]) -> str:
        if transport_type == TransportType.BUS:
            return " AND has_bus = 1"
        if transport_type == TransportType.TRENO:
            return " AND has_train = 1"
        if transport_type == TransportType.METRO:
            return " AND has_metro = 1"
        return ""

    def _has_city_column(self, conn) -> bool:
        cur = conn.cursor()
        cols = {row[1] for row in cur.execute("PRAGMA table_info(stops)").fetchall()}
        return "city" in cols and "normalized_city" in cols

    def get_cities(self, transport_type: TransportType = TransportType.QUALSIASI) -> List[dict]:
        """Return sorted list of municipalities (`city`) and distinct stop counts for a transport mode."""
        with get_db(self.db_path) as conn:
            if not self._has_city_column(conn):
                return []
            sql = (
                "SELECT city, COUNT(DISTINCT stop_name) as cnt "
                "FROM stops WHERE city IS NOT NULL AND city != ''"
                + self._transport_clause(transport_type)
                + " GROUP BY city ORDER BY city ASC"
            )
            rows = conn.cursor().execute(sql).fetchall()
            return [{"city": r["city"], "count": int(r["cnt"])} for r in rows]

    def get_stops_by_type(
        self,
        transport_type: TransportType = TransportType.QUALSIASI,
        city: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Stop]:
        """Get distinct stops available for a given transport type (optionally filtered by city)."""
        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            city_col = "coalesce(city, '') as city" if has_city else "'' as city"
            group_cols = "stop_name, coalesce(city, '')" if has_city else "stop_name"

            sql = f"""
                SELECT MIN(stop_id) as stop_id, stop_name, normalized_name, {city_col}, lat, lon,
                       MAX(has_bus) as has_bus, MAX(has_train) as has_train, MAX(has_metro) as has_metro
                FROM stops
                WHERE 1=1
            """
            params: List = []
            sql += self._transport_clause(transport_type)

            if city and has_city:
                norm_city = normalize_stop_name(city)
                if norm_city:
                    sql += " AND normalized_city = ?"
                    params.append(norm_city)

            sql += f" GROUP BY {group_cols} ORDER BY stop_name ASC"
            if limit is not None and limit > 0:
                sql += " LIMIT ?"
                params.append(limit)

            rows = conn.cursor().execute(sql, tuple(params)).fetchall()
            return [self._row_to_stop(r) for r in rows]

    def search_stops(
        self,
        query: str,
        transport_type: Optional[TransportType] = None,
        city: Optional[str] = None,
        limit: int = 30,
    ) -> List[Stop]:
        """Search stops matching a text query (by stop name, city, combined city+stop, or fuzzy similarity)."""
        norm_q = normalize_stop_name(query)
        if not norm_q:
            if city:
                return self.get_stops_by_type(
                    transport_type=transport_type or TransportType.QUALSIASI,
                    city=city,
                    limit=limit,
                )
            return []

        results: List[Stop] = []
        seen_keys: Set[Tuple[str, str]] = set()

        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            city_col = "coalesce(city, '') as city" if has_city else "'' as city"
            group_cols = "stop_name, coalesce(city, '')" if has_city else "stop_name"

            if has_city:
                where_match = (
                    "(normalized_name LIKE ? OR normalized_name LIKE ? "
                    "OR normalized_city = ? OR normalized_city LIKE ? "
                    "OR (normalized_city || ' ' || normalized_name) LIKE ? "
                    "OR (normalized_name || ' ' || normalized_city) LIKE ?)"
                )
                params: List = [
                    f"{norm_q}%",
                    f"% {norm_q}%",
                    norm_q,
                    f"{norm_q}%",
                    f"%{norm_q}%",
                    f"%{norm_q}%",
                ]
            else:
                where_match = "(normalized_name LIKE ? OR normalized_name LIKE ?)"
                params = [f"{norm_q}%", f"% {norm_q}%"]

            sql = f"""
                SELECT MIN(stop_id) as stop_id, stop_name, normalized_name, {city_col}, lat, lon,
                       MAX(has_bus) as has_bus, MAX(has_train) as has_train, MAX(has_metro) as has_metro
                FROM stops
                WHERE {where_match}
            """
            sql += self._transport_clause(transport_type)

            if city and has_city:
                norm_city = normalize_stop_name(city)
                if norm_city:
                    sql += " AND normalized_city = ?"
                    params.append(norm_city)

            sql += f" GROUP BY {group_cols} ORDER BY (CASE WHEN normalized_name = ? THEN 0 WHEN normalized_name LIKE ? THEN 1 ELSE 2 END), stop_name LIMIT ?"
            params.extend([norm_q, f"{norm_q}%", limit])

            rows = conn.cursor().execute(sql, tuple(params)).fetchall()
            for r in rows:
                st = self._row_to_stop(r)
                k = (st.stop_name, st.city)
                if k not in seen_keys:
                    seen_keys.add(k)
                    results.append(st)

        # Augment with fuzzy/similar stop matching (e.g. "Catania Santa Sofia" -> "Parcheggio Santa Sofia")
        if len(results) < limit:
            fuzzy_matches = self.find_similar_stops(
                query=query,
                transport_type=transport_type or TransportType.QUALSIASI,
                city=city,
                limit=limit - len(results),
            )
            for st, _score in fuzzy_matches:
                k = (st.stop_name, st.city)
                if k not in seen_keys:
                    seen_keys.add(k)
                    results.append(st)

        return results[:limit]

    # ==========================================
    # Similar / Fuzzy Matching & Geocoding Nearest-Stop Resolution
    # ==========================================

    @staticmethod
    def _extract_core_tokens(norm_str: str, extra_ignore: Optional[Set[str]] = None) -> List[str]:
        tokens = []
        for t in norm_str.split():
            if t == "S" or t == "STA":
                tokens.append("SANTA")
                continue
            if t in _GENERIC_STOP_TOKENS:
                continue
            if extra_ignore and t in extra_ignore:
                continue
            tokens.append(t)
        return tokens

    def find_similar_stops(
        self,
        query: str,
        transport_type: TransportType = TransportType.QUALSIASI,
        city: Optional[str] = None,
        limit: int = 8,
        min_score: float = 0.68,
    ) -> List[Tuple[Stop, float]]:
        """Find stops with similar names using city-aware token overlap and sequence similarity."""
        norm_q = normalize_stop_name(query)
        if not norm_q or len(norm_q) < 3:
            return []

        candidates = self.get_stops_by_type(transport_type=transport_type, city=city)
        if not candidates:
            return []

        known_cities: Dict[str, str] = {}
        for c in candidates:
            if c.city:
                nc = normalize_stop_name(c.city)
                if nc:
                    known_cities[nc] = c.city

        # Detect if query mentions a municipality alongside a specific stop name (e.g. "Catania Santa Sofia")
        detected_city_norm: Optional[str] = normalize_stop_name(city) if city else None
        remainder_q = norm_q
        if not detected_city_norm:
            for nc in sorted(known_cities.keys(), key=len, reverse=True):
                if norm_q != nc and f" {nc} " in f" {norm_q} ":
                    remainder = f" {norm_q} ".replace(f" {nc} ", " ").strip()
                    if remainder:
                        detected_city_norm = nc
                        remainder_q = remainder
                        break

        city_ignore_tokens = set(detected_city_norm.split()) if detected_city_norm else set()
        q_core = self._extract_core_tokens(remainder_q, extra_ignore=city_ignore_tokens)
        if not q_core:
            q_core = remainder_q.split()

        scored: List[Tuple[Stop, float]] = []
        for st in candidates:
            st_norm = st.normalized_name
            st_city_norm = normalize_stop_name(st.city)

            # City consistency check when user explicitly specified a city in query
            city_bonus = 0.0
            if detected_city_norm:
                if st_city_norm == detected_city_norm or detected_city_norm in st_norm:
                    city_bonus = 0.12
                elif st_city_norm:
                    # Different municipality -> skip unless no city metadata
                    continue

            st_core = self._extract_core_tokens(st_norm, extra_ignore=city_ignore_tokens)
            if not st_core:
                st_core = st_norm.split()

            # 1. Core token fuzzy overlap score
            matched_q = 0.0
            for qt in q_core:
                best_tok = 0.0
                for st_t in st_core:
                    if qt == st_t:
                        best_tok = 1.0
                        break
                    if len(qt) >= 4 and len(st_t) >= 4 and (st_t.startswith(qt) or qt.startswith(st_t)):
                        best_tok = max(best_tok, 0.90)
                    elif len(qt) >= 4 and len(st_t) >= 4:
                        ratio = difflib.SequenceMatcher(None, qt, st_t).ratio()
                        if ratio >= 0.78:
                            best_tok = max(best_tok, ratio)
                matched_q += best_tok

            q_coverage = matched_q / max(1, len(q_core))
            if q_core and q_coverage < 0.5:
                # Never match stops that only share generic prefixes like "Via", "Piazza", or "Catania"
                # without matching the actual distinctive stop/place token(s)
                continue
            st_coverage = matched_q / max(1, len(st_core))
            core_score = (0.70 * q_coverage) + (0.30 * st_coverage) if matched_q > 0 else 0.0

            # 2. Core-token string similarity (excluding generic words so "Piazza X" != "Piazza Y")
            seq_ratio = difflib.SequenceMatcher(None, " ".join(q_core), " ".join(st_core)).ratio()

            final_score = min(1.0, max(core_score, seq_ratio * 0.92) + (city_bonus if matched_q >= 0.75 else 0.0))
            if final_score >= min_score:
                scored.append((st, final_score))

        scored.sort(key=lambda x: (-x[1], len(x[0].stop_name)))
        if not scored:
            return []

        top_score = scored[0][1]
        # Keep tight cluster near best match so unrelated partial matches aren't mixed in
        cutoff = max(min_score, top_score - 0.12)
        return [(s, sc) for s, sc in scored if sc >= cutoff][:limit]

    def find_nearest_stops(
        self,
        lat: float,
        lon: float,
        transport_type: TransportType = TransportType.QUALSIASI,
        max_distance_m: float = 3500.0,
        limit: int = 5,
    ) -> List[Stop]:
        """Return stops closest to given WGS84 coordinates, sorted by Haversine distance in meters."""
        if not (lat and lon):
            return []

        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            city_col = "coalesce(city, '') as city" if has_city else "'' as city"
            sql = (
                f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, "
                "has_bus, has_train, has_metro FROM stops WHERE lat != 0 AND lon != 0"
                + self._transport_clause(transport_type)
            )
            rows = conn.cursor().execute(sql).fetchall()

        by_key: Dict[Tuple[str, str], Stop] = {}
        for r in rows:
            dist = haversine_meters(lat, lon, float(r["lat"]), float(r["lon"]))
            if dist > max_distance_m:
                continue
            dist_int = int(round(dist))
            st = self._row_to_stop(r, distance_meters=dist_int)
            k = (st.stop_name, st.city)
            existing = by_key.get(k)
            if existing is None or (st.distance_meters or 999999) < (existing.distance_meters or 999999):
                if existing is not None:
                    st.has_bus = st.has_bus or existing.has_bus
                    st.has_train = st.has_train or existing.has_train
                    st.has_metro = st.has_metro or existing.has_metro
                by_key[k] = st
            else:
                existing.has_bus = existing.has_bus or st.has_bus
                existing.has_train = existing.has_train or st.has_train
                existing.has_metro = existing.has_metro or st.has_metro

        ordered = sorted(by_key.values(), key=lambda s: (s.distance_meters or 999999, s.stop_name))
        return ordered[:limit]

    def _expand_stop_ids_for_stops(
        self,
        stops: List[Stop],
        transport_type: TransportType = TransportType.QUALSIASI,
    ) -> List[str]:
        """Collect all physical directional stop_ids belonging to matched logical stops."""
        if not stops:
            return []
        names = list({s.stop_name for s in stops})
        placeholders = ",".join("?" for _ in names)
        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            sql = f"SELECT stop_id, stop_name, {'coalesce(city, \"\") as city' if has_city else '\"\" as city'} FROM stops WHERE stop_name IN ({placeholders})"
            sql += self._transport_clause(transport_type)
            rows = conn.cursor().execute(sql, tuple(names)).fetchall()

        allowed_pairs = {(s.stop_name, s.city) for s in stops}
        out_ids = []
        for r in rows:
            if not any(s.city for s in stops) or (r["stop_name"], r["city"]) in allowed_pairs:
                out_ids.append(str(r["stop_id"]))
        return out_ids or [s.stop_id for s in stops]

    def resolve_location(
        self,
        name_or_query: str,
        transport_type: TransportType = TransportType.QUALSIASI,
        allow_geocoding: bool = True,
        allow_network_geocoding: bool = True,
    ) -> LocationResolution:
        """Smart multi-stage resolver: Exact/Substring -> City -> Similar Stop Name -> Geocoded Nearest Stop."""
        if not name_or_query or not name_or_query.strip():
            return LocationResolution(raw_query=name_or_query or "")

        norm = normalize_stop_name(name_or_query)
        if not norm:
            return LocationResolution(raw_query=name_or_query)

        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            city_col = "coalesce(city, '') as city" if has_city else "'' as city"
            cur = conn.cursor()

            # Stage 1A-bis: Explicit "Stop Name (City)" format (e.g. "Corso Sicilia (Giarre)")
            if has_city:
                paren_match = re.match(r"^(.+?)\s*\(([^()]+)\)\s*$", name_or_query.strip())
                if paren_match:
                    p_stop = normalize_stop_name(paren_match.group(1))
                    p_city = normalize_stop_name(paren_match.group(2))
                    if p_stop and p_city:
                        p_rows = cur.execute(
                            f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, has_bus, has_train, has_metro "
                            "FROM stops WHERE normalized_name = ? AND normalized_city = ?"
                            + self._transport_clause(transport_type)
                            + " ORDER BY stop_name",
                            (p_stop, p_city),
                        ).fetchall()
                        if p_rows:
                            stops = [self._row_to_stop(r) for r in p_rows]
                            return LocationResolution(
                                raw_query=name_or_query,
                                stop_ids=[str(r["stop_id"]) for r in p_rows],
                                matched_stops=stops[:6],
                                match_kind="exact",
                                resolved_label=stops[0].display_name,
                            )

            # Stage 1A: Check if query is an exact municipality name (e.g. "Catania", "Paternò", "Randazzo")
            is_city_query = False
            if has_city:
                city_cnt = cur.execute(
                    "SELECT count(*) FROM stops WHERE normalized_city = ?" + self._transport_clause(transport_type),
                    (norm,),
                ).fetchone()[0]
                is_city_query = city_cnt > 0

            if is_city_query:
                # For rail/metro modes, if canonical station(s) match the town name directly
                # (e.g. PATERNO', RANDAZZO, BRONTE, ADRANO CENTRO), prefer them over distant rural stops
                if transport_type in (TransportType.TRENO, TransportType.METRO):
                    named_rail_rows = cur.execute(
                        f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, has_bus, has_train, has_metro "
                        "FROM stops WHERE (normalized_name = ? OR normalized_name LIKE ?)"
                        + self._transport_clause(transport_type)
                        + " ORDER BY (CASE WHEN normalized_name = ? THEN 0 ELSE 1 END), stop_name",
                        (norm, f"%{norm}%", norm),
                    ).fetchall()
                    if named_rail_rows:
                        stops = [self._row_to_stop(r) for r in named_rail_rows]
                        return LocationResolution(
                            raw_query=name_or_query,
                            stop_ids=[str(r["stop_id"]) for r in named_rail_rows],
                            matched_stops=stops[:6],
                            match_kind="exact",
                            resolved_label=stops[0].display_name,
                        )

                # For general/bus queries on a municipality, prefer central town stops + Metro/Train hubs
                # before falling back to all suburban/rural contrada stops in large municipalities
                hub_rows = cur.execute(
                    f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, has_bus, has_train, has_metro "
                    "FROM stops WHERE ("
                    "  normalized_name = ? OR normalized_name LIKE ? "
                    "  OR (normalized_city = ? AND (has_metro = 1 OR has_train = 1))"
                    ")"
                    + self._transport_clause(transport_type)
                    + " ORDER BY (CASE WHEN normalized_name = ? THEN 0 WHEN has_metro = 1 OR has_train = 1 THEN 1 ELSE 2 END), stop_name",
                    (norm, f"%{norm}%", norm, norm),
                ).fetchall()

                rows = hub_rows if len(hub_rows) >= 2 else cur.execute(
                    f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, has_bus, has_train, has_metro "
                    "FROM stops WHERE (normalized_city = ? OR normalized_name = ? OR normalized_name LIKE ?)"
                    + self._transport_clause(transport_type)
                    + " ORDER BY (CASE WHEN normalized_name = ? THEN 0 WHEN has_metro = 1 OR has_train = 1 THEN 1 ELSE 2 END), stop_name",
                    (norm, norm, f"%{norm}%", norm),
                ).fetchall()

                if rows:
                    stops = [self._row_to_stop(r) for r in rows]
                    city_label = stops[0].city or name_or_query.strip()
                    return LocationResolution(
                        raw_query=name_or_query,
                        stop_ids=[str(r["stop_id"]) for r in rows],
                        matched_stops=stops[:8],
                        match_kind="city",
                        resolved_label=city_label,
                    )

            # Stage 1B: Direct normalized stop_name match
            rows = cur.execute(
                f"SELECT stop_id, stop_name, normalized_name, {city_col}, lat, lon, has_bus, has_train, has_metro "
                "FROM stops WHERE (normalized_name = ? OR normalized_name LIKE ? OR normalized_name LIKE ?)"
                + self._transport_clause(transport_type)
                + " ORDER BY (CASE WHEN normalized_name = ? THEN 0 WHEN normalized_name LIKE ? THEN 1 ELSE 2 END), stop_name",
                (norm, f"{norm}%", f"%{norm}%", norm, f"{norm}%"),
            ).fetchall()
            if rows:
                stops = [self._row_to_stop(r) for r in rows]
                return LocationResolution(
                    raw_query=name_or_query,
                    stop_ids=[str(r["stop_id"]) for r in rows],
                    matched_stops=stops[:6],
                    match_kind="exact",
                    resolved_label=stops[0].display_name,
                )

        # Stage 2: Fuzzy / Similar stop name matching (e.g. "Catania Santa Sofia" -> "Parcheggio Santa Sofia")
        similar = self.find_similar_stops(name_or_query, transport_type=transport_type, limit=4, min_score=0.68)
        if similar:
            matched_stops = [s for s, _ in similar]
            stop_ids = self._expand_stop_ids_for_stops(matched_stops, transport_type=transport_type)
            return LocationResolution(
                raw_query=name_or_query,
                stop_ids=stop_ids,
                matched_stops=matched_stops,
                match_kind="similar",
                resolved_label=matched_stops[0].display_name,
            )

        # Stage 3: Address / Landmark Geocoding -> Nearest FCE Stops
        if allow_geocoding:
            geo = geocode_place_in_etna_area(name_or_query, allow_network=allow_network_geocoding)
            if geo:
                glat, glon, glabel = geo
                nearest = self.find_nearest_stops(
                    glat, glon, transport_type=transport_type, max_distance_m=3500.0, limit=5
                )
                if nearest:
                    min_dist = nearest[0].distance_meters or 0
                    # Include stops within +550m of the closest stop so multi-modal hubs (Metro + Bus) are both considered
                    cluster = [s for s in nearest if (s.distance_meters or 0) <= min_dist + 550][:4]
                    stop_ids = self._expand_stop_ids_for_stops(cluster, transport_type=transport_type)
                    return LocationResolution(
                        raw_query=name_or_query,
                        stop_ids=stop_ids,
                        matched_stops=cluster,
                        match_kind="nearest_place",
                        resolved_label=glabel,
                        nearest_distance_m=min_dist,
                        place_coords=(glat, glon),
                    )

        return LocationResolution(raw_query=name_or_query)

    def _resolve_stop_ids(
        self,
        name_or_query: str,
        transport_type: TransportType = TransportType.QUALSIASI,
        allow_geocoding: bool = False,
    ) -> List[str]:
        """Resolve a stop name query to matching stop_ids for the transport mode."""
        res = self.resolve_location(
            name_or_query, transport_type=transport_type, allow_geocoding=allow_geocoding
        )
        return res.stop_ids

    # ==========================================
    # Route Path Tracing
    # ==========================================

    def _fetch_trip_segment_path(self, conn, trip_id: str, seq_start: int, seq_end: int) -> List[str]:
        """Return ordered stop names traversed on `trip_id` from `seq_start` to `seq_end` inclusive."""
        rows = conn.cursor().execute(
            """
            SELECT s.stop_name
            FROM stop_times st
            JOIN stops s ON st.stop_id = s.stop_id
            WHERE st.trip_id = ? AND st.stop_sequence >= ? AND st.stop_sequence <= ?
            ORDER BY st.stop_sequence ASC
            """,
            (trip_id, seq_start, seq_end),
        ).fetchall()
        path: List[str] = []
        for r in rows:
            nm = r["stop_name"]
            if not path or path[-1] != nm:
                path.append(nm)
        return path

    def trace_line_stops(
        self,
        line_query: str,
        near_coords: Optional[Tuple[float, float]] = None,
        near_place: Optional[str] = None,
        limit_routes: int = 3,
    ) -> List[dict]:
        """Trace the ordered path of stops followed by a route/line (e.g. 'METRO', '23M', '5N', 'TRENO')."""
        norm_q = normalize_stop_name(line_query)
        if not norm_q:
            return []

        if near_coords is None and near_place:
            geo = geocode_place_in_etna_area(near_place, allow_network=True)
            if geo:
                near_coords = (geo[0], geo[1])

        ttype_guess = TransportType.from_str(line_query)
        with get_db(self.db_path) as conn:
            has_city = self._has_city_column(conn)
            city_col = "coalesce(s.city, '') as city" if has_city else "'' as city"
            cur = conn.cursor()

            if ttype_guess != TransportType.QUALSIASI and norm_q in (
                "METRO",
                "METROPOLITANA",
                "TRENO",
                "TRENI",
                "LITTORINA",
                "SUPERFICIE",
            ):
                routes = cur.execute(
                    "SELECT route_id, route_short_name, route_long_name, transport_type "
                    "FROM routes WHERE transport_type = ? LIMIT ?",
                    (ttype_guess.value, limit_routes),
                ).fetchall()
            else:
                routes = cur.execute(
                    """
                    SELECT route_id, route_short_name, route_long_name, transport_type
                    FROM routes
                    WHERE UPPER(coalesce(route_short_name, '')) = ?
                       OR UPPER(coalesce(route_long_name, '')) LIKE ?
                       OR UPPER(coalesce(route_short_name, '')) LIKE ?
                    ORDER BY CASE WHEN UPPER(coalesce(route_short_name, '')) = ? THEN 0 ELSE 1 END
                    LIMIT ?
                    """,
                    (norm_q, f"%{norm_q}%", f"{norm_q}%", norm_q, limit_routes),
                ).fetchall()

            traced_lines: List[dict] = []
            for rt in routes:
                rid = rt["route_id"]
                r_label = (
                    rt["route_short_name"]
                    if rt["route_short_name"] and rt["route_short_name"] != "NULL"
                    else (rt["route_long_name"] or rid)
                )
                # Pick longest canonical trip for this route
                trip_row = cur.execute(
                    """
                    SELECT t.trip_id, coalesce(t.trip_headsign, '') as headsign, COUNT(st.stop_id) as scnt
                    FROM trips t
                    JOIN stop_times st ON t.trip_id = st.trip_id
                    WHERE t.route_id = ?
                    GROUP BY t.trip_id
                    ORDER BY scnt DESC
                    LIMIT 1
                    """,
                    (rid,),
                ).fetchone()
                if not trip_row:
                    continue

                stop_rows = cur.execute(
                    f"""
                    SELECT s.stop_name, {city_col}, s.lat, s.lon
                    FROM stop_times st
                    JOIN stops s ON st.stop_id = s.stop_id
                    WHERE st.trip_id = ?
                    ORDER BY st.stop_sequence ASC
                    """,
                    (trip_row["trip_id"],),
                ).fetchall()

                stops_list = []
                closest_stop_info = None
                for sr in stop_rows:
                    nm = sr["stop_name"]
                    ct = sr["city"]
                    if not stops_list or stops_list[-1]["name"] != nm:
                        stops_list.append({"name": nm, "city": ct, "lat": sr["lat"], "lon": sr["lon"]})
                    if near_coords and sr["lat"] and sr["lon"]:
                        d_m = int(round(haversine_meters(near_coords[0], near_coords[1], sr["lat"], sr["lon"])))
                        if closest_stop_info is None or d_m < closest_stop_info["distance_meters"]:
                            closest_stop_info = {"stop_name": nm, "city": ct, "distance_meters": d_m}

                entry = {
                    "route_id": rid,
                    "route_name": r_label,
                    "transport_type": rt["transport_type"],
                    "headsign": trip_row["headsign"],
                    "stops": [s["name"] for s in stops_list],
                    "stops_detailed": stops_list,
                }
                if closest_stop_info:
                    entry["closest_stop_to_target"] = closest_stop_info
                traced_lines.append(entry)

            return traced_lines

    # ==========================================
    # Timetable & Connection Engine
    # ==========================================

    @staticmethod
    def _standardize_time(target_time: Optional[str] = None) -> str:
        if not target_time or not target_time.strip():
            return get_current_time_str()
        cleaned_time = target_time.strip().replace(".", ":")
        parts = cleaned_time.split(":")
        if len(parts) == 1:
            return f"{int(parts[0]):02d}:00"
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"

    def find_trips_from_resolutions(
        self,
        origin_res: LocationResolution,
        dest_res: LocationResolution,
        target_time: str = "08:00",
        transport_type: TransportType = TransportType.QUALSIASI,
        filter_type: FilterType = FilterType.DEPARTURE_AFTER,
        limit: int = 10,
    ) -> List[Itinerary]:
        """Find direct or 1-transfer itineraries given pre-resolved origin and destination locations."""
        cleaned_time = self._standardize_time(target_time)
        origin_ids = origin_res.stop_ids
        dest_ids = dest_res.stop_ids

        if not origin_ids or not dest_ids:
            return []

        # Avoid trivial zero-length self-matches if city resolution overlapped
        overlap = set(origin_ids) & set(dest_ids)
        if overlap and len(origin_ids) == len(overlap) and len(dest_ids) == len(overlap):
            return []

        direct_itineraries = self._query_direct(
            origin_ids=origin_ids,
            dest_ids=dest_ids,
            target_time=cleaned_time,
            transport_type=transport_type,
            filter_type=filter_type,
            limit=limit,
        )

        # Also check 1-transfer connections if no direct trips exist OR if the first direct trip
        # waits more than 45 minutes (so earlier Metro+Bus / Bus+Train combinations are shown!)
        need_connections = not direct_itineraries
        if direct_itineraries and filter_type == FilterType.DEPARTURE_AFTER:
            wait_first = _hhmm_to_minutes(direct_itineraries[0].departure_time) - _hhmm_to_minutes(cleaned_time)
            if wait_first > 45 or len(direct_itineraries) < 2:
                need_connections = True

        if not need_connections:
            return direct_itineraries

        connecting_itineraries = self._query_connecting(
            origin_ids=origin_ids,
            dest_ids=dest_ids,
            target_time=cleaned_time,
            transport_type=transport_type,
            filter_type=filter_type,
            limit=limit,
        )
        if not direct_itineraries:
            return connecting_itineraries
        if not connecting_itineraries:
            return direct_itineraries

        # Keep transfers alongside direct trips only when they beat the first direct arrival
        # (or fall outside direct coverage), avoiding redundant feeder hops onto the same direct vehicle
        earliest_direct_arr = min(_hhmm_to_minutes(d.arrival_time) for d in direct_itineraries)
        direct_arr_times = {d.arrival_time for d in direct_itineraries}
        filtered_conn = [
            c for c in connecting_itineraries
            if c.arrival_time not in direct_arr_times
            and (
                _hhmm_to_minutes(c.arrival_time) + 5 <= earliest_direct_arr
                or len(direct_itineraries) < 2
            )
        ]

        combined = direct_itineraries + filtered_conn
        if filter_type == FilterType.ARRIVAL_BEFORE:
            combined.sort(key=lambda it: _hhmm_to_minutes(it.arrival_time), reverse=True)
        else:
            combined.sort(key=lambda it: (_hhmm_to_minutes(it.departure_time), _hhmm_to_minutes(it.arrival_time)))
        return combined[:limit]

    def find_trips(
        self,
        origin: str,
        destination: str,
        target_time: str = "08:00",
        transport_type: TransportType = TransportType.QUALSIASI,
        filter_type: FilterType = FilterType.DEPARTURE_AFTER,
        limit: int = 10,
        allow_geocoding: bool = False,
    ) -> List[Itinerary]:
        """Find direct or connecting itineraries between two stations, cities, similar stop names, or places."""
        orig_res = self.resolve_location(
            origin, transport_type=transport_type, allow_geocoding=allow_geocoding
        )
        dest_res = self.resolve_location(
            destination, transport_type=transport_type, allow_geocoding=allow_geocoding
        )

        if not orig_res.stop_ids or not dest_res.stop_ids:
            logger.info(
                "Could not resolve stops for %s: origin=%s (%d), dest=%s (%d)",
                transport_type.value,
                origin,
                len(orig_res.stop_ids),
                destination,
                len(dest_res.stop_ids),
            )
            return []

        return self.find_trips_from_resolutions(
            origin_res=orig_res,
            dest_res=dest_res,
            target_time=target_time,
            transport_type=transport_type,
            filter_type=filter_type,
            limit=limit,
        )

    def _query_direct(
        self,
        origin_ids: List[str],
        dest_ids: List[str],
        target_time: str,
        transport_type: TransportType,
        filter_type: FilterType,
        limit: int,
    ) -> List[Itinerary]:
        """Query direct trips on the same vehicle/trip and populate traced stop paths."""
        orig_placeholders = ",".join("?" for _ in origin_ids)
        dest_placeholders = ",".join("?" for _ in dest_ids)

        sql = f"""
            SELECT
                st1.trip_id as trip_id,
                st1.stop_sequence as seq1,
                st2.stop_sequence as seq2,
                s1.stop_name as dep_stop,
                st1.departure_time as dep_time,
                s2.stop_name as arr_stop,
                st2.arrival_time as arr_time,
                r.transport_type,
                coalesce(nullif(r.route_short_name, 'NULL'), r.route_long_name, '') as route_name,
                coalesce(t.trip_headsign, '') as headsign
            FROM stop_times st1
            JOIN stop_times st2 ON st1.trip_id = st2.trip_id AND st2.stop_sequence > st1.stop_sequence
            JOIN stops s1 ON st1.stop_id = s1.stop_id
            JOIN stops s2 ON st2.stop_id = s2.stop_id
            JOIN trips t ON st1.trip_id = t.trip_id
            JOIN routes r ON t.route_id = r.route_id
            WHERE st1.stop_id IN ({orig_placeholders})
              AND st2.stop_id IN ({dest_placeholders})
              AND s1.stop_name != s2.stop_name
        """
        params: List = list(origin_ids) + list(dest_ids)

        if transport_type != TransportType.QUALSIASI:
            sql += " AND r.transport_type = ?"
            params.append(transport_type.value)

        if filter_type == FilterType.DEPARTURE_AFTER:
            sql += " AND st1.departure_time >= ?"
            params.append(target_time)
            sql += " ORDER BY st1.departure_time ASC, (st2.stop_sequence - st1.stop_sequence) ASC"
        elif filter_type == FilterType.ARRIVAL_BEFORE:
            sql += " AND st2.arrival_time <= ?"
            params.append(target_time)
            sql += " ORDER BY st2.arrival_time DESC, (st2.stop_sequence - st1.stop_sequence) ASC"
        elif filter_type == FilterType.DEPARTURE_NEAR:
            hour_prefix = target_time.split(":")[0] + ":"
            sql += " AND st1.departure_time LIKE ?"
            params.append(f"{hour_prefix}%")
            sql += " ORDER BY st1.departure_time ASC, (st2.stop_sequence - st1.stop_sequence) ASC"
        elif filter_type == FilterType.ARRIVAL_NEAR:
            hour_prefix = target_time.split(":")[0] + ":"
            sql += " AND st2.arrival_time LIKE ?"
            params.append(f"{hour_prefix}%")
            sql += " ORDER BY st2.arrival_time ASC, (st2.stop_sequence - st1.stop_sequence) ASC"

        sql += " LIMIT ?"
        params.append(limit * 6)

        with get_db(self.db_path) as conn:
            rows = conn.cursor().execute(sql, tuple(params)).fetchall()

            seen_trips: Set[str] = set()
            seen_times: Set[Tuple[str, str, str]] = set()
            itineraries: List[Itinerary] = []
            valid_tt = {e.value for e in TransportType}

            for r in rows:
                tid = str(r["trip_id"])
                time_key = (r["dep_time"], r["arr_time"], r["route_name"])
                if tid in seen_trips or time_key in seen_times:
                    continue
                seen_trips.add(tid)
                seen_times.add(time_key)

                tt = (
                    TransportType(r["transport_type"])
                    if r["transport_type"] in valid_tt
                    else TransportType.QUALSIASI
                )
                stops_path = self._fetch_trip_segment_path(conn, tid, int(r["seq1"]), int(r["seq2"]))
                segment = TripSegment(
                    transport_type=tt,
                    route_name=r["route_name"],
                    headsign=r["headsign"],
                    departure_stop=r["dep_stop"],
                    departure_time=r["dep_time"],
                    arrival_stop=r["arr_stop"],
                    arrival_time=r["arr_time"],
                    stops_path=stops_path,
                )
                itineraries.append(
                    Itinerary(
                        departure_stop=r["dep_stop"],
                        departure_time=r["dep_time"],
                        arrival_stop=r["arr_stop"],
                        arrival_time=r["arr_time"],
                        is_direct=True,
                        segments=[segment],
                    )
                )
                if len(itineraries) >= limit:
                    break

            return itineraries

    def _get_hub_neighbors(self, conn) -> Dict[str, List[Tuple[str, float]]]:
        """Return cached map `stop_id -> [(neighbor_stop_id, walk_meters), ...]` for stops within 280m."""
        rows = conn.cursor().execute("SELECT stop_id, lat, lon FROM stops").fetchall()
        cache_key = hash((str(self.db_path or ""), len(rows)))
        cached = self._hub_neighbors_cache.get(cache_key)
        if cached is not None:
            return cached

        stops_pts = [(str(r["stop_id"]), float(r["lat"] or 0.0), float(r["lon"] or 0.0)) for r in rows]
        neighbors: Dict[str, List[Tuple[str, float]]] = {sid: [(sid, 0.0)] for sid, _, _ in stops_pts}

        n = len(stops_pts)
        for i in range(n):
            sid1, lat1, lon1 = stops_pts[i]
            if not (lat1 and lon1):
                continue
            for j in range(i + 1, n):
                sid2, lat2, lon2 = stops_pts[j]
                if not (lat2 and lon2):
                    continue
                if abs(lat1 - lat2) > 0.0026 or abs(lon1 - lon2) > 0.0032:
                    continue
                d_m = haversine_meters(lat1, lon1, lat2, lon2)
                if d_m <= 280.0:
                    neighbors[sid1].append((sid2, d_m))
                    neighbors[sid2].append((sid1, d_m))

        self._hub_neighbors_cache[cache_key] = neighbors
        return neighbors

    def _query_connecting(
        self,
        origin_ids: List[str],
        dest_ids: List[str],
        target_time: str,
        transport_type: TransportType,
        filter_type: FilterType,
        limit: int,
    ) -> List[Itinerary]:
        """Query 1-transfer connections via two fast indexed SQLite queries + walking-hub hash join (<=280m)."""
        if not origin_ids or not dest_ids:
            return []

        with get_db(self.db_path) as conn:
            cur = conn.cursor()
            hub_neighbors = self._get_hub_neighbors(conn)

            orig_placeholders = ",".join("?" for _ in origin_ids)
            leg1_sql = f"""
                SELECT
                    st1.trip_id as trip_id,
                    t1.route_id as route_id,
                    st1.stop_sequence as seq1,
                    st2.stop_sequence as seq2,
                    s_orig.stop_name as dep_stop,
                    st1.departure_time as dep_time,
                    st2.stop_id as t1_id,
                    s_t1.stop_name as t1_stop,
                    st2.arrival_time as arr_time,
                    r1.transport_type as ttype,
                    coalesce(nullif(r1.route_short_name, 'NULL'), r1.route_long_name, '') as route_name,
                    coalesce(t1.trip_headsign, '') as headsign
                FROM stop_times st1
                JOIN stop_times st2 ON st1.trip_id = st2.trip_id AND st2.stop_sequence > st1.stop_sequence
                JOIN trips t1 ON st1.trip_id = t1.trip_id
                JOIN routes r1 ON t1.route_id = r1.route_id
                JOIN stops s_orig ON st1.stop_id = s_orig.stop_id
                JOIN stops s_t1 ON st2.stop_id = s_t1.stop_id
                WHERE st1.stop_id IN ({orig_placeholders})
            """
            leg1_params: List = list(origin_ids)
            if transport_type != TransportType.QUALSIASI:
                leg1_sql += " AND r1.transport_type = ?"
                leg1_params.append(transport_type.value)

            target_mins = _hhmm_to_minutes(target_time)
            if filter_type == FilterType.ARRIVAL_BEFORE:
                min_dep_hhmm = f"{max(0, (target_mins - 240) // 60):02d}:00"
                leg1_sql += " AND st2.arrival_time <= ? AND st1.departure_time >= ?"
                leg1_params.extend([target_time, min_dep_hhmm])
            elif filter_type == FilterType.DEPARTURE_NEAR:
                hour_prefix = target_time.split(":")[0] + ":"
                leg1_sql += " AND st1.departure_time LIKE ?"
                leg1_params.append(f"{hour_prefix}%")
            elif filter_type == FilterType.ARRIVAL_NEAR:
                min_dep_hhmm = f"{max(0, (target_mins - 240) // 60):02d}:00"
                max_arr_hhmm = f"{min(29, (target_mins // 60) + 1):02d}:59"
                leg1_sql += " AND st1.departure_time >= ? AND st2.arrival_time <= ?"
                leg1_params.extend([min_dep_hhmm, max_arr_hhmm])
            else:
                max_dep_hhmm = f"{min(29, (target_mins + 300) // 60):02d}:59"
                leg1_sql += " AND st1.departure_time >= ? AND st1.departure_time <= ?"
                leg1_params.extend([target_time, max_dep_hhmm])

            leg1_rows = cur.execute(leg1_sql, tuple(leg1_params)).fetchall()
            if not leg1_rows:
                return []

            # Collect candidate transfer departure stop_ids reachable within <=280m from leg1 arrivals
            candidate_t2_set: Set[str] = set()
            min_leg1_arr = "99:99"
            for r1 in leg1_rows:
                arr_t = r1["arr_time"]
                if arr_t < min_leg1_arr:
                    min_leg1_arr = arr_t
                for nb_id, _ in hub_neighbors.get(str(r1["t1_id"]), [(str(r1["t1_id"]), 0.0)]):
                    candidate_t2_set.add(nb_id)

            if not candidate_t2_set:
                return []

            t2_list = list(candidate_t2_set)
            t2_placeholders = ",".join("?" for _ in t2_list)
            dest_placeholders = ",".join("?" for _ in dest_ids)

            leg2_sql = f"""
                SELECT
                    st3.trip_id as trip_id,
                    t2.route_id as route_id,
                    st3.stop_sequence as seq1,
                    st4.stop_sequence as seq2,
                    st3.stop_id as t2_id,
                    s_t2.stop_name as t2_stop,
                    st3.departure_time as dep_time,
                    s_dest.stop_name as arr_stop,
                    st4.arrival_time as arr_time,
                    r2.transport_type as ttype,
                    coalesce(nullif(r2.route_short_name, 'NULL'), r2.route_long_name, '') as route_name,
                    coalesce(t2.trip_headsign, '') as headsign
                FROM stop_times st3
                JOIN stop_times st4 ON st3.trip_id = st4.trip_id AND st4.stop_sequence > st3.stop_sequence
                JOIN trips t2 ON st3.trip_id = t2.trip_id
                JOIN routes r2 ON t2.route_id = r2.route_id
                JOIN stops s_t2 ON st3.stop_id = s_t2.stop_id
                JOIN stops s_dest ON st4.stop_id = s_dest.stop_id
                WHERE st3.stop_id IN ({t2_placeholders})
                  AND st4.stop_id IN ({dest_placeholders})
                  AND st3.departure_time >= ?
            """
            leg2_params: List = t2_list + list(dest_ids) + [min_leg1_arr]
            if transport_type != TransportType.QUALSIASI:
                leg2_sql += " AND r2.transport_type = ?"
                leg2_params.append(transport_type.value)

            if filter_type == FilterType.ARRIVAL_BEFORE:
                leg2_sql += " AND st4.arrival_time <= ?"
                leg2_params.append(target_time)
            elif filter_type == FilterType.ARRIVAL_NEAR:
                hour_prefix = target_time.split(":")[0] + ":"
                leg2_sql += " AND st4.arrival_time LIKE ?"
                leg2_params.append(f"{hour_prefix}%")

            leg2_rows = cur.execute(leg2_sql, tuple(leg2_params)).fetchall()
            if not leg2_rows:
                return []

            leg2_by_t2: Dict[str, List] = {}
            for r2 in leg2_rows:
                leg2_by_t2.setdefault(str(r2["t2_id"]), []).append(r2)

            candidates = []
            for r1 in leg1_rows:
                t1_id = str(r1["t1_id"])
                arr1_mins = _hhmm_to_minutes(r1["arr_time"])
                for nb_id, walk_m in hub_neighbors.get(t1_id, [(t1_id, 0.0)]):
                    r2_list = leg2_by_t2.get(nb_id)
                    if not r2_list:
                        continue
                    min_buffer = 3 if walk_m > 20.0 else 0
                    for r2 in r2_list:
                        if r1["trip_id"] == r2["trip_id"] or r1["route_id"] == r2["route_id"]:
                            continue
                        wait_mins = _hhmm_to_minutes(r2["dep_time"]) - arr1_mins
                        if wait_mins < min_buffer or wait_mins > 90:
                            continue
                        candidates.append((r1, r2, walk_m))

            if not candidates:
                return []

            if filter_type == FilterType.ARRIVAL_BEFORE:
                candidates.sort(
                    key=lambda x: (-_hhmm_to_minutes(x[1]["arr_time"]), -_hhmm_to_minutes(x[0]["dep_time"]), x[2])
                )
            elif filter_type == FilterType.ARRIVAL_NEAR:
                candidates.sort(
                    key=lambda x: (_hhmm_to_minutes(x[1]["arr_time"]), -_hhmm_to_minutes(x[0]["dep_time"]), x[2])
                )
            else:
                candidates.sort(
                    key=lambda x: (_hhmm_to_minutes(x[0]["dep_time"]), _hhmm_to_minutes(x[1]["arr_time"]), x[2])
                )

            seen_slots: Set[Tuple[str, str]] = set()
            itineraries: List[Itinerary] = []
            valid_tt = {e.value for e in TransportType}

            for r1, r2, _walk_m in candidates:
                dedup_key = (r1["dep_time"], r2["arr_time"])
                if dedup_key in seen_slots:
                    continue
                seen_slots.add(dedup_key)

                t1_type = (
                    TransportType(r1["ttype"])
                    if r1["ttype"] in valid_tt
                    else TransportType.QUALSIASI
                )
                t2_type = (
                    TransportType(r2["ttype"])
                    if r2["ttype"] in valid_tt
                    else TransportType.QUALSIASI
                )

                path1 = self._fetch_trip_segment_path(
                    conn, str(r1["trip_id"]), int(r1["seq1"]), int(r1["seq2"])
                )
                path2 = self._fetch_trip_segment_path(
                    conn, str(r2["trip_id"]), int(r2["seq1"]), int(r2["seq2"])
                )

                seg1 = TripSegment(
                    transport_type=t1_type,
                    route_name=r1["route_name"],
                    headsign=r1["headsign"],
                    departure_stop=r1["dep_stop"],
                    departure_time=r1["dep_time"],
                    arrival_stop=r1["t1_stop"],
                    arrival_time=r1["arr_time"],
                    stops_path=path1,
                )
                seg2 = TripSegment(
                    transport_type=t2_type,
                    route_name=r2["route_name"],
                    headsign=r2["headsign"],
                    departure_stop=r2["t2_stop"],
                    departure_time=r2["dep_time"],
                    arrival_stop=r2["arr_stop"],
                    arrival_time=r2["arr_time"],
                    stops_path=path2,
                )
                itineraries.append(
                    Itinerary(
                        departure_stop=r1["dep_stop"],
                        departure_time=r1["dep_time"],
                        arrival_stop=r2["arr_stop"],
                        arrival_time=r2["arr_time"],
                        is_direct=False,
                        segments=[seg1, seg2],
                    )
                )
                if len(itineraries) >= limit:
                    break

            return itineraries


transit_service = TransitService()
