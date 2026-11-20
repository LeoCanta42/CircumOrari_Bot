"""FastAPI REST router for webhooks, Vercel cron, REST transit endpoints and Web UI."""

import asyncio
import hmac
import html
import logging
import re
from pathlib import Path
from typing import Optional, List, Any, Dict, Iterable
from fastapi import APIRouter, Request, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from telegram import Update

from circumorari_bot.config import settings
from circumorari_bot.core.models import TransportType, FilterType, AIQueryRequest, AIQueryResponse, NewsItem, DocumentsInfo
from circumorari_bot.services.transit_service import transit_service
from circumorari_bot.services.news_service import news_service
from circumorari_bot.services.documents_service import documents_service
from circumorari_bot.services.ai_service import ai_service
from circumorari_bot.bot.bot_app import create_bot_application
from circumorari_bot.bot.handlers.news import check_and_broadcast_news
from circumorari_bot.data.db import get_persistence_status
from circumorari_bot.data.users_repo import UsersRepository

logger = logging.getLogger(__name__)
router = APIRouter()

# Cached Telegram Application instance and event loop tracking for serverless invocations
_telegram_app = None
_telegram_app_loop = None
_webhook_updates_synced = False
_bot_commands_synced = False

ALLOWED_TELEGRAM_UPDATES: List[str] = [
    "message",
    "edited_message",
    "callback_query",
    "pre_checkout_query",
    "my_chat_member",
]


def _is_valid_secret(provided: Optional[str], expected_secrets: Iterable[Optional[str]]) -> bool:
    """Fail-closed constant-time secret verification against non-empty configured secrets."""
    valid = [s for s in expected_secrets if isinstance(s, str) and s]
    if not valid or not isinstance(provided, str) or not provided:
        return False
    return any(hmac.compare_digest(provided, secret) for secret in valid)


def get_telegram_app():
    global _telegram_app
    if _telegram_app is None:
        _telegram_app = create_bot_application()
    return _telegram_app


async def _prepare_telegram_app():
    """Prepare the cached Telegram Application for the current request's asyncio event loop.

    On Vercel's Python ASGI runtime, each HTTP request executes inside a new event loop
    that is closed when the request completes. Rebuilding the underlying HTTPX clients
    when the event loop changes prevents `RuntimeError: Event loop is closed` on
    subsequent invocations (such as answering `pre_checkout_query`).
    """
    global _telegram_app_loop
    app = get_telegram_app()
    current_loop = asyncio.get_running_loop()

    if _telegram_app_loop is not None and _telegram_app_loop is not current_loop:
        for req in getattr(app.bot, "_request", ()):
            if hasattr(req, "_build_client"):
                req._client = req._build_client()
    else:
        for req in getattr(app.bot, "_request", ()):
            if hasattr(req, "initialize"):
                await req.initialize()

    _telegram_app_loop = current_loop

    if not getattr(app, "_initialized", False):
        await app.initialize()

    return app


async def _shutdown_telegram_app_requests(app) -> None:
    """Cleanly close per-request HTTPX transport pools before serverless loop teardown."""
    for req in getattr(app.bot, "_request", ()):
        try:
            if hasattr(req, "shutdown"):
                await req.shutdown()
        except Exception:
            pass


async def _ensure_bot_commands_synced(app) -> int:
    """Ensure bot commands are synced with Telegram Bot API on serverless deployments."""
    global _bot_commands_synced
    if _bot_commands_synced:
        return 0
    token = getattr(app.bot, "token", "") or ""
    if not token or token.startswith("0000000000"):
        return 0
    try:
        from circumorari_bot.bot.bot_app import sync_bot_commands
        count = await sync_bot_commands(app.bot)
        _bot_commands_synced = True
        return count
    except Exception as exc:
        logger.warning("Could not auto-sync bot commands: %s", exc)
        return 0


async def _ensure_webhook_allowed_updates(app) -> Optional[Dict[str, Any]]:
    """Ensure the Telegram webhook includes `pre_checkout_query` in `allowed_updates`."""
    global _webhook_updates_synced
    token = getattr(app.bot, "token", "") or ""
    if not token or token.startswith("0000000000"):
        return None

    try:
        await _ensure_bot_commands_synced(app)
        info = await app.bot.get_webhook_info()
        target_url = getattr(info, "url", None) or settings.WEBHOOK_URL or ""
        current_allowed = list(getattr(info, "allowed_updates", None) or [])

        if target_url and (not getattr(info, "url", None) or "pre_checkout_query" not in current_allowed):
            await app.bot.set_webhook(
                url=target_url,
                allowed_updates=ALLOWED_TELEGRAM_UPDATES,
                secret_token=settings.WEBHOOK_SECRET or None,
            )
            _webhook_updates_synced = True
            info = await app.bot.get_webhook_info()
            current_allowed = list(getattr(info, "allowed_updates", None) or [])
        elif target_url:
            _webhook_updates_synced = True

        return {
            "webhook_active": bool(getattr(info, "url", None)),
            "allowed_updates": current_allowed,
            "pending_update_count": getattr(info, "pending_update_count", 0),
            "last_error_message": getattr(info, "last_error_message", None),
        }
    except Exception as e:
        logger.warning("Could not verify/sync Telegram webhook allowed_updates: %s", e)
        return None


# ==========================================
# Telegram Webhook & Cron Endpoints
# ==========================================

@router.post("/api/webhook")
@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None)
):
    """Handle incoming Telegram updates forwarded via Webhook."""
    if not _is_valid_secret(x_telegram_bot_api_secret_token, [settings.WEBHOOK_SECRET]):
        raise HTTPException(status_code=403, detail="Invalid or unconfigured webhook secret")

    try:
        data = await request.json()
        app = await _prepare_telegram_app()
        try:
            if not _webhook_updates_synced and isinstance(data, dict) and "pre_checkout_query" not in data:
                await _ensure_webhook_allowed_updates(app)

            update = Update.de_json(data, app.bot)
            await app.process_update(update)
        finally:
            await _shutdown_telegram_app_requests(app)
        return {"ok": True}
    except Exception as e:
        logger.error("Error processing telegram webhook: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": "Internal processing error"})


@router.get("/api/cron/news")
@router.get("/cron/news")
async def cron_news_checker(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None)
):
    """Scheduled task invoked by Vercel Cron to check news and broadcast updates."""
    token = (
        authorization[7:].strip()
        if authorization and authorization.startswith("Bearer ")
        else authorization
    ) or secret
    if not _is_valid_secret(token, [settings.CRON_SECRET]):
        raise HTTPException(status_code=403, detail="Invalid or unconfigured cron authorization secret")

    app = await _prepare_telegram_app()
    try:
        sent_count = await check_and_broadcast_news(app.bot)
    finally:
        await _shutdown_telegram_app_requests(app)

    repo = UsersRepository()
    deactivated = repo.deactivate_expired_users()
    return {
        "status": "ok",
        "broadcast_sent": sent_count,
        "deactivated_expired_ai": deactivated,
        "message": f"Broadcasted {sent_count} new announcements and checked expired subscriptions"
    }


@router.get("/api/cron/cleanup")
@router.get("/cron/cleanup")
async def cron_cleanup_task(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None)
):
    """Scheduled maintenance task: deactivate expired AI users and prune inactive records (GDPR)."""
    token = (
        authorization[7:].strip()
        if authorization and authorization.startswith("Bearer ")
        else authorization
    ) or secret
    if not _is_valid_secret(token, [settings.CRON_SECRET]):
        raise HTTPException(status_code=403, detail="Invalid or unconfigured cron authorization secret")

    repo = UsersRepository()
    deactivated = repo.deactivate_expired_users()
    pruned = repo.prune_inactive(days=60)
    return {
        "status": "ok",
        "deactivated_expired_ai": deactivated,
        "pruned_inactive_users": pruned,
    }


# ==========================================
# REST Transit & AI Endpoints
# ==========================================

@router.get("/api/cities")
async def list_cities(type: Optional[str] = None):
    """List municipalities (Comuni) served with stop counts."""
    ttype = TransportType.from_str(type) if type else TransportType.QUALSIASI
    return transit_service.get_cities(ttype)


@router.get("/api/stops")
async def list_stops(
    query: Optional[str] = Query(None, max_length=150),
    type: Optional[str] = None,
    city: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=1000)
):
    """Search or list transit stops (optionally filtered by transport mode and city)."""
    ttype = TransportType.from_str(type) if type else None
    if query:
        stops = transit_service.search_stops(query, transport_type=ttype, city=city, limit=limit)
    else:
        stops = transit_service.get_stops_by_type(
            ttype or TransportType.QUALSIASI, city=city, limit=limit
        )
    return [
        {
            "id": s.stop_id,
            "name": s.stop_name,
            "city": s.city,
            "display_name": s.display_name,
            "lat": s.lat,
            "lon": s.lon,
            "bus": s.has_bus,
            "train": s.has_train,
            "metro": s.has_metro,
        }
        for s in stops
    ]


