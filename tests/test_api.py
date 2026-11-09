"""Integration tests for FastAPI endpoints."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from starlette.testclient import TestClient
from api.index import app

client = TestClient(app)


def test_health_endpoint():
    """Verify health endpoint."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "fce-bot"


def test_home_page():
    """Verify HTML home dashboard loads."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Circumetnea" in resp.text


def test_favicon():
    """Verify favicon route returns valid icon."""
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert any(t in resp.headers["content-type"] for t in ("image/x-icon", "image/png", "image/svg+xml"))


def test_pwa_manifest_and_sw():
    """Verify PWA manifest and service worker routes."""
    # Manifest prioritizing SVG vector icon
    resp_m = client.get("/manifest.webmanifest")
    assert resp_m.status_code == 200
    m_data = resp_m.json()
    assert m_data["name"] == "Ferrovia Circumetnea & Metro"
    assert m_data["display"] == "standalone"
    assert m_data["icons"][0]["src"] == "/static/icons/icon.svg"
    assert "svg" in m_data["icons"][0]["type"]
    assert len(m_data["icons"]) >= 3

    # Service Worker
    resp_sw = client.get("/sw.js")
    assert resp_sw.status_code == 200
    assert "javascript" in resp_sw.headers["content-type"]
    assert "CACHE_NAME" in resp_sw.text
    assert resp_sw.headers.get("service-worker-allowed") == "/"

    # Static Icon
    resp_icon = client.get("/static/icons/icon.svg")
    assert resp_icon.status_code == 200
    assert "svg" in resp_icon.headers["content-type"]


def test_api_stops():
    """Verify stops API endpoint."""
    resp = client.get("/api/stops?query=Milo")
    assert resp.status_code == 200
    stops = resp.json()
    assert len(stops) > 0
    assert any("MILO" in s["name"].upper() for s in stops)


def test_api_stops_type_filtering():
    """Verify stops API filtered by transport mode."""
    resp = client.get("/api/stops?type=METRO&limit=100")
    assert resp.status_code == 200
    stops = resp.json()
    assert len(stops) == 12
    assert all(s["metro"] is True for s in stops)


def test_api_timetable():
    """Verify timetable lookup API endpoint."""
    resp = client.get("/api/timetable?origin=PATERNO'&destination=RANDAZZO&time=08:00")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    assert len(data["itineraries"]) > 0


def test_api_timetable_mode_filtering():
    """Verify transport mode strict filtering in timetable queries."""
    # Searching train route under METRO must return 0 results
    resp_metro_err = client.get("/api/timetable?origin=PATERNO'&destination=RANDAZZO&transport=METRO")
    assert resp_metro_err.status_code == 200
    assert resp_metro_err.json()["count"] == 0

    # Searching train route under TRENO must return train results
    resp_train = client.get("/api/timetable?origin=PATERNO'&destination=RANDAZZO&transport=TRENO")
    assert resp_train.status_code == 200
    train_data = resp_train.json()
    assert train_data["count"] > 0
    for it in train_data["itineraries"]:
        for seg in it["segments"]:
            assert seg["type"] == "TRENO"

    # Searching metro route under METRO must return metro results
    resp_metro = client.get("/api/timetable?origin=MONTE PO&destination=STESICORO&transport=METRO")
    assert resp_metro.status_code == 200
    metro_data = resp_metro.json()
    assert metro_data["count"] > 0
    for it in metro_data["itineraries"]:
        for seg in it["segments"]:
            assert seg["type"] == "METRO"


def test_api_documents():
    """Verify documents API endpoint."""
    resp = client.get("/api/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert "metro_url" in data
    assert "train_url" in data
    assert "bus_url" in data


def test_api_ai_ask():
    """Verify AI trip assistant API endpoint is disabled from web (returns 403 Forbidden)."""
    resp = client.post("/api/ai/ask", json={"question": "Da Paterno a Randazzo alle 08:30"})
    assert resp.status_code == 403
    assert "disabilitato" in resp.json()["detail"].lower()


def test_gdpr_user_erasure_and_manual_opt_in():
    """Verify GDPR right to erasure (Art. 17) and explicit news opt-in in UsersRepository."""
    from fce_bot.data.users_repo import UsersRepository
    repo = UsersRepository()
    test_chat_id = 999888777

    # 1. Normal interaction: user must NOT be automatically subscribed to news broadcasts
    repo.register_or_update(chat_id=test_chat_id)
    assert repo.is_user_subscribed(test_chat_id) is False
    subscribed = repo.get_subscribed_chat_ids()
    assert str(test_chat_id) not in subscribed

    # 2. Explicit manual opt-in via button: user is subscribed
    repo.subscribe_user(chat_id=test_chat_id, username="tester")
    assert repo.is_user_subscribed(test_chat_id) is True
    assert str(test_chat_id) in repo.get_subscribed_chat_ids()

    # 3. Explicit manual opt-out via button
    repo.unsubscribe_user(chat_id=test_chat_id)
    assert repo.is_user_subscribed(test_chat_id) is False
    assert str(test_chat_id) not in repo.get_subscribed_chat_ids()

    # 4. Erase user (Right to Erasure)
    deleted = repo.delete_user(test_chat_id)
    assert deleted is True


def test_admin_security_by_default():
    """Verify that unconfigured admin list securely rejects unauthorized users."""
    from fce_bot.bot.handlers.admin import is_admin
    # When admin_user_ids is empty, any random ID must be rejected
    assert is_admin(123456789) is False


def test_api_cities_and_city_stop_filtering():
    """Verify /api/cities and /api/stops?city=... endpoints."""
    resp_cities = client.get("/api/cities")
    assert resp_cities.status_code == 200
    cities = resp_cities.json()
    assert len(cities) >= 20
    assert any(c["city"] == "Catania" and c["count"] > 20 for c in cities)

    resp_stops = client.get("/api/stops?city=Belpasso&limit=100")
    assert resp_stops.status_code == 200
    stops = resp_stops.json()
    assert len(stops) > 5
    assert all(s["city"] == "Belpasso" for s in stops)
    assert all("display_name" in s for s in stops)


def test_api_timetable_similar_stop_and_traced_path():
    """Verify /api/timetable resolves similar stop names and includes traced stops_path."""
    resp = client.get("/api/timetable?origin=Catania%20Santa%20Sofia&destination=Stesicoro&time=09:00")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    assert data["origin_match_kind"] == "similar"
    assert "Santa Sofia" in data["resolved_origin"]
    first_seg = data["itineraries"][0]["segments"][0]
    assert isinstance(first_seg["stops_path"], list)
    assert len(first_seg["stops_path"]) >= 2


def test_web_ui_city_filters_and_stop_picker_present():
    """Verify HTML home dashboard exposes Comune filters and rich scrollable stop dropdowns."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="originCityFilter"' in html
    assert 'id="destCityFilter"' in html
    assert 'id="originDropdown"' in html
    assert 'id="destDropdown"' in html
    assert "stops-path-details" in html


