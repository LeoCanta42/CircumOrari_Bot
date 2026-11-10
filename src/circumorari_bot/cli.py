"""Command-line interface for CircumOrari Bot operations."""

import sys
import argparse
import asyncio
import logging
from circumorari_bot.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
)
logger = logging.getLogger("circumorari_bot.cli")


def run_bot() -> None:
    """Run Telegram bot using standard Long Polling (ideal for local/VPS/Docker)."""
    from telegram import Update
    from circumorari_bot.bot.bot_app import create_bot_application

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured! Please set it in .env")
        sys.exit(1)

    logger.info("Starting CircumOrari Telegram Bot in polling mode...")
    app = create_bot_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Run local FastAPI web server & REST API."""
    import uvicorn
    logger.info("Starting FastAPI server on http://%s:%d...", host, port)
    uvicorn.run("api.index:app", host=host, port=port, reload=reload)


def sync_gtfs(feed_url: str = None) -> None:
    """Download and sync latest GTFS feed from circumetnea.it (including stop municipality enrichment)."""
    from circumorari_bot.data.gtfs_loader import download_and_sync_gtfs
    logger.info("Synchronizing GTFS feed...")
    stops, trips, st = download_and_sync_gtfs(feed_url=feed_url)
    logger.info("GTFS sync completed: %d stops, %d trips, %d stop_times", stops, trips, st)


def reset_commands() -> int:
    """Synchronize and re-flush official slash commands with Telegram Bot API."""
    from circumorari_bot.bot.bot_app import create_bot_application, sync_bot_commands

    app = create_bot_application()

    async def _run():
        await app.initialize()
        try:
            return await sync_bot_commands(app.bot)
        finally:
            await app.shutdown()

    logger.info("Synchronizing official slash commands with Telegram Bot API...")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            count = pool.submit(asyncio.run, _run()).result()
    else:
        count = asyncio.run(_run())

    print(f"✅ Successfully registered {count} slash commands with Telegram Bot API.")
    return count


def enrich_cities() -> None:
    """Run municipality (Comune) enrichment subtask on existing GTFS stops."""
    from circumorari_bot.data.gtfs_loader import enrich_existing_stops_cities
    updated = enrich_existing_stops_cities()
    logger.info("Municipality enrichment completed: %d stops enriched", updated)


def check_news() -> None:
    """Check for new articles once and broadcast."""
    from circumorari_bot.bot.bot_app import create_bot_application
    from circumorari_bot.bot.handlers.news import check_and_broadcast_news

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
    from circumorari_bot.data.users_repo import UsersRepository
    from circumorari_bot.data.db import get_db, get_persistence_status

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

    print("=== CircumOrari Bot System Statistics ===")
    print(f"Storage Backend:    {p_status['label']}")
    print(f"Users Total:        {stats['total_users']}")
    print(f"Subscribed Users:   {stats['subscribed_users']}")
    print(f"Active (30d):       {stats['active_last_30_days']}")
    print(f"GTFS Routes:        {routes}")
    print(f"GTFS Municipalities:{cities:>5}")
    print(f"GTFS Stops:         {stops}")
    print(f"GTFS Trips:         {trips}")
    print(f"GTFS Stop Times:    {st}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CircumOrari Bot CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("bot", help="Run Telegram bot in polling mode")
    web_p = subparsers.add_parser("serve", help="Run web server")
    web_p.add_argument("--host", default="0.0.0.0", help="Host address")
    web_p.add_argument("--port", type=int, default=8000, help="Port")
    web_p.add_argument("--reload", action="store_true", help="Enable auto-reload")

    sync_p = subparsers.add_parser("sync-gtfs", help="Download and parse official GTFS feed")
    sync_p.add_argument("--url", default=None, help="Explicit GTFS feed URL (bypasses auto-scraping)")
    subparsers.add_parser("enrich-cities", help="Enrich GTFS stops with their municipality (Comune)")
    subparsers.add_parser("check-news", help="Check for new articles and broadcast")
    worker_p = subparsers.add_parser("news-worker", help="Run news polling loop daemon")
    worker_p.add_argument("--interval", type=int, default=1800, help="Check interval in seconds")
    subparsers.add_parser("reset-commands", help="Re-flush official slash commands with Telegram")
    subparsers.add_parser("stats", help="Print system statistics")

    args = parser.parse_args()

    if args.command == "bot":
        run_bot()
    elif args.command == "serve":
        run_server(host=args.host, port=args.port, reload=args.reload)
    elif args.command == "sync-gtfs":
        sync_gtfs(feed_url=args.url)
    elif args.command == "enrich-cities":
        enrich_cities()
    elif args.command == "check-news":
        check_news()
    elif args.command == "news-worker":
        run_news_worker(interval_seconds=args.interval)
    elif args.command in ("reset-commands", "reflush-commands"):
        reset_commands()
    elif args.command == "stats":
        show_stats()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