@router.get("/api/timetable")
async def get_timetable(
    origin: str = Query(..., max_length=150),
    destination: str = Query(..., max_length=150),
    time: str = "08:00",
    transport: Optional[str] = "QUALSIASI",
    filter: Optional[str] = "near_partenza2",
    limit: int = Query(10, ge=1, le=50)
):
    """Query transit timetable between two stops, cities, or similar station names."""
    ttype = TransportType.from_str(transport or "")
    ftype = FilterType(filter) if filter in [e.value for e in FilterType] else FilterType.DEPARTURE_AFTER
    orig_res = transit_service.resolve_location(
        origin, transport_type=ttype, allow_geocoding=True, allow_network_geocoding=False
    )
    dest_res = transit_service.resolve_location(
        destination, transport_type=ttype, allow_geocoding=True, allow_network_geocoding=False
    )
    itineraries = transit_service.find_trips_from_resolutions(
        origin_res=orig_res,
        dest_res=dest_res,
        target_time=time,
        transport_type=ttype,
        filter_type=ftype,
        limit=limit
    )
    return {
        "origin": origin,
        "destination": destination,
        "resolved_origin": (
            orig_res.matched_stops[0].display_name
            if orig_res.match_kind == "nearest_place" and orig_res.matched_stops
            else (orig_res.resolved_label or origin)
        ),
        "resolved_destination": (
            dest_res.matched_stops[0].display_name
            if dest_res.match_kind == "nearest_place" and dest_res.matched_stops
            else (dest_res.resolved_label or destination)
        ),
        "origin_match_kind": orig_res.match_kind,
        "destination_match_kind": dest_res.match_kind,
        "origin_distance_m": orig_res.nearest_distance_m,
        "destination_distance_m": dest_res.nearest_distance_m,
        "requested_time": time,
        "count": len(itineraries),
        "itineraries": [
            {
                "departure_stop": it.departure_stop,
                "departure_time": it.departure_time,
                "arrival_stop": it.arrival_stop,
                "arrival_time": it.arrival_time,
                "duration_minutes": it.duration_minutes,
                "direct": it.is_direct,
                "segments": [
                    {
                        "type": seg.transport_type.value,
                        "route": seg.route_name,
                        "headsign": seg.headsign,
                        "departure_stop": seg.departure_stop,
                        "departure_time": seg.departure_time,
                        "arrival_stop": seg.arrival_stop,
                        "arrival_time": seg.arrival_time,
                        "duration_minutes": seg.duration_minutes,
                        "stops_path": seg.stops_path,
                        "intermediate_stops": seg.intermediate_stops,
                    }
                    for seg in it.segments
                ]
            }
            for it in itineraries
        ]
    }


@router.get("/api/news")
async def get_news(limit: int = Query(10, ge=1, le=30)) -> List[NewsItem]:
    """Retrieve latest Circumetnea news."""
    return news_service.fetch_news(limit=limit)


@router.get("/api/documents")
async def get_documents() -> DocumentsInfo:
    """Retrieve official PDF timetable download links."""
    return documents_service.get_documents()


@router.post("/api/ai/ask")
async def ai_trip_advisor(req: AIQueryRequest) -> AIQueryResponse:
    """Natural Language Trip Planning Assistant API (Disabled from Web)."""
    raise HTTPException(
        status_code=403,
        detail="L'accesso all'assistente AI da web è disabilitato. L'assistente è disponibile esclusivamente su Telegram (@circumorari_bot) per gli utenti autorizzati dall'amministratore."
    )


@router.get("/api/health")
async def health_check(
    sync_webhook: bool = Query(False),
    secret: Optional[str] = Query(None),
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Health check endpoint (minimal public status; privileged metadata requires secret auth)."""
    result: Dict[str, Any] = {
        "status": "healthy",
        "service": "circumorari-bot",
        "version": "2.0.0",
    }
    if sync_webhook:
        auth_secret = x_telegram_bot_api_secret_token or secret
        if not _is_valid_secret(auth_secret, [settings.WEBHOOK_SECRET, settings.CRON_SECRET]):
            raise HTTPException(status_code=403, detail="Invalid or unconfigured authorization secret")
        p_status = get_persistence_status()
        result["storage_backend"] = p_status["backend"]
        result["permanent_storage"] = p_status["permanent"]
        app = await _prepare_telegram_app()
        try:
            result["webhook"] = await _ensure_webhook_allowed_updates(app)
            result["commands_synced"] = await _ensure_bot_commands_synced(app)
        finally:
            await _shutdown_telegram_app_requests(app)
    return result


@router.post("/api/commands/sync")
@router.get("/api/commands/sync")
async def sync_telegram_commands(
    secret: Optional[str] = Query(None),
    x_telegram_bot_api_secret_token: Optional[str] = Header(None)
):
    """Endpoint to trigger re-flushing official slash commands with Telegram Bot API."""
    auth_secret = x_telegram_bot_api_secret_token or secret
    if not _is_valid_secret(auth_secret, [settings.WEBHOOK_SECRET, settings.CRON_SECRET]):
        raise HTTPException(status_code=403, detail="Invalid or unconfigured authorization secret")

    app = await _prepare_telegram_app()
    try:
        from circumorari_bot.bot.bot_app import sync_bot_commands
        count = await sync_bot_commands(app.bot)
    finally:
        await _shutdown_telegram_app_requests(app)

    return {
        "status": "ok",
        "commands_synced": count,
        "message": f"Successfully registered {count} slash commands with Telegram Bot API."
    }


# ==========================================
# PWA & Static Assets Endpoints
# ==========================================

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICONS_DIR = STATIC_DIR / "icons"
SW_PATH = STATIC_DIR / "sw.js"
ALLOWED_ICONS: Dict[str, tuple[Path, str]] = {
    "icon.svg": (ICONS_DIR / "icon.svg", "image/svg+xml"),
    "icon-192.png": (ICONS_DIR / "icon-192.png", "image/png"),
    "icon-512.png": (ICONS_DIR / "icon-512.png", "image/png"),
    "maskable-512.png": (ICONS_DIR / "maskable-512.png", "image/png"),
    "favicon.ico": (ICONS_DIR / "favicon.ico", "image/x-icon"),
}


@router.get("/manifest.webmanifest", include_in_schema=False)
@router.get("/manifest.json", include_in_schema=False)
async def web_manifest():
    """Serve PWA Web App Manifest prioritizing crisp vector SVG graphics."""
    manifest_data = {
        "name": "Ferrovia Circumetnea & Metro",
        "short_name": "CircumOrari",
        "description": "Orari ufficiali, itinerari e avvisi di servizio per Ferrovia Circumetnea e Metropolitana di Catania",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#092644",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/static/icons/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any"
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icons/maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            }
        ]
    }
    return JSONResponse(
        content=manifest_data,
        media_type="application/manifest+json"
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve PWA Service Worker script at root scope."""
    if SW_PATH.exists():
        content = SW_PATH.read_text(encoding="utf-8")
        return Response(
            content=content,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )
    raise HTTPException(status_code=404, detail="Service worker not found")


@router.get("/static/icons/{filename}", include_in_schema=False)
async def serve_icon(filename: str):
    """Serve PWA application vector and raster icons."""
    icon_info = ALLOWED_ICONS.get(filename)
    if not icon_info:
        raise HTTPException(status_code=404, detail="Icon not found")

    file_path, media_type = icon_info
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Icon not found")

    return Response(
        content=file_path.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve crisp favicon matching icon.svg graphics."""
    ico_path = ICONS_DIR / "favicon.ico"
    if ico_path.exists():
        return Response(
            content=ico_path.read_bytes(),
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=604800"}
        )
    svg_path = ICONS_DIR / "icon.svg"
    if svg_path.exists():
        return Response(
            content=svg_path.read_bytes(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=604800"}
        )
    return Response(status_code=204)


# ==========================================
# Interactive Web Dashboard
# ==========================================

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def home_dashboard():
    """Serve responsive passenger-facing transit dashboard."""
    docs = documents_service.get_documents()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    content = (
        template.replace("__METRO_URL__", html.escape(docs.metro_url, quote=True))
        .replace("__TRAIN_URL__", html.escape(docs.train_url, quote=True))
        .replace("__BUS_URL__", html.escape(docs.bus_url, quote=True))
    )
    return HTMLResponse(content=content)
