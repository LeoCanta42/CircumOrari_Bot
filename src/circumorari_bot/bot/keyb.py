"""Inline keyboards for Telegram bot interaction."""

from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from circumorari_bot.core.models import TransportType, FilterType, Stop


def main_menu_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    """Main dashboard menu buttons (includes Admin Panel button if user is an admin)."""
    rows = [
        [
            InlineKeyboardButton("🔍 Cerca Linea / Orari", callback_data="wizard_start"),
            InlineKeyboardButton("🤖 Assistente AI", callback_data="ai_help"),
        ],
        [
            InlineKeyboardButton("📢 Ultime Notizie", callback_data="news_latest"),
            InlineKeyboardButton("🔔 Notifiche & Avvisi", callback_data="news_sub_menu"),
        ],
        [
            InlineKeyboardButton("📄 Orari Ufficiali PDF", callback_data="doc_all"),
            InlineKeyboardButton("ℹ️ Guida & Info", callback_data="help_info"),
        ],
    ]
    if is_admin_user:
        rows.append([
            InlineKeyboardButton("🛠 Pannello Admin", callback_data="adm_menu"),
        ])
    return InlineKeyboardMarkup(rows)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Dashboard keyboard for administrative management."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Gestione Utenti", callback_data="adm_users:all:0"),
            InlineKeyboardButton("🔎 Cerca Utente", callback_data="adm_search_prompt"),
        ],
        [
            InlineKeyboardButton("⚙️ Impostazioni AI & Prezzi", callback_data="adm_ai_cfg"),
            InlineKeyboardButton("📊 Statistiche", callback_data="adm_stats"),
        ],
        [
            InlineKeyboardButton("🔄 Sincronizza GTFS", callback_data="adm_sync_gtfs"),
            InlineKeyboardButton("📰 Controlla Notizie", callback_data="adm_check_news"),
        ],
        [
            InlineKeyboardButton("📢 Invia Broadcast", callback_data="adm_broadcast_prompt"),
            InlineKeyboardButton("⚡ Resetta Comandi Bot", callback_data="adm_reset_commands"),
        ],
        [
            InlineKeyboardButton("🧹 Pulisci Inattivi (60gg)", callback_data="adm_prune"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main"),
        ],
    ])


def admin_users_list_keyboard(
    users: List[dict],
    filter_mode: str = "all",
    page: int = 0,
    page_size: int = 8
) -> InlineKeyboardMarkup:
    """Paginated inline keyboard of users for the admin panel."""
    total_users = len(users)
    total_pages = max(1, (total_users + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    page_users = users[start_idx : start_idx + page_size]

    keyboard = []

    # Filter tabs row
    f_all = "🔹 Tutti" if filter_mode == "all" else "Tutti"
    f_sub = "🔹 🔔 Iscritti" if filter_mode == "sub" else "🔔 Iscritti"
    f_ai = "🔹 🤖 AI" if filter_mode == "ai" else "🤖 AI"
    keyboard.append([
        InlineKeyboardButton(f_all, callback_data="adm_users:all:0"),
        InlineKeyboardButton(f_sub, callback_data="adm_users:sub:0"),
        InlineKeyboardButton(f_ai, callback_data="adm_users:ai:0"),
    ])

    for u in page_users:
        cid = str(u.get("chat_id", ""))
        uname = u.get("username")
        fname = u.get("first_name")
        is_sub = bool(u.get("is_subscribed"))
        is_ai = bool(u.get("ai_allowed"))

        badges = ""
        if is_ai:
            badges += "🤖"
        if is_sub:
            badges += "🔔"
        if not badges:
            badges = "👤"

        if uname:
            label = f"{badges} @{uname}"
            if fname:
                label += f" ({fname[:12]})"
        elif fname:
            label = f"{badges} {fname[:16]} [{cid}]"
        else:
            label = f"{badges} ID: {cid}"

        cb_val = f"adm_u_view:{cid}:{filter_mode}:{page}"[:64]
        keyboard.append([
            InlineKeyboardButton(label[:40], callback_data=cb_val)
        ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prec", callback_data=f"adm_users:{filter_mode}:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Succ ➡️", callback_data=f"adm_users:{filter_mode}:{page + 1}"))
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton("🔎 Cerca per @username / ID", callback_data="adm_search_prompt"),
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu"),
    ])
    return InlineKeyboardMarkup(keyboard)


def admin_user_detail_keyboard(
    user: dict,
    back_filter: str = "all",
    back_page: int = 0
) -> InlineKeyboardMarkup:
    """Action keyboard for a single user record in the admin panel."""
    cid = str(user.get("chat_id", ""))
    is_sub = bool(user.get("is_subscribed"))
    is_ai = bool(user.get("ai_allowed"))
    trial_used = bool(user.get("trial_used"))

    sub_btn = (
        InlineKeyboardButton("🔕 Disattiva Notifiche", callback_data=f"adm_u_sub:{cid}:0:{back_filter}:{back_page}")
        if is_sub
        else InlineKeyboardButton("🔔 Attiva Notifiche", callback_data=f"adm_u_sub:{cid}:1:{back_filter}:{back_page}")
    )
    ai_btn = (
        InlineKeyboardButton("🚫 Revoca AI", callback_data=f"adm_u_ai:{cid}:0:{back_filter}:{back_page}")
        if is_ai
        else InlineKeyboardButton("🟢 Abilita AI", callback_data=f"adm_u_ai:{cid}:1:{back_filter}:{back_page}")
    )

    rows = [
        [
            InlineKeyboardButton("➕ +30gg AI", callback_data=f"adm_u_add30:{cid}:{back_filter}:{back_page}"),
            InlineKeyboardButton("🎁 Prova 3gg", callback_data=f"adm_u_trial:{cid}:{back_filter}:{back_page}"),
        ],
        [
            InlineKeyboardButton("♾️ Accesso Illimitato", callback_data=f"adm_u_perm:{cid}:{back_filter}:{back_page}"),
            ai_btn,
        ],
        [
            InlineKeyboardButton(
                "🔄 Resetta Prova" if trial_used else "✅ Prova Non Usata",
                callback_data=f"adm_u_rtrial:{cid}:{back_filter}:{back_page}"
            ),
            sub_btn,
        ],
        [
            InlineKeyboardButton("🗑 Cancella Utente (GDPR)", callback_data=f"adm_u_del_ask:{cid}:{back_filter}:{back_page}"),
        ],
        [
            InlineKeyboardButton("⬅️ Lista Utenti", callback_data=f"adm_users:{back_filter}:{back_page}"),
            InlineKeyboardButton("🛠 Pannello Admin", callback_data="adm_menu"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def admin_ai_settings_keyboard(
    current_settings: dict,
    admin_test_mode: bool = False
) -> InlineKeyboardMarkup:
    """Keyboard for managing AI subscription prices, duration, trial and payments (strictly Telegram Stars)."""
    trial_enabled = current_settings.get("trial_enabled", True)
    trial_days = current_settings.get("trial_days", 3)
    sub_days = current_settings.get("sub_days", 30)
    stars = current_settings.get("sub_stars", 50)

    trial_toggle_label = "🎁 Prova: 🟢 ON (Attiva)" if trial_enabled else "🎁 Prova: 🔴 OFF (Spenta)"

    price_row = [
        InlineKeyboardButton("⭐ Stars", callback_data="noop"),
        InlineKeyboardButton(f"{'✅ ' if stars == 1 else ''}1⭐ (Test)", callback_data="adm_ai_cfg:stars:1"),
        InlineKeyboardButton(f"{'✅ ' if stars == 25 else ''}25⭐", callback_data="adm_ai_cfg:stars:25"),
        InlineKeyboardButton(f"{'✅ ' if stars == 50 else ''}50⭐", callback_data="adm_ai_cfg:stars:50"),
        InlineKeyboardButton(f"{'✅ ' if stars == 100 else ''}100⭐", callback_data="adm_ai_cfg:stars:100"),
    ]

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(trial_toggle_label, callback_data="adm_ai_cfg:toggle_trial"),
        ],
        [
            InlineKeyboardButton("⏱️ Prova", callback_data="noop"),
            InlineKeyboardButton(f"{'✅ ' if trial_days == 1 else ''}1gg", callback_data="adm_ai_cfg:trial_days:1"),
            InlineKeyboardButton(f"{'✅ ' if trial_days == 3 else ''}3gg", callback_data="adm_ai_cfg:trial_days:3"),
            InlineKeyboardButton(f"{'✅ ' if trial_days == 7 else ''}7gg", callback_data="adm_ai_cfg:trial_days:7"),
        ],
        [
            InlineKeyboardButton("📅 Periodo", callback_data="noop"),
            InlineKeyboardButton(f"{'✅ ' if sub_days == 15 else ''}15gg", callback_data="adm_ai_cfg:sub_days:15"),
            InlineKeyboardButton(f"{'✅ ' if sub_days == 30 else ''}30gg", callback_data="adm_ai_cfg:sub_days:30"),
            InlineKeyboardButton(f"{'✅ ' if sub_days == 60 else ''}60gg", callback_data="adm_ai_cfg:sub_days:60"),
        ],
        price_row,
        [
            InlineKeyboardButton("🧹 Disattiva Utenti Scaduti Adesso", callback_data="adm_ai_cfg:clean_expired"),
        ],
        [
            InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu"),
        ],
    ])


def ai_subscription_prompt_keyboard(
    can_trial: bool = False,
    is_expired: bool = False,
    price_display: str = "50 ⭐",
    sub_days: int = 30,
    is_info_view: bool = False
) -> InlineKeyboardMarkup:
    """Prompt keyboard presented to user for activating trial or purchasing monthly AI access."""
    rows = []
    if can_trial:
        rows.append([
            InlineKeyboardButton("🎁 Attiva Prova Gratuita (3 giorni)", callback_data="ai_trial_activate")
        ])

    btn_label = (
        f"💳 Rinnova Abbonamento ({price_display} / {sub_days}gg)"
        if is_expired
        else f"💳 Abbonati a {price_display} ({sub_days} giorni)"
    )
    rows.append([
        InlineKeyboardButton(btn_label, callback_data="ai_subscribe")
    ])
    if is_info_view:
        rows.append([
            InlineKeyboardButton("↩️ Torna all'Abbonamento", callback_data="ai_help"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("ℹ️ Info Privacy & Abbonamento", callback_data="ai_sub_info"),
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(rows)


def admin_user_delete_confirm_keyboard(
    chat_id: str,
    back_filter: str = "all",
    back_page: int = 0
) -> InlineKeyboardMarkup:
    """Confirmation keyboard before deleting a user record."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚠️ Sì, Elimina Definitivamente", callback_data=f"adm_u_del_ok:{chat_id}:{back_filter}:{back_page}"),
        ],
        [
            InlineKeyboardButton("↩️ Annulla", callback_data=f"adm_u_view:{chat_id}:{back_filter}:{back_page}"),
        ],
    ])


def subscription_manage_keyboard(is_subscribed: bool) -> InlineKeyboardMarkup:
    """Keyboard for opting in/out of service news broadcasts."""
    if is_subscribed:
        action_btn = InlineKeyboardButton("🔕 Disattiva Notifiche", callback_data="sub_disable")
    else:
        action_btn = InlineKeyboardButton("🔔 Iscriviti agli Avvisi", callback_data="sub_enable")

    return InlineKeyboardMarkup([
        [action_btn],
        [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
    ])


def transport_select_keyboard() -> InlineKeyboardMarkup:
    """Step 1: Choose mode of transport."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚌 Bus / Autolinee", callback_data="tt_bus"),
            InlineKeyboardButton("🚆 Treno (Superficie)", callback_data="tt_train"),
        ],
        [
            InlineKeyboardButton("🚇 Metropolitana", callback_data="tt_metro"),
            InlineKeyboardButton("🚍 Tutti i Mezzi", callback_data="tt_any"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main"),
        ],
    ])


def stop_cities_keyboard(
    available_stops: List[Stop],
    is_departure: bool = True
) -> InlineKeyboardMarkup:
    """Keyboard grouping stops by municipality (Comune / City) for fast manual lookup."""
    city_counts: dict[str, int] = {}
    for s in available_stops:
        if s.city:
            city_counts[s.city] = city_counts.get(s.city, 0) + 1

    cities = sorted(city_counts.keys())
    cb_prefix = "city_dep:" if is_departure else "city_arr:"
    alpha_cb = "alpha_dep" if is_departure else "alpha_arr"
    all_cb = "all_dep" if is_departure else "all_arr"
    back_cb = "wizard_start" if is_departure else "back_to_departure"

    keyboard = []
    row = []
    for c in cities:
        label = f"🏙 {c} ({city_counts[c]})"
        row.append(InlineKeyboardButton(label[:30], callback_data=f"{cb_prefix}{c}"[:64]))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("🔤 Per Iniziale (A-Z)", callback_data=alpha_cb),
        InlineKeyboardButton("🌐 Tutte le fermate", callback_data=all_cb),
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ Indietro", callback_data=back_cb)
    ])
    return InlineKeyboardMarkup(keyboard)


