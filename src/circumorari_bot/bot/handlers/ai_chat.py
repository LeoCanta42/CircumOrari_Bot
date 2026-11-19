"""AI assistant natural language chat message handler."""

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from circumorari_bot.config import settings
from circumorari_bot.core.rate_limiter import ai_rate_limiter
from circumorari_bot.services.ai_service import ai_service
from circumorari_bot.data.users_repo import UsersRepository
from circumorari_bot.data.settings_repo import SettingsRepository
from circumorari_bot.bot.keyb import main_menu_keyboard, ai_subscription_prompt_keyboard
from circumorari_bot.bot.handlers.admin import is_admin, handle_admin_text_input
from circumorari_bot.bot.handlers.timetable import handle_wizard_text_input

logger = logging.getLogger(__name__)
users_repo = UsersRepository()
settings_repo = SettingsRepository()


def _format_window_label(minutes: int) -> str:
    """Format sliding window duration in Italian."""
    return "1 minuto" if minutes == 1 else f"{minutes} minuti"


def _format_wait_time(seconds: int) -> str:
    """Format remaining wait seconds into human-friendly Italian text."""
    secs = max(1, int(seconds))
    if secs < 60:
        return "1 secondo" if secs == 1 else f"{secs} secondi"
    mins, rem_secs = divmod(secs, 60)
    min_label = _format_window_label(mins)
    if rem_secs == 0:
        return min_label
    sec_label = "1 secondo" if rem_secs == 1 else f"{rem_secs} secondi"
    return f"{min_label} e {sec_label}"


def _build_ai_cost_disclaimer() -> str:
    """Build explicit AI optionality, experimental status, fair-use limits, and non-refundable donation notice."""
    max_req = settings_repo.get_ai_rate_limit_requests()
    win_min = settings_repo.get_ai_rate_limit_window_minutes()
    req_word = "richiesta" if max_req == 1 else "richieste"
    win_label = _format_window_label(win_min)
    return (
        "💡 *Informazioni Importanti, Costi & Donazione:*\n"
        "• *Funzione opzionale:* l'Assistente AI è completamente *opzionale*. "
        "Tutte le altre funzioni del bot (orari, fermate vicine, notizie, tabelle PDF) "
        "sono e restano *100% gratuite e open source* (es. /viaggio, /fermate, /posizione).\n"
        f"• *Servizio sperimentale & limiti:* l'Assistente AI *non è stato testato con molti utenti*, "
        f"applica limiti di fair use (*{max_req} {req_word} ogni {win_label}*) e *potrebbe occasionalmente non funzionare* "
        "o subire rallentamenti/errori.\n"
        "• *Non rimborsabile (Donazione):* l'importo pagato *non è rimborsabile*, poiché copre i costi operativi "
        "dei modelli AI sostenuti dallo sviluppatore ed è in ogni caso considerato una *donazione* a supporto del progetto."
    )


