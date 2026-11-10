"""Interactive timetable wizard and transit query handlers (with City filtering & inline text search)."""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from circumorari_bot.bot.keyb import (
    filter_type_keyboard,
    hours_keyboard,
    main_menu_keyboard,
    stop_cities_keyboard,
    stop_letters_keyboard,
    stops_pagination_keyboard,
    transport_select_keyboard,
    trip_results_keyboard,
)
from circumorari_bot.core.models import FilterType, TransportType
from circumorari_bot.services.transit_service import transit_service

logger = logging.getLogger(__name__)


async def viaggio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut command: /viaggio <partenza> - <arrivo> [ora]."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.effective_message.reply_text(
            "ℹ️ *Uso del comando /viaggio:*\n"
            "`/viaggio Catania - Bronte 14:00`\n"
            "`/viaggio Catania Santa Sofia - Stesicoro 09:00`\n"
            "`/viaggio Randazzo Paternò`\n"
            "Oppure usa il pulsante *Cerca Linea / Orari* per la procedura guidata (anche per Comune!).",
            parse_mode="Markdown",
        )
        return

    from circumorari_bot.services.ai_service import ai_service

    resp = ai_service.answer_query(text)
    await update.effective_message.reply_text(resp.answer, parse_mode="Markdown")


async def fermate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /fermate [query o comune] to search stations by name, similar name, or city."""
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        cities = transit_service.get_cities()
        city_list = ", ".join(f"_{c['city']}_ ({c['count']})" for c in cities[:25])
        msg = (
            "🚉 *Ricerca Fermate e Comuni*\n\n"
            "Puoi cercare una fermata per *nome*, *nome simile* o *Comune*:\n"
            "• `/fermate Catania`\n"
            "• `/fermate Santa Sofia`\n"
            "• `/fermate Belpasso`\n"
        )
        if city_list:
            msg += f"\n🏙 *Comuni serviti:*\n{city_list}"
        await update.effective_message.reply_text(msg, parse_mode="Markdown")
        return

    stops = transit_service.search_stops(query, limit=25)
    if not stops:
        await update.effective_message.reply_text(
            f"❌ Nessuna fermata trovata per '{query}'.",
            parse_mode="Markdown",
        )
        return

    lines = [f"🔍 *Fermate trovate per '{query}' ({len(stops)}):*\n"]
    for s in stops:
        icons = []
        if s.has_metro:
            icons.append("🚇 Metro")
        if s.has_train:
            icons.append("🚆 Treno")
        if s.has_bus:
            icons.append("🚌 Bus")
        types_str = " | ".join(icons) or "Bus"
        lines.append(f"• *{s.display_name}* ({types_str})")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_wizard_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Allow users in the manual timetable wizard to type a stop or city name directly in chat."""
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return False

    step = user_data.get("wizard_input_step")
    if step not in ("dep", "arr"):
        return False

    msg = update.effective_message
    query_text = (msg.text or "").strip() if msg else ""
    if not query_text or query_text.startswith("/"):
        return False

    ttype = user_data.get("transport_type", TransportType.QUALSIASI)
    matched = transit_service.search_stops(query_text, transport_type=ttype, limit=20)
    is_dep = step == "dep"

    if not matched:
        await msg.reply_text(
            f"❌ Nessuna fermata trovata per *{query_text}* ({ttype.value}).\n"
            "Riprova scrivendo un altro nome/comune oppure usa i pulsanti qui sopra.",
            parse_mode="Markdown",
        )
        return True

    # Ensure matched stops are also available in user_data lookup list when clicked
    existing = user_data.get("available_stops") or []
    existing_ids = {str(s.stop_id) for s in existing}
    for m in matched:
        if str(m.stop_id) not in existing_ids:
            existing.append(m)
            existing_ids.add(str(m.stop_id))
    user_data["available_stops"] = existing

    role_txt = "PARTENZA" if is_dep else "ARRIVO"
    await msg.reply_text(
        f"🔎 *Fermate di {role_txt} trovate per '{query_text}':*\n"
        "Tocca la fermata desiderata:",
        reply_markup=stops_pagination_keyboard(matched, page=0, is_departure=is_dep),
        parse_mode="Markdown",
    )
    return True