def test_telegram_webhook_secret_auth():
    """Verify webhook secret verification returns 403 on invalid secret."""
    from fce_bot.config import settings

    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.WEBHOOK_SECRET = "test_webhook_secret_123"

        # Missing secret header -> 403
        resp_missing = client.post("/api/webhook", json={"update_id": 1})
        assert resp_missing.status_code == 403

        # Invalid secret header -> 403
        resp_invalid = client.post(
            "/api/webhook",
            json={"update_id": 1},
            headers={"x-telegram-bot-api-secret-token": "wrong_secret"}
        )
        assert resp_invalid.status_code == 403
    finally:
        settings.WEBHOOK_SECRET = orig_secret


def test_telegram_webhook_fast_path_pre_checkout_query_valid():
    """Verify fast-path immediately calls answerPreCheckoutQuery(ok=True) for valid subscription payload."""
    from unittest.mock import patch, AsyncMock, MagicMock
    import httpx
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = ""

        webhook_payload = {
            "update_id": 9999,
            "pre_checkout_query": {
                "id": "pcq_test_valid_123",
                "from": {"id": 12345, "is_bot": False, "first_name": "Mario"},
                "currency": "XTR",
                "total_amount": 50,
                "invoice_payload": "ai_sub:12345:1700000000",
            }
        }

        mock_post_resp = MagicMock(spec=httpx.Response)
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"ok": True, "result": True}

        mock_app = MagicMock()
        mock_app._initialized = True
        mock_app.process_update = AsyncMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_httpx_post:
            mock_httpx_post.return_value = mock_post_resp
            with patch("fce_bot.api.router.get_telegram_app", return_value=mock_app):
                resp = client.post("/api/webhook", json=webhook_payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Verify fast-path called answerPreCheckoutQuery with ok=True
        mock_httpx_post.assert_awaited_once()
        call_url = mock_httpx_post.call_args[0][0]
        call_json = mock_httpx_post.call_args[1]["json"]
        assert call_url == "https://api.telegram.org/bot123456:TEST_TOKEN/answerPreCheckoutQuery"
        assert call_json["pre_checkout_query_id"] == "pcq_test_valid_123"
        assert call_json["ok"] is True
        assert "error_message" not in call_json

        # Verify app also processed the update
        mock_app.process_update.assert_awaited_once()
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret


def test_telegram_webhook_fast_path_pre_checkout_query_invalid():
    """Verify fast-path rejects invalid payload with ok=False and error message."""
    from unittest.mock import patch, AsyncMock, MagicMock
    import httpx
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = ""

        webhook_payload = {
            "update_id": 9999,
            "pre_checkout_query": {
                "id": "pcq_test_invalid_456",
                "from": {"id": 12345, "is_bot": False, "first_name": "Mario"},
                "currency": "XTR",
                "total_amount": 50,
                "invoice_payload": "corrupted_payload_data",
            }
        }

        mock_post_resp = MagicMock(spec=httpx.Response)
        mock_post_resp.status_code = 200

        mock_app = MagicMock()
        mock_app._initialized = True
        mock_app.process_update = AsyncMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_httpx_post:
            mock_httpx_post.return_value = mock_post_resp
            with patch("fce_bot.api.router.get_telegram_app", return_value=mock_app):
                resp = client.post("/api/webhook", json=webhook_payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Verify fast-path called answerPreCheckoutQuery with ok=False and error message
        mock_httpx_post.assert_awaited_once()
        call_json = mock_httpx_post.call_args[1]["json"]
        assert call_json["pre_checkout_query_id"] == "pcq_test_invalid_456"
        assert call_json["ok"] is False
        assert "error_message" in call_json
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret


def test_telegram_webhook_fast_path_http_exception_resilience():
    """Verify webhook continues gracefully even if outbound fast-path HTTP call encounters error."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = ""

        webhook_payload = {
            "update_id": 9999,
            "pre_checkout_query": {
                "id": "pcq_test_err_789",
                "from": {"id": 12345, "is_bot": False, "first_name": "Mario"},
                "currency": "XTR",
                "total_amount": 50,
                "invoice_payload": "ai_sub:12345:1700000000",
            }
        }

        mock_app = MagicMock()
        mock_app._initialized = True
        mock_app.process_update = AsyncMock()

        with patch("httpx.AsyncClient.post", side_effect=Exception("Connection timed out")):
            with patch("fce_bot.api.router.get_telegram_app", return_value=mock_app):
                resp = client.post("/api/webhook", json=webhook_payload)

        # Webhook should NOT fail with 500 when fast-path network fails
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_app.process_update.assert_awaited_once()
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret


def test_webhook_info_endpoint():
    """Verify /api/webhook/info inspects Telegram webhook configuration."""
    from unittest.mock import patch, AsyncMock, MagicMock
    import httpx
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = "mgmt_secret"

        # 1. Unauthenticated request -> 403
        resp_unauth = client.get("/api/webhook/info")
        assert resp_unauth.status_code == 403

        # 2. Authenticated request
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": {
                "url": "https://example.vercel.app/api/webhook",
                "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
                "pending_update_count": 0,
            }
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            resp_auth = client.get("/api/webhook/info?secret=mgmt_secret")

        assert resp_auth.status_code == 200
        data = resp_auth.json()
        assert data["ok"] is True
        assert data["result"]["url"] == "https://example.vercel.app/api/webhook"
        assert "pre_checkout_query" in data["result"]["allowed_updates"]
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret


def test_webhook_set_endpoint():
    """Verify /api/webhook/set sets webhook with required allowed_updates."""
    from unittest.mock import patch, AsyncMock, MagicMock
    import httpx
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    orig_url = settings.WEBHOOK_URL
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = "mgmt_secret"
        settings.WEBHOOK_URL = ""

        # 1. Unauthenticated request -> 403
        resp_unauth = client.post("/api/webhook/set")
        assert resp_unauth.status_code == 403

        # 2. Missing URL -> 400
        resp_missing = client.post("/api/webhook/set?secret=mgmt_secret")
        assert resp_missing.status_code == 400
        assert "WEBHOOK_URL" in resp_missing.json()["detail"]

        # 3. Successful GET configuration
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "description": "Webhook was set"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            resp_set_get = client.get(
                "/api/webhook/set?secret=mgmt_secret&url=https://test.vercel.app/api/webhook"
            )

        assert resp_set_get.status_code == 200
        assert resp_set_get.json()["ok"] is True
        mock_post.assert_awaited_once()
        call_json = mock_post.call_args[1]["json"]
        assert call_json["url"] == "https://test.vercel.app/api/webhook"
        assert "pre_checkout_query" in call_json["allowed_updates"]
        assert "message" in call_json["allowed_updates"]
        assert "callback_query" in call_json["allowed_updates"]
        assert call_json["secret_token"] == "mgmt_secret"

        # 4. Successful POST configuration with body
        mock_post.reset_mock()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            resp_set_post = client.post(
                "/api/webhook/set",
                headers={"Authorization": "Bearer mgmt_secret"},
                json={
                    "url": "https://test.vercel.app/api/webhook",
                    "drop_pending_updates": True,
                }
            )

        assert resp_set_post.status_code == 200
        assert resp_set_post.json()["ok"] is True
        call_json2 = mock_post.call_args[1]["json"]
        assert call_json2["drop_pending_updates"] is True
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret
        settings.WEBHOOK_URL = orig_url


def test_cli_webhook_commands():
    """Verify CLI set-webhook and webhook-info commands."""
    from unittest.mock import patch, MagicMock
    from fce_bot import cli
    from fce_bot.config import settings

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = "test_sec"

        mock_resp_info = MagicMock()
        mock_resp_info.json.return_value = {
            "ok": True,
            "result": {
                "url": "https://example.com/api/webhook",
                "allowed_updates": ["message", "pre_checkout_query"],
                "has_custom_certificate": False,
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": None,
                "max_connections": 40,
            }
        }
        with patch("httpx.get", return_value=mock_resp_info):
            cli.webhook_info()

        mock_resp_set = MagicMock()
        mock_resp_set.json.return_value = {"ok": True, "description": "Webhook set"}
        with patch("httpx.post", return_value=mock_resp_set) as mock_p:
            cli.set_webhook(url="https://example.com/api/webhook", drop_pending=True)
            mock_p.assert_called_once()
            args, kwargs = mock_p.call_args
            assert "setWebhook" in args[0]
            assert "pre_checkout_query" in kwargs["json"]["allowed_updates"]
            assert kwargs["json"]["drop_pending_updates"] is True
    finally:
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret
