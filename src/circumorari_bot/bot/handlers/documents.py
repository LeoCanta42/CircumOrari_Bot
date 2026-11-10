"""Document links and PDF download handlers."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from circumorari_bot.services.documents_service import documents_service
from circumorari_bot.bot.keyb import documents_keyboard, main_menu_keyboard

logger = logging.getLogger(__name__)


async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /documenti command."""
    docs = documents_service.get_documents()
    text = (
        "📄 *Documenti e Tabelle Orari Ufficiali*\n\n"
        "Puoi scaricare direttamente i PDF ufficiali aggiornati dal sito Circumetnea:"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=documents_keyboard(docs.metro_url, docs.train_url, docs.bus_url),
        parse_mode="Markdown"
    )


async def documents_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    docs = documents_service.get_documents()

    if data == "doc_all":
        text = (
            "📄 *Documenti e Tabelle Orari Ufficiali*\n\n"
            "Seleziona il documento da scaricare in formato PDF:"
        )
        await query.edit_message_text(
            text,
            reply_markup=documents_keyboard(docs.metro_url, docs.train_url, docs.bus_url),
            parse_mode="Markdown"
        )
        return

    if data == "doc_metro":
        await query.edit_message_text(
            f"🚇 *Orario Metropolitana di Catania*\n\n"
            f"Consulta e scarica la tabella oraria ufficiale della Metropolitana (tratta Monte Po - Stesicoro).\n\n"
            f"📄 [Scarica PDF Metropolitana]({docs.metro_url})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚇 Scarica PDF Metro", url=docs.metro_url)],
                [InlineKeyboardButton("📄 Tutti i Documenti PDF", callback_data="doc_all")],
                [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
            ]),
            parse_mode="Markdown"
        )
        return

    if data == "doc_train":
        await query.edit_message_text(
            f"🚆 *Orario Treni Superficie (Circumetnea)*\n\n"
            f"Consulta e scarica la tabella oraria ufficiale dei treni storici e moderni (tratta Catania - Randazzo - Riposto).\n\n"
            f"📄 [Scarica PDF Treni]({docs.train_url})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚆 Scarica PDF Treni", url=docs.train_url)],
                [InlineKeyboardButton("📄 Tutti i Documenti PDF", callback_data="doc_all")],
                [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
            ]),
            parse_mode="Markdown"
        )
        return

    if data == "doc_bus":
        await query.edit_message_text(
            f"🚌 *Orario Autolinee (Bus)*\n\n"
            f"Consulta e scarica la tabella oraria ufficiale delle corse in autobus.\n\n"
            f"📄 [Scarica PDF Autolinee]({docs.bus_url})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚌 Scarica PDF Bus", url=docs.bus_url)],
                [InlineKeyboardButton("📄 Tutti i Documenti PDF", callback_data="doc_all")],
                [InlineKeyboardButton("⬅️ Menu Principale", callback_data="menu_main")],
            ]),
            parse_mode="Markdown"
        )
        return
