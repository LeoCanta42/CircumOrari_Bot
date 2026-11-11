"""Handlers for user location retrieval, nearest stops calculation, and spatial formatting."""

import logging
from typing import Optional
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from circumorari_bot.core.models import TransportType, Stop
from circumorari_bot.services.transit_service import transit_service, TransitService
from circumorari_bot.bot.keyb import (
    location_request_keyboard,
    nearest_stops_result_keyboard,
    main_menu_keyboard,
)
from circumorari_bot.bot.handlers.admin import is_admin

logger = logging.getLogger(__name__)

PROMPT_LOCATION_TEXT = (
    "📍 *Trova le Fermate più Vicine*\n\n"
    "Condividi la tua posizione per scoprire le stazioni e le fermate FCE (Metro, Treno e Bus) più vicine a te.\n\n"
    "Tocca il pulsante qui sotto *\"📍 Invia la mia posizione\"* oppure *\"❌ Annulla\"* per tornare indietro."
)


def format_distance(distance_meters: Optional[int]) -> str:
    """Format meters into human-readable distance (e.g. 120 m or 1.5 km)."""
    if distance_meters is None:
        return "N/D"
    if distance_meters < 1000:
        return f"{distance_meters} m"
    km = distance_meters / 1000.0
    return f"{km:.1f} km"


def format_stop_badges(stop: Stop) -> str:
    """Format transport mode badges for a stop (Bus, Treno, Metro)."""
    badges = []
    if stop.has_metro:
        badges.append("🚇 Metro")
    if stop.has_train:
        badges.append("🚆 Treno")
    if stop.has_bus:
        badges.append("🚌 Bus")
    return " · ".join(badges) if badges else "🚏 Fermata"


def format_nearest_stops_message(
    lat: float,
    lon: float,
    ts: Optional[TransitService] = None,
) -> str:
    """Calculate and format closest stops using pure spatial Haversine distance without AI."""
    if ts is None:
        ts = transit_service

    overall_stops = ts.find_nearest_stops(
        lat, lon, transport_type=TransportType.QUALSIASI, max_distance_m=None, limit=5
    )
    if not overall_stops:
        return "📍 Non sono state trovate fermate registrate nel database."

    min_dist = overall_stops[0].distance_meters or 0
    is_far = min_dist > 70000  # > 70 km away

    lines = []
    if is_far:
        far_str = format_distance(min_dist)
        lines.append(
            f"⚠️ *Nota di prossimità*\n"
            f"La tua posizione risulta a circa *{far_str}* dalla rete Circumetnea/Catania "
            f"(fuori dall'area servita da FCE).\n"
            f"Ecco comunque le stazioni e fermate della rete più vicine raggiungibili:"
        )
    else:
        lines.append("📍 *Fermate più vicine alla tua posizione:*")

    lines.append("")

    for idx, stop in enumerate(overall_stops, start=1):
        dist_str = format_distance(stop.distance_meters)
        badges = format_stop_badges(stop)
        clean_name = stop.stop_name.replace("*", "")
        clean_city = (
            f" ({stop.city.replace('*', '')})"
            if stop.city and stop.city.lower() not in clean_name.lower()
            else ""
        )
        maps_link = f"https://www.google.com/maps/search/?api=1&query={stop.lat:.6f},{stop.lon:.6f}"
        lines.append(f"{idx}. *{clean_name}*{clean_city} – *{dist_str}*")
        lines.append(f"   {badges} · [📍 Apri Mappa]({maps_link})")
        lines.append("")

    # Nearest stop for each specific transport mode
    nearest_metro = ts.find_nearest_stops(lat, lon, transport_type=TransportType.METRO, max_distance_m=None, limit=1)
    nearest_train = ts.find_nearest_stops(lat, lon, transport_type=TransportType.TRENO, max_distance_m=None, limit=1)
    nearest_bus = ts.find_nearest_stops(lat, lon, transport_type=TransportType.BUS, max_distance_m=None, limit=1)

    type_items = []
    if nearest_metro:
        m = nearest_metro[0]
        m_dist = format_distance(m.distance_meters)
        m_map = f"https://www.google.com/maps/search/?api=1&query={m.lat:.6f},{m.lon:.6f}"
        type_items.append(f"• 🚇 *Metro più vicina:* *{m.stop_name.replace('*', '')}* ({m_dist}) · [Mappa]({m_map})")

    if nearest_train:
        t = nearest_train[0]
        t_dist = format_distance(t.distance_meters)
        t_map = f"https://www.google.com/maps/search/?api=1&query={t.lat:.6f},{t.lon:.6f}"
        type_items.append(f"• 🚆 *Treno più vicino:* *{t.stop_name.replace('*', '')}* ({t_dist}) · [Mappa]({t_map})")

    if nearest_bus:
        b = nearest_bus[0]
        b_dist = format_distance(b.distance_meters)
        b_map = f"https://www.google.com/maps/search/?api=1&query={b.lat:.6f},{b.lon:.6f}"
        type_items.append(f"• 🚌 *Bus più vicino:* *{b.stop_name.replace('*', '')}* ({b_dist}) · [Mappa]({b_map})")

    if type_items:
        lines.append("🚏 *Più vicine per tipologia di mezzo:*")
        lines.extend(type_items)
        lines.append("")

    lines.append("💡 _Tocca \"Cerca Linea / Orari\" per trovare corse da una di queste stazioni._")
    return "\n".join(lines)


async def prompt_location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to send their location via a reply keyboard with request_location=True."""
    query = update.callback_query
    if query:
        await query.answer()

    chat = update.effective_chat
    if not chat:
        return

    await context.bot.send_message(
        chat_id=chat.id,
        text=PROMPT_LOCATION_TEXT,
        reply_markup=location_request_keyboard(),
        parse_mode="Markdown",
    )


async def prompt_location_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /posizione and /vicino commands. If coordinates are provided as arguments, calculate directly."""
    message = update.effective_message
    if not message:
        return

    args = getattr(context, "args", None)
    if args and len(args) >= 2:
        try:
            lat = float(args[0].replace(",", "."))
            lon = float(args[1].replace(",", "."))
            result_text = format_nearest_stops_message(lat, lon, transit_service)
            await message.reply_text(
                result_text,
                reply_markup=nearest_stops_result_keyboard(),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return
        except ValueError:
            pass

    await message.reply_text(
        PROMPT_LOCATION_TEXT,
        reply_markup=location_request_keyboard(),
        parse_mode="Markdown",
    )


async def location_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '❌ Annulla' button tap: remove reply keyboard and show main menu."""
    message = update.effective_message
    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    admin_flag = is_admin(chat_id)

    if message:
        await message.reply_text(
            "❌ Richiesta posizione annullata.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.reply_text(
            "Come posso aiutarti? Scegli un'opzione dal menu principale:",
            reply_markup=main_menu_keyboard(is_admin_user=admin_flag),
        )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming location coordinates and present closest stops."""
    message = update.effective_message
    if not message or not message.location:
        return

    chat = update.effective_chat
    if not chat:
        return

    lat = float(message.location.latitude)
    lon = float(message.location.longitude)

    # Immediately remove reply keyboard to keep the chat tidy
    status_msg = await message.reply_text(
        "📍 Coordinate ricevute! Ricerca delle fermate più vicine in corso...",
        reply_markup=ReplyKeyboardRemove(),
    )

    result_text = format_nearest_stops_message(lat, lon, transit_service)

    try:
        await status_msg.edit_text(
            result_text,
            reply_markup=nearest_stops_result_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        await message.reply_text(
            result_text,
            reply_markup=nearest_stops_result_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
