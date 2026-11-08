"""Municipality (Comune) enrichment for GTFS stops and Etna-area geocoding."""

import json
import logging
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

_MUNICIPALITIES_PATH = Path(__file__).resolve().parent / "etna_municipalities.json"
_MUNICIPALITIES_CACHE: Optional[List[dict]] = None
_REVERSE_GEOCODE_CACHE: Dict[Tuple[float, float], str] = {}
_FORWARD_GEOCODE_CACHE: Dict[str, Optional[Tuple[float, float, str]]] = {}

# Known municipality aliases for name-based fallback when coordinates are missing
KNOWN_CITY_ALIASES: List[Tuple[str, str]] = [
    ("SANTA MARIA DI LICODIA", "Santa Maria di Licodia"),
    ("LICODIA", "Santa Maria di Licodia"),
    ("MOTTA SANT ANASTASIA", "Motta Sant'Anastasia"),
    ("MOTTA S ANASTASIA", "Motta Sant'Anastasia"),
    ("CASTIGLIONE DI SICILIA", "Castiglione di Sicilia"),
    ("CASTIGLIONE", "Castiglione di Sicilia"),
    ("PASSOPISCIARO", "Castiglione di Sicilia"),
    ("SOLICCHIATA", "Castiglione di Sicilia"),
    ("VERZELLA", "Castiglione di Sicilia"),
    ("ROVELLO", "Castiglione di Sicilia"),
    ("PIEDIMONTE ETNEO", "Piedimonte Etneo"),
    ("PIEDIMONTE", "Piedimonte Etneo"),
    ("FIUMEFREDDO DI SICILIA", "Fiumefreddo di Sicilia"),
    ("FIUMEFREDDO", "Fiumefreddo di Sicilia"),
    ("CAMPOROTONDO ETNEO", "Camporotondo Etneo"),
    ("CAMPO ROTONDO", "Camporotondo Etneo"),
    ("CAMPOROTONDO", "Camporotondo Etneo"),
    ("SAN PIETRO CLARENZA", "San Pietro Clarenza"),
    ("ACI SANT ANTONIO", "Aci Sant'Antonio"),
    ("SANT ALFIO", "Sant'Alfio"),
    ("ZAFFERANA ETNEA", "Zafferana Etnea"),
    ("ZAFFERANA", "Zafferana Etnea"),
    ("LINGUAGLOSSA", "Linguaglossa"),
    ("CALATABIANO", "Calatabiano"),
    ("MISTERBIANCO", "Misterbianco"),
    ("BIANCAVILLA", "Biancavilla"),
    ("MASCALUCIA", "Mascalucia"),
    ("TRECASTAGNI", "Trecastagni"),
    ("ACIREALE", "Acireale"),
    ("RANDAZZO", "Randazzo"),
    ("BELPASSO", "Belpasso"),
    ("VALCORRENTE", "Belpasso"),
    ("ETNAPOLIS", "Belpasso"),
    ("NICOLOSI", "Nicolosi"),
    ("PATERNO", "Paternò"),
    ("RAGALNA", "Ragalna"),
    ("MASCALI", "Mascali"),
    ("RIPOSTO", "Riposto"),
    ("MALETTO", "Maletto"),
    ("CATANIA", "Catania"),
    ("GIARRE", "Giarre"),
    ("BRONTE", "Bronte"),
    ("ADRANO", "Adrano"),
    ("PEDARA", "Pedara"),
]

# Offline instant gazetteer of major landmarks, hospitals, universities & hubs in Catania / Etna area
ETNA_LANDMARKS_GAZETTEER: Dict[str, Tuple[float, float, str]] = {
    "POLICLINICO": (37.52935, 15.06942, "Policlinico Universitario Rodolico (Catania)"),
    "POLICLINICO CATANIA": (37.52935, 15.06942, "Policlinico Universitario Rodolico (Catania)"),
    "OSPEDALE POLICLINICO": (37.52935, 15.06942, "Policlinico Universitario Rodolico (Catania)"),
    "CITTADELLA UNIVERSITARIA": (37.52560, 15.07210, "Cittadella Universitaria UNICT (Catania)"),
    "TORRE BIOLOGICA": (37.52850, 15.06720, "Torre Biologica UNICT (Catania)"),
    "SANTA SOFIA": (37.53520, 15.06630, "Via Santa Sofia (Catania)"),
    "VIA SANTA SOFIA": (37.53520, 15.06630, "Via Santa Sofia (Catania)"),
    "PIAZZA DUOMO": (37.50250, 15.08727, "Piazza del Duomo (Catania)"),
    "DUOMO CATANIA": (37.50250, 15.08727, "Piazza del Duomo (Catania)"),
    "DUOMO DI CATANIA": (37.50250, 15.08727, "Piazza del Duomo (Catania)"),
    "CATTEDRALE CATANIA": (37.50250, 15.08727, "Cattedrale di Sant'Agata / Piazza Duomo (Catania)"),
    "LIOTRU": (37.50250, 15.08727, "Fontana dell'Elefante - Piazza Duomo (Catania)"),
    "PESCHERIA": (37.50170, 15.08650, "La Pescheria / Piazza Duomo (Catania)"),
    "PIAZZA UNIVERSITA": (37.50365, 15.08680, "Piazza Università (Catania)"),
    "MONASTERO DEI BENEDETTINI": (37.50370, 15.08020, "Monastero dei Benedettini (Catania)"),
    "BENEDETTINI": (37.50370, 15.08020, "Monastero dei Benedettini (Catania)"),
    "CASTELLO URSINO": (37.49895, 15.08465, "Castello Ursino (Catania)"),
    "TEATRO MASSIMO BELLINI": (37.50425, 15.09030, "Teatro Massimo Vincenzo Bellini (Catania)"),
    "PIAZZA TEATRO MASSIMO": (37.50425, 15.09030, "Piazza Teatro Massimo (Catania)"),
    "VILLA BELLINI": (37.51170, 15.08370, "Giardino Bellini / Villa Bellini (Catania)"),
    "GIARDINO BELLINI": (37.51170, 15.08370, "Giardino Bellini (Catania)"),
    "VIA ETNEA": (37.51100, 15.08520, "Via Etnea (Catania)"),
    "PIAZZA STESICORO": (37.50773, 15.08786, "Piazza Stesicoro (Catania)"),
    "ANFITEATRO ROMANO": (37.50760, 15.08570, "Anfiteatro Romano - Piazza Stesicoro (Catania)"),
    "FIERA DI CATANIA": (37.50820, 15.08920, "Fiera 'A Fera o Luni' - Piazza Carlo Alberto (Catania)"),
    "PIAZZA CARLO ALBERTO": (37.50850, 15.09010, "Piazza Carlo Alberto (Catania)"),
    "STAZIONE CENTRALE": (37.50685, 15.09990, "Stazione Catania Centrale FS (Piazza Papa Giovanni XXIII)"),
    "CATANIA CENTRALE": (37.50685, 15.09990, "Stazione Catania Centrale FS (Piazza Papa Giovanni XXIII)"),
    "PIAZZA PAPA GIOVANNI XXIII": (37.50810, 15.09980, "Piazza Papa Giovanni XXIII (Catania)"),
    "LE CIMINIERE": (37.50910, 15.10250, "Centro Fieristico Le Ciminiere (Catania)"),
    "CIMINIERE": (37.50910, 15.10250, "Centro Fieristico Le Ciminiere (Catania)"),
    "CORSO ITALIA CATANIA": (37.51639, 15.09658, "Corso Italia (Catania)"),
    "PIAZZA EUROPA": (37.51710, 15.10450, "Piazza Europa (Catania)"),
    "LUNGOMARE CATANIA": (37.52100, 15.10820, "Lungomare di Catania (Ognina)"),
    "TRIBUNALE CATANIA": (37.51620, 15.09380, "Tribunale di Catania - Piazza Verga"),
    "PIAZZA VERGA": (37.51620, 15.09380, "Piazza Giovanni Verga (Catania)"),
    "VULCANIA": (37.52450, 15.08480, "Piazza Aldo Moro / Vulcania (Catania)"),
    "STADIO ANGELO MASSIMINO": (37.51630, 15.07150, "Stadio Angelo Massimino - Cibali (Catania)"),
    "STADIO CIBALI": (37.51630, 15.07150, "Stadio Angelo Massimino - Cibali (Catania)"),
    "OSPEDALE GARIBALDI NESIMA": (37.51465, 15.04625, "Ospedale Garibaldi-Nesima (Catania)"),
    "GARIBALDI NESIMA": (37.51465, 15.04625, "Ospedale Garibaldi-Nesima (Catania)"),
    "OSPEDALE GARIBALDI CENTRO": (37.51385, 15.07920, "Ospedale Garibaldi Centro (Piazza Santa Maria di Gesù, Catania)"),
    "OSPEDALE CANNIZZARO": (37.53480, 15.10720, "Ospedale Cannizzaro (Catania)"),
    "OSPEDALE SAN MARCO": (37.48410, 15.04280, "Ospedale San Marco - Librino (Catania)"),
    "AEROPORTO FONTANAROSSA": (37.46980, 15.06640, "Aeroporto di Catania-Fontanarossa"),
    "AEROPORTO CATANIA": (37.46980, 15.06640, "Aeroporto di Catania-Fontanarossa"),
    "FONTANAROSSA": (37.46980, 15.06640, "Aeroporto di Catania-Fontanarossa"),
    "ETNAPOLIS": (37.54211, 14.95523, "Centro Commerciale Etnapolis (Belpasso)"),
    "CENTRO SICILIA": (37.48010, 15.01080, "Parco Commerciale Centro Sicilia (Misterbianco)"),
    "ETNALAND": (37.53640, 14.93360, "Parco Tematico Etnaland (Belpasso)"),
    "KATANE": (37.53890, 15.05180, "Centro Commerciale Katanè (Gravina di Catania)"),
}


