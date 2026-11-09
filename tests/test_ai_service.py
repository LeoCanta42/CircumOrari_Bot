"""Unit tests for AI trip planning assistant and entity extractor."""

import pytest
from fce_bot.core.models import TransportType
from fce_bot.services.ai_service import ai_service
from fce_bot.data.db import normalize_stop_name


def test_extract_entities_standard_format():
    """Verify origin, destination, and time extraction."""
    text = "Come posso andare da Paterno a Randazzo alle 14:30 in treno?"
    origin, dest, time_val, ttype = ai_service.extract_entities(text)
    assert normalize_stop_name(origin) == "PATERNO"
    assert normalize_stop_name(dest) == "RANDAZZO"
    assert time_val == "14:30"
    assert ttype == TransportType.TRENO


def test_extract_entities_metro():
    """Verify metro query extraction."""
    text = "C'è la metro da Monte Po a Borgo alle 9?"
    origin, dest, time_val, ttype = ai_service.extract_entities(text)
    assert "MONTE PO" in normalize_stop_name(origin)
    assert "BORGO" in normalize_stop_name(dest)
    assert time_val == "09:00"
    assert ttype == TransportType.METRO


def test_ai_answer_query():
    """Verify answer generation with factual itineraries."""
    text = "Da Paterno a Randazzo alle 08:30"
    resp = ai_service.answer_query(text)
    assert resp.origin is not None
    assert resp.destination is not None
    assert resp.itineraries_count > 0
    assert "randazzo" in resp.answer.lower()


def test_ai_ticket_query():
    """Verify ticket purchase inquiry returns clean text without markdown link artifacts."""
    resp = ai_service.answer_query("Dove posso acquistare i biglietti FCE?")
    assert len(resp.answer) > 20
    # Should not contain raw markdown link brackets like [Orari Metro](http...)
    assert "](" not in resp.answer
    assert any(w in resp.answer.lower() for w in ["bigliett", "titoli di viaggio", "acquist"])


def test_ai_documents_clean_answer():
    """Verify document request advises consulting official schedules without dumping raw URLs."""
    resp = ai_service.answer_query("Vorrei scaricare la tabella PDF degli orari")
    assert "](" not in resp.answer
    assert "orari" in resp.answer.lower()


def test_ai_arrival_query_paterno_adrano():
    """Verify natural language query asking to arrive by a specific time."""
    query = "Trovami una soluzione che mi faccia arrivare da Paterno ad Adrano per le 15"
    resp = ai_service.answer_query(query)
    assert resp.origin is not None and any(w in resp.origin.lower() for w in ["paterno", "paternò"])
    assert resp.destination is not None and "adrano" in resp.destination.lower()
    assert resp.time == "15:00"
    assert resp.itineraries_count > 0
    assert any(hour in resp.answer for hour in ["14:", "14.5", "14.1", "14.2"])


def test_ai_arrival_query_varied_phrasing():
    """Verify natural language query with colloquial phrasing ('Dimmi come faccio ad essere a...')."""
    query = "Dimmi come faccio ad essere a Adrano partendo da Paterno per le 15"
    resp = ai_service.answer_query(query)
    assert resp.origin is not None and any(w in resp.origin.lower() for w in ["paterno", "paternò"])
    assert resp.destination is not None and "adrano" in resp.destination.lower()
    assert resp.time == "15:00"
    assert resp.itineraries_count > 0
    assert any(hour in resp.answer for hour in ["14:", "14.5", "14.1", "14.2"])


def test_ai_imprecise_stop_catania_santa_sofia(monkeypatch):
    """Verify AI trip assistant resolves similar stop names like 'Catania Santa Sofia' -> 'Parcheggio Santa Sofia'."""
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)
    resp = ai_service.answer_query("Come posso andare da Catania Santa Sofia a Stesicoro alle 09:00?")
    assert resp.itineraries_count > 0
    assert "santa sofia" in (resp.origin or "").lower()
    assert "santa sofia" in resp.answer.lower()
    assert "stesicoro" in resp.answer.lower()


