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


def test_telegram_webhook_pre_checkout_and_event_loop_isolation():
    """Verify multiple webhook invocations across separate asyncio event loops succeed without 'Event loop is closed'."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    from fce_bot.api import router as router_mod
    from fce_bot.bot.bot_app import create_bot_application
    from fce_bot.config import settings
    from fce_bot.data.users_repo import UsersRepository

    orig_token = settings.TELEGRAM_BOT_TOKEN
    orig_secret = settings.WEBHOOK_SECRET
    orig_app = router_mod._telegram_app
    orig_loop = router_mod._telegram_app_loop
    orig_synced = router_mod._webhook_updates_synced

    cid = 776655443
    users_repo = UsersRepository()
    users_repo.delete_user(cid)

    try:
        settings.TELEGRAM_BOT_TOKEN = "123456:TEST_TOKEN"
        settings.WEBHOOK_SECRET = "vercel_test_secret"
        router_mod._telegram_app = create_bot_application()
        router_mod._telegram_app_loop = None
        router_mod._webhook_updates_synced = True

        api_calls = []
        seen_clients = []

        async def fake_do_post(self_bot, endpoint, data=None, **kwargs):
            # Verify the running event loop is open when Telegram Bot API calls are made
            loop = asyncio.get_running_loop()
            assert not loop.is_closed()
            # Record the underlying HTTPX client instance to verify it is rebuilt per loop
            seen_clients.append(self_bot._request[0]._client)
            api_calls.append((endpoint, data))
            if endpoint == "getMe":
                return {"id": 123456, "is_bot": True, "first_name": "FCE Bot", "username": "fce_bot"}
            if endpoint == "sendInvoice":
                return {
                    "message_id": 101,
                    "date": 1700000000,
                    "chat": {"id": cid, "type": "private"},
                }
            if endpoint == "sendMessage":
                return {
                    "message_id": 102,
                    "date": 1700000005,
                    "chat": {"id": cid, "type": "private"},
                    "text": data.get("text", ""),
                }
            return True

        with patch("telegram.Bot._do_post", new=fake_do_post):
            # Simulate Vercel's per-request new_event_loop() + loop.close() lifecycle
            def run_in_isolated_vercel_loop(coro):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            class DummyRequest:
                def __init__(self, payload):
                    self._payload = payload

                async def json(self):
                    return self._payload

            # Request 1 (Loop 1): User clicks "ai_subscribe" callback -> sends invoice
            req1_payload = {
                "update_id": 1001,
                "callback_query": {
                    "id": "cbq_1",
                    "from": {"id": cid, "is_bot": False, "first_name": "Test"},
                    "chat_instance": "ci_1",
                    "data": "ai_subscribe",
                    "message": {
                        "message_id": 50,
                        "date": 1700000000,
                        "chat": {"id": cid, "type": "private"},
                        "text": "Menu",
                    },
                },
            }
            res1 = run_in_isolated_vercel_loop(
                router_mod.telegram_webhook(
                    DummyRequest(req1_payload),
                    x_telegram_bot_api_secret_token="vercel_test_secret",
                )
            )
            assert res1 == {"ok": True}
            assert any(ep == "sendInvoice" for ep, _ in api_calls)

            # Request 2 (Loop 2 - after Loop 1 is closed!): Telegram sends pre_checkout_query -> answers ok=True
            req2_payload = {
                "update_id": 1002,
                "pre_checkout_query": {
                    "id": "pcq_vercel_999",
                    "from": {"id": cid, "is_bot": False, "first_name": "Test"},
                    "currency": "XTR",
                    "total_amount": 1,
                    "invoice_payload": f"ai_sub:{cid}:1700000000",
                },
            }
            res2 = run_in_isolated_vercel_loop(
                router_mod.telegram_webhook(
                    DummyRequest(req2_payload),
                    x_telegram_bot_api_secret_token="vercel_test_secret",
                )
            )
            assert res2 == {"ok": True}
            pcq_calls = [d for ep, d in api_calls if ep == "answerPreCheckoutQuery"]
            assert len(pcq_calls) == 1
            assert pcq_calls[0]["pre_checkout_query_id"] == "pcq_vercel_999"
            assert pcq_calls[0]["ok"] is True

            # Request 3 (Loop 3 - invalid pre_checkout_query payload): rejected with ok=False
            req3_payload = {
                "update_id": 1003,
                "pre_checkout_query": {
                    "id": "pcq_invalid_888",
                    "from": {"id": cid, "is_bot": False, "first_name": "Test"},
                    "currency": "XTR",
                    "total_amount": 1,
                    "invoice_payload": "invalid_payload",
                },
            }
            res3 = run_in_isolated_vercel_loop(
                router_mod.telegram_webhook(
                    DummyRequest(req3_payload),
                    x_telegram_bot_api_secret_token="vercel_test_secret",
                )
            )
            assert res3 == {"ok": True}
            pcq_invalid_calls = [
                d for ep, d in api_calls
                if ep == "answerPreCheckoutQuery" and d.get("pre_checkout_query_id") == "pcq_invalid_888"
            ]
            assert len(pcq_invalid_calls) == 1
            assert pcq_invalid_calls[0]["ok"] is False
            assert "error_message" in pcq_invalid_calls[0]

            # Request 4 (Loop 4): Telegram sends successful_payment -> activates AI subscription
            req4_payload = {
                "update_id": 1004,
                "message": {
                    "message_id": 105,
                    "date": 1700000010,
                    "chat": {"id": cid, "type": "private"},
                    "from": {"id": cid, "is_bot": False, "first_name": "Test"},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 1,
                        "invoice_payload": f"ai_sub:{cid}:1700000000",
                        "telegram_payment_charge_id": "tg_charge_1",
                        "provider_payment_charge_id": "prov_charge_1",
                    },
                },
            }
            res4 = run_in_isolated_vercel_loop(
                router_mod.telegram_webhook(
                    DummyRequest(req4_payload),
                    x_telegram_bot_api_secret_token="vercel_test_secret",
                )
            )
            assert res4 == {"ok": True}
            assert users_repo.is_ai_allowed(cid) is True
            # Verify HTTPX client was rebuilt across isolated event loops
            assert len(seen_clients) >= 4
            assert seen_clients[0] is not seen_clients[-1]
    finally:
        users_repo.delete_user(cid)
        settings.TELEGRAM_BOT_TOKEN = orig_token
        settings.WEBHOOK_SECRET = orig_secret
        router_mod._telegram_app = orig_app
        router_mod._telegram_app_loop = orig_loop
        router_mod._webhook_updates_synced = orig_synced


def test_telegram_webhook_auto_sync_allowed_updates():
    """Verify automatic allowed_updates self-healing when pre_checkout_query is missing."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from fce_bot.api import router as router_mod
    from fce_bot.config import settings

    orig_secret = settings.WEBHOOK_SECRET
    orig_url = settings.WEBHOOK_URL
    orig_synced = router_mod._webhook_updates_synced

    try:
        settings.WEBHOOK_SECRET = "sync_secret"
        settings.WEBHOOK_URL = "https://fce-bot.vercel.app/api/webhook"
        router_mod._webhook_updates_synced = False

        info_before = MagicMock()
        info_before.url = "https://fce-bot.vercel.app/api/webhook"
        info_before.allowed_updates = ["message", "callback_query"]  # missing pre_checkout_query!
        info_before.pending_update_count = 0
        info_before.last_error_message = None

        info_after = MagicMock()
        info_after.url = "https://fce-bot.vercel.app/api/webhook"
        info_after.allowed_updates = router_mod.ALLOWED_TELEGRAM_UPDATES
        info_after.pending_update_count = 0
        info_after.last_error_message = None

        mock_app = MagicMock()
        mock_app._initialized = True
        mock_app.bot.token = "123456:REAL_LOOKING_TOKEN"
        mock_app.bot._request = ()
        mock_app.bot.get_webhook_info = AsyncMock(side_effect=[info_before, info_after])
        mock_app.bot.set_webhook = AsyncMock(return_value=True)

        with patch("fce_bot.api.router.get_telegram_app", return_value=mock_app):
            resp = client.get("/api/health?sync_webhook=true")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["webhook"]["webhook_active"] is True
        assert "pre_checkout_query" in data["webhook"]["allowed_updates"]

        mock_app.bot.set_webhook.assert_awaited_once_with(
            url="https://fce-bot.vercel.app/api/webhook",
            allowed_updates=router_mod.ALLOWED_TELEGRAM_UPDATES,
            secret_token="sync_secret",
        )
        assert router_mod._webhook_updates_synced is True
    finally:
        settings.WEBHOOK_SECRET = orig_secret
        settings.WEBHOOK_URL = orig_url
        router_mod._webhook_updates_synced = orig_synced