def _load_municipalities() -> List[dict]:
    """Load bundled Etna-area ISTAT municipal boundaries once."""
    global _MUNICIPALITIES_CACHE
    if _MUNICIPALITIES_CACHE is None:
        try:
            if _MUNICIPALITIES_PATH.exists():
                _MUNICIPALITIES_CACHE = json.loads(_MUNICIPALITIES_PATH.read_text(encoding="utf-8"))
            else:
                _MUNICIPALITIES_CACHE = []
        except Exception as exc:
            logger.warning("Failed to load etna_municipalities.json: %s", exc)
            _MUNICIPALITIES_CACHE = []
    return _MUNICIPALITIES_CACHE


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray-casting point-in-polygon ring check."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_muni(lon: float, lat: float, muni: dict) -> bool:
    """Check if (lon, lat) lies inside a municipality polygon/multipolygon."""
    bbox = muni.get("bbox")
    if bbox and (lon < bbox[0] or lat < bbox[1] or lon > bbox[2] or lat > bbox[3]):
        return False

    gtype = muni.get("type")
    coords = muni.get("coordinates", [])
    if gtype == "Polygon":
        if not coords:
            return False
        return _point_in_ring(lon, lat, coords[0]) and not any(
            _point_in_ring(lon, lat, hole) for hole in coords[1:]
        )
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly and _point_in_ring(lon, lat, poly[0]) and not any(
                _point_in_ring(lon, lat, hole) for hole in poly[1:]
            ):
                return True
    return False


def _clean_text_upper(s: str) -> str:
    if not s:
        return ""
    s = s.upper()
    s = (
        s.replace("Ù", "U")
        .replace("Ò", "O")
        .replace("È", "E")
        .replace("É", "E")
        .replace("À", "A")
        .replace("Ì", "I")
        .replace("’", " ")
        .replace("'", " ")
    )
    s = re.sub(r"[().,\-–_/\\]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve_city_by_coords(lat: float, lon: float, allow_network: bool = False) -> str:
    """Resolve municipality (Comune) for given coordinates using bundled ISTAT boundaries + optional Nominatim fallback."""
    if not lat or not lon:
        return ""

    for muni in _load_municipalities():
        if _point_in_muni(lon, lat, muni):
            return muni["name"]

    if allow_network:
        cell = (round(lat, 3), round(lon, 3))
        if cell in _REVERSE_GEOCODE_CACHE:
            return _REVERSE_GEOCODE_CACHE[cell]
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "accept-language": "it"},
                headers={"User-Agent": "FCE-Bot/2.0 (+https://github.com/LeoCanta42/FCE_bot)"},
                timeout=4,
            )
            if resp.status_code == 200:
                addr = resp.json().get("address", {})
                city = (
                    addr.get("city")
                    or addr.get("town")
                    or addr.get("village")
                    or addr.get("municipality")
                    or ""
                )
                _REVERSE_GEOCODE_CACHE[cell] = city
                return city
        except Exception as exc:
            logger.debug("Nominatim reverse geocode fallback failed for (%s, %s): %s", lat, lon, exc)

    return ""


def resolve_city_from_name(stop_name: str) -> str:
    """Fallback municipality extraction from stop name tokens."""
    padded = f" {_clean_text_upper(stop_name)} "
    for alias, canonical in KNOWN_CITY_ALIASES:
        if f" {alias} " in padded:
            return canonical
    return ""