def stop_letters_keyboard(
    available_stops: List[Stop],
    is_departure: bool = True
) -> InlineKeyboardMarkup:
    """Keyboard displaying available initial letters of stops, plus options to filter by city or show all."""
    letters = sorted(list(set(s.stop_name[0].upper() for s in available_stops if s.stop_name)))
    cb_prefix = "let_dep:" if is_departure else "let_arr:"
    cities_cb = "cities_dep" if is_departure else "cities_arr"
    all_cb = "all_dep" if is_departure else "all_arr"
    back_cb = "wizard_start" if is_departure else "back_to_departure"
    has_cities = any(s.city for s in available_stops)

    keyboard = []
    chunk_size = 5
    for i in range(0, len(letters), chunk_size):
        row = [
            InlineKeyboardButton(f" {ltr} ", callback_data=f"{cb_prefix}{ltr}")
            for ltr in letters[i : i + chunk_size]
        ]
        keyboard.append(row)

    if has_cities:
        keyboard.append([
            InlineKeyboardButton("🏙 Filtra per Comune", callback_data=cities_cb),
            InlineKeyboardButton("🌐 Tutte le fermate", callback_data=all_cb),
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🌐 Tutte le fermate", callback_data=all_cb)
        ])
    keyboard.append([
        InlineKeyboardButton("⬅️ Indietro", callback_data=back_cb)
    ])
    return InlineKeyboardMarkup(keyboard)


