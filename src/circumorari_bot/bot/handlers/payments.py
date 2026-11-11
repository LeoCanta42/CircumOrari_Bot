"""Telegram Payments, free trial activation, and subscription handlers."""

import logging
import time
from telegram import Update, LabeledPrice
from telegram.ext import ContextTypes
from circumorari_bot.config import settings
from circumorari_bot.data.users_repo import UsersRepository
from circumorari_bot.data.settings_repo import SettingsRepository
from circumorari_bot.bot.keyb import main_menu_keyboard, ai_subscription_prompt_keyboard
from circumorari_bot.bot.handlers.admin import is_admin

logger = logging.getLogger(__name__)
users_repo = UsersRepository()
settings_repo = SettingsRepository()


async def send_subscription_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Telegram payment invoice for monthly AI access."""
    chat = update.effective_chat
    if not chat:
        return

    sub_days = settings_repo.get_sub_days()
    stars_amount = settings_repo.get_sub_stars()
    inv_currency = "XTR"
    inv_token = ""  # Telegram Stars requires empty provider_token
    prices = [LabeledPrice(f"Abbonamento AI ({sub_days} giorni)", stars_amount)]
    cost_label = f"{stars_amount} ⭐"

    title = f"Abbonamento Assistente AI ({sub_days}gg)"
    description = (
        f"Accesso illimitato {sub_days}gg all'Assistente AI CircumOrari. "
        f"Il pagamento copre i costi vivi dei modelli e sostiene il progetto come donazione. "
        f"Orari e fermate restano sempre gratuiti. Costo: {cost_label}."
    )
    payload = f"ai_sub:{chat.id}:{int(time.time())}"

    try:
        await context.bot.send_invoice(
            chat_id=chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token=inv_token,
            currency=inv_currency,
            prices=prices,
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            send_phone_number_to_provider=False,
            send_email_to_provider=False,
            is_flexible=False,
        )
    except Exception as e:
        logger.error("Failed to send payment invoice to %s: %s", chat.id, e)
        # Inform user clearly if invoice delivery fails
        err_msg = (
            "💳 *Attivazione Abbonamento AI*\n\n"
            f"Il costo dell'abbonamento ({sub_days} giorni effettivi) è di *{stars_amount} ⭐*.\n\n"
            "Non è stato possibile recapitare la fattura Telegram Stars in questo momento.\n"
            f"Puoi comunicare il tuo Chat ID `{chat.id}` all'amministratore per l'abilitazione manuale immediata."
        )
        if update.callback_query:
            await update.callback_query.message.reply_text(
                err_msg,
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(is_admin_user=is_admin(chat.id))
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                err_msg,
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(is_admin_user=is_admin(chat.id))
            )


async def ai_subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback triggered by 'ai_subscribe' button."""
    query = update.callback_query
    if query:
        await query.answer()
    await send_subscription_invoice(update, context)


