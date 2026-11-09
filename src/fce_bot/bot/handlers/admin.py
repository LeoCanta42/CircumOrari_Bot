"""Admin maintenance, user management, and broadcast handlers."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from fce_bot.config import settings
from fce_bot.data.users_repo import UsersRepository
from fce_bot.data.settings_repo import SettingsRepository
from fce_bot.data.gtfs_loader import download_and_sync_gtfs
from fce_bot.data.db import get_db, get_persistent_db, get_persistence_status
from fce_bot.bot.keyb import (
    admin_panel_keyboard,
    admin_users_list_keyboard,
    admin_user_detail_keyboard,
    admin_user_delete_confirm_keyboard,
    admin_ai_settings_keyboard,
)
from fce_bot.bot.handlers.news import check_and_broadcast_news

logger = logging.getLogger(__name__)
users_repo = UsersRepository()
settings_repo = SettingsRepository()


def is_admin(user_id: int) -> bool:
    """Check if given Telegram user is configured as an admin.
    
    Secure by default: if no admins are explicitly configured in ADMIN_USER_IDS,
    access to privileged administrative commands is denied.
    """
    if not settings.admin_user_ids:
        logger.warning("Admin command attempted but no ADMIN_USER_IDS configured. Denying access for user %s", user_id)
        return False
    return user_id in settings.admin_user_ids


def _format_stats_text() -> str:
    """Generate formatted system and database statistics message."""
    stats = users_repo.get_stats()
    persistence = get_persistence_status()
    with get_db() as conn:
        cur = conn.cursor()
        stops_cnt = cur.execute("SELECT count(*) as cnt FROM stops").fetchone()["cnt"]
        trips_cnt = cur.execute("SELECT count(*) as cnt FROM trips").fetchone()["cnt"]
        routes_cnt = cur.execute("SELECT count(*) as cnt FROM routes").fetchone()["cnt"]
        times_cnt = cur.execute("SELECT count(*) as cnt FROM stop_times").fetchone()["cnt"]
        try:
            cities_cnt = cur.execute("SELECT count(DISTINCT city) as cnt FROM stops WHERE city != ''").fetchone()["cnt"]
        except Exception:
            cities_cnt = 0

    try:
        with get_persistent_db() as pconn:
            news_cnt = pconn.cursor().execute("SELECT count(*) as cnt FROM news_cache").fetchone()["cnt"]
    except Exception:
        news_cnt = 0

    ai_active = stats.get("ai_active_users", stats.get("ai_allowed_users", 0))
    ai_trials = stats.get("ai_trial_users", 0)
    ai_expired = stats.get("ai_expired_users", 0)

    return (
        "📊 *Statistiche Sistema FCE Bot*\n\n"
        f"💾 *Storage Utenti:* {persistence['label']}\n\n"
        f"👥 *Utenti Totali:* `{stats['total_users']}`\n"
        f"🔔 *Iscritti attivi:* `{stats['subscribed_users']}`\n"
        f"🤖 *Utenti con accesso AI:* `{stats['ai_allowed_users']}` (Attivi: `{ai_active}` | Scaduti: `{ai_expired}`)\n"
        f"🎁 *Prove gratuite utilizzate:* `{ai_trials}`\n"
        f"⚡ *Attivi ultimi 30gg:* `{stats['active_last_30_days']}`\n\n"
        "🗄 *Database GTFS:*\n"
        f"• Fermate censite: `{stops_cnt}` (`{cities_cnt}` comuni)\n"
        f"• Linee/Percorsi: `{routes_cnt}`\n"
        f"• Corse giornaliere: `{trips_cnt}`\n"
        f"• Orari di passaggio: `{times_cnt}`\n"
        f"• Notizie memorizzate: `{news_cnt}`\n"
    )


def _escape_md(val: str) -> str:
    """Safely escape legacy Telegram Markdown v1 special characters in dynamic text."""
    if not val:
        return ""
    return (
        str(val)
        .replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "'")
        .replace("[", "(")
        .replace("]", ")")
    )


def _format_user_card(user: dict) -> str:
    """Format a user record into a detailed Telegram Markdown card."""
    cid = str(user.get("chat_id", "N/A"))
    uname = user.get("username")
    fname = user.get("first_name")
    last_act = user.get("last_active") or "N/A"
    is_sub = bool(user.get("is_subscribed"))
    is_ai = bool(user.get("ai_allowed"))
    ai_exp = user.get("ai_expires_at")
    trial_used = bool(user.get("trial_used"))

    try:
        is_cfg_admin = int(cid) in settings.admin_user_ids
    except (ValueError, TypeError):
        is_cfg_admin = False

    is_admin_test = is_cfg_admin and settings_repo.get_admin_ai_test_mode(cid)
    uname_display = f"`@{uname}`" if uname else "_(Nessun username)_"
    fname_display = f"`{fname}`" if fname else "_(Non specificato)_"
    sub_display = "🟢 Attive (Iscritto)" if is_sub else "🔴 Disattivate"
    if is_cfg_admin and not is_admin_test:
        ai_display = "👑 Abilitato (Admin di sistema)"
    elif is_ai:
        if ai_exp is None:
            ai_display = "🟢 Abilitato (Permanente)"
        else:
            ai_display = f"🟢 Abilitato fino al `{ai_exp}` UTC"
    elif ai_exp:
        ai_display = f"🔴 Scaduto il `{ai_exp}` UTC"
    else:
        ai_display = "🔴 Disabilitato"

    if is_admin_test:
        ai_display += " _(🧪 Test Acquisto Attivo)_"

    trial_display = "Utilizzata" if trial_used else "Disponibile (Non ancora usata)"

    return (
        "👤 *Scheda Utente FCE Bot*\n\n"
        f"🆔 *Chat ID:* `{cid}`\n"
        f"🏷 *Username:* {uname_display}\n"
        f"📛 *Nome:* {fname_display}\n"
        f"🕒 *Ultimo accesso:* `{last_act}`\n\n"
        f"🔔 *Notifiche & Avvisi:* {sub_display}\n"
        f"🤖 *Accesso Assistente AI:* {ai_display}\n"
        f"🎁 *Prova Gratuita 3gg:* `{trial_display}`\n"
    )


# ==========================================
# Slash Commands for Admin
# ==========================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the interactive Admin Dashboard (/admin)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if isinstance(getattr(context, "user_data", None), dict):
        context.user_data.pop("admin_state", None)

    stats = users_repo.get_stats()
    persistence = get_persistence_status()
    text = (
        "🛠 *Pannello di Controllo Amministratore*\n\n"
        f"💾 Storage: {persistence['label']}\n"
        f"👥 Utenti registrati: `{stats['total_users']}` | "
        f"🔔 Iscritti: `{stats['subscribed_users']}` | "
        f"🤖 AI: `{stats['ai_allowed_users']}`\n\n"
        "Seleziona un'operazione dal menu sottostante oppure usa i comandi rapidi:\n"
        "• `/user <@username o chat_id>` — Gestisci singolo utente\n"
        "• `/users [filtro]` — Lista e cerca utenti\n"
        "• `/allow_ai` / `/disallow_ai <@user|id>` — Permessi AI\n"
        "• `/sub_user` / `/unsub_user <@user|id>` — Notifiche utente\n"
        "• `/delete_user <@user|id>` — Cancella record utente\n"
        "• `/broadcast <messaggio>` — Invia avviso agli iscritti"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=admin_panel_keyboard(),
        parse_mode="Markdown"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user and database statistics (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    msg = _format_stats_text()
    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def sync_gtfs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger manual GTFS database re-sync from circumetnea.it (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Download e sincronizzazione GTFS in corso...")
    try:
        stops, trips, st = download_and_sync_gtfs()
        await status_msg.edit_text(
            f"✅ *Sincronizzazione completata con successo!*\n"
            f"• Fermate: `{stops}`\n"
            f"• Corse: `{trips}`\n"
            f"• Passaggi orari: `{st}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("GTFS sync failed: %s", e)
        await status_msg.edit_text(f"❌ Errore durante la sincronizzazione: `{e}`", parse_mode="Markdown")


async def _execute_broadcast(bot, broadcast_text: str) -> tuple[int, int]:
    """Send broadcast text to all subscribed users and return (sent, failed)."""
    user_ids = users_repo.get_subscribed_chat_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=f"📢 *Avviso FCE Bot:*\n\n{broadcast_text}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send broadcast message to all subscribed users (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    broadcast_text = " ".join(context.args) if context.args else ""
    if not broadcast_text:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il messaggio da inviare.\nEsempio: `/broadcast Avviso sciopero domani`",
            parse_mode="Markdown"
        )
        return

    sent, failed = await _execute_broadcast(context.bot, broadcast_text)
    await update.effective_message.reply_text(
        f"📤 Broadcast inviato:\n• Consegnati: `{sent}`\n• Falliti: `{failed}`",
        parse_mode="Markdown"
    )


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect and manage a user by @username or chat_id (/user <@username|chat_id>)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci lo `@username` o il `chat_id` dell'utente da cercare.\n"
            "Esempio: `/user @mario_rossi` oppure `/user 123456789`",
            parse_mode="Markdown"
        )
        return

    identifier = " ".join(context.args).strip()
    user = users_repo.get_user(identifier)
    if not user:
        matches = users_repo.search_users(identifier, limit=10)
        if len(matches) == 1:
            user = matches[0]
        elif len(matches) > 1:
            await update.effective_message.reply_text(
                f"🔎 Trovati `{len(matches)}` utenti per `{identifier}`. Seleziona un utente:",
                reply_markup=admin_users_list_keyboard(matches, filter_mode="all", page=0),
                parse_mode="Markdown"
            )
            return
        else:
            await update.effective_message.reply_text(
                f"❌ Nessun utente trovato per `{identifier}`.",
                parse_mode="Markdown"
            )
            return

    await update.effective_message.reply_text(
        _format_user_card(user),
        reply_markup=admin_user_detail_keyboard(user),
        parse_mode="Markdown"
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List or search registered users (/users [query])."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if query:
        users = users_repo.search_users(query, limit=100)
        header = f"🔎 *Risultati ricerca utenti per '{query}':* `{len(users)}`"
    else:
        users = users_repo.get_all_users(filter_mode="all", limit=100)
        header = f"👥 *Elenco Utenti Registrati:* `{len(users)}`"

    await update.effective_message.reply_text(
        header + "\nSeleziona un utente per gestire AI, notifiche o cancellare il record:",
        reply_markup=admin_users_list_keyboard(users, filter_mode="all", page=0),
        parse_mode="Markdown"
    )


async def allow_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Authorize a Telegram user (by @username or chat_id) to use the AI trip assistant (Admin only).
    
    Usage:
      /allow_ai <@username|chat_id> [giorni|perm]
      Examples:
      /allow_ai 123456789        -> Abilita per 30 giorni
      /allow_ai @mario 15        -> Abilita per 15 giorni
      /allow_ai @mario perm      -> Accesso permanente illimitato
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente da abilitare all'AI.\n"
            "Esempi:\n"
            "• `/allow_ai 123456789` (default 30 giorni)\n"
            "• `/allow_ai @username 60` (specifica 60 giorni)\n"
            "• `/allow_ai @username perm` (accesso permanente)",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database. Assicurati che abbia avviato il bot o usa il suo `chat_id` numerico.",
            parse_mode="Markdown"
        )
        return

    days_arg = context.args[1].strip().lower() if len(context.args) > 1 else None
    if days_arg in ("perm", "inf", "unlimited", "vita"):
        users_repo.set_ai_permanent(target_chat_id)
        period_desc = "in modo permanente (illimitato)"
    elif days_arg and days_arg.isdigit():
        d = int(days_arg)
        ok, exp = users_repo.extend_ai_subscription(target_chat_id, days=d)
        period_desc = f"per {d} giorni (fino al `{exp}` UTC)"
    else:
        sub_d = settings_repo.get_sub_days()
        ok, exp = users_repo.extend_ai_subscription(target_chat_id, days=sub_d)
        period_desc = f"per {sub_d} giorni (fino al `{exp}` UTC)"

    notified = False
    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "🎉 *Accesso AI Abilitato!*\n\n"
                f"L'amministratore ha autorizzato il tuo account all'uso dell'Assistente AI FCE {period_desc}.\n"
                "Da adesso puoi scrivergli liberamente in chat per chiedere orari, coincidenze e percorsi!"
            ),
            parse_mode="Markdown"
        )
        notified = True
    except Exception as exc:
        logger.debug("Could not notify user %s of AI activation: %s", target_chat_id, exc)

    msg = f"✅ Accesso AI *abilitato* per la chat ID: `{target_chat_id}` {period_desc}"
    if identifier.lstrip("@") != str(target_chat_id):
        msg += f" (`{identifier}`)"
    if notified:
        msg += "\n🔔 L'utente è stato notificato con successo in chat."
    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def disallow_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke AI trip assistant access for a Telegram user by @username or chat_id (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente a cui revocare l'accesso AI.\n"
            "Esempio: `/disallow_ai 123456789` oppure `/disallow_ai @username`",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database.",
            parse_mode="Markdown"
        )
        return

    users_repo.set_ai_allowed(target_chat_id, False)

    await update.effective_message.reply_text(
        f"🚫 Accesso AI *revocato* per la chat ID: `{target_chat_id}`",
        parse_mode="Markdown"
    )


async def sub_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable notification subscription for a user by @username or chat_id (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente a cui attivare le notifiche.\n"
            "Esempio: `/sub_user @username` oppure `/sub_user 123456789`",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database.",
            parse_mode="Markdown"
        )
        return

    users_repo.set_subscription(target_chat_id, True)
    await update.effective_message.reply_text(
        f"🔔 Iscrizione notifiche *attivata* per la chat ID: `{target_chat_id}`",
        parse_mode="Markdown"
    )


async def unsub_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable notification subscription for a user by @username or chat_id (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente a cui disattivare le notifiche.\n"
            "Esempio: `/unsub_user @username` oppure `/unsub_user 123456789`",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database.",
            parse_mode="Markdown"
        )
        return

    users_repo.set_subscription(target_chat_id, False)
    await update.effective_message.reply_text(
        f"🔕 Iscrizione notifiche *disattivata* per la chat ID: `{target_chat_id}`",
        parse_mode="Markdown"
    )


async def delete_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a user record by @username or chat_id (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente da eliminare dal database.\n"
            "Esempio: `/delete_user @username` oppure `/delete_user 123456789`",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database.",
            parse_mode="Markdown"
        )
        return

    deleted = users_repo.delete_user(target_chat_id)
    if deleted:
        await update.effective_message.reply_text(
            f"🗑 Record utente `{target_chat_id}` *eliminato definitivamente* dal database.",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text(
            f"⚠️ Nessun record presente nel database per `{target_chat_id}`.",
            parse_mode="Markdown"
        )


async def list_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all chat IDs authorized to use the AI assistant (Admin only)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    ai_users = users_repo.get_all_users(filter_mode="ai", limit=200)
    admin_ids = [str(aid) for aid in settings.admin_user_ids]

    lines = ["🤖 *Utenti con Accesso AI Abilitato:*", ""]
    if admin_ids:
        lines.append("👑 *Amministratori (abilitati automaticamente):*")
        for aid in admin_ids:
            lines.append(f"• `{aid}`")
        lines.append("")

    lines.append(f"👥 *Utenti whitelisted:* `{len(ai_users)}`")
    if ai_users:
        for u in ai_users:
            cid = u["chat_id"]
            uname = f" (`@{u['username']}`)" if u.get("username") else ""
            fname = f" - `{u['first_name']}`" if u.get("first_name") else ""
            lines.append(f"• `{cid}`{uname}{fname}")
    else:
        lines.append("_(Nessun utente esterno attualmente abilitato)_")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _format_ai_settings_text(admin_id: int | str | None = None, bot=None) -> str:
    """Generate summary of AI pricing, duration, trial, and payment settings for admin."""
    cfg = settings_repo.get_all_ai_settings()
    trial_str = "🟢 Attiva" if cfg["trial_enabled"] else "🔴 Disattivata"
    token_status = "🟢 Configurato" if cfg["has_provider_token"] else "⭐ Telegram Stars (Zero configurazione)"
    price_label = f"`{cfg['sub_stars']} ⭐` (Stars)" if cfg["currency"] == "XTR" else f"`{cfg['sub_price_eur']:.2f} €`"

    stars_balance_str = ""
    if bot and hasattr(bot, "get_my_star_balance"):
        try:
            bal_obj = await bot.get_my_star_balance()
            amt = getattr(bal_obj, "amount", None)
            if isinstance(amt, (int, float)):
                stars_balance_str = f"⭐ *Saldo Guadagni Bot:* `{amt} ⭐`\n"
        except Exception as e:
            logger.debug("Could not fetch bot star balance: %s", e)

    return (
        "⚙️ *Configurazione Abbonamenti AI & Pagamenti*\n\n"
        f"💵 *Prezzo Abbonamento:* {price_label} (mese da {cfg['sub_days']} giorni)\n"
        f"📅 *Durata Periodo:* `{cfg['sub_days']}` giorni effettivi\n"
        f"🎁 *Prova Gratuita:* {trial_str} (`{cfg['trial_days']}` giorni, 1 sola volta per utente)\n"
        f"💱 *Valuta Attiva:* `{cfg['currency']}`\n"
        f"🔑 *Provider Telegram:* {token_status} (`{cfg['provider_type']}`)\n"
        f"{stars_balance_str}\n"
        "💡 *Regole del servizio:*\n"
        "• Il mese è calcolato in giorni solari continui dal momento del pagamento.\n"
        "• Alla scadenza, gli account vengono disattivati automaticamente.\n"
        "• Nessun dato sensibile di pagamento viene conservato nel bot (GDPR Minimization).\n\n"
        "Tocca i pulsanti sottostanti per personalizzare i parametri:"
    )


# ==========================================
# Interactive Admin Panel Callbacks & Inputs
# ==========================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button callbacks from the Admin Panel (pattern '^adm_')."""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id if user else (update.effective_chat.id if update.effective_chat else 0)

    if not is_admin(user_id):
        await query.answer("⛔ Accesso riservato agli amministratori.", show_alert=True)
        return

    data = query.data or ""
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict):
        user_data.pop("admin_state", None)

    # 1. Main Admin Panel Menu
    if data == "adm_menu":
        await query.answer()
        stats = users_repo.get_stats()
        text = (
            "🛠 *Pannello di Controllo Amministratore*\n\n"
            f"👥 Utenti totali: `{stats['total_users']}`\n"
            f"🔔 Iscritti notifiche: `{stats['subscribed_users']}`\n"
            f"🤖 Abilitati AI: `{stats['ai_allowed_users']}`\n"
            f"⚡ Attivi 30gg: `{stats['active_last_30_days']}`\n\n"
            "Scegli un'azione da eseguire:"
        )
        await query.edit_message_text(
            text,
            reply_markup=admin_panel_keyboard(),
            parse_mode="Markdown"
        )
        return

    # 1b. AI Configuration & Pricing Dashboard
    if data == "adm_ai_cfg" or data.startswith("adm_ai_cfg:"):
        if data == "adm_ai_cfg":
            await query.answer()
        elif data == "adm_ai_cfg:toggle_trial":
            cur_en = settings_repo.is_trial_enabled()
            settings_repo.set_trial_enabled(not cur_en)
            await query.answer("🎁 Prova: " + ("Disattivata" if cur_en else "Attivata!"))
        elif data.startswith("adm_ai_cfg:trial_days:"):
            d = int(data.split(":")[2])
            settings_repo.set_trial_days(d)
            await query.answer(f"⏱️ Prova impostata a {d} giorni")
        elif data.startswith("adm_ai_cfg:sub_days:"):
            d = int(data.split(":")[2])
            settings_repo.set_sub_days(d)
            await query.answer(f"📅 Mese impostato a {d} giorni")
        elif data.startswith("adm_ai_cfg:price:"):
            p = float(data.split(":")[2])
            settings_repo.set_sub_price_eur(p)
            await query.answer(f"💵 Prezzo impostato a {p:.2f} €")
        elif data.startswith("adm_ai_cfg:stars:"):
            s = int(data.split(":")[2])
            settings_repo.set_sub_stars(s)
            await query.answer(f"⭐ Prezzo impostato a {s} Stars")
        elif data == "adm_ai_cfg:toggle_currency":
            c = settings_repo.get_currency()
            new_c = "XTR" if c == "EUR" else "EUR"
            settings_repo.set_currency(new_c)
            await query.answer(f"💱 Valuta impostata a {new_c}")
        elif data == "adm_ai_cfg:toggle_admin_test":
            cur_test = settings_repo.get_admin_ai_test_mode(user_id)
            new_test = not cur_test
            settings_repo.set_admin_ai_test_mode(user_id, new_test)
            if new_test:
                users_repo.set_ai_allowed(user_id, False)
                users_repo.reset_trial(user_id)
                await query.answer(
                    "🧪 Modalità test attivata!\nAI disabilitata per il tuo account admin.\nOra puoi testare prova e acquisto a 1 Stella.",
                    show_alert=True
                )
            else:
                users_repo.set_ai_permanent(user_id)
                await query.answer(
                    "👑 Modalità test disattivata!\nAccesso VIP illimitato ripristinato.",
                    show_alert=True
                )
        elif data == "adm_ai_cfg:clean_expired":
            cnt = users_repo.deactivate_expired_users()
            await query.answer(f"🧹 Disattivati {cnt} utenti con abbonamento/prova scaduti!", show_alert=True)

        is_test_mode = settings_repo.get_admin_ai_test_mode(user_id)
        msg_text = await _format_ai_settings_text(admin_id=user_id, bot=context.bot if context else None)
        try:
            await query.edit_message_text(
                msg_text,
                reply_markup=admin_ai_settings_keyboard(
                    settings_repo.get_all_ai_settings(),
                    admin_test_mode=is_test_mode
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("AI settings edit notice: %s", e)
        return

    # 2. System Statistics
    if data == "adm_stats":
        await query.answer()
        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Aggiorna", callback_data="adm_stats")],
            [InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")],
        ])
        try:
            await query.edit_message_text(
                _format_stats_text(),
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("Admin stats edit notice: %s", e)
        return

    # 3. Paginated Users List
    if data.startswith("adm_users:"):
        await query.answer()
        parts = data.split(":")
        filter_mode = parts[1] if len(parts) > 1 else "all"
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

        users = users_repo.get_all_users(filter_mode=filter_mode, limit=200)
        mode_labels = {
            "all": "Tutti gli utenti",
            "sub": "Utenti iscritti alle notifiche",
            "ai": "Utenti con accesso AI",
        }
        title = mode_labels.get(filter_mode, "Utenti")
        text = (
            f"👥 *Gestione Utenti — {title}* (`{len(users)}`)\n\n"
            "Seleziona un utente dalla lista per visualizzare i dettagli, abilitare/disabilitare AI e notifiche, o cancellare il record:"
        )
        await query.edit_message_text(
            text,
            reply_markup=admin_users_list_keyboard(users, filter_mode=filter_mode, page=page),
            parse_mode="Markdown"
        )
        return

    # 4. View Single User Details
    if data.startswith("adm_u_view:"):
        await query.answer()
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        user_rec = users_repo.get_user(target_cid)
        if not user_rec:
            await query.edit_message_text(
                f"❌ L'utente `{target_cid}` non è più presente nel database.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Torna alla Lista Utenti", callback_data=f"adm_users:{back_filter}:{back_page}")]
                ]),
                parse_mode="Markdown"
            )
            return

        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 5. Toggle User AI Permission
    if data.startswith("adm_u_ai:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        new_val = parts[2] == "1" if len(parts) > 2 else False
        back_filter = parts[3] if len(parts) > 3 else "all"
        back_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

        users_repo.set_ai_allowed(target_cid, new_val)
        if new_val:
            try:
                await context.bot.send_message(
                    chat_id=target_cid,
                    text=(
                        "🎉 *Accesso AI Abilitato!*\n\n"
                        "L'amministratore ha autorizzato il tuo account all'uso dell'Assistente AI FCE.\n"
                        "Da adesso puoi scrivergli liberamente in chat per chiedere orari, coincidenze e percorsi!"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as exc:
                logger.debug("Could not notify user %s of AI activation: %s", target_cid, exc)
            await query.answer("✅ Accesso AI abilitato!")
        else:
            await query.answer("🚫 Accesso AI revocato.")

        user_rec = users_repo.get_user(target_cid) or {
            "chat_id": target_cid, "username": None, "first_name": None,
            "last_active": None, "is_subscribed": 0, "ai_allowed": 1 if new_val else 0
        }
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 5b. Add 30 Days AI Access
    if data.startswith("adm_u_add30:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        sub_d = settings_repo.get_sub_days()
        ok, exp = users_repo.extend_ai_subscription(target_cid, days=sub_d)
        await query.answer(f"➕ Aggiunti {sub_d} giorni all'utente!")
        try:
            await context.bot.send_message(
                chat_id=target_cid,
                text=(
                    f"🎉 *Abbonamento AI Esteso!*\n\n"
                    f"L'amministratore ha esteso il tuo accesso all'Assistente AI di {sub_d} giorni.\n"
                    f"📅 *Scadenza:* `{exp}` UTC"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        user_rec = users_repo.get_user(target_cid)
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 5c. Assign Free Trial to User
    if data.startswith("adm_u_trial:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        t_days = settings_repo.get_trial_days()
        users_repo.reset_trial(target_cid)  # Allow admin to grant trial even if used
        ok, exp = users_repo.activate_trial(target_cid, days=t_days)
        await query.answer("🎁 Prova assegnata con successo!")
        try:
            await context.bot.send_message(
                chat_id=target_cid,
                text=(
                    f"🎁 *Prova Gratuita AI Assegnata!*\n\n"
                    f"L'amministratore ti ha assegnato {t_days} giorni di prova dell'Assistente AI.\n"
                    f"📅 *Scadenza:* `{exp}` UTC"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        user_rec = users_repo.get_user(target_cid)
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 5d. Grant Permanent AI Access
    if data.startswith("adm_u_perm:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        users_repo.set_ai_permanent(target_cid)
        await query.answer("♾️ Accesso permanente illimitato conferito!")
        try:
            await context.bot.send_message(
                chat_id=target_cid,
                text=(
                    "👑 *Accesso AI Permanente!*\n\n"
                    "L'amministratore ti ha concesso accesso illimitato permanente all'Assistente AI."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        user_rec = users_repo.get_user(target_cid)
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 5e. Reset Trial Flag
    if data.startswith("adm_u_rtrial:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        users_repo.reset_trial(target_cid)
        await query.answer("🔄 Prova resettata (l'utente può attivarla di nuovo)!")
        user_rec = users_repo.get_user(target_cid)
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 6. Toggle User Notification Subscription
    if data.startswith("adm_u_sub:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        new_val = parts[2] == "1" if len(parts) > 2 else False
        back_filter = parts[3] if len(parts) > 3 else "all"
        back_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

        users_repo.set_subscription(target_cid, new_val)
        await query.answer("🔔 Notifiche attivate!" if new_val else "🔕 Notifiche disattivate.")

        user_rec = users_repo.get_user(target_cid) or {
            "chat_id": target_cid, "username": None, "first_name": None,
            "last_active": None, "is_subscribed": 1 if new_val else 0, "ai_allowed": 0
        }
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 7. Ask Confirmation to Delete User Record
    if data.startswith("adm_u_del_ask:"):
        await query.answer()
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        user_rec = users_repo.get_user(target_cid)
        uname_info = f" (`@{user_rec['username']}`)" if user_rec and user_rec.get("username") else ""
        await query.edit_message_text(
            f"⚠️ *Conferma Eliminazione Utente*\n\n"
            f"Sei sicuro di voler cancellare definitivamente il record dell'utente `{target_cid}`{uname_info} dal database?\n"
            "Verranno rimossi iscrizione notifiche, permessi AI e cronologia di accesso.",
            reply_markup=admin_user_delete_confirm_keyboard(target_cid, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 8. Execute User Record Deletion
    if data.startswith("adm_u_del_ok:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        users_repo.delete_user(target_cid)
        await query.answer("🗑 Utente eliminato definitivamente.")

        users = users_repo.get_all_users(filter_mode=back_filter, limit=200)
        await query.edit_message_text(
            f"✅ Utente `{target_cid}` eliminato con successo.\n\n"
            f"👥 *Utenti rimanenti nella vista:* `{len(users)}`",
            reply_markup=admin_users_list_keyboard(users, filter_mode=back_filter, page=back_page),
            parse_mode="Markdown"
        )
        return

    # 9. Interactive User Search Prompt
    if data == "adm_search_prompt":
        await query.answer()
        if isinstance(user_data, dict):
            user_data["admin_state"] = "awaiting_user_search"
        await query.edit_message_text(
            "🔎 *Cerca Utente per @username, Nome o Chat ID*\n\n"
            "Scrivi ora in chat lo `@username` (es. `@mario`), il nome o il `chat_id` dell'utente che vuoi gestire:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annulla", callback_data="adm_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    # 10. Interactive Broadcast Prompt
    if data == "adm_broadcast_prompt":
        await query.answer()
        if isinstance(user_data, dict):
            user_data["admin_state"] = "awaiting_broadcast"
        sub_count = len(users_repo.get_subscribed_chat_ids())
        await query.edit_message_text(
            "📢 *Invio Messaggio Broadcast*\n\n"
            f"Il messaggio verrà inviato a tutti gli utenti iscritti alle notifiche (`{sub_count}` utenti attivi).\n\n"
            "✍️ *Scrivi ora in chat il testo del messaggio da trasmettere:*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annulla", callback_data="adm_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    # 11. Trigger GTFS Sync from Button
    if data == "adm_sync_gtfs":
        await query.answer("⏳ Avvio sincronizzazione GTFS...")
        await query.edit_message_text("⏳ *Download e sincronizzazione GTFS in corso...*\nAttendere qualche secondo.", parse_mode="Markdown")
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]])
        try:
            stops, trips, st = download_and_sync_gtfs()
            await query.edit_message_text(
                f"✅ *Sincronizzazione GTFS completata!*\n\n"
                f"• Fermate: `{stops}`\n"
                f"• Corse: `{trips}`\n"
                f"• Passaggi orari: `{st}`",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error("GTFS sync failed from admin panel: %s", e)
            await query.edit_message_text(
                f"❌ *Errore durante la sincronizzazione:*\n`{e}`",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        return

    # 12. Trigger RSS News Check & Broadcast from Button
    if data == "adm_check_news":
        await query.answer("📰 Controllo feed RSS FCE...")
        await query.edit_message_text("⏳ *Controllo nuove notizie Circumetnea in corso...*", parse_mode="Markdown")
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]])
        try:
            sent_count = await check_and_broadcast_news(context.bot)
            await query.edit_message_text(
                f"📰 *Controllo Notizie Completato*\n\n"
                f"• Nuove notifiche inviate: `{sent_count}`",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error("Admin news check failed: %s", e)
            await query.edit_message_text(
                f"❌ *Errore durante il controllo notizie:*\n`{e}`",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        return

    # 13. Prune Inactive Users (60+ days)
    if data == "adm_prune":
        removed = users_repo.prune_inactive(days=60)
        await query.answer(f"🧹 Rimossi {removed} utenti inattivi.")
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]])
        await query.edit_message_text(
            f"🧹 *Pulizia Utenti Inattivi (GDPR 60gg)*\n\n"
            f"• Record eliminati: `{removed}`",
            reply_markup=back_kb,
            parse_mode="Markdown"
        )
        return


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Process freeform text input when an admin is in an interactive admin state.
    
    Returns True if the message was consumed by the admin flow, False otherwise.
    """
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return False

    state = user_data.get("admin_state")
    if not state:
        return False

    user_data.pop("admin_state", None)
    text = (update.effective_message.text or "").strip()
    if not text:
        return False

    if state == "awaiting_user_search":
        user = users_repo.get_user(text)
        if user:
            await update.effective_message.reply_text(
                _format_user_card(user),
                reply_markup=admin_user_detail_keyboard(user),
                parse_mode="Markdown"
            )
            return True

        matches = users_repo.search_users(text, limit=50)
        if len(matches) == 1:
            await update.effective_message.reply_text(
                _format_user_card(matches[0]),
                reply_markup=admin_user_detail_keyboard(matches[0]),
                parse_mode="Markdown"
            )
        elif len(matches) > 1:
            await update.effective_message.reply_text(
                f"🔎 Trovati `{len(matches)}` utenti per `{text}`:",
                reply_markup=admin_users_list_keyboard(matches, filter_mode="all", page=0),
                parse_mode="Markdown"
            )
        else:
            await update.effective_message.reply_text(
                f"❌ Nessun utente trovato per `{text}`.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Nuova Ricerca", callback_data="adm_search_prompt")],
                    [InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")],
                ]),
                parse_mode="Markdown"
            )
        return True

    if state == "awaiting_broadcast":
        sent, failed = await _execute_broadcast(context.bot, text)
        await update.effective_message.reply_text(
            f"📤 *Broadcast Inviato con Successo!*\n\n"
            f"• Consegnati: `{sent}`\n"
            f"• Falliti: `{failed}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]
            ]),
            parse_mode="Markdown"
        )
        return True

    return False
