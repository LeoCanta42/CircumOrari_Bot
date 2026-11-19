"""Telegram Application factory and handler configuration."""

import logging
from telegram import Bot, BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from circumorari_bot.config import settings
from circumorari_bot.data.settings_repo import SettingsRepository
from circumorari_bot.data.users_repo import UsersRepository
from circumorari_bot.bot.handlers.start import (
    start_command,
    help_command,
    chat_id_command,
    privacy_command,
    cancella_command,
)
from circumorari_bot.bot.handlers.timetable import (
    viaggio_command,
    fermate_command,
    wizard_callback,
)
from circumorari_bot.bot.handlers.news import (
    news_command,
    news_callback,
    news_sub_menu_callback,
    sub_enable_callback,
    sub_disable_callback,
    iscriviti_command,
    disiscriviti_command,
)
from circumorari_bot.bot.handlers.documents import documents_command, documents_callback
from circumorari_bot.bot.handlers.location import (
    prompt_location_command,
    prompt_location_callback,
    location_cancel_handler,
    location_handler,
)
from circumorari_bot.bot.handlers.ai_chat import ai_message_handler, ai_help_callback
from circumorari_bot.bot.handlers.payments import (
    ai_subscribe_callback,
    ai_trial_activate_callback,
    ai_sub_info_callback,
    pre_checkout_handler,
    successful_payment_handler,
    abbonamento_command,
)
from circumorari_bot.bot.handlers.admin import (
    admin_command,
    admin_callback,
    stats_command,
    sync_gtfs_command,
    broadcast_command,
    user_command,
    users_command,
    allow_ai_command,
    disallow_ai_command,
    sub_user_command,
    unsub_user_command,
    delete_user_command,
    ban_user_command,
    unban_user_command,
    maintenance_command,
    list_ai_command,
    reset_commands_command,
)

logger = logging.getLogger(__name__)
users_repo = UsersRepository()
settings_repo = SettingsRepository()