async def ai_trial_activate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Activate the 1-time free trial (default 3 days) for the user."""
    query = update.callback_query
    if query:
        await query.answer()

    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    admin_flag = is_admin(chat_id)

    if not settings_repo.is_trial_enabled():
        price_display = settings_repo.get_price_display()
        await query.edit_message_text(
            "⚠️ *Prova Gratuita non disponibile*\n\n"
            "La promozione di prova gratuita è temporaneamente disattivata.\n"
            f"Puoi attivare l'abbonamento per soli *{price_display}*.",
            reply_markup=ai_subscription_prompt_keyboard(
                can_trial=False,
                price_display=price_display,
                sub_days=settings_repo.get_sub_days()
            ),
            parse_mode="Markdown"
        )
        return

    trial_days = settings_repo.get_trial_days()
    ok, result = users_repo.activate_trial(chat_id, days=trial_days)

    if ok:
        success_msg = (
            f"🎉 *Prova Gratuita Attivata!*\n\n"
            f"Il tuo accesso all'Assistente AI è ora pienamente attivo fino al:\n"
            f"📅 `{result}` (UTC)\n\n"
            "Puoi iniziare subito a scrivere qualsiasi domanda sui percorsi e orari, ad esempio:\n"
            "• _\"Come vado da Catania a Bronte oggi pomeriggio?\"_\n"
            "• _\"A che ora passa la metro a Stesicoro?\"_\n\n"
            "Al termine della prova, potrai decidere se rinnovare per 30 giorni."
        )
        await query.edit_message_text(
            success_msg,
            reply_markup=main_menu_keyboard(is_admin_user=admin_flag),
            parse_mode="Markdown"
        )
    else:
        price_display = settings_repo.get_price_display()
        already_msg = (
            "⚠️ *Prova Gratuita già utilizzata*\n\n"
            "Hai già usufruito della prova gratuita una tantum riservata a questa utenza.\n\n"
            f"Puoi attivare l'accesso completo per {settings_repo.get_sub_days()} giorni a soli "
            f"*{price_display}*."
        )
        await query.edit_message_text(
            already_msg,
            reply_markup=ai_subscription_prompt_keyboard(
                can_trial=False,
                is_expired=True,
                price_display=price_display,
                sub_days=settings_repo.get_sub_days()
            ),
            parse_mode="Markdown"
        )


async def ai_sub_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provide transparent pricing, trial, and GDPR privacy details for AI subscription."""
    query = update.callback_query
    if query:
        await query.answer()

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    status = users_repo.get_ai_status(chat_id)
    price_display = settings_repo.get_price_display()
    sub_days = settings_repo.get_sub_days()
    trial_days = settings_repo.get_trial_days()

    trial_status = "Utilizzata" if status.get("trial_used") else f"Disponibile ({trial_days} giorni)"
    exp_str = status.get("ai_expires_at") or "Nessun abbonamento attivo"
    if status.get("is_admin"):
        exp_str = "Accesso Illimitato (Admin)"

    info_text = (
        "ℹ️ *Informazioni Abbonamento, Costi & Donazione AI*\n\n"
        "💡 *Perché l'Assistente AI richiede un contributo?*\n"
        "Tutte le funzionalità ordinarie di CircumOrari (ricerca orari, fermate vicine, avvisi e tabelle PDF) "
        "sono e saranno sempre *100% gratuite e open source*.\n"
        "L'integrazione con i modelli di intelligenza artificiale comporta invece costi computazionali vivi sostenuti "
        "direttamente dallo sviluppatore. Il piccolo contributo mensile copre queste spese e funge da "
        "donazione a supporto dello sviluppo e mantenimento del bot.\n\n"
        f"• *Costo:* {price_display} ogni {sub_days} giorni.\n"
        f"• *Durata:* Il mese è calcolato in {sub_days} giorni solari effettivi dalla data di pagamento.\n"
        f"• *Prova Gratuita:* 1 sola volta per utente ({trial_days} giorni).\n"
        f"• *Rinnovo:* Nessun addebito automatico a sorpresa. Sei sempre tu a scegliere se rinnovare.\n\n"
        "🔒 *Privacy e GDPR (Minimizzazione Dati):*\n"
        "Non memorizziamo dati di carte di credito o coordinate bancarie. Le transazioni sono gestite "
        "in sicurezza direttamente da Telegram e dai circuiti certificati.\n"
        "Viene registrato unicamente il timestamp di scadenza per abilitare l'AI e il flag della prova una tantum.\n"
        "Inviando il comando /cancella, tutti i dati vengono rimossi istantaneamente.\n\n"
        f"📊 *Il tuo stato attuale:*\n"
        f"• Prova Gratuita: `{trial_status}`\n"
        f"• Scadenza AI: `{exp_str}`"
    )

    can_trial = not status.get("trial_used") and settings_repo.is_trial_enabled()
    await query.edit_message_text(
        info_text,
        reply_markup=ai_subscription_prompt_keyboard(
            can_trial=can_trial,
            is_expired=status.get("is_expired", False),
            price_display=price_display,
            sub_days=sub_days,
            is_info_view=True
        ),
        parse_mode="Markdown"
    )


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to Telegram PreCheckoutQuery to validate purchase before charging."""
    query = update.pre_checkout_query
    if not query:
        return

    payload = query.invoice_payload or ""
    if not payload.startswith("ai_sub:"):
        await query.answer(ok=False, error_message="Ordine non valido o scaduto.")
        return

    # Everything verified, approve transaction
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle successful payment confirmation message from Telegram."""
    msg = update.effective_message
    if not msg or not msg.successful_payment:
        return

    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    sub_days = settings_repo.get_sub_days()
    ok, new_expires_at = users_repo.extend_ai_subscription(chat_id, days=sub_days)

    if ok:
        logger.info("Payment successful for chat_id %s: extended AI access to %s", chat_id, new_expires_at)
        text = (
            "🎉 *Pagamento Completato con Successo!*\n\n"
            f"Il tuo abbonamento all'Assistente AI è attivo per i prossimi *{sub_days} giorni*.\n"
            f"📅 *Scadenza:* `{new_expires_at}` (UTC)\n\n"
            "Puoi iniziare subito ad utilizzare l'assistente conversazionale scrivendo qui in chat!"
        )
    else:
        text = (
            "✅ *Pagamento Ricevuto!*\n\n"
            "La transazione è andata a buon fine. Contatta l'amministratore se l'abilitazione non risulta attiva."
        )

    await msg.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(is_admin_user=is_admin(chat_id))
    )


async def abbonamento_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /abbonamento to purchase or renew AI access."""
    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id
    status = users_repo.get_ai_status(chat_id)
    price_display = settings_repo.get_price_display()
    sub_days = settings_repo.get_sub_days()

    if status.get("is_admin"):
        await update.effective_message.reply_text(
            "👑 *Account Amministratore*\n\nHai già accesso illimitato permanente all'Assistente AI.",
            parse_mode="Markdown"
        )
        return

    if status.get("ai_allowed") and status.get("ai_expires_at"):
        days_rem = status.get("days_remaining", 0)
        exp_str = status.get("ai_expires_at")
        text = (
            f"🤖 *Il tuo Abbonamento AI è Attivo*\n\n"
            f"📅 *Scadenza:* `{exp_str}`\n"
            f"⏳ *Giorni rimanenti:* `{days_rem}`\n\n"
            f"Puoi estendere l'abbonamento di ulteriori {sub_days} giorni a {price_display}.\n\n"
            "Grazie per il tuo supporto! Il tuo contributo copre i costi operativi dei modelli AI e sostiene il progetto open source CircumOrari."
        )
    else:
        text = (
            "🤖 *Abbonamento Assistente AI CircumOrari*\n\n"
            f"Attiva l'accesso per {sub_days} giorni a soli *{price_display}*.\n"
            "Nessun vincolo, nessun rinnovo automatico.\n\n"
            "💡 *Sostieni il Progetto:*\n"
            "Mentre orari, fermate vicine, avvisi e tabelle PDF sono completamente gratuiti e open source, "
            "l'Assistente AI comporta costi computazionali diretti. Il pagamento copre le spese dei modelli "
            "e funge da donazione a supporto del bot."
        )

    can_trial = not status.get("trial_used") and settings_repo.is_trial_enabled()
    await update.effective_message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=ai_subscription_prompt_keyboard(
            can_trial=can_trial,
            is_expired=status.get("is_expired", False),
            price_display=price_display,
            sub_days=sub_days
        )
    )
