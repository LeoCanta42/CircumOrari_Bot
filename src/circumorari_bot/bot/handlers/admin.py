"""Admin maintenance, user management, and broadcast handlers."""

import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from circumorari_bot.config import settings
from circumorari_bot.data.users_repo import UsersRepository
from circumorari_bot.data.settings_repo import SettingsRepository
from circumorari_bot.data.gtfs_loader import download_and_sync_gtfs
from circumorari_bot.data.db import get_db, get_persistent_db, get_persistence_status
from circumorari_bot.bot.keyb import (
    admin_panel_keyboard,
    admin_maintenance_keyboard,
    admin_users_list_keyboard,
    admin_user_detail_keyboard,
    admin_user_delete_confirm_keyboard,
    admin_ai_settings_keyboard,
    main_menu_keyboard,
)
from circumorari_bot.bot.handlers.news import check_and_broadcast_news

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
    banned_cnt = stats.get("banned_users", 0)

    return (
        "📊 *Statistiche Sistema CircumOrari*\n\n"
        f"💾 *Storage Utenti:* {persistence['label']}\n\n"
        f"👥 *Utenti Totali:* `{stats['total_users']}`\n"
        f"🔔 *Iscritti attivi:* `{stats['subscribed_users']}`\n"
        f"🤖 *Utenti con accesso AI:* `{stats['ai_allowed_users']}` (Attivi: `{ai_active}` | Scaduti: `{ai_expired}`)\n"
        f"🎁 *Prove gratuite utilizzate:* `{ai_trials}`\n"
        f"🚫 *Utenti bannati (Blacklist):* `{banned_cnt}`\n"
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

    is_banned = bool(user.get("is_banned")) and not is_cfg_admin
    ban_reason = user.get("ban_reason")
    banned_at = user.get("banned_at")
    if is_banned:
        since_str = f" dal `{banned_at}` UTC" if banned_at else ""
        reason_str = _escape_md(ban_reason) if ban_reason else "_(Nessun motivo specificato)_"
        ban_section = (
            f"🛡 *Stato Accesso Bot:* 🚫 *BANNATO (In Blacklist)*{since_str}\n"
            f"📝 *Motivo Ban:* {reason_str}\n"
        )
    else:
        ban_section = "🛡 *Stato Accesso Bot:* 🟢 Attivo\n"

    return (
        "👤 *Scheda Utente CircumOrari*\n\n"
        f"🆔 *Chat ID:* `{cid}`\n"
        f"🏷 *Username:* {uname_display}\n"
        f"📛 *Nome:* {fname_display}\n"
        f"🕒 *Ultimo accesso:* `{last_act}`\n\n"
        f"{ban_section}"
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
        context.user_data.pop("ban_target", None)

    stats = users_repo.get_stats()
    persistence = get_persistence_status()
    maint_active = settings_repo.is_maintenance_mode()
    maint_banner = "\n⚠️ *MODALITÀ MANUTENZIONE ATTIVA*\n" if maint_active else ""
    text = (
        "🛠 *Pannello di Controllo Amministratore*\n"
        f"{maint_banner}\n"
        f"💾 Storage: {persistence['label']}\n"
        f"👥 Utenti registrati: `{stats['total_users']}` | "
        f"🔔 Iscritti: `{stats['subscribed_users']}` | "
        f"🤖 AI: `{stats['ai_allowed_users']}` | "
        f"🚫 Bannati: `{stats.get('banned_users', 0)}`\n\n"
        "Seleziona un'operazione dal menu sottostante oppure usa i comandi rapidi:\n"
        "• `/user <@username o chat_id>` — Gestisci singolo utente\n"
        "• `/users [filtro]` — Lista e cerca utenti\n"
        "• `/allow_ai` / `/disallow_ai <@user|id>` — Permessi AI\n"
        "• `/sub_user` / `/unsub_user <@user|id>` — Notifiche utente\n"
        "• `/ban <@user|id> [motivo]` — Banna utente (blacklist)\n"
        "• `/unban <@user|id>` — Rimuovi utente dalla blacklist\n"
        "• `/delete_user <@user|id>` — Cancella record utente\n"
        "• `/manutenzione [on|off]` — Gestisci modalità manutenzione\n"
        "• `/broadcast <messaggio>` — Invia avviso agli iscritti"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=admin_panel_keyboard(maintenance_mode=maint_active),
        parse_mode="Markdown"
    )


def _format_maintenance_text() -> str:
    """Format current maintenance mode status and waitlist summary for admin."""
    is_active = settings_repo.is_maintenance_mode()
    waiting_count = settings_repo.count_maintenance_waitlist_users()
    status_line = "🔴 *ATTIVA* (gli utenti ricevono avviso di manutenzione)" if is_active else "🟢 *DISATTIVA* (bot pienamente operativo)"
    return (
        "🛠 *Gestione Modalità Manutenzione*\n\n"
        f"• *Stato attuale:* {status_line}\n"
        f"• *Utenti in lista d'attesa:* `{waiting_count}`\n\n"
        "_Gli amministratori bypassano sempre la modalità manutenzione per poter gestire e testare il bot. "
        "Gli utenti non bannati che scrivono al bot durante la manutenzione vengono salvati automaticamente "
        "in lista d'attesa per ricevere l'avviso di ritorno online._"
    )


async def _notify_maintenance_waitlist(bot) -> tuple[int, int]:
    """Send back-online notice to waiting non-banned users and clear the waitlist."""
    chat_ids = settings_repo.get_maintenance_waitlist_users()
    sent = 0
    failed = 0
    msg_text = (
        "✅ *CircumOrari Bot è di nuovo online!*\n\n"
        "La manutenzione è terminata e il bot è tornato pienamente operativo.\n"
        "Puoi riprendere a consultare orari, fermate e servizi normalmente!"
    )
    for cid in chat_ids:
        try:
            await bot.send_message(
                chat_id=cid,
                text=msg_text,
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(is_admin_user=False),
            )
            sent += 1
        except Exception as exc:
            logger.debug("Failed back-online notification to %s: %s", cid, exc)
            failed += 1
    settings_repo.clear_maintenance_waitlist()
    return sent, failed


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command /manutenzione [on|off] to inspect or toggle maintenance mode."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    arg = context.args[0].strip().lower() if getattr(context, "args", None) else ""
    if arg in ("on", "attiva", "1", "true"):
        settings_repo.set_maintenance_mode(True)
    elif arg in ("off", "disattiva", "0", "false"):
        settings_repo.set_maintenance_mode(False)

    is_active = settings_repo.is_maintenance_mode()
    waiting_count = settings_repo.count_maintenance_waitlist_users()
    await update.effective_message.reply_text(
        _format_maintenance_text(),
        reply_markup=admin_maintenance_keyboard(is_active=is_active, waiting_count=waiting_count),
        parse_mode="Markdown",
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
        logger.error("GTFS sync failed: %s", e, exc_info=True)
        await status_msg.edit_text("❌ Errore durante la sincronizzazione GTFS. Controlla i log del server.")


async def reset_commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to re-flush and register official slash commands with Telegram."""
    user = update.effective_user
    uid = getattr(user, "id", None) or (update.effective_chat.id if update.effective_chat else 0)
    if not is_admin(uid):
        msg = update.effective_message
        if msg:
            await msg.reply_text("⛔ Comando riservato agli amministratori.")
        return

    msg = update.effective_message
    from circumorari_bot.bot.bot_app import sync_bot_commands
    try:
        count = await sync_bot_commands(context.bot)
        if msg:
            await msg.reply_text(
                f"✅ Comandi del bot aggiornati con successo su Telegram ({count} comandi registrati per il menu comandi)."
            )
    except Exception as exc:
        logger.error("Error syncing bot commands via command: %s", exc, exc_info=True)
        if msg:
            await msg.reply_text("❌ Errore durante l'aggiornamento dei comandi. Controlla i log del server.")


async def _execute_broadcast(bot, broadcast_text: str) -> tuple[int, int]:
    """Send broadcast text to all subscribed users and return (sent, failed)."""
    user_ids = users_repo.get_subscribed_chat_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=f"📢 *Avviso CircumOrari:*\n\n{broadcast_text}",
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
                f"L'amministratore ha autorizzato il tuo account all'uso dell'Assistente AI CircumOrari {period_desc}.\n"
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


async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user and add them to the blacklist with optional reason (/ban <@username|chat_id> [motivo])."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente da bannare (con eventuale motivo).\n"
            "Esempi:\n"
            "• `/ban 123456789 Spam ripetuto`\n"
            "• `/ban @username Abuso del servizio`",
            parse_mode="Markdown"
        )
        return

    identifier = context.args[0].strip()
    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else None
    target_chat_id = users_repo.resolve_chat_id(identifier)
    if not target_chat_id:
        await update.effective_message.reply_text(
            f"❌ Utente `{identifier}` non trovato nel database. Usa il `chat_id` numerico o verifica lo `@username`.",
            parse_mode="Markdown"
        )
        return

    try:
        if int(target_chat_id) in settings.admin_user_ids:
            await update.effective_message.reply_text("⛔ Non puoi bannare un amministratore di sistema.")
            return
    except (ValueError, TypeError):
        pass

    ok = users_repo.ban_user(target_chat_id, reason=reason)
    if not ok:
        await update.effective_message.reply_text(
            f"❌ Impossibile bannare l'utente `{target_chat_id}`.",
            parse_mode="Markdown"
        )
        return

    reason_line = (
        f"\n📝 *Motivo:* {_escape_md(reason)}"
        if reason
        else "\n📝 *Motivo:* _(Nessun motivo specificato)_"
    )
    await update.effective_message.reply_text(
        f"🚫 Utente `{target_chat_id}` *bannato e aggiunto alla blacklist*.{reason_line}",
        parse_mode="Markdown"
    )


async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a user from the blacklist (/unban <@username|chat_id>)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ Inserisci il `chat_id` o `@username` dell'utente da rimuovere dalla blacklist.\n"
            "Esempio: `/unban 123456789` oppure `/unban @username`",
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

    users_repo.unban_user(target_chat_id)
    await update.effective_message.reply_text(
        f"✅ Utente `{target_chat_id}` *sbannato e rimosso dalla blacklist*.",
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
    price_label = f"`{cfg['sub_stars']} ⭐` (Telegram Stars)"

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
        f"⚡ *Fair Use AI:* `{cfg['ai_rate_limit_requests']}` richieste ogni `{cfg['ai_rate_limit_window_minutes']}` minuti\n"
        "💱 *Valuta:* `⭐ Telegram Stars (XTR)`\n"
        "🔑 *Provider:* `⭐ Telegram Stars (Nativo - Zero Config)`\n"
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
        user_data.pop("ban_target", None)

    # 1. Main Admin Panel Menu
    if data == "adm_menu":
        await query.answer()
        stats = users_repo.get_stats()
        maint_active = settings_repo.is_maintenance_mode()
        maint_banner = "\n⚠️ *MODALITÀ MANUTENZIONE ATTIVA*\n" if maint_active else ""
        text = (
            "🛠 *Pannello di Controllo Amministratore*\n"
            f"{maint_banner}\n"
            f"👥 Utenti totali: `{stats['total_users']}`\n"
            f"🔔 Iscritti notifiche: `{stats['subscribed_users']}`\n"
            f"🤖 Abilitati AI: `{stats['ai_allowed_users']}`\n"
            f"🚫 Bannati: `{stats.get('banned_users', 0)}`\n"
            f"⚡ Attivi 30gg: `{stats['active_last_30_days']}`\n\n"
            "Scegli un'azione da eseguire:"
        )
        await query.edit_message_text(
            text,
            reply_markup=admin_panel_keyboard(maintenance_mode=maint_active),
            parse_mode="Markdown"
        )
        return

    # 1a. Maintenance Mode Panel & Waitlist Notification
    if data == "adm_maint" or data.startswith("adm_maint:"):
        summary_prefix = ""
        if data == "adm_maint":
            await query.answer()
        elif data == "adm_maint:on":
            settings_repo.set_maintenance_mode(True)
            await query.answer("🔴 Modalità manutenzione attivata!")
        elif data == "adm_maint:off":
            settings_repo.set_maintenance_mode(False)
            await query.answer("🟢 Bot ripristinato in modalità normale!")
        elif data == "adm_maint:off_notify":
            settings_repo.set_maintenance_mode(False)
            sent, failed = await _notify_maintenance_waitlist(context.bot)
            await query.answer(f"🟢 Bot online! Avvisati {sent} utenti.")
            summary_prefix = (
                f"📢 *Notifica Ritorno Online Inviata:* `{sent}` consegnati, `{failed}` falliti.\n\n"
            )
        elif data == "adm_maint:notify":
            sent, failed = await _notify_maintenance_waitlist(context.bot)
            await query.answer(f"📢 Avvisati {sent} utenti!")
            summary_prefix = (
                f"📢 *Notifica Ritorno Online Inviata:* `{sent}` consegnati, `{failed}` falliti.\n\n"
            )
        elif data == "adm_maint:clear":
            cleared = settings_repo.clear_maintenance_waitlist()
            await query.answer(f"🧹 Rimossi {cleared} utenti dalla lista attesa.")

        is_active = settings_repo.is_maintenance_mode()
        waiting_count = settings_repo.count_maintenance_waitlist_users()
        try:
            await query.edit_message_text(
                summary_prefix + _format_maintenance_text(),
                reply_markup=admin_maintenance_keyboard(is_active=is_active, waiting_count=waiting_count),
                parse_mode="Markdown",
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("Maintenance view edit notice: %s", e)
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
            await query.answer("⭐ Al momento sono supportate esclusivamente le Telegram Stars.", show_alert=True)
        elif data.startswith("adm_ai_cfg:stars:"):
            s = int(data.split(":")[2])
            settings_repo.set_sub_stars(s)
            await query.answer(f"⭐ Prezzo impostato a {s} Stars")
        elif data.startswith("adm_ai_cfg:rl_req:"):
            try:
                n = int(data.split(":")[2])
                if n < 1:
                    raise ValueError
            except (IndexError, ValueError):
                await query.answer("❌ Valore non valido.", show_alert=True)
                return
            settings_repo.set_ai_rate_limit_requests(n)
            await query.answer(f"⚡ Fair Use impostato a {n} {'richiesta' if n == 1 else 'richieste'}")
        elif data.startswith("adm_ai_cfg:rl_min:"):
            try:
                m = int(data.split(":")[2])
                if m < 1:
                    raise ValueError
            except (IndexError, ValueError):
                await query.answer("❌ Valore non valido.", show_alert=True)
                return
            settings_repo.set_ai_rate_limit_window_minutes(m)
            await query.answer(f"⏳ Finestra Fair Use impostata a {m} {'minuto' if m == 1 else 'minuti'}")
        elif data == "adm_ai_cfg:rl_custom":
            await query.answer()
            if isinstance(user_data, dict):
                user_data["admin_state"] = "awaiting_fair_use_config"
            cur_req = settings_repo.get_ai_rate_limit_requests()
            cur_min = settings_repo.get_ai_rate_limit_window_minutes()
            await query.edit_message_text(
                "✏️ *Personalizza Fair Use Assistente AI*\n\n"
                f"Configurazione attuale: `{cur_req}` richieste ogni `{cur_min}` minuti.\n\n"
                "Invia ora in chat due numeri interi nel formato:\n"
                "`<richieste> <minuti>`\n\n"
                "Esempi validi:\n"
                "• `3 10` → 3 richieste ogni 10 minuti\n"
                "• `5 15` → 5 richieste ogni 15 minuti\n\n"
                "_Intervalli consentiti: 1–1000 richieste, 1–1440 minuti._",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Annulla", callback_data="adm_ai_cfg")]
                ]),
                parse_mode="Markdown"
            )
            return
        elif data == "adm_ai_cfg:toggle_currency":
            await query.answer("⭐ Al momento sono supportate esclusivamente le Telegram Stars.", show_alert=True)
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
            "banned": "Utenti bannati (Blacklist)",
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
                        "L'amministratore ha autorizzato il tuo account all'uso dell'Assistente AI CircumOrari.\n"
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

    # 6b. Ask Ban Reason / Confirm Ban
    if data.startswith("adm_u_ban_ask:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        try:
            if int(target_cid) in settings.admin_user_ids:
                await query.answer("⛔ Non puoi bannare un amministratore di sistema.", show_alert=True)
                return
        except (ValueError, TypeError):
            pass

        await query.answer()
        if isinstance(user_data, dict):
            user_data["admin_state"] = "awaiting_ban_reason"
            user_data["ban_target"] = {
                "chat_id": target_cid,
                "back_filter": back_filter,
                "back_page": back_page,
            }

        user_rec = users_repo.get_user(target_cid)
        uname_info = f" (`@{user_rec['username']}`)" if user_rec and user_rec.get("username") else ""
        existing_reason = user_rec.get("ban_reason") if user_rec else None
        existing_line = (
            f"\n📝 *Motivo attuale:* {_escape_md(existing_reason)}\n"
            if existing_reason
            else ""
        )
        await query.edit_message_text(
            f"🚫 *Ban Utente (Blacklist)*\n\n"
            f"Utente: `{target_cid}`{uname_info}{existing_line}\n"
            "✍️ *Scrivi ora in chat il motivo del ban* (verrà salvato nella scheda utente), "
            "oppure premi il pulsante sottostante per bannare senza motivo:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Banna senza motivo", callback_data=f"adm_u_ban_now:{target_cid}:{back_filter}:{back_page}"[:64])],
                [InlineKeyboardButton("❌ Annulla", callback_data=f"adm_u_view:{target_cid}:{back_filter}:{back_page}"[:64])],
            ]),
            parse_mode="Markdown"
        )
        return

    # 6c. Ban User Immediately Without Reason
    if data.startswith("adm_u_ban_now:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        try:
            if int(target_cid) in settings.admin_user_ids:
                await query.answer("⛔ Non puoi bannare un amministratore di sistema.", show_alert=True)
                return
        except (ValueError, TypeError):
            pass

        ok = users_repo.ban_user(target_cid, reason=None)
        if not ok:
            await query.answer("❌ Impossibile bannare l'utente specificato.", show_alert=True)
            return
        await query.answer("🚫 Utente bannato e aggiunto alla blacklist.")

        user_rec = users_repo.get_user(target_cid) or {
            "chat_id": target_cid, "username": None, "first_name": None,
            "last_active": None, "is_subscribed": 0, "ai_allowed": 0,
            "is_banned": 1, "ban_reason": None, "banned_at": None,
        }
        await query.edit_message_text(
            _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return

    # 6d. Unban User
    if data.startswith("adm_u_unban:"):
        parts = data.split(":")
        target_cid = parts[1] if len(parts) > 1 else ""
        back_filter = parts[2] if len(parts) > 2 else "all"
        back_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        users_repo.unban_user(target_cid)
        await query.answer("✅ Utente sbannato e rimosso dalla blacklist!")

        user_rec = users_repo.get_user(target_cid) or {
            "chat_id": target_cid, "username": None, "first_name": None,
            "last_active": None, "is_subscribed": 0, "ai_allowed": 0,
            "is_banned": 0, "ban_reason": None, "banned_at": None,
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
            logger.error("GTFS sync failed from admin panel: %s", e, exc_info=True)
            await query.edit_message_text(
                "❌ *Errore durante la sincronizzazione GTFS.*\nControlla i log del server.",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        return

    # 12. Trigger RSS News Check & Broadcast from Button
    if data == "adm_check_news":
        await query.answer("📰 Controllo feed RSS notizie...")
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
            logger.error("Admin news check failed: %s", e, exc_info=True)
            await query.edit_message_text(
                "❌ *Errore durante il controllo notizie.*\nControlla i log del server.",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        return

    # 12b. Trigger Bot Commands Sync from Button
    if data == "adm_reset_commands":
        await query.answer("⏳ Aggiornamento comandi del bot...")
        from circumorari_bot.bot.bot_app import sync_bot_commands
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]])
        try:
            count = await sync_bot_commands(context.bot)
            await query.edit_message_text(
                f"✅ *Comandi Bot Aggiornati*\n\n"
                f"Sono stati registrati con successo `{count}` comandi ufficiali nel menu Telegram.",
                reply_markup=back_kb,
                parse_mode="Markdown"
            )
        except Exception as exc:
            logger.error("Error syncing bot commands via admin panel: %s", exc, exc_info=True)
            await query.edit_message_text(
                "❌ *Errore durante l'aggiornamento dei comandi.*\nControlla i log del server.",
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
    if state != "awaiting_ban_reason":
        user_data.pop("ban_target", None)
    text = (update.effective_message.text or "").strip()
    if not text:
        return False

    if state == "awaiting_fair_use_config":
        parts = [p for p in re.split(r"[\s/:,]+", text) if p]
        valid = (
            len(parts) == 2
            and parts[0].isdigit()
            and parts[1].isdigit()
            and 1 <= int(parts[0]) <= 1000
            and 1 <= int(parts[1]) <= 1440
        )
        if not valid:
            user_data["admin_state"] = "awaiting_fair_use_config"
            await update.effective_message.reply_text(
                "❌ *Formato non valido.*\n\n"
                "Inserisci esattamente due numeri interi positivi `<richieste> <minuti>` separati da spazio (es. `3 10` oppure `5 15`).\n"
                "_Intervalli consentiti: 1–1000 richieste, 1–1440 minuti._",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Annulla", callback_data="adm_ai_cfg")]
                ]),
                parse_mode="Markdown"
            )
            return True

        req_val = int(parts[0])
        min_val = int(parts[1])
        settings_repo.set_ai_rate_limit_requests(req_val)
        settings_repo.set_ai_rate_limit_window_minutes(min_val)
        uid = update.effective_user.id if update.effective_user else 0
        is_test_mode = settings_repo.get_admin_ai_test_mode(uid)
        cfg_text = await _format_ai_settings_text(admin_id=uid, bot=getattr(context, "bot", None))
        await update.effective_message.reply_text(
            f"✅ *Fair Use aggiornato:* `{req_val}` richieste ogni `{min_val}` minuti.\n\n" + cfg_text,
            reply_markup=admin_ai_settings_keyboard(
                settings_repo.get_all_ai_settings(),
                admin_test_mode=is_test_mode,
            ),
            parse_mode="Markdown"
        )
        return True

    if state == "awaiting_ban_reason":
        ban_target = user_data.pop("ban_target", None) or {}
        target_cid = str(ban_target.get("chat_id", "")).strip()
        back_filter = str(ban_target.get("back_filter", "all"))
        back_page = int(ban_target.get("back_page", 0))

        if not target_cid:
            await update.effective_message.reply_text(
                "❌ Nessun utente selezionato per il ban.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Pannello Admin", callback_data="adm_menu")]
                ]),
            )
            return True

        try:
            if int(target_cid) in settings.admin_user_ids:
                await update.effective_message.reply_text("⛔ Non puoi bannare un amministratore di sistema.")
                return True
        except (ValueError, TypeError):
            pass

        ok = users_repo.ban_user(target_cid, reason=text)
        if not ok:
            await update.effective_message.reply_text(
                f"❌ Impossibile bannare l'utente `{target_cid}`.",
                parse_mode="Markdown"
            )
            return True

        user_rec = users_repo.get_user(target_cid) or {
            "chat_id": target_cid, "username": None, "first_name": None,
            "last_active": None, "is_subscribed": 0, "ai_allowed": 0,
            "is_banned": 1, "ban_reason": text.strip()[:500], "banned_at": None,
        }
        await update.effective_message.reply_text(
            f"🚫 *Utente `{target_cid}` bannato e aggiunto alla blacklist!*\n\n" + _format_user_card(user_rec),
            reply_markup=admin_user_detail_keyboard(user_rec, back_filter=back_filter, back_page=back_page),
            parse_mode="Markdown"
        )
        return True

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