async def blacklist_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global pre-processor halting all updates from banned users/chats before any handler runs."""
    if getattr(update, "channel_post", None):
        return

    candidate_ids: list[int | str] = []
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if user and getattr(user, "id", None) is not None:
        candidate_ids.append(user.id)
    if chat and getattr(chat, "type", None) == "private" and getattr(chat, "id", None) is not None:
        if chat.id not in candidate_ids:
            candidate_ids.append(chat.id)

    if not candidate_ids:
        return

    admin_ids = settings.admin_user_ids
    for cid in candidate_ids:
        try:
            if int(cid) in admin_ids:
                return
        except (ValueError, TypeError):
            pass

    for cid in candidate_ids:
        if users_repo.is_user_banned(cid):
            logger.info("Blocked update from banned user/chat %s", cid)
            if getattr(update, "pre_checkout_query", None):
                try:
                    await update.pre_checkout_query.answer(
                        ok=False,
                        error_message="⛔ Accesso al bot revocato.",
                    )
                except Exception as exc:
                    logger.debug("Could not reject pre_checkout_query for banned user %s: %s", cid, exc)
            elif getattr(update, "callback_query", None):
                try:
                    await update.callback_query.answer(
                        "⛔ Il tuo account è stato bloccato e non può utilizzare questo bot.",
                        show_alert=True,
                    )
                except Exception as exc:
                    logger.debug("Could not answer callback_query for banned user %s: %s", cid, exc)
            elif (
                getattr(update, "effective_message", None)
                and chat
                and getattr(chat, "type", None) == "private"
            ):
                try:
                    await update.effective_message.reply_text(
                        "⛔ Il tuo account è stato bloccato e non può utilizzare questo bot."
                    )
                except Exception as exc:
                    logger.debug("Could not send ban notice to user %s: %s", cid, exc)
            raise ApplicationHandlerStop


async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block non-admin updates during maintenance mode and record waitlist for back-online alerts."""
    if getattr(update, "channel_post", None):
        return
    if not settings_repo.is_maintenance_mode():
        return

    candidate_ids: list[int | str] = []
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if user and getattr(user, "id", None) is not None:
        candidate_ids.append(user.id)
    is_private = bool(chat and getattr(chat, "type", None) == "private")
    if is_private and getattr(chat, "id", None) is not None and chat.id not in candidate_ids:
        candidate_ids.append(chat.id)

    admin_ids = settings.admin_user_ids
    for cid in candidate_ids:
        try:
            if int(cid) in admin_ids:
                return
        except (ValueError, TypeError):
            pass

    pre_checkout = getattr(update, "pre_checkout_query", None)
    if not is_private and not pre_checkout:
        return

    target_id = chat.id if is_private else (getattr(user, "id", None) if user else None)
    if target_id is not None:
        settings_repo.add_maintenance_waitlist_user(target_id)

    if pre_checkout:
        try:
            await pre_checkout.answer(
                ok=False,
                error_message="🛠 Il bot è temporaneamente in manutenzione. Riprova più tardi.",
            )
        except Exception as exc:
            logger.debug("Could not reject pre_checkout_query in maintenance for %s: %s", target_id, exc)
    elif getattr(update, "callback_query", None):
        try:
            await update.callback_query.answer(
                "🛠 Il bot è temporaneamente in manutenzione. Riprova più tardi!",
                show_alert=True,
            )
        except Exception as exc:
            logger.debug("Could not answer callback_query in maintenance for %s: %s", target_id, exc)
    elif getattr(update, "effective_message", None) and is_private:
        try:
            await update.effective_message.reply_text(
                "🛠 *Bot in Manutenzione*\n\n"
                "CircumOrari Bot è temporaneamente in manutenzione per aggiornamenti e miglioramenti.\n"
                "Ti avviseremo o potrai riprovare non appena il servizio tornerà online!",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.debug("Could not send maintenance reply to %s: %s", target_id, exc)

    raise ApplicationHandlerStop


async def track_user_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Automatically register/update private chat users on any message, command, or button click.
    
    New users are stored with notifications disabled (is_subscribed=0) and AI disabled (ai_allowed=0)
    unless they explicitly opt-in or an admin enables them.
    """
    if getattr(update, "channel_post", None):
        return
    chat = update.effective_chat
    if not chat or getattr(chat, "type", None) != "private":
        return

    # Do not re-create record when user is executing /cancella or /stop (GDPR Right to Erasure)
    msg = update.effective_message
    if msg and msg.text:
        cmd = msg.text.strip().split()[0].lower().split("@")[0]
        if cmd in ("/cancella", "/stop"):
            return

    user = update.effective_user
    users_repo.register_or_update(
        chat_id=chat.id,
        username=getattr(user, "username", None) if user else None,
        first_name=getattr(user, "first_name", None) if user else None,
        is_subscribed=False,
    )

# List of public slash commands registered with Telegram for autocomplete menu
PUBLIC_BOT_COMMANDS = [
    BotCommand("start", "Menu principale e calcolo viaggio"),
    BotCommand("viaggio", "Cerca corsa (es. /viaggio Catania - Bronte)"),
    BotCommand("fermate", "Cerca stazioni e fermate"),
    BotCommand("posizione", "Trova le fermate più vicine alla tua posizione"),
    BotCommand("news", "Ultime notizie e avvisi di servizio"),
    BotCommand("iscriviti", "Ricevi notifiche su scioperi e variazioni"),
    BotCommand("disiscriviti", "Disattiva la ricezione delle notifiche"),
    BotCommand("abbonamento", "Attiva o rinnova l'abbonamento all'Assistente AI"),
    BotCommand("documenti", "Scarica le tabelle orarie PDF ufficiali"),
    BotCommand("help", "Guida dettagliata all'uso del bot"),
    BotCommand("privacy", "Informativa GDPR e privacy"),
    BotCommand("cancella", "Cancella tutti i tuoi dati dal bot"),
    BotCommand("chat_id", "Mostra l'identificativo di questa chat"),
]


async def sync_bot_commands(bot: Bot) -> int:
    """Register public bot commands with Telegram for '/' autocomplete menu."""
    token = getattr(bot, "token", "") or ""
    if not token or token.startswith("0000000000"):
        return 0

    await bot.set_my_commands(PUBLIC_BOT_COMMANDS)
    logger.info("Bot commands successfully registered with Telegram (%d commands).", len(PUBLIC_BOT_COMMANDS))
    return len(PUBLIC_BOT_COMMANDS)


async def post_init(application: Application) -> None:
    """Register public bot commands with Telegram for '/' autocomplete."""
    try:
        await sync_bot_commands(application.bot)
    except Exception as exc:
        logger.warning("Could not set bot commands: %s", exc)


async def error_handler(update: object, context) -> None:
    """Log unhandled exceptions during Telegram update processing without crashing."""
    err = getattr(context, "error", None)
    if err and "Message is not modified" in str(err):
        return
    logger.error("Unhandled exception while processing Telegram update: %s", err, exc_info=err)


def create_bot_application() -> Application:
    """Instantiate and configure the python-telegram-bot Application."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN is not set. Bot will run in unconfigured mode.")
        token = "0000000000:DUMMY_TOKEN_FOR_INITIALIZATION"

    builder = ApplicationBuilder().token(token).post_init(post_init)
    app = builder.build()
    app.add_error_handler(error_handler)

    # Global blacklist guard (group -3): halt all updates from banned users first (never waitlisted)
    app.add_handler(TypeHandler(Update, blacklist_guard), group=-3)

    # Global maintenance guard (group -2): halt non-admin updates during maintenance & save waitlist
    app.add_handler(TypeHandler(Update, maintenance_guard), group=-2)

    # Pre-processor (group -1): automatically save/update private user with is_subscribed=0, ai_allowed=0
    app.add_handler(TypeHandler(Update, track_user_interaction), group=-1)

    private_chat = filters.ChatType.PRIVATE

    # Commands - strictly restricted to direct private 1-to-1 chats
    app.add_handler(CommandHandler("start", start_command, filters=private_chat))
    app.add_handler(CommandHandler("help", help_command, filters=private_chat))
    app.add_handler(CommandHandler("abbonamento", abbonamento_command, filters=private_chat))
    app.add_handler(CommandHandler("viaggio", viaggio_command, filters=private_chat))
    app.add_handler(CommandHandler("fermate", fermate_command, filters=private_chat))
    app.add_handler(CommandHandler(["posizione", "vicino"], prompt_location_command, filters=private_chat))
    app.add_handler(CommandHandler("news", news_command, filters=private_chat))
    app.add_handler(CommandHandler("iscriviti", iscriviti_command, filters=private_chat))
    app.add_handler(CommandHandler("disiscriviti", disiscriviti_command, filters=private_chat))
    app.add_handler(CommandHandler("documenti", documents_command, filters=private_chat))
    app.add_handler(CommandHandler("chat_id", chat_id_command, filters=private_chat))
    app.add_handler(CommandHandler("privacy", privacy_command, filters=private_chat))
    app.add_handler(CommandHandler("cancella", cancella_command, filters=private_chat))
    app.add_handler(CommandHandler("stop", cancella_command, filters=private_chat))

    # Admin commands (strictly private chat only)
    app.add_handler(CommandHandler("admin", admin_command, filters=private_chat))
    app.add_handler(CommandHandler("stats", stats_command, filters=private_chat))
    app.add_handler(CommandHandler("sync_gtfs", sync_gtfs_command, filters=private_chat))
    app.add_handler(CommandHandler("broadcast", broadcast_command, filters=private_chat))
    app.add_handler(CommandHandler("user", user_command, filters=private_chat))
    app.add_handler(CommandHandler("users", users_command, filters=private_chat))
    app.add_handler(CommandHandler("allow_ai", allow_ai_command, filters=private_chat))
    app.add_handler(CommandHandler("disallow_ai", disallow_ai_command, filters=private_chat))
    app.add_handler(CommandHandler("revoke_ai", disallow_ai_command, filters=private_chat))
    app.add_handler(CommandHandler("sub_user", sub_user_command, filters=private_chat))
    app.add_handler(CommandHandler("unsub_user", unsub_user_command, filters=private_chat))
    app.add_handler(CommandHandler("delete_user", delete_user_command, filters=private_chat))
    app.add_handler(CommandHandler("del_user", delete_user_command, filters=private_chat))
    app.add_handler(CommandHandler("ban", ban_user_command, filters=private_chat))
    app.add_handler(CommandHandler("unban", unban_user_command, filters=private_chat))
    app.add_handler(CommandHandler("manutenzione", maintenance_command, filters=private_chat))
    app.add_handler(CommandHandler("list_ai", list_ai_command, filters=private_chat))
    app.add_handler(CommandHandler(["reset_commands", "reflush_commands"], reset_commands_command, filters=private_chat))

    # Callback queries
    app.add_handler(CallbackQueryHandler(start_command, pattern="^menu_main$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern="^help_info$"))
    app.add_handler(CallbackQueryHandler(prompt_location_callback, pattern="^prompt_location$"))
    app.add_handler(CallbackQueryHandler(ai_help_callback, pattern="^ai_help$"))
    app.add_handler(CallbackQueryHandler(ai_subscribe_callback, pattern="^ai_subscribe$"))
    app.add_handler(CallbackQueryHandler(ai_trial_activate_callback, pattern="^ai_trial_activate$"))
    app.add_handler(CallbackQueryHandler(ai_sub_info_callback, pattern="^ai_sub_info$"))
    app.add_handler(CallbackQueryHandler(news_callback, pattern="^news_latest$"))
    app.add_handler(CallbackQueryHandler(news_sub_menu_callback, pattern="^news_sub_menu$"))
    app.add_handler(CallbackQueryHandler(sub_enable_callback, pattern="^sub_enable$"))
    app.add_handler(CallbackQueryHandler(sub_disable_callback, pattern="^sub_disable$"))
    app.add_handler(CallbackQueryHandler(documents_callback, pattern="^doc_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(wizard_callback))

    # Payments handlers (Pre-Checkout verification and successful payment notification)
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(private_chat & filters.SUCCESSFUL_PAYMENT, successful_payment_handler), group=1)

    # Location and location prompt cancellation handlers
    app.add_handler(MessageHandler(private_chat & filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(private_chat & filters.Regex(r"(?i)^(?:❌\s*)?annulla$"), location_cancel_handler))

    # Natural language AI chat fallback - strictly private 1-to-1 chat only
    app.add_handler(MessageHandler(private_chat & filters.TEXT & ~filters.COMMAND, ai_message_handler))

    return app
