"""Unit tests for transit search and timetable calculation service."""

import pytest
from circumorari_bot.core.models import TransportType, FilterType, Departure
from circumorari_bot.services.transit_service import transit_service


def test_search_stops():
    """Verify stop search returns matching records."""
    stops = transit_service.search_stops("Milo")
    assert len(stops) > 0
    assert any("MILO" in s.stop_name.upper() for s in stops)


def test_get_stops_by_type():
    """Verify retrieval of stops filtered by transport mode."""
    metro_stops = transit_service.get_stops_by_type(TransportType.METRO)
    assert len(metro_stops) >= 10
    names = [s.stop_name for s in metro_stops]
    assert "MONTE PO" in names
    assert "BORGO" in names


def test_direct_train_trip():
    """Verify finding direct train trips between Paternò and Randazzo."""
    trips = transit_service.find_trips(
        origin="PATERNO'",
        destination="RANDAZZO",
        target_time="08:00",
        transport_type=TransportType.TRENO,
        filter_type=FilterType.DEPARTURE_AFTER
    )
    assert len(trips) > 0
    first = trips[0]
    assert first.is_direct is True
    assert first.departure_time >= "08:00"
    assert first.duration_minutes > 0


def test_metro_trip():
    """Verify finding metro trips between Monte Po and Borgo."""
    trips = transit_service.find_trips(
        origin="MONTE PO",
        destination="BORGO",
        target_time="09:00",
        transport_type=TransportType.METRO
    )
    assert len(trips) > 0
    assert trips[0].segments[0].transport_type == TransportType.METRO


def test_unresolvable_stop_returns_empty():
    """Verify query with nonexistent stop name handles gracefully."""
    trips = transit_service.find_trips(
        origin="NON_EXISTENT_STATION_XYZ",
        destination="RANDAZZO",
        target_time="10:00"
    )
    assert trips == []


def test_metro_does_not_return_train_trips():
    """Verify that searching train stations with METRO transport type returns no trips."""
    trips = transit_service.find_trips(
        origin="PATERNO'",
        destination="RANDAZZO",
        target_time="08:00",
        transport_type=TransportType.METRO
    )
    assert trips == []


def test_transport_type_from_str():
    """Verify string parsing of transport modes."""
    assert TransportType.from_str("METRO") == TransportType.METRO
    assert TransportType.from_str("Metropolitana") == TransportType.METRO
    assert TransportType.from_str("TRENO") == TransportType.TRENO
    assert TransportType.from_str("TR") == TransportType.TRENO
    assert TransportType.from_str("BUS") == TransportType.BUS
    assert TransportType.from_str("QUALSIASI") == TransportType.QUALSIASI


def test_similar_stop_name_catania_santa_sofia():
    """Verify imprecise stop names like 'Catania Santa Sofia' match 'Parcheggio Santa Sofia' and find routes."""
    res = transit_service.resolve_location("Catania Santa Sofia")
    assert res.match_kind == "similar"
    assert len(res.matched_stops) > 0
    assert "SANTA SOFIA" in res.matched_stops[0].stop_name.upper()
    assert res.matched_stops[0].city == "Catania"

    # Multi-modal route (Bus -> Metro Borgo -> Stesicoro) with traced stop sequence
    trips = transit_service.find_trips(
        origin="Catania Santa Sofia",
        destination="Stesicoro",
        target_time="09:00",
    )
    assert len(trips) > 0
    first = trips[0]
    assert len(first.segments) >= 1
    assert all(len(seg.stops_path) >= 2 for seg in first.segments)


def test_nearest_stops_and_address_landmark_resolution():
    """Verify arbitrary places/addresses resolve to the nearest stop with walking distance."""
    res = transit_service.resolve_location("Piazza Duomo Catania", allow_geocoding=True, allow_network_geocoding=False)
    assert res.match_kind == "nearest_place"
    assert res.nearest_distance_m is not None and res.nearest_distance_m < 1500
    assert any("STESICORO" in s.stop_name.upper() for s in res.matched_stops)

    trips = transit_service.find_trips_from_resolutions(
        origin_res=res,
        dest_res=transit_service.resolve_location("Paternò"),
        target_time="09:00",
    )
    assert len(trips) > 0


def test_trace_line_stops_and_closest_stop():
    """Verify tracing full stop sequences of a line and finding closest stop on that line to a place."""
    traced = transit_service.trace_line_stops("METRO", near_place="Piazza Duomo Catania")
    assert len(traced) >= 1
    metro_line = traced[0]
    assert len(metro_line["stops"]) == 12
    assert "MONTE PO" in metro_line["stops"]
    assert "STESICORO" in metro_line["stops"]
    assert "closest_stop_to_target" in metro_line
    assert metro_line["closest_stop_to_target"]["stop_name"] == "STESICORO"