def test_ai_landmark_nearest_stop_query(monkeypatch):
    """Verify AI trip assistant resolves places/addresses to the nearest FCE stop and computes the route."""
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)
    resp = ai_service.answer_query("Come arrivo da Piazza Duomo Catania a Paternò alle 09:00?")
    assert resp.itineraries_count > 0
    assert "stesicoro" in resp.answer.lower()
    assert any(w in resp.answer.lower() for w in ["vicin", "m)", "metri", "fermata"])


def test_ai_line_path_tracing_query(monkeypatch):
    """Verify AI trip assistant can trace the stops along a line."""
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)
    resp = ai_service.answer_query("Quali fermate fa la metropolitana di Catania?")
    assert "monte po" in resp.answer.lower()
    assert "borgo" in resp.answer.lower()
    assert "stesicoro" in resp.answer.lower()


def test_ai_query_without_time_defaults_to_now(monkeypatch):
    """Verify queries without time like 'Voglio andare a Paterno\\' dalla Torre Biologica' default time to actual now."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fixed_now = datetime(2026, 11, 9, 13, 15, tzinfo=ZoneInfo("Europe/Rome"))

    # Test rule-based extraction
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)
    query = "Voglio andare a Paterno' dalla Torre Biologica"
    entities = ai_service.extract_entities(query, now=fixed_now)
    assert entities.origin is not None and "torre biologica" in entities.origin.lower()
    assert entities.destination is not None and "paterno" in entities.destination.lower()
    assert entities.time == "13:15"
    assert entities.has_explicit_time is False

    resp = ai_service.answer_query(query, now=fixed_now)
    assert resp.time == "13:15"
    assert resp.itineraries_count > 0
    assert "torre biologica" in resp.answer.lower() or "policlinico" in resp.answer.lower()


def test_ai_query_asap_and_relative_time(monkeypatch):
    """Verify queries with 'il prima possibile' and 'tra 5 minuti' calculate correct relative times."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fixed_now = datetime(2026, 11, 9, 13, 15, tzinfo=ZoneInfo("Europe/Rome"))
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)

    # 1. 'il prima possibile'
    q_asap = "Voglio andare a Paterno' dalla Torre Biologica il prima possibile"
    e_asap = ai_service.extract_entities(q_asap, now=fixed_now)
    assert "torre biologica" in (e_asap.origin or "").lower()
    assert "paterno" in (e_asap.destination or "").lower()
    assert e_asap.time == "13:15"
    assert "il prima possibile" not in (e_asap.origin or "").lower()

    # 2. 'tra 5 minuti'
    q_5m = "Voglio andare a Paterno' dalla Torre Biologica tra 5 minuti"
    e_5m = ai_service.extract_entities(q_5m, now=fixed_now)
    assert "torre biologica" in (e_5m.origin or "").lower()
    assert "paterno" in (e_5m.destination or "").lower()
    assert e_5m.time == "13:20"
    assert "tra 5 minuti" not in (e_5m.origin or "").lower()

    # 3. 'fra mezz'ora'
    q_half = "Da Catania a Misterbianco fra mezz'ora"
    e_half = ai_service.extract_entities(q_half, now=fixed_now)
    assert "catania" in (e_half.origin or "").lower()
    assert "misterbianco" in (e_half.destination or "").lower()
    assert e_half.time == "13:45"
    assert "mezz'ora" not in (e_half.destination or "").lower()

    # 4. 'subito'
    q_subito = "Vado a Paternò subito partendo da Catania"
    e_subito = ai_service.extract_entities(q_subito, now=fixed_now)
    assert e_subito.time == "13:15"


def test_ai_late_night_query_morning_fallback(monkeypatch):
    """Verify late night queries with no remaining trips today gracefully provide next morning departures."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    late_now = datetime(2026, 11, 9, 23, 30, tzinfo=ZoneInfo("Europe/Rome"))
    monkeypatch.setattr("fce_bot.services.ai_service.settings.GEMINI_API_KEY", None)

    query = "Voglio andare a Paterno' dalla Torre Biologica"
    resp = ai_service.answer_query(query, now=late_now)
    assert resp.time == "23:30"
    assert resp.itineraries_count > 0
    assert any(w in resp.answer.lower() for w in ["prime corse", "mattina", "terminato"])