async def ai_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle freeform natural language text messages using AI service (private chat only)."""
    # Strictly ignore channel posts, groups, or non-private updates
    if getattr(update, "channel_post", None):
        return
    chat = update.effective_chat
    if not chat or getattr(chat, "type", None) in ("channel", "group", "supergroup"):
        return

    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    # Ignore commands
    if text.startswith("/"):
        return

    user = update.effective_user
    chat = update.effective_chat
    if user and chat:
        users_repo.register_or_update(
            chat_id=chat.id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None)
        )

    uid = getattr(user, "id", None) or getattr(chat, "id", None)
    admin_flag = is_admin(uid) if isinstance(uid, int) else False

    # If admin is currently in an interactive admin prompt (user search / broadcast / ban / fair-use), handle it here
    user_data = getattr(context, "user_data", None)
    if admin_flag and isinstance(user_data, dict) and user_data.get("admin_state"):
        handled = await handle_admin_text_input(update, context)
        if handled:
            return

    # If user is in the manual timetable wizard stop selection step, allow typing a stop/city name directly
    if isinstance(user_data, dict) and user_data.get("wizard_input_step") in ("dep", "arr"):
        t_low = text.lower()
        looks_like_full_ai_sentence = (
            len(text.split()) >= 5
            and any(k in t_low for k in ("da ", "come ", "orari", "alle ", "arrivare", "qual "))
            and users_repo.is_ai_allowed(chat.id)
        )
        if looks_like_full_ai_sentence:
            user_data.pop("wizard_input_step", None)
        else:
            handled_wiz = await handle_wizard_text_input(update, context)
            if handled_wiz:
                return

    # Reject overly long AI messages before consuming a rate-limit slot or calling Gemini
    if len(text) > 600:
        await message.reply_text(
            "⚠️ La domanda è troppo lunga (massimo 600 caratteri). Riformulala in modo più sintetico.",
            reply_markup=main_menu_keyboard(is_admin_user=admin_flag),
        )
        return

    # Permission check: AI is restricted to paid, trial, and authorized users only
    if not users_repo.is_ai_allowed(chat.id):
        logger.info("Unauthorized AI access attempt from chat_id %s", chat.id)
        status = users_repo.get_ai_status(chat.id)
        price_str = settings_repo.get_price_display()
        sub_days = settings_repo.get_sub_days()
        can_trial = not status.get("trial_used") and settings_repo.is_trial_enabled()
        is_expired = status.get("is_expired", False)
        disclaimer = _build_ai_cost_disclaimer()

        if is_expired:
            exp_str = status.get("ai_expires_at") or "recente"
            restricted_msg = (
                "🔒 *Assistente AI ad accesso limitato (Abbonamento Scaduto)*\n\n"
                f"Il tuo periodo di accesso all'Assistente AI è terminato il `{exp_str}` (UTC).\n\n"
                f"🆔 Il tuo ID chat è: `{chat.id}`\n\n"
                f"Per continuare ad utilizzarlo, puoi rinnovare l'accesso per altri {sub_days} giorni a soli *{price_str}*.\n\n"
                f"{disclaimer}"
            )
        elif can_trial:
            restricted_msg = (
                "🤖 *Assistente AI ad accesso limitato*\n\n"
                "L'assistente conversazionale AI ti permette di trovare corse, orari e coincidenze parlando naturalmente.\n\n"
                f"🆔 Il tuo ID chat è: `{chat.id}`\n\n"
                f"🎁 *Hai a disposizione 1 Prova Gratuita di {settings_repo.get_trial_days()} giorni!*\n"
                "Puoi attivarla subito gratuitamente senza alcun costo o impegno.\n\n"
                f"Al termine, potrai rinnovare per {sub_days} giorni a soli *{price_str}*.\n\n"
                f"{disclaimer}"
            )
        else:
            restricted_msg = (
                "🔒 *Assistente AI ad accesso limitato*\n\n"
                "L'assistente conversazionale AI è attivo per gli utenti abbonati.\n\n"
                f"🆔 Il tuo ID chat è: `{chat.id}`\n\n"
                f"Puoi attivare l'accesso per {sub_days} giorni a soli *{price_str}*.\n\n"
                f"{disclaimer}"
            )

        reply_markup = ai_subscription_prompt_keyboard(
            can_trial=can_trial,
            is_expired=is_expired,
            price_display=price_str,
            sub_days=sub_days
        )
        await message.reply_text(
            restricted_msg,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    # Dynamic Fair-Use rate limit check (admins bypass unless in test mode)
    max_requests = settings_repo.get_ai_rate_limit_requests()
    window_minutes = settings_repo.get_ai_rate_limit_window_minutes()
    window_seconds = window_minutes * 60
    bypass_rate_limit = admin_flag and not settings_repo.get_admin_ai_test_mode(uid)

    if not bypass_rate_limit:
        allowed, retry_after, used_count = ai_rate_limiter.check_and_record(
            chat.id,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        window_label = _format_window_label(window_minutes)
        req_word = "richiesta" if max_requests == 1 else "richieste"

        if not allowed:
            logger.info("Rate limit exceeded for user %s: retry after %ds", chat.id, retry_after)
            wait_str = _format_wait_time(retry_after)
            rate_limit_msg = (
                "⏳ *Limite di richieste raggiunto*\n\n"
                "Per garantire un servizio equo e performante a tutti i passeggeri, "
                f"l'Assistente AI è limitato a un massimo di *{max_requests} {req_word} ogni {window_label}* per utente "
                f"(utilizzate: *{used_count}/{max_requests}*).\n\n"
                f"Per favore, attendi *{wait_str}* prima di inviare una nuova domanda."
            )
            await message.reply_text(
                rate_limit_msg,
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(is_admin_user=admin_flag)
            )
            return

        ai_task = asyncio.create_task(asyncio.to_thread(ai_service.answer_query, text))
        tracking_msg = (
            "⏳ *Elaborazione risposta AI in corso...*\n"
            f"📊 _(Richiesta {used_count}/{max_requests} inviata nella finestra attuale di {window_label})_"
        )
        try:
            await message.reply_text(tracking_msg, parse_mode="Markdown")
        except Exception as exc:
            logger.debug("Could not send AI tracking status message: %s", exc)
        try:
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        except Exception as exc:
            logger.debug("Could not send typing chat action: %s", exc)
        response = await ai_task
    else:
        try:
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        except Exception as exc:
            logger.debug("Could not send typing chat action: %s", exc)
        response = await asyncio.to_thread(ai_service.answer_query, text)

    try:
        await message.reply_text(
            response.answer,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(is_admin_user=admin_flag),
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning("Markdown parse error on AI response: %s. Sending plain text fallback.", e)
        await message.reply_text(
            response.answer,
            reply_markup=main_menu_keyboard(is_admin_user=admin_flag),
            disable_web_page_preview=True
        )


async def ai_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how to use the AI assistant and display subscription status."""
    query = update.callback_query
    await query.answer()

    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    uid = getattr(user, "id", None) or chat_id
    admin_flag = is_admin(uid) if isinstance(uid, int) else False

    # If user is not authorized for AI, display subscription / trial offer
    if chat_id and not users_repo.is_ai_allowed(chat_id):
        status = users_repo.get_ai_status(chat_id)
        price_str = settings_repo.get_price_display()
        sub_days = settings_repo.get_sub_days()
        can_trial = not status.get("trial_used") and settings_repo.is_trial_enabled()
        is_expired = status.get("is_expired", False)
        disclaimer = _build_ai_cost_disclaimer()

        if is_expired:
            exp_str = status.get("ai_expires_at") or "recente"
            restricted_msg = (
                "🔒 *Assistente AI ad accesso limitato (Abbonamento Scaduto)*\n\n"
                f"Il tuo periodo di accesso all'Assistente AI è terminato il `{exp_str}` (UTC).\n\n"
                f"🆔 Il tuo ID chat è: `{chat_id}`\n\n"
                f"Puoi riattivare l'accesso per {sub_days} giorni a soli *{price_str}*.\n\n"
                f"{disclaimer}"
            )
        elif can_trial:
            restricted_msg = (
                "🤖 *Assistente AI ad accesso limitato*\n\n"
                "L'assistente intelligente risponde in tempo reale su orari di treni, bus e metro.\n\n"
                f"🆔 Il tuo ID chat è: `{chat_id}`\n\n"
                f"🎁 *Hai a disposizione 1 Prova Gratuita di {settings_repo.get_trial_days()} giorni!*\n"
                "Attivala con un solo tocco senza inserire alcuna carta.\n\n"
                f"Successivamente potrai rinnovare per {sub_days} giorni a soli *{price_str}*.\n\n"
                f"{disclaimer}"
            )
        else:
            restricted_msg = (
                "🔒 *Assistente AI ad accesso limitato*\n\n"
                f"L'assistente conversazionale è disponibile per {sub_days} giorni a soli *{price_str}*.\n\n"
                f"🆔 Il tuo ID chat è: `{chat_id}`\n\n"
                f"{disclaimer}"
            )

        reply_markup = ai_subscription_prompt_keyboard(
            can_trial=can_trial,
            is_expired=is_expired,
            price_display=price_str,
            sub_days=sub_days
        )
        try:
            await query.edit_message_text(
                restricted_msg,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("AI help restricted edit notice: %s", e)
        return

    # For authorized users, display instructions and their active subscription status
    status = users_repo.get_ai_status(chat_id) if chat_id else {}
    if status.get("is_admin"):
        sub_info = "👑 *Stato Accesso:* Illimitato permanente (Admin)"
    elif status.get("is_permanent"):
        sub_info = "🟢 *Stato Accesso:* Permanente"
    elif status.get("ai_expires_at"):
        days_rem = status.get("days_remaining", 0)
        sub_info = f"🟢 *Stato Accesso:* Attivo fino al `{status['ai_expires_at']}` UTC ({days_rem} giorni rimasti)"
    else:
        sub_info = "🟢 *Stato Accesso:* Abilitato"

    max_req = settings_repo.get_ai_rate_limit_requests()
    win_min = settings_repo.get_ai_rate_limit_window_minutes()
    req_word = "richiesta" if max_req == 1 else "richieste"
    win_label = _format_window_label(win_min)

    help_msg = (
        "🤖 *Assistente AI per la Mobilità*\n\n"
        f"{sub_info}\n\n"
        "Non serve cliccare decine di pulsanti! Puoi scrivermi qualsiasi domanda come se parlassi con una persona reale.\n\n"
        "💬 *Esempi di domande che puoi fare:*\n"
        "• _\"Come vado da Catania a Bronte alle 14:00?\"_\n"
        "• _\"Orari della littorina da Paternò a Randazzo verso le 16\"_\n"
        "• _\"A che ora c'è la metro da Monte Po a Borgo stasera alle 20?\"_\n"
        "• _\"Dove posso scaricare i pdf degli orari?\"_\n\n"
        f"⚡ *Fair use*: massimo *{max_req} {req_word} ogni {win_label}* per utente per garantire disponibilità e tempi di risposta ottimali.\n\n"
        "👉 Scrivi adesso la tua richiesta qui sotto!"
    )
    try:
        await query.edit_message_text(
            help_msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Cerca Linea / Orari", callback_data="wizard_start")],
                [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug("AI help edit notice: %s", e)