def stops_pagination_keyboard(
    stops: List[Stop],
    page: int = 0,
    is_departure: bool = True,
    letter: Optional[str] = None,
    city: Optional[str] = None,
    page_size: int = 20
) -> InlineKeyboardMarkup:
    """Paginated inline keyboard for choosing departure/arrival stops (20 stops per page + city/letter filters)."""
    if city:
        filtered_stops = [s for s in stops if s.city and s.city.upper() == city.upper()]
    elif letter:
        filtered_stops = [s for s in stops if s.stop_name and s.stop_name.upper().startswith(letter.upper())]
    else:
        filtered_stops = stops

    total_stops = len(filtered_stops)
    total_pages = max(1, (total_stops + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    page_stops = filtered_stops[start_idx : start_idx + page_size]

    cb_prefix = "dep_stop:" if is_departure else "arr_stop:"
    if city:
        scope_param = f"CITY_{city}"
    elif letter:
        scope_param = letter
    else:
        scope_param = "ALL"
    nav_prefix = f"page_dep:{scope_param}:" if is_departure else f"page_arr:{scope_param}:"

    keyboard = []
    # 2 buttons per row (up to 20 stops per page = 10 compact rows)
    row = []
    for s in page_stops:
        btn_label = s.stop_name[:28] if city else s.display_name[:28]
        row.append(InlineKeyboardButton(btn_label, callback_data=f"{cb_prefix}{s.stop_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Navigation buttons (with fast +/-5 page jumps when many pages exist)
    if total_pages > 1:
        nav_row = []
        if total_pages > 4 and page >= 5:
            nav_row.append(InlineKeyboardButton("⏪ -5", callback_data=f"{nav_prefix}{max(0, page - 5)}"[:64]))
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prec", callback_data=f"{nav_prefix}{page - 1}"[:64]))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Succ ➡️", callback_data=f"{nav_prefix}{page + 1}"[:64]))
        if total_pages > 4 and page <= total_pages - 6:
            nav_row.append(InlineKeyboardButton("+5 ⏩", callback_data=f"{nav_prefix}{min(total_pages - 1, page + 5)}"[:64]))
        keyboard.append(nav_row)

    # Filter / Change city or letter buttons
    alpha_cb = "alpha_dep" if is_departure else "alpha_arr"
    cities_cb = "cities_dep" if is_departure else "cities_arr"
    has_cities = any(s.city for s in stops)
    if len(stops) > 12:
        filter_row = []
        if has_cities:
            filter_row.append(InlineKeyboardButton("🏙 Per Comune", callback_data=cities_cb))
        filter_row.append(
            InlineKeyboardButton("🔤 Cambia Lettera" if letter else "🔤 Per Lettera", callback_data=alpha_cb)
        )
        keyboard.append(filter_row)

    # Back button
    keyboard.append([
        InlineKeyboardButton("⬅️ Indietro", callback_data="wizard_start" if is_departure else "back_to_departure")
    ])

    return InlineKeyboardMarkup(keyboard)


def filter_type_keyboard() -> InlineKeyboardMarkup:
    """Step 3: Choose filtering condition for schedule lookup."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🕒 Partenza vicino l'ora scelta", callback_data=f"filt:{FilterType.DEPARTURE_NEAR.value}"),
        ],
        [
            InlineKeyboardButton("🏁 Arrivo vicino l'ora scelta", callback_data=f"filt:{FilterType.ARRIVAL_NEAR.value}"),
        ],
        [
            InlineKeyboardButton("⏱ Dalla partenza in poi (consigliato)", callback_data=f"filt:{FilterType.DEPARTURE_AFTER.value}"),
        ],
        [
            InlineKeyboardButton("⌛ Prima dell'orario di arrivo", callback_data=f"filt:{FilterType.ARRIVAL_BEFORE.value}"),
        ],
        [
            InlineKeyboardButton("⬅️ Indietro", callback_data="back_to_arrival"),
        ],
    ])


def hours_keyboard() -> InlineKeyboardMarkup:
    """Step 4: Choose target hour."""
    hours = [
        "05:00", "06:00", "07:00", "08:00",
        "09:00", "10:00", "11:00", "12:00",
        "13:00", "14:00", "15:00", "16:00",
        "17:00", "18:00", "19:00", "20:00",
        "21:00", "22:00"
    ]
    keyboard = []
    row = []
    for h in hours:
        row.append(InlineKeyboardButton(h, callback_data=f"hour:{h}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back_to_filters")])
    return InlineKeyboardMarkup(keyboard)


def documents_keyboard(metro_url: str, train_url: str, bus_url: str) -> InlineKeyboardMarkup:
    """Keyboard with direct external links to download PDF schedules."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚇 Scarica PDF Metro", url=metro_url)],
        [InlineKeyboardButton("🚆 Scarica PDF Treni Superficie", url=train_url)],
        [InlineKeyboardButton("🚌 Scarica PDF Autolinee", url=bus_url)],
        [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
    ])


def trip_results_keyboard() -> InlineKeyboardMarkup:
    """Action buttons shown beneath trip search results."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Nuova Ricerca", callback_data="wizard_start"),
            InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main"),
        ],
    ])