def test_city_filtering_and_disambiguated_stop_names():
    """Verify municipality listing, city stop filtering, and disambiguating identical stop names across cities."""
    cities = transit_service.get_cities()
    city_names = {c["city"] for c in cities}
    assert {"Catania", "Belpasso", "Paternò", "Randazzo", "Bronte"}.issubset(city_names)

    belpasso_stops = transit_service.get_stops_by_type(city="Belpasso")
    assert len(belpasso_stops) > 5
    assert all(s.city == "Belpasso" for s in belpasso_stops)

    # Disambiguate 'Corso Sicilia' in Giarre vs Motta Sant'Anastasia
    res_giarre = transit_service.resolve_location("Corso Sicilia (Giarre)")
    res_motta = transit_service.resolve_location("Corso Sicilia (Motta Sant'Anastasia)")
    assert res_giarre.match_kind == "exact"
    assert res_motta.match_kind == "exact"
    assert res_giarre.matched_stops[0].city == "Giarre"
    assert res_motta.matched_stops[0].city == "Motta Sant'Anastasia"
    assert set(res_giarre.stop_ids).isdisjoint(set(res_motta.stop_ids))


def test_resolve_gtfs_url_success(monkeypatch):
    """Verify dynamic scraping successfully discovers GTFS download link."""
    from unittest.mock import MagicMock
    import requests
    from circumorari_bot.data.gtfs_loader import resolve_gtfs_url

    mock_html = """
    <html>
        <body>
            <h1>Pubblicazione Orari GTFS</h1>
            <a href="/download/general-transit-feed-specification-fce-new/?wpdmdl=99999">
                General Transit Feed Specification FCE - 2026/2027
            </a>
        </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html
    mock_resp.raise_for_status = MagicMock()

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: mock_resp)

    url = resolve_gtfs_url("https://www.circumetnea.it/mock-gtfs-page/")
    assert url == "https://www.circumetnea.it/download/general-transit-feed-specification-fce-new/?wpdmdl=99999"


def test_resolve_gtfs_url_subpage_follow(monkeypatch):
    """Verify resolving follows publication subpage link if scraped from index page."""
    from unittest.mock import MagicMock
    import requests
    from circumorari_bot.data.gtfs_loader import resolve_gtfs_url

    page1_html = """
    <html>
        <body>
            <a href="/pubblicazione-orari-del-trasporto-pubblico-locale-in-formato-gtfs/">
                Pubblicazione orari in formato GTFS
            </a>
        </body>
    </html>
    """
    page2_html = """
    <html>
        <body>
            <a href="https://www.circumetnea.it/download/gtfs-archive/?wpdmdl=88888">
                Download GTFS Archive
            </a>
        </body>
    </html>
    """

    def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "pubblicazione" in url:
            resp.text = page2_html
        else:
            resp.text = page1_html
        return resp

    monkeypatch.setattr(requests, "get", mock_get)

    url = resolve_gtfs_url("https://www.circumetnea.it/le-nostre-linee/")
    assert url == "https://www.circumetnea.it/download/gtfs-archive/?wpdmdl=88888"


def test_resolve_gtfs_url_fallback_on_error_and_empty(monkeypatch):
    """Verify scraper gracefully falls back to settings.CIRCUMETNEA_GTFS_URL on network failure or empty links."""
    import requests
    from circumorari_bot.config import settings
    from circumorari_bot.data.gtfs_loader import resolve_gtfs_url

    # 1. Network exception
    def mock_fail(*args, **kwargs):
        raise requests.RequestException("Connection error")

    monkeypatch.setattr(requests, "get", mock_fail)
    url_err = resolve_gtfs_url()
    assert url_err == settings.CIRCUMETNEA_GTFS_URL

    # 2. HTML without GTFS links
    from unittest.mock import MagicMock
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><p>No links here</p></body></html>"
    mock_resp.raise_for_status = MagicMock()

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: mock_resp)
    url_empty = resolve_gtfs_url()
    assert url_empty == settings.CIRCUMETNEA_GTFS_URL


def test_download_and_sync_gtfs_explicit_vs_resolved_url(monkeypatch):
    """Verify download_and_sync_gtfs uses explicit URL or resolves dynamically."""
    from unittest.mock import MagicMock
    import requests
    from circumorari_bot.data import gtfs_loader

    resolve_called = []

    def mock_resolve(page_url=None):
        resolve_called.append(True)
        return "https://www.circumetnea.it/dynamically-resolved-feed.zip"

    monkeypatch.setattr(gtfs_loader, "resolve_gtfs_url", mock_resolve)
    monkeypatch.setattr(gtfs_loader, "load_gtfs_from_zip", lambda *args, **kwargs: (10, 20, 30))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"fake_zip"
    mock_resp.raise_for_status = MagicMock()

    fetched_urls = []

    def mock_get(url, *args, **kwargs):
        fetched_urls.append(url)
        return mock_resp

    monkeypatch.setattr(requests, "get", mock_get)

    # Call with None -> should invoke resolve_gtfs_url
    gtfs_loader.download_and_sync_gtfs(feed_url=None)
    assert len(resolve_called) == 1
    assert fetched_urls[-1] == "https://www.circumetnea.it/dynamically-resolved-feed.zip"

    # Call with explicit URL -> should NOT invoke resolve_gtfs_url
    explicit = "https://example.com/custom_feed.zip"
    gtfs_loader.download_and_sync_gtfs(feed_url=explicit)
    assert len(resolve_called) == 1  # count did not increase
    assert fetched_urls[-1] == explicit


def test_get_upcoming_departures_metro():
    """Verify upcoming metro departures: chronological order, non-terminus boarding only."""
    deps = transit_service.get_upcoming_departures(
        stop_name="STESICORO",
        current_time="08:00",
        limit=5,
    )
    assert len(deps) > 0
    assert all(d.departure_time >= "08:00" for d in deps)
    assert all(d.transport_type == TransportType.METRO for d in deps)
    # Ensure terminus drop-offs (arriving at Stesicoro) are never included as departures
    assert all("STESICORO" not in d.headsign.upper() for d in deps)
    first = deps[0]
    assert first.stop_name == "STESICORO"
    assert "Monte Po" in first.display_line or "MONTE PO" in first.headsign.upper()
    assert "🚇 Metro" in first.display_line


def test_get_upcoming_departures_train_and_bus():
    """Verify train and bus upcoming departures by stop_id and transport mode filter."""
    # Paterno train departures
    deps_train = transit_service.get_upcoming_departures(
        stop_name="PATERNO'",
        current_time="08:00",
        limit=4,
        transport_type=TransportType.TRENO,
    )
    assert len(deps_train) > 0
    assert all(d.transport_type == TransportType.TRENO for d in deps_train)
    assert any("🚆 Treno" in d.display_line for d in deps_train)

    # Borgo bus stop by stop_id ('1123')
    deps_bus = transit_service.get_upcoming_departures(
        stop_id="1123",
        current_time="08:00",
        limit=4,
    )
    assert len(deps_bus) > 0
    assert all(d.transport_type == TransportType.BUS for d in deps_bus)
    assert any("🚌 Bus" in d.display_line for d in deps_bus)


def test_get_upcoming_departures_empty_and_night():
    """Verify handling of night hours (no trips) and nonexistent stop names."""
    # Beyond last operating trip
    deps_night = transit_service.get_upcoming_departures(
        stop_name="STESICORO",
        current_time="23:59",
    )
    assert deps_night == []

    # Nonexistent stop
    deps_none = transit_service.get_upcoming_departures(
        stop_name="STOP_DOES_NOT_EXIST_9999",
        current_time="08:00",
    )
    assert deps_none == []


def test_departure_display_line_formatting():
    """Verify clean single-line representation and destination prefixes."""
    # Metro generic route name simplification
    d1 = Departure(
        departure_time="08:50",
        transport_type=TransportType.METRO,
        route_name="METROPOLITANA",
        headsign="STESICORO",
    )
    assert d1.display_line == "08:50 – 🚇 Metro per Stesicoro"

    # Bus with line number and already title-cased destination
    d2 = Departure(
        departure_time="09:05",
        transport_type=TransportType.BUS,
        route_name="1-2",
        headsign="Adrano",
    )
    assert d2.display_line == "09:05 – 🚌 Bus 1-2 per Adrano"

    # Headsign with "Direzione" prefix
    d3 = Departure(
        departure_time="10:15",
        transport_type=TransportType.TRENO,
        route_name="SUPERFICIE",
        headsign="Direzione Randazzo",
    )
    assert d3.display_line == "10:15 – 🚆 Treno per Randazzo"

    # Model to_dict and from_dict serialization
    d1_dict = d1.to_dict()
    assert d1_dict["departure_time"] == "08:50"
    assert d1_dict["transport_type"] == "METRO"
    d1_reconstructed = Departure.from_dict(d1_dict)
    assert d1_reconstructed.departure_time == d1.departure_time
    assert d1_reconstructed.display_line == d1.display_line


