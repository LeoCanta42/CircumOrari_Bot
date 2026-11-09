"""Command-line interface for FCE Bot operations."""

import sys
import argparse
import asyncio
import logging
from typing import Optional
from fce_bot.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
)
logger = logging.getLogger("fce_bot.cli")


def run_bot() -> None:
    """Run Telegram bot using standard Long Polling (ideal for local/VPS/Docker)."""
    from fce_bot.bot.bot_app import create_bot_application

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please set it in .env")
        sys.exit(1)

    logger.info("Starting FCE Telegram Bot in polling mode...")
    app = create_bot_application()
    app.run_polling()


def run_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Run local FastAPI web server & REST API."""
    import uvicorn
    logger.info("Starting FastAPI server on http://%s:%d...", host, port)
    uvicorn.run("api.index:app", host=host, port=port, reload=reload)


def sync_gtfs() -> None:
    """Download and sync latest GTFS feed from circumetnea.it (including stop municipality enrichment)."""
    from fce_bot.data.gtfs_loader import download_and_sync_gtfs
    logger.info("Synchronizing GTFS feed...")
    stops, trips, st = download_and_sync_gtfs()
    logger.info("GTFS sync completed: %d stops, %d trips, %d stop_times", stops, trips, st)


def enrich_cities() -> None:
    """Run municipality (Comune) enrichment subtask on existing GTFS stops."""
    from fce_bot.data.gtfs_loader import enrich_existing_stops_cities
    updated = enrich_existing_stops_cities()
    logger.info("Municipality enrichment completed: %d stops enriched", updated)


def check_news() -> None:
    """Check for new articles once and broadcast."""
    from fce_bot.bot.bot_app import create_bot_application
    from fce_bot.bot.handlers.news import check_and_broadcast_news

    app = create_bot_application()

    async def _run():
        await app.initialize()
        count = await check_and_broadcast_news(app.bot)
        logger.info("News check finished: %d new announcements broadcasted", count)

    asyncio.run(_run())


def run_news_worker(interval_seconds: int = 1800) -> None:
    """Run persistent worker checking for news at regular intervals."""
    import time
    logger.info("Starting periodic news worker (checking every %d seconds)...", interval_seconds)
    while True:
        try:
            check_news()
        except Exception as e:
            logger.error("Error during scheduled news check: %s", e)
        time.sleep(interval_seconds)


def show_stats() -> None:
    """Print current system statistics."""
    from fce_bot.data.users_repo import UsersRepository
    from fce_bot.data.db import get_db, get_persistence_status

    repo = UsersRepository()
    stats = repo.get_stats()
    p_status = get_persistence_status()
    with get_db() as conn:
        cur = conn.cursor()
        stops = cur.execute("SELECT count(*) FROM stops").fetchone()[0]
        cities = cur.execute("SELECT count(DISTINCT city) FROM stops WHERE city != ''").fetchone()[0]
        trips = cur.execute("SELECT count(*) FROM trips").fetchone()[0]
        st = cur.execute("SELECT count(*) FROM stop_times").fetchone()[0]
        routes = cur.execute("SELECT count(*) FROM routes").fetchone()[0]

    print("=== FCE Bot System Statistics ===")
    print(f"Storage Backend:    {p_status['label']}")
    print(f"Users Total:        {stats['total_users']}")
    print(f"Subscribed Users:   {stats['subscribed_users']}")
    print(f"Active (30d):       {stats['active_last_30_days']}")
    print(f"GTFS Routes:        {routes}")
    print(f"GTFS Municipalities:{cities:>5}")
    print(f"GTFS Stops:         {stops}")
    print(f"GTFS Trips:         {trips}")
    print(f"GTFS Stop Times:    {st}")


def webhook_info() -> None:
    """Inspect current Telegram webhook configuration and allowed updates."""
    import httpx
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please set it in .env")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getWebhookInfo"
    try:
        resp = httpx.get(url, timeout=10.0)
        data = resp.json()
    except Exception as e:
        logger.error("Failed to query getWebhookInfo: %s", e)
        sys.exit(1)

    print("=== Telegram Webhook Info ===")
    if data.get("ok"):
        res = data.get("result", {})
        print(f"URL:                     {res.get('url') or '(none)'}")
        print(f"Has Custom Certificate:  {res.get('has_custom_certificate')}")
        print(f"Pending Update Count:    {res.get('pending_update_count')}")
        print(f"Allowed Updates:         {res.get('allowed_updates') or 'ALL (default)'}")
        print(f"Last Error Date:         {res.get('last_error_date')}")
        print(f"Last Error Message:      {res.get('last_error_message')}")
        print(f"Max Connections:         {res.get('max_connections')}")
    else:
        print(f"Error from Telegram API: {data}")


def set_webhook(url: Optional[str] = None, drop_pending: bool = False) -> None:
    """Configure or repair Telegram webhook with required allowed_updates."""
    import httpx
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please set it in .env")
        sys.exit(1)

    target_url = url or settings.WEBHOOK_URL
    if not target_url:
        logger.error("No webhook URL specified! Provide --url or configure WEBHOOK_URL in .env")
        sys.exit(1)

    payload = {
        "url": target_url,
        "allowed_updates": ["message", "callback_query", "pre_checkout_query", "my_chat_member"],
        "drop_pending_updates": drop_pending,
    }
    if settings.WEBHOOK_SECRET:
        payload["secret_token"] = settings.WEBHOOK_SECRET

    api_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    try:
        resp = httpx.post(api_url, json=payload, timeout=10.0)
        data = resp.json()
    except Exception as e:
        logger.error("Failed to set webhook via Telegram API: %s", e)
        sys.exit(1)

    print("=== Set Telegram Webhook ===")
    print(f"Target URL:        {target_url}")
    print(f"Secret Configured: {'Yes' if settings.WEBHOOK_SECRET else 'No'}")
    print(f"Allowed Updates:   {payload['allowed_updates']}")
    print(f"Telegram Result:   {data}")
    if data.get("ok"):
        print("Webhook successfully configured!")
    else:
        print("Warning: Telegram rejected webhook setup.")


def main() -> None:
    parser = argparse.ArgumentParser(description="FCE Circumetnea Bot CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("bot", help="Run Telegram bot in polling mode")
    web_p = subparsers.add_parser("serve", help="Run web server")
    web_p.add_argument("--host", default="0.0.0.0", help="Host address")
    web_p.add_argument("--port", type=int, default=8000, help="Port")
    web_p.add_argument("--reload", action="store_true", help="Enable auto-reload")

    subparsers.add_parser("sync-gtfs", help="Download and parse official GTFS feed")
    subparsers.add_parser("enrich-cities", help="Enrich GTFS stops with their municipality (Comune)")
    subparsers.add_parser("check-news", help="Check for new articles and broadcast")
    worker_p = subparsers.add_parser("news-worker", help="Run news polling loop daemon")
    worker_p.add_argument("--interval", type=int, default=1800, help="Check interval in seconds")
    subparsers.add_parser("stats", help="Print system statistics")
    subparsers.add_parser("webhook-info", help="Inspect Telegram webhook status and allowed_updates")
    set_p = subparsers.add_parser("set-webhook", help="Configure Telegram webhook with allowed_updates")
    set_p.add_argument("--url", default=None, help="Target webhook URL (defaults to WEBHOOK_URL in .env)")
    set_p.add_argument("--drop-pending", action="store_true", help="Drop pending updates on Telegram")

    args = parser.parse_args()

    if args.command == "bot":
        run_bot()
    elif args.command == "serve":
        run_server(host=args.host, port=args.port, reload=args.reload)
    elif args.command == "sync-gtfs":
        sync_gtfs()
    elif args.command == "enrich-cities":
        enrich_cities()
    elif args.command == "check-news":
        check_news()
    elif args.command == "news-worker":
        run_news_worker(interval_seconds=args.interval)
    elif args.command == "stats":
        show_stats()
    elif args.command == "webhook-info":
        webhook_info()
    elif args.command == "set-webhook":
        set_webhook(url=args.url, drop_pending=args.drop_pending)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