def resolve_stop_city(stop_name: str, lat: float = 0.0, lon: float = 0.0, allow_network: bool = False) -> str:
    """Subtask resolver: determine exact city (Comune) of a GTFS stop from coordinates or name."""
    if lat and lon:
        city = resolve_city_by_coords(lat, lon, allow_network=allow_network)
        if city:
            return city
    return resolve_city_from_name(stop_name)


def enrich_stops_batch(
    raw_stops: List[Tuple[str, str, str, float, float, int, int, int]],
    allow_network: bool = True
) -> List[Tuple[str, str, str, str, str, float, float, int, int, int]]:
    """GTFS Sync Subtask: enrich a batch of parsed GTFS stops with `city` and `normalized_city`."""
    enriched = []
    for sid, sname, norm_name, lat, lon, has_bus, has_train, has_metro in raw_stops:
        city = resolve_stop_city(sname, lat, lon, allow_network=allow_network)
        norm_city = _clean_text_upper(city)
        enriched.append((sid, sname, norm_name, city, norm_city, lat, lon, has_bus, has_train, has_metro))
    return enriched


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in meters between two WGS84 coordinates."""
    if not (lat1 and lon1 and lat2 and lon2):
        return float("inf")
    r_earth = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def geocode_place_in_etna_area(
    place_query: str,
    allow_network: bool = True
) -> Optional[Tuple[float, float, str]]:
    """Resolve a freeform address, landmark, or place in the Catania/Etna area to (lat, lon, label).

    Checks the instant local landmark gazetteer first, then queries OpenStreetMap Nominatim
    bounded to eastern Sicily if network access is allowed.
    """
    if not place_query or len(place_query.strip()) < 3:
        return None

    clean_key = _clean_text_upper(place_query)
    # Strip leading articles/prepositions common in Italian queries
    clean_key_stripped = re.sub(
        r"^(?:IN|A|AD|AL|ALLO|ALLA|ALLE|AGLI|AI|DA|DAL|DALLA|PRESSO|VICINO\s+A|VICINO|ZONA|QUARTIERE)\s+",
        "",
        clean_key
    ).strip()

    for key_candidate in (clean_key, clean_key_stripped):
        if key_candidate in ETNA_LANDMARKS_GAZETTEER:
            return ETNA_LANDMARKS_GAZETTEER[key_candidate]
        # Check without trailing "DI CATANIA" / "CATANIA" if landmark is unambiguous
        without_ct = re.sub(r"\s+(?:DI\s+)?CATANIA$", "", key_candidate).strip()
        if without_ct in ETNA_LANDMARKS_GAZETTEER:
            return ETNA_LANDMARKS_GAZETTEER[without_ct]

    if not allow_network:
        return None

    if clean_key_stripped in _FORWARD_GEOCODE_CACHE:
        return _FORWARD_GEOCODE_CACHE[clean_key_stripped]

    # Query Nominatim bounded to eastern Sicily (Catania / Etna / Alcantara area)
    has_city_hint = any(alias in f" {clean_key_stripped} " for alias, _ in KNOWN_CITY_ALIASES)
    candidate_queries = [f"{place_query.strip()}, Sicilia, Italia"]
    if not has_city_hint:
        candidate_queries.insert(0, f"{place_query.strip()}, Provincia di Catania, Sicilia, Italia")

    headers = {"User-Agent": "FCE-Bot/2.0 (+https://github.com/LeoCanta42/FCE_bot)"}
    for q_str in candidate_queries:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": q_str,
                    "format": "jsonv2",
                    "limit": 3,
                    "countrycodes": "it",
                    "viewbox": "14.60,38.05,15.35,37.30",
                    "bounded": 1,
                    "accept-language": "it",
                },
                headers=headers,
                timeout=4,
            )
            if resp.status_code == 200:
                items = resp.json()
                if items:
                    best = items[0]
                    lat = float(best["lat"])
                    lon = float(best["lon"])
                    if 37.30 <= lat <= 38.05 and 14.60 <= lon <= 15.35:
                        disp = best.get("name") or best.get("display_name", place_query).split(",")[0].strip()
                        city = resolve_city_by_coords(lat, lon, allow_network=False)
                        label = f"{disp} ({city})" if city and city.lower() not in disp.lower() else disp
                        result = (lat, lon, label)
                        _FORWARD_GEOCODE_CACHE[clean_key_stripped] = result
                        return result
        except Exception as exc:
            logger.debug("Nominatim forward geocode failed for '%s': %s", q_str, exc)

    _FORWARD_GEOCODE_CACHE[clean_key_stripped] = None
    return None