def _initial_stops_prompt(stops, is_departure: bool, header_prefix: str = "") -> tuple[str, object]:
    """Return initial stop selection prompt and keyboard (preferring City grouping when >12 stops)."""
    role = "Step 2/4 - Fermata di PARTENZA" if is_departure else "Step 3/4 - Fermata di ARRIVO"
    icon = "📍" if is_departure else "🏁"
    action = "partire" if is_departure else "arrivare"

    if len(stops) <= 12:
        text = f"{header_prefix}{icon} *{role}*\nSeleziona la fermata da cui vuoi {action}:"
        return text, stops_pagination_keyboard(stops, page=0, is_departure=is_departure)

    if any(s.city for s in stops):
        text = (
            f"{header_prefix}{icon} *{role}*\n"
            "🏙 Scegli il *Comune*, filtra per lettera, oppure *scrivi direttamente in chat* il nome della fermata:"
        )
        return text, stop_cities_keyboard(stops, is_departure=is_departure)

    text = f"{header_prefix}{icon} *{role}*\nScegli l'iniziale della fermata:"
    return text, stop_letters_keyboard(stops, is_departure=is_departure)


# ==========================================
# Wizard Callback Query Handlers
# ==========================================


async def wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactive timetable lookup button callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return

    if data in ("wizard_start", "back_to_transport"):
        context.user_data.clear()
        await query.edit_message_text(
            "🧭 *Step 1/4 - Tipologia Trasporto*\n\n"
            "Scegli con quale mezzo desideri viaggiare:",
            reply_markup=transport_select_keyboard(),
            parse_mode="Markdown",
        )
        return

    if data.startswith("tt_"):
        tt_key = data.split("_")[1]
        tt_map = {
            "bus": TransportType.BUS,
            "train": TransportType.TRENO,
            "metro": TransportType.METRO,
            "any": TransportType.QUALSIASI,
        }
        selected_tt = tt_map.get(tt_key, TransportType.QUALSIASI)
        context.user_data["transport_type"] = selected_tt

        stops = transit_service.get_stops_by_type(selected_tt)
        context.user_data["available_stops"] = stops
        context.user_data["dep_page"] = 0
        context.user_data["dep_letter"] = None
        context.user_data["dep_city"] = None
        context.user_data["wizard_input_step"] = "dep"

        text, kb = _initial_stops_prompt(stops, is_departure=True)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # City filter menu for departure / arrival
    if data in ("cities_dep", "cities_arr"):
        is_dep = data == "cities_dep"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        context.user_data["wizard_input_step"] = "dep" if is_dep else "arr"
        dep_stop = context.user_data.get("dep_stop")
        prefix = f"📍 Partenza: *{dep_stop.display_name}*\n\n" if (not is_dep and dep_stop) else ""
        text, kb = _initial_stops_prompt(stops, is_departure=is_dep, header_prefix=prefix)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # User picked a Comune for departure
    if data.startswith("city_dep:"):
        city = data.split(":", 1)[1]
        context.user_data["dep_city"] = city
        context.user_data["dep_letter"] = None
        context.user_data["dep_page"] = 0
        context.user_data["wizard_input_step"] = "dep"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops

        await query.edit_message_text(
            f"📍 *Step 2/4 - Fermata di PARTENZA* (🏙 {city})\n\n"
            "Seleziona la fermata (oppure scrivi il nome in chat):",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=True, city=city),
            parse_mode="Markdown",
        )
        return

    # User picked an initial letter for departure
    if data.startswith("let_dep:"):
        letter = data.split(":")[1]
        context.user_data["dep_letter"] = letter
        context.user_data["dep_city"] = None
        context.user_data["dep_page"] = 0
        context.user_data["wizard_input_step"] = "dep"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops

        await query.edit_message_text(
            f"📍 *Step 2/4 - Fermata di PARTENZA* (Lettera {letter})\n\n"
            "Seleziona la fermata da cui vuoi partire:",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=True, letter=letter),
            parse_mode="Markdown",
        )
        return

    # User chose to see all departure stops
    if data == "all_dep":
        context.user_data["dep_letter"] = None
        context.user_data["dep_city"] = None
        context.user_data["dep_page"] = 0
        context.user_data["wizard_input_step"] = "dep"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops

        await query.edit_message_text(
            "📍 *Step 2/4 - Fermata di PARTENZA*\n\n"
            "Seleziona la fermata da cui vuoi partire:",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=True),
            parse_mode="Markdown",
        )
        return

    # Return to letters keyboard for departure
    if data == "alpha_dep":
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        context.user_data["wizard_input_step"] = "dep"
        tt_val = context.user_data.get("transport_type", TransportType.QUALSIASI).value

        await query.edit_message_text(
            f"📍 *Step 2/4 - Fermata di PARTENZA* ({tt_val})\n\n"
            "Scegli l'iniziale della fermata di partenza:",
            reply_markup=stop_letters_keyboard(stops, is_departure=True),
            parse_mode="Markdown",
        )
        return

    # Pagination for departure stops
    if data.startswith("page_dep:"):
        parts = data.split(":")
        letter = None
        city = None
        if len(parts) == 3:
            scope_str = parts[1]
            page = int(parts[2])
            if scope_str.startswith("CITY_"):
                city = scope_str[5:]
            elif scope_str != "ALL":
                letter = scope_str
        else:
            page = int(parts[1])
            letter = context.user_data.get("dep_letter")
            city = context.user_data.get("dep_city")

        context.user_data["dep_page"] = page
        context.user_data["dep_letter"] = letter
        context.user_data["dep_city"] = city
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops

        try:
            await query.edit_message_reply_markup(
                reply_markup=stops_pagination_keyboard(stops, page=page, is_departure=True, letter=letter, city=city)
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("Page edit error: %s", e)
        return

    # Departure stop chosen
    if data.startswith("dep_stop:"):
        stop_id = data.split(":")[1]
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        selected_stop = next((s for s in stops if str(s.stop_id) == str(stop_id)), None)
        context.user_data["dep_stop"] = selected_stop

        context.user_data["arr_page"] = 0
        context.user_data["arr_letter"] = None
        context.user_data["arr_city"] = None
        context.user_data["wizard_input_step"] = "arr"
        dep_name = selected_stop.display_name if selected_stop else "Scelta"

        text, kb = _initial_stops_prompt(
            stops, is_departure=False, header_prefix=f"📍 Partenza: *{dep_name}*\n\n"
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # User picked a Comune for arrival
    if data.startswith("city_arr:"):
        city = data.split(":", 1)[1]
        context.user_data["arr_city"] = city
        context.user_data["arr_letter"] = None
        context.user_data["arr_page"] = 0
        context.user_data["wizard_input_step"] = "arr"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n\n"
            f"🏁 *Step 3/4 - Fermata di ARRIVO* (🏙 {city})\n"
            "Seleziona la fermata di destinazione:",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=False, city=city),
            parse_mode="Markdown",
        )
        return

    # User picked an initial letter for arrival
    if data.startswith("let_arr:"):
        letter = data.split(":")[1]
        context.user_data["arr_letter"] = letter
        context.user_data["arr_city"] = None
        context.user_data["arr_page"] = 0
        context.user_data["wizard_input_step"] = "arr"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n\n"
            f"🏁 *Step 3/4 - Fermata di ARRIVO* (Lettera {letter})\n"
            "Seleziona la fermata di destinazione:",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=False, letter=letter),
            parse_mode="Markdown",
        )
        return

    # User chose to see all arrival stops
    if data == "all_arr":
        context.user_data["arr_letter"] = None
        context.user_data["arr_city"] = None
        context.user_data["arr_page"] = 0
        context.user_data["wizard_input_step"] = "arr"
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n\n"
            "🏁 *Step 3/4 - Fermata di ARRIVO*\n"
            "Seleziona la fermata di destinazione:",
            reply_markup=stops_pagination_keyboard(stops, page=0, is_departure=False),
            parse_mode="Markdown",
        )
        return

    # Return to letters keyboard for arrival
    if data == "alpha_arr":
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        context.user_data["wizard_input_step"] = "arr"
        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n\n"
            "🏁 *Step 3/4 - Fermata di ARRIVO*\n"
            "Scegli l'iniziale della fermata di destinazione:",
            reply_markup=stop_letters_keyboard(stops, is_departure=False),
            parse_mode="Markdown",
        )
        return

    # Pagination for arrival stops
    if data.startswith("page_arr:"):
        parts = data.split(":")
        letter = None
        city = None
        if len(parts) == 3:
            scope_str = parts[1]
            page = int(parts[2])
            if scope_str.startswith("CITY_"):
                city = scope_str[5:]
            elif scope_str != "ALL":
                letter = scope_str
        else:
            page = int(parts[1])
            letter = context.user_data.get("arr_letter")
            city = context.user_data.get("arr_city")

        context.user_data["arr_page"] = page
        context.user_data["arr_letter"] = letter
        context.user_data["arr_city"] = city
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops

        try:
            await query.edit_message_reply_markup(
                reply_markup=stops_pagination_keyboard(stops, page=page, is_departure=False, letter=letter, city=city)
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("Page edit error: %s", e)
        return

    # Back to departure from arrival
    if data == "back_to_departure":
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        context.user_data["wizard_input_step"] = "dep"
        dep_city = context.user_data.get("dep_city")
        dep_letter = context.user_data.get("dep_letter")
        dep_page = context.user_data.get("dep_page", 0)

        if dep_city:
            await query.edit_message_text(
                f"📍 *Step 2/4 - Fermata di PARTENZA* (🏙 {dep_city})\n\nSeleziona la fermata da cui vuoi partire:",
                reply_markup=stops_pagination_keyboard(stops, page=dep_page, is_departure=True, city=dep_city),
                parse_mode="Markdown",
            )
        elif dep_letter:
            await query.edit_message_text(
                f"📍 *Step 2/4 - Fermata di PARTENZA* (Lettera {dep_letter})\n\nSeleziona la fermata da cui vuoi partire:",
                reply_markup=stops_pagination_keyboard(stops, page=dep_page, is_departure=True, letter=dep_letter),
                parse_mode="Markdown",
            )
        else:
            text, kb = _initial_stops_prompt(stops, is_departure=True)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # Arrival stop chosen
    if data.startswith("arr_stop:"):
        stop_id = data.split(":")[1]
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        selected_stop = next((s for s in stops if str(s.stop_id) == str(stop_id)), None)
        context.user_data["arr_stop"] = selected_stop
        context.user_data.pop("wizard_input_step", None)

        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"
        arr_name = selected_stop.display_name if selected_stop else "Arrivo"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n"
            f"🏁 Destinazione: *{arr_name}*\n\n"
            "⏱ *Step 4/4 - Criterio di Ricerca*\n"
            "Come desideri impostare la ricerca?",
            reply_markup=filter_type_keyboard(),
            parse_mode="Markdown",
        )
        return

    # Back to arrival from filter selection
    if data == "back_to_arrival":
        stops = context.user_data.get("available_stops") or transit_service.get_stops_by_type(
            context.user_data.get("transport_type", TransportType.QUALSIASI)
        )
        context.user_data["available_stops"] = stops
        context.user_data["wizard_input_step"] = "arr"
        arr_city = context.user_data.get("arr_city")
        arr_letter = context.user_data.get("arr_letter")
        arr_page = context.user_data.get("arr_page", 0)
        dep_stop = context.user_data.get("dep_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"

        if arr_city:
            await query.edit_message_text(
                f"📍 Partenza: *{dep_name}*\n\n"
                f"🏁 *Step 3/4 - Fermata di ARRIVO* (🏙 {arr_city})\n"
                "Seleziona la fermata di destinazione:",
                reply_markup=stops_pagination_keyboard(stops, page=arr_page, is_departure=False, city=arr_city),
                parse_mode="Markdown",
            )
        elif arr_letter:
            await query.edit_message_text(
                f"📍 Partenza: *{dep_name}*\n\n"
                f"🏁 *Step 3/4 - Fermata di ARRIVO* (Lettera {arr_letter})\n"
                "Seleziona la fermata di destinazione:",
                reply_markup=stops_pagination_keyboard(stops, page=arr_page, is_departure=False, letter=arr_letter),
                parse_mode="Markdown",
            )
        else:
            text, kb = _initial_stops_prompt(
                stops, is_departure=False, header_prefix=f"📍 Partenza: *{dep_name}*\n\n"
            )
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # Filter criterion chosen
    if data.startswith("filt:"):
        filt_val = data.split(":")[1]
        context.user_data["filter_type"] = FilterType(filt_val)
        context.user_data.pop("wizard_input_step", None)
        await query.edit_message_text(
            "🕒 *Scegli l'orario desiderato:*",
            reply_markup=hours_keyboard(),
            parse_mode="Markdown",
        )
        return

    # Back to filters from hours
    if data == "back_to_filters":
        dep_stop = context.user_data.get("dep_stop")
        arr_stop = context.user_data.get("arr_stop")
        dep_name = dep_stop.display_name if dep_stop else "Partenza"
        arr_name = arr_stop.display_name if arr_stop else "Arrivo"

        await query.edit_message_text(
            f"📍 Partenza: *{dep_name}*\n"
            f"🏁 Destinazione: *{arr_name}*\n\n"
            "⏱ *Step 4/4 - Criterio di Ricerca*\n"
            "Come desideri impostare la ricerca?",
            reply_markup=filter_type_keyboard(),
            parse_mode="Markdown",
        )
        return

    # Hour chosen: execute lookup
    if data.startswith("hour:"):
        chosen_hour = data.split(":", 1)[1]
        dep_stop = context.user_data.get("dep_stop")
        arr_stop = context.user_data.get("arr_stop")
        ttype = context.user_data.get("transport_type", TransportType.QUALSIASI)
        ftype = context.user_data.get("filter_type", FilterType.DEPARTURE_AFTER)
        context.user_data.pop("wizard_input_step", None)

        if not dep_stop or not arr_stop:
            await query.edit_message_text(
                "⚠️ Sessione scaduta. Ricomincia la ricerca dal menu.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        itineraries = transit_service.find_trips(
            origin=dep_stop.stop_name,
            destination=arr_stop.stop_name,
            target_time=chosen_hour,
            transport_type=ttype,
            filter_type=ftype,
            limit=6,
        )

        dep_name = dep_stop.display_name
        arr_name = arr_stop.display_name

        if not itineraries:
            await query.edit_message_text(
                f"❌ Nessuna corsa trovata tra *{dep_name}* e *{arr_name}* all'orario selezionato ({chosen_hour}).\n\n"
                "💡 Prova a selezionare un'ora differente, scegli *Tutti i Mezzi*, o consulta i documenti PDF ufficiali.",
                reply_markup=trip_results_keyboard(),
                parse_mode="Markdown",
            )
            return

        result_header = (
            f"🚉 *Itinerari trovati*\n"
            f"Da: *{dep_name}*\n"
            f"A: *{arr_name}*\n"
            f"Filtro: `{chosen_hour}`\n\n"
        )
        it_texts = [it.format_telegram() for it in itineraries]
        full_text = result_header + "\n".join(it_texts)

        if len(full_text) > 4000:
            full_text = full_text[:3990] + "\n..."

        await query.edit_message_text(
            full_text,
            reply_markup=trip_results_keyboard(),
            parse_mode="Markdown",
        )
        return
