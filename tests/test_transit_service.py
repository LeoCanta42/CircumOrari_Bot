"""Unit tests for transit search and timetable calculation service."""

import pytest
from fce_bot.core.models import TransportType, FilterType
from fce_bot.services.transit_service import transit_service


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
