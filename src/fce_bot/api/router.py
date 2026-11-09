"""FastAPI REST router for webhooks, Vercel cron, REST transit endpoints and Web UI."""

import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Any, Dict
from fastapi import APIRouter, Request, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from telegram import Update

from fce_bot.config import settings
from fce_bot.core.models import TransportType, FilterType, AIQueryRequest, AIQueryResponse, NewsItem, DocumentsInfo
from fce_bot.services.transit_service import transit_service
from fce_bot.services.news_service import news_service
from fce_bot.services.documents_service import documents_service
from fce_bot.services.ai_service import ai_service
from fce_bot.bot.bot_app import create_bot_application
from fce_bot.bot.handlers.news import check_and_broadcast_news
from fce_bot.data.db import get_persistence_status
from fce_bot.data.users_repo import UsersRepository

logger = logging.getLogger(__name__)
router = APIRouter()

# Cached Telegram Application instance and event loop tracking for serverless invocations
_telegram_app = None
_telegram_app_loop = None
_webhook_updates_synced = False

ALLOWED_TELEGRAM_UPDATES: List[str] = [
    "message",
    "edited_message",
    "callback_query",
    "pre_checkout_query",
    "my_chat_member",
]


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


async def _ensure_webhook_allowed_updates(app) -> Optional[Dict[str, Any]]:
    """Ensure the Telegram webhook includes `pre_checkout_query` in `allowed_updates`."""
    global _webhook_updates_synced
    token = getattr(app.bot, "token", "") or ""
    if not token or token.startswith("0000000000"):
        return None

    try:
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
    if settings.WEBHOOK_SECRET and x_telegram_bot_api_secret_token != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

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
    if settings.CRON_SECRET:
        token = (authorization or "").replace("Bearer ", "") or secret
        if token != settings.CRON_SECRET:
            raise HTTPException(status_code=403, detail="Invalid cron authorization secret")

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
    if settings.CRON_SECRET:
        token = (authorization or "").replace("Bearer ", "") or secret
        if token != settings.CRON_SECRET:
            raise HTTPException(status_code=403, detail="Invalid cron authorization secret")

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
    """List municipalities (Comuni) served by FCE with stop counts."""
    ttype = TransportType.from_str(type) if type else TransportType.QUALSIASI
    return transit_service.get_cities(ttype)


@router.get("/api/stops")
async def list_stops(
    query: Optional[str] = None,
    type: Optional[str] = None,
    city: Optional[str] = None,
    limit: int = 50
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
    origin: str,
    destination: str,
    time: str = "08:00",
    transport: Optional[str] = "QUALSIASI",
    filter: Optional[str] = "near_partenza2",
    limit: int = 10
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
async def get_news(limit: int = 10) -> List[NewsItem]:
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
        detail="L'accesso all'assistente AI da web è disabilitato. L'assistente è disponibile esclusivamente su Telegram (@fce_app_bot) per gli utenti autorizzati dall'amministratore."
    )


@router.get("/api/health")
async def health_check(sync_webhook: bool = Query(False)):
    """Health check endpoint."""
    p_status = get_persistence_status()
    result: Dict[str, Any] = {
        "status": "healthy",
        "service": "fce-bot",
        "version": "2.0.0",
        "storage_backend": p_status["backend"],
        "permanent_storage": p_status["permanent"],
    }
    if sync_webhook:
        app = await _prepare_telegram_app()
        try:
            result["webhook"] = await _ensure_webhook_allowed_updates(app)
        finally:
            await _shutdown_telegram_app_requests(app)
    return result


# ==========================================
# PWA & Static Assets Endpoints
# ==========================================

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICONS_DIR = STATIC_DIR / "icons"
SW_PATH = STATIC_DIR / "sw.js"


@router.get("/manifest.webmanifest", include_in_schema=False)
@router.get("/manifest.json", include_in_schema=False)
async def web_manifest():
    """Serve PWA Web App Manifest prioritizing crisp vector SVG graphics."""
    manifest_data = {
        "name": "Ferrovia Circumetnea & Metro",
        "short_name": "FCE Orari",
        "description": "Orari ufficiali, itinerari e avvisi di servizio per Ferrovia Circumetnea e Metropolitana di Catania",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#991b1b",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/static/icons/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
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
    file_path = ICONS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Icon not found")
    if filename.endswith(".svg"):
        media_type = "image/svg+xml"
    elif filename.endswith(".ico"):
        media_type = "image/x-icon"
    else:
        media_type = "image/png"
    return Response(
        content=file_path.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800"}
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
        template.replace("__METRO_URL__", docs.metro_url)
        .replace("__TRAIN_URL__", docs.train_url)
        .replace("__BUS_URL__", docs.bus_url)
    )
    return HTMLResponse(content=content)
