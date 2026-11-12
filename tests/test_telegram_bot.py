"""Tests for Telegram bot handlers, keyboards, and wizard flow."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, CallbackQuery
from circumorari_bot.core.models import TransportType, FilterType, Stop
from circumorari_bot.bot.keyb import (
    main_menu_keyboard,
    transport_select_keyboard,
    stop_letters_keyboard,
    stops_pagination_keyboard,
    filter_type_keyboard,
    hours_keyboard,
    trip_results_keyboard,
    subscription_manage_keyboard,
)
from circumorari_bot.bot.handlers.start import start_command, help_command, HELP_TEXT
from circumorari_bot.bot.handlers.timetable import wizard_callback
from circumorari_bot.bot.handlers.news import (
    news_sub_menu_callback,
    sub_enable_callback,
    sub_disable_callback,
)
from circumorari_bot.data.users_repo import UsersRepository


def test_help_text_markdown_syntax():
    """Verify HELP_TEXT has balanced markdown formatting and developer attribution."""
    assert "`chat_id`" in HELP_TEXT
    assert "chat_id " not in HELP_TEXT
    # Check developer and repository references
    assert "LeoCanta42" in HELP_TEXT
    assert "https://github.com/LeoCanta42/CircumOrari_Bot" in HELP_TEXT
    # Check that italic/bold markers don't leave hanging tags
    assert HELP_TEXT.count("*") % 2 == 0


def test_stop_letters_keyboard():
    """Verify alphabetical stop letter keyboard extraction and layout."""
    mock_stops = [
        Stop(stop_id="1", stop_name="Acireale", normalized_name="acireale"),
        Stop(stop_id="2", stop_name="Borgo", normalized_name="borgo"),
        Stop(stop_id="3", stop_name="Catania", normalized_name="catania"),
        Stop(stop_id="4", stop_name="Paterno'", normalized_name="paterno"),
        Stop(stop_id="5", stop_name="Randazzo", normalized_name="randazzo"),
    ]
    markup = stop_letters_keyboard(mock_stops, is_departure=True)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    cb_data = [btn.callback_data for btn in buttons]

    assert "let_dep:A" in cb_data
    assert "let_dep:B" in cb_data
    assert "let_dep:C" in cb_data
    assert "let_dep:P" in cb_data
    assert "let_dep:R" in cb_data
    assert "all_dep" in cb_data
    assert "wizard_start" in cb_data


def test_stops_pagination_keyboard_with_letter_filter():
    """Verify filtering stops by initial letter."""
    mock_stops = [
        Stop(stop_id="1", stop_name="Paterno'", normalized_name="paterno"),
        Stop(stop_id="2", stop_name="Piano Tavola", normalized_name="piano tavola"),
        Stop(stop_id="3", stop_name="Pozzillo", normalized_name="pozzillo"),
        Stop(stop_id="4", stop_name="Randazzo", normalized_name="randazzo"),
    ]
    # Filter with letter P
    markup = stops_pagination_keyboard(mock_stops, page=0, is_departure=True, letter="P")
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    btn_texts = [btn.text for btn in buttons]

    assert "Paterno'" in btn_texts
    assert "Piano Tavola" in btn_texts
    assert "Pozzillo" in btn_texts
    assert "Randazzo" not in btn_texts


@pytest.mark.anyio
async def test_help_command_callback_query():
    """Verify help_command edits message in-place without spawning duplicates."""
    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.data = "help_info"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    await help_command(update, context)
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    args, kwargs = query.edit_message_text.call_args
    assert "Guida all'uso" in args[0]
    assert kwargs.get("parse_mode") == "Markdown"


@pytest.mark.anyio
async def test_start_command_callback_query():
    """Verify start_command edits message in-place on menu_main callback."""
    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.data = "menu_main"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()
    context.user_data = {}

    await start_command(update, context)
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    args, kwargs = query.edit_message_text.call_args
    assert "Benvenuto su CircumOrari Bot" in args[0]


@pytest.mark.anyio
async def test_wizard_full_flow():
    """Verify complete end-to-end interactive timetable wizard navigation."""
    context = MagicMock()
    context.user_data = {}

    # 1. Start wizard
    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.data = "wizard_start"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    await wizard_callback(update, context)
    assert "Step 1/4" in query.edit_message_text.call_args[0][0]

    # 2. Select Train mode
    query.data = "tt_train"
    await wizard_callback(update, context)
    assert context.user_data["transport_type"] == TransportType.TRENO
    assert len(context.user_data["available_stops"]) > 0

    # 3. Filter departure by letter P
    query.data = "let_dep:P"
    await wizard_callback(update, context)
    assert context.user_data["dep_letter"] == "P"

    # 4. Pick departure stop
    p_stops = [s for s in context.user_data["available_stops"] if s.stop_name.upper().startswith("P")]
    paterno_stop = next(s for s in p_stops if "PATERNO" in s.stop_name.upper())
    query.data = f"dep_stop:{paterno_stop.stop_id}"
    await wizard_callback(update, context)
    assert context.user_data["dep_stop"].stop_id == paterno_stop.stop_id

    # 5. Filter arrival by letter R
    query.data = "let_arr:R"
    await wizard_callback(update, context)
    assert context.user_data["arr_letter"] == "R"

    # 6. Pick arrival stop
    r_stops = [s for s in context.user_data["available_stops"] if s.stop_name.upper().startswith("R")]
    randazzo_stop = next(s for s in r_stops if "RANDAZZO" in s.stop_name.upper())
    query.data = f"arr_stop:{randazzo_stop.stop_id}"
    await wizard_callback(update, context)
    assert context.user_data["arr_stop"].stop_id == randazzo_stop.stop_id

    # 7. Test back_to_arrival navigation
    query.data = "back_to_arrival"
    await wizard_callback(update, context)
    assert "Fermata di ARRIVO" in query.edit_message_text.call_args[0][0]

    # Re-select arrival
    query.data = f"arr_stop:{randazzo_stop.stop_id}"
    await wizard_callback(update, context)

    # 8. Select filter criterion
    query.data = f"filt:{FilterType.DEPARTURE_AFTER.value}"
    await wizard_callback(update, context)
    assert "Scegli l'orario" in query.edit_message_text.call_args[0][0]

    # 9. Test back_to_filters navigation
    query.data = "back_to_filters"
    await wizard_callback(update, context)
    assert "Criterio di Ricerca" in query.edit_message_text.call_args[0][0]

    # Re-select filter
    query.data = f"filt:{FilterType.DEPARTURE_AFTER.value}"
    await wizard_callback(update, context)

    # 10. Select hour
    query.data = "hour:08:00"
    await wizard_callback(update, context)
    assert "Itinerari trovati" in query.edit_message_text.call_args[0][0]


@pytest.mark.anyio
async def test_manual_news_subscription_flow():
    """Verify manual opt-in and opt-out buttons for news broadcast alerts."""
    repo = UsersRepository()
    test_cid = 888777666
    repo.delete_user(test_cid)

    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    chat = MagicMock()
    chat.id = test_cid
    update.effective_chat = chat
    context = MagicMock()

    # 1. Open subscription preferences menu
    query.data = "news_sub_menu"
    await news_sub_menu_callback(update, context)
    assert repo.is_user_subscribed(test_cid) is False
    assert "Non iscritto" in query.edit_message_text.call_args[0][0]

    # 2. Click button to manually subscribe
    query.data = "sub_enable"
    await sub_enable_callback(update, context)
    assert repo.is_user_subscribed(test_cid) is True
    assert "Iscrizione completata con successo" in query.edit_message_text.call_args[0][0]

    # 3. Click button to manually unsubscribe
    query.data = "sub_disable"
    await sub_disable_callback(update, context)
    assert repo.is_user_subscribed(test_cid) is False
    assert "Notifiche disattivate" in query.edit_message_text.call_args[0][0]

    # Clean up
    repo.delete_user(test_cid)


@pytest.mark.anyio
async def test_bot_post_init_and_commands():
    """Verify bot application factory, public commands list, and post_init registration."""
    from circumorari_bot.bot.bot_app import create_bot_application, post_init, PUBLIC_BOT_COMMANDS

    app = create_bot_application()
    assert app is not None

    # Check commands list
    cmd_names = [c.command for c in PUBLIC_BOT_COMMANDS]
    assert "start" in cmd_names
    assert "viaggio" in cmd_names
    assert "fermate" in cmd_names
    assert "posizione" in cmd_names
    assert "news" in cmd_names
    assert "iscriviti" in cmd_names
    assert "disiscriviti" in cmd_names
    assert "documenti" in cmd_names
    assert "help" in cmd_names
    assert "privacy" in cmd_names
    assert "cancella" in cmd_names
    assert "chat_id" in cmd_names

    # Admin commands should not be in public autocomplete
    assert "stats" not in cmd_names
    assert "broadcast" not in cmd_names

    # Mock app for post_init with real-like token
    mock_app = MagicMock()
    mock_app.bot.token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
    mock_app.bot.set_my_commands = AsyncMock()

    await post_init(mock_app)
    mock_app.bot.set_my_commands.assert_awaited_once_with(PUBLIC_BOT_COMMANDS)

    # Verify MessageHandler only matches private 1-to-1 chats and rejects channels/groups
    from telegram.ext import MessageHandler
    from telegram import Message as TgMessage, Chat as TgChat
    msg_handlers = [h for h in app.handlers[0] if isinstance(h, MessageHandler)]
    assert len(msg_handlers) == 3
    u_channel = Update(999, channel_post=TgMessage(999, None, TgChat(-100123, "channel"), text="prova"))
    assert all(h.filters.check_update(u_channel) is False for h in msg_handlers)
    u_group = Update(999, message=TgMessage(999, None, TgChat(-100456, "supergroup"), text="prova"))
    assert all(h.filters.check_update(u_group) is False for h in msg_handlers)
    u_priv = Update(999, message=TgMessage(999, None, TgChat(12345, "private"), text="prova"))
    text_handler = msg_handlers[-1]
    assert text_handler.filters.check_update(u_priv) is True

    # Post init with dummy token should safely return without network call
    mock_dummy_app = MagicMock()
    mock_dummy_app.bot.token = "0000000000:DUMMY"
    mock_dummy_app.bot.set_my_commands = AsyncMock()
    await post_init(mock_dummy_app)
    mock_dummy_app.bot.set_my_commands.assert_not_called()


@pytest.mark.anyio
async def test_channel_and_group_messages_are_strictly_ignored():
    """Verify bot never responds to messages from channels, groups, or channel posts."""
    from circumorari_bot.bot.handlers.ai_chat import ai_message_handler
    from circumorari_bot.bot.handlers.start import start_command

    # 1. Channel post update
    channel_update = MagicMock(spec=Update)
    channel_update.channel_post = MagicMock()
    channel_update.callback_query = None
    channel_chat = MagicMock()
    channel_chat.type = "channel"
    channel_update.effective_chat = channel_chat
    channel_msg = MagicMock()
    channel_msg.text = "A che ora passa la metro?"
    channel_msg.reply_text = AsyncMock()
    channel_update.effective_message = channel_msg
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await ai_message_handler(channel_update, context)
    channel_msg.reply_text.assert_not_called()
    context.bot.send_chat_action.assert_not_called()

    # 2. Supergroup / Group chat message
    group_update = MagicMock(spec=Update)
    group_update.channel_post = None
    group_chat = MagicMock()
    group_chat.type = "supergroup"
    group_update.effective_chat = group_chat
    group_msg = MagicMock()
    group_msg.text = "C'è un treno per Randazzo?"
    group_msg.reply_text = AsyncMock()
    group_update.effective_message = group_msg

    await ai_message_handler(group_update, context)
    group_msg.reply_text.assert_not_called()
    context.bot.send_chat_action.assert_not_called()

    # 3. Start command on channel
    await start_command(channel_update, context)
    channel_msg.reply_text.assert_not_called()


@pytest.mark.anyio
async def test_ai_permission_flow_and_admin_commands():
    """Verify AI permission checking, unauthorized message blocking, and admin allow/disallow."""
    from circumorari_bot.data.users_repo import UsersRepository
    from circumorari_bot.bot.handlers.ai_chat import ai_message_handler
    from circumorari_bot.bot.handlers.admin import allow_ai_command, disallow_ai_command, list_ai_command
    from circumorari_bot.config import settings

    repo = UsersRepository()
    test_user_id = 777666555
    repo.delete_user(test_user_id)

    # Initially unauthorized
    assert repo.is_ai_allowed(test_user_id) is False

    # Unauthorized user sending question gets restricted notice and is not processed
    update = MagicMock(spec=Update)
    update.channel_post = None
    chat = MagicMock()
    chat.type = "private"
    chat.id = test_user_id
    update.effective_chat = chat
    msg = MagicMock()
    msg.text = "A che ora c'è la metro?"
    msg.reply_text = AsyncMock()
    update.effective_message = msg
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await ai_message_handler(update, context)
    msg.reply_text.assert_awaited_once()
    assert "ad accesso limitato" in msg.reply_text.call_args[0][0]
    assert str(test_user_id) in msg.reply_text.call_args[0][0]

    # Non-admin trying to allow AI is denied
    admin_update = MagicMock(spec=Update)
    admin_user = MagicMock()
    admin_user.id = 999999999
    admin_update.effective_user = admin_user
    admin_msg = MagicMock()
    admin_msg.reply_text = AsyncMock()
    admin_update.effective_message = admin_msg
    admin_context = MagicMock()
    admin_context.args = [str(test_user_id)]

    await allow_ai_command(admin_update, admin_context)
    admin_msg.reply_text.assert_awaited_once_with("⛔ Comando riservato agli amministratori.")

    # Mock admin in settings.admin_user_ids
    original_admins = settings.ADMIN_USER_IDS_RAW
    try:
        settings.ADMIN_USER_IDS_RAW = str(admin_user.id)
        assert admin_user.id in settings.admin_user_ids
        # Admin is automatically allowed AI
        assert repo.is_ai_allowed(admin_user.id) is True

        # Admin executes /allow_ai <chat_id>
        admin_msg.reply_text.reset_mock()
        await allow_ai_command(admin_update, admin_context)
        assert repo.is_ai_allowed(test_user_id) is True
        assert str(test_user_id) in repo.get_ai_allowed_chat_ids()
        assert "Accesso AI *abilitato*" in admin_msg.reply_text.call_args[0][0]

        # Admin checks /list_ai
        admin_msg.reply_text.reset_mock()
        await list_ai_command(admin_update, admin_context)
        assert str(test_user_id) in admin_msg.reply_text.call_args[0][0]

        # Now test_user sends message again -> passes permission check and typing action is triggered
        msg.reply_text.reset_mock()
        with pytest.MonkeyPatch().context() as mp:
            from circumorari_bot.core.models import AIQueryResponse
            from circumorari_bot.services.ai_service import ai_service
            mp.setattr(ai_service, "answer_query", lambda q: AIQueryResponse(answer="Ecco la risposta AI", itineraries_count=1))
            await ai_message_handler(update, context)
            context.bot.send_chat_action.assert_awaited_once_with(chat_id=test_user_id, action="typing")
            assert "Ecco la risposta AI" in msg.reply_text.call_args[0][0]

        # Admin executes /disallow_ai <chat_id>
        admin_msg.reply_text.reset_mock()
        await disallow_ai_command(admin_update, admin_context)
        assert repo.is_ai_allowed(test_user_id) is False
        assert "Accesso AI *revocato*" in admin_msg.reply_text.call_args[0][0]
    finally:
        settings.ADMIN_USER_IDS_RAW = original_admins
        repo.delete_user(test_user_id)


@pytest.mark.anyio
async def test_admin_username_management_and_panel_flow():
    """Verify admin can search/view user by username, toggle AI & notifications, delete record, and use admin panel."""
    from circumorari_bot.data.users_repo import UsersRepository
    from circumorari_bot.bot.handlers.admin import (
        admin_command,
        admin_callback,
        user_command,
        users_command,
        allow_ai_command,
        disallow_ai_command,
        sub_user_command,
        unsub_user_command,
        delete_user_command,
    )
    from circumorari_bot.bot.handlers.start import start_command
    from circumorari_bot.bot.handlers.ai_chat import ai_message_handler
    from circumorari_bot.config import settings

    repo = UsersRepository()
    target_id = 666555444
    target_username = "mario_circumorari_test"
    admin_id = 111222333
    repo.delete_user(target_id)

    # Register target user with username
    repo.register_or_update(chat_id=target_id, username=target_username, first_name="Mario", is_subscribed=False)

    original_admins = settings.ADMIN_USER_IDS_RAW
    try:
        settings.ADMIN_USER_IDS_RAW = str(admin_id)

        # 1. Verify /start shows 'Pannello Admin' button ONLY for admin
        admin_start_upd = MagicMock(spec=Update)
        admin_start_upd.callback_query = None
        admin_chat = MagicMock()
        admin_chat.type = "private"
        admin_chat.id = admin_id
        admin_start_upd.effective_chat = admin_chat
        admin_usr = MagicMock()
        admin_usr.id = admin_id
        admin_usr.username = "admin_boss"
        admin_usr.first_name = "Admin"
        admin_start_upd.effective_user = admin_usr
        admin_msg = MagicMock()
        admin_msg.reply_text = AsyncMock()
        admin_start_upd.effective_message = admin_msg
        ctx = MagicMock()
        ctx.user_data = {}
        ctx.bot.send_message = AsyncMock()

        await start_command(admin_start_upd, ctx)
        admin_kb = admin_msg.reply_text.call_args.kwargs["reply_markup"]
        admin_cb_data = [btn.callback_data for row in admin_kb.inline_keyboard for btn in row]
        assert "adm_menu" in admin_cb_data

        # Regular user should NOT see 'adm_menu'
        user_start_upd = MagicMock(spec=Update)
        user_start_upd.callback_query = None
        u_chat = MagicMock()
        u_chat.type = "private"
        u_chat.id = target_id
        user_start_upd.effective_chat = u_chat
        u_usr = MagicMock()
        u_usr.id = target_id
        u_usr.username = target_username
        u_usr.first_name = "Mario"
        user_start_upd.effective_user = u_usr
        u_msg = MagicMock()
        u_msg.reply_text = AsyncMock()
        user_start_upd.effective_message = u_msg
        await start_command(user_start_upd, ctx)
        u_kb = u_msg.reply_text.call_args.kwargs["reply_markup"]
        u_cb_data = [btn.callback_data for row in u_kb.inline_keyboard for btn in row]
        assert "adm_menu" not in u_cb_data

        # 2. Admin looks up user by @username via /user @mario_circumorari_test
        admin_msg.reply_text.reset_mock()
        ctx.args = [f"@{target_username}"]
        await user_command(admin_start_upd, ctx)
        card_text = admin_msg.reply_text.call_args[0][0]
        assert str(target_id) in card_text
        assert f"@{target_username}" in card_text

        # 3. Admin enables & disables AI by @username
        admin_msg.reply_text.reset_mock()
        ctx.args = [f"@{target_username}"]
        await allow_ai_command(admin_start_upd, ctx)
        assert repo.is_ai_allowed(target_id) is True

        ctx.args = [target_username]  # without @ also works
        await disallow_ai_command(admin_start_upd, ctx)
        assert repo.is_ai_allowed(target_id) is False

        # 4. Admin enables & disables notification subscription by @username
        ctx.args = [f"@{target_username}"]
        await sub_user_command(admin_start_upd, ctx)
        assert repo.is_user_subscribed(target_id) is True

        await unsub_user_command(admin_start_upd, ctx)
        assert repo.is_user_subscribed(target_id) is False

        # 5. Interactive Admin Panel callbacks (toggle AI, toggle sub, search prompt, delete user)
        cb_update = MagicMock(spec=Update)
        cb_query = MagicMock(spec=CallbackQuery)
        cb_query.answer = AsyncMock()
        cb_query.edit_message_text = AsyncMock()
        cb_update.callback_query = cb_query
        cb_update.effective_user = admin_usr
        cb_update.effective_chat = admin_chat

        # Open admin menu
        cb_query.data = "adm_menu"
        await admin_callback(cb_update, ctx)
        assert "Pannello di Controllo Amministratore" in cb_query.edit_message_text.call_args[0][0]

        # Open users list
        cb_query.data = "adm_users:all:0"
        await admin_callback(cb_update, ctx)
        assert "Gestione Utenti" in cb_query.edit_message_text.call_args[0][0]

        # Toggle AI ON via inline button
        cb_query.data = f"adm_u_ai:{target_id}:1:all:0"
        await admin_callback(cb_update, ctx)
        assert repo.is_ai_allowed(target_id) is True

        # Toggle Notifications ON via inline button
        cb_query.data = f"adm_u_sub:{target_id}:1:all:0"
        await admin_callback(cb_update, ctx)
        assert repo.is_user_subscribed(target_id) is True

        # Interactive search prompt -> admin sends username in chat
        cb_query.data = "adm_search_prompt"
        await admin_callback(cb_update, ctx)
        assert ctx.user_data.get("admin_state") == "awaiting_user_search"

        admin_msg.reply_text.reset_mock()
        admin_msg.text = f"@{target_username}"
        admin_start_upd.channel_post = None
        await ai_message_handler(admin_start_upd, ctx)
        assert ctx.user_data.get("admin_state") is None
        assert "Scheda Utente CircumOrari" in admin_msg.reply_text.call_args[0][0]

        # Interactive Broadcast prompt -> admin sends message in chat
        cb_query.data = "adm_broadcast_prompt"
        await admin_callback(cb_update, ctx)
        assert ctx.user_data.get("admin_state") == "awaiting_broadcast"

        admin_msg.reply_text.reset_mock()
        admin_msg.text = "Comunicazione di test agli iscritti"
        await ai_message_handler(admin_start_upd, ctx)
        assert "Broadcast Inviato con Successo" in admin_msg.reply_text.call_args[0][0]

        # Delete user record via inline button confirmation
        cb_query.data = f"adm_u_del_ask:{target_id}:all:0"
        await admin_callback(cb_update, ctx)
        assert "Conferma Eliminazione Utente" in cb_query.edit_message_text.call_args[0][0]

        cb_query.data = f"adm_u_del_ok:{target_id}:all:0"
        await admin_callback(cb_update, ctx)
        assert repo.get_user(target_id) is None

        # Re-register and test /delete_user @username command
        repo.register_or_update(chat_id=target_id, username=target_username, first_name="Mario")
        assert repo.get_user(target_username) is not None
        ctx.args = [f"@{target_username}"]
        await delete_user_command(admin_start_upd, ctx)
        assert repo.get_user(target_id) is None

        # 6. Verify track_user_interaction saves any incoming private user with is_subscribed=0 and ai_allowed=0
        from circumorari_bot.bot.bot_app import track_user_interaction
        new_uid = 555444333
        repo.delete_user(new_uid)
        auto_upd = MagicMock(spec=Update)
        auto_upd.channel_post = None
        auto_chat = MagicMock()
        auto_chat.type = "private"
        auto_chat.id = new_uid
        auto_upd.effective_chat = auto_chat
        auto_user = MagicMock()
        auto_user.id = new_uid
        auto_user.username = "nuovo_utente_circumorari"
        auto_user.first_name = "Luca"
        auto_upd.effective_user = auto_user
        auto_msg = MagicMock()
        auto_msg.text = "/viaggio Catania Bronte"
        auto_upd.effective_message = auto_msg

        await track_user_interaction(auto_upd, ctx)
        saved_u = repo.get_user("nuovo_utente_circumorari")
        assert saved_u is not None
        assert saved_u["chat_id"] == str(new_uid)
        assert saved_u["username"] == "nuovo_utente_circumorari"
        assert saved_u["first_name"] == "Luca"
        assert saved_u["is_subscribed"] == 0
        assert saved_u["ai_allowed"] == 0
        repo.delete_user(new_uid)
    finally:
        settings.ADMIN_USER_IDS_RAW = original_admins
        repo.delete_user(target_id)
        repo.delete_user(admin_id)


def test_stop_cities_keyboard_and_20_per_page_pagination():
    """Verify Telegram Comune filter keyboard and enlarged 20-stops-per-page pagination."""
    from circumorari_bot.bot.keyb import stop_cities_keyboard

    mock_stops = [
        Stop(stop_id=str(i), stop_name=f"Fermata {i:02d}", normalized_name=f"FERMATA {i:02d}", city="Catania")
        for i in range(1, 26)
    ] + [
        Stop(stop_id="99", stop_name="Piano Tavola", normalized_name="PIANO TAVOLA", city="Belpasso"),
    ]

    city_kb = stop_cities_keyboard(mock_stops, is_departure=True)
    city_cbs = [btn.callback_data for row in city_kb.inline_keyboard for btn in row]
    assert "city_dep:Catania" in city_cbs
    assert "city_dep:Belpasso" in city_cbs
    assert "alpha_dep" in city_cbs

    # Filtering by Catania shows 20 stops on page 0 (instead of old 8), with next page button
    page_kb = stops_pagination_keyboard(mock_stops, page=0, is_departure=True, city="Catania")
    stop_btns = [
        btn for row in page_kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("dep_stop:")
    ]
    assert len(stop_btns) == 20


@pytest.mark.anyio
async def test_wizard_city_filter_and_inline_text_search():
    """Verify selecting stops by Comune and typing stop names directly in chat (even without AI access)."""
    from circumorari_bot.bot.handlers.ai_chat import ai_message_handler

    repo = UsersRepository()
    non_ai_uid = 444333222
    repo.delete_user(non_ai_uid)
    assert repo.is_ai_allowed(non_ai_uid) is False

    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot.send_chat_action = AsyncMock()

    upd = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    upd.callback_query = query

    # 1. Select 'Tutti i Mezzi' -> opens Comune selection and activates wizard_input_step='dep'
    query.data = "tt_any"
    await wizard_callback(upd, ctx)
    assert ctx.user_data.get("wizard_input_step") == "dep"
    assert "Comune" in query.edit_message_text.call_args[0][0]

    # 2. Select Comune 'Belpasso' -> shows Belpasso stops
    query.data = "city_dep:Belpasso"
    await wizard_callback(upd, ctx)
    assert ctx.user_data.get("dep_city") == "Belpasso"
    assert "Belpasso" in query.edit_message_text.call_args[0][0]

    # 3. Non-AI user types 'Santa Sofia' directly in chat during wizard -> gets matching stop buttons!
    text_upd = MagicMock(spec=Update)
    text_upd.channel_post = None
    chat = MagicMock()
    chat.type = "private"
    chat.id = non_ai_uid
    text_upd.effective_chat = chat
    msg = MagicMock()
    msg.text = "Santa Sofia"
    msg.reply_text = AsyncMock()
    text_upd.effective_message = msg

    await ai_message_handler(text_upd, ctx)
    msg.reply_text.assert_awaited_once()
    reply_txt = msg.reply_text.call_args[0][0]
    assert "Fermate di PARTENZA trovate" in reply_txt
    kb = msg.reply_text.call_args.kwargs["reply_markup"]
    btn_labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Santa Sofia" in lbl for lbl in btn_labels)
    repo.delete_user(non_ai_uid)


@pytest.mark.anyio
async def test_ai_message_handler_rate_limiting(monkeypatch):
    """Verify sliding-window rate limiting on AI messages: blocks 4th request and permits admin bypass."""
    from circumorari_bot.data.users_repo import UsersRepository
    from circumorari_bot.bot.handlers.ai_chat import ai_message_handler
    from circumorari_bot.core.rate_limiter import ai_rate_limiter
    from circumorari_bot.config import settings
    from circumorari_bot.core.models import AIQueryResponse

    monkeypatch.setattr(
        "circumorari_bot.bot.handlers.ai_chat.ai_service.answer_query",
        lambda text: AIQueryResponse(answer=f"Risposta mock per: {text}")
    )

    repo = UsersRepository()
    test_user_id = 888777666
    ai_rate_limiter.reset(test_user_id)
    repo.delete_user(test_user_id)
    repo.set_ai_allowed(test_user_id, True)  # Authorize AI access

    try:
        # 1. First 3 messages should be processed normally
        for i in range(3):
            upd = MagicMock(spec=Update)
            upd.channel_post = None
            chat = MagicMock()
            chat.type = "private"
            chat.id = test_user_id
            upd.effective_chat = chat
            user = MagicMock()
            user.id = test_user_id
            user.username = "test_rate_limited"
            upd.effective_user = user
            msg = MagicMock()
            msg.text = f"Domanda numero {i+1} da Catania a Bronte"
            msg.reply_text = AsyncMock()
            upd.effective_message = msg
            ctx = MagicMock()
            ctx.bot.send_chat_action = AsyncMock()

            await ai_message_handler(upd, ctx)
            ctx.bot.send_chat_action.assert_awaited_once_with(chat_id=test_user_id, action="typing")
            msg.reply_text.assert_awaited_once()
            reply_text = msg.reply_text.call_args[0][0]
            assert "Limite di richieste raggiunto" not in reply_text

        # 2. 4th message should be immediately blocked by rate limiter
        upd_blocked = MagicMock(spec=Update)
        upd_blocked.channel_post = None
        chat = MagicMock()
        chat.type = "private"
        chat.id = test_user_id
        upd_blocked.effective_chat = chat
        user = MagicMock()
        user.id = test_user_id
        upd_blocked.effective_user = user
        msg_blocked = MagicMock()
        msg_blocked.text = "Quarta domanda consecutiva"
        msg_blocked.reply_text = AsyncMock()
        upd_blocked.effective_message = msg_blocked
        ctx_blocked = MagicMock()
        ctx_blocked.bot.send_chat_action = AsyncMock()

        await ai_message_handler(upd_blocked, ctx_blocked)
        # Should NOT trigger typing action
        ctx_blocked.bot.send_chat_action.assert_not_called()
        msg_blocked.reply_text.assert_awaited_once()
        blocked_text = msg_blocked.reply_text.call_args[0][0]
        assert "Limite di richieste raggiunto" in blocked_text
        assert "3 richieste al minuto" in blocked_text
        assert "secondi" in blocked_text

        # 3. Admin user bypasses rate limit
        admin_id = 999111222
        ai_rate_limiter.reset(admin_id)
        repo.delete_user(admin_id)
        repo.set_ai_allowed(admin_id, True)

        orig_admins = settings.ADMIN_USER_IDS_RAW
        settings.ADMIN_USER_IDS_RAW = str(admin_id)
        try:
            for j in range(4):
                upd_adm = MagicMock(spec=Update)
                upd_adm.channel_post = None
                c_adm = MagicMock()
                c_adm.type = "private"
                c_adm.id = admin_id
                upd_adm.effective_chat = c_adm
                u_adm = MagicMock()
                u_adm.id = admin_id
                upd_adm.effective_user = u_adm
                m_adm = MagicMock()
                m_adm.text = f"Admin query {j+1}"
                m_adm.reply_text = AsyncMock()
                upd_adm.effective_message = m_adm
                ctx_adm = MagicMock()
                ctx_adm.bot.send_chat_action = AsyncMock()

                await ai_message_handler(upd_adm, ctx_adm)
                ctx_adm.bot.send_chat_action.assert_awaited_once()
                m_adm.reply_text.assert_awaited_once()
                assert "Limite di richieste raggiunto" not in m_adm.reply_text.call_args[0][0]
        finally:
            settings.ADMIN_USER_IDS_RAW = orig_admins
            repo.delete_user(admin_id)
            ai_rate_limiter.reset(admin_id)
    finally:
        repo.delete_user(test_user_id)
        ai_rate_limiter.reset(test_user_id)


@pytest.mark.anyio
async def test_sync_bot_commands_and_admin_flows():
    """Verify sync_bot_commands helper, /reset_commands command authorization, and admin callback."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from circumorari_bot.bot.bot_app import sync_bot_commands, PUBLIC_BOT_COMMANDS
    from circumorari_bot.bot.handlers.admin import reset_commands_command, admin_callback
    from circumorari_bot.config import settings
    from circumorari_bot.cli import reset_commands

    # 1. Direct helper execution
    mock_bot = MagicMock()
    mock_bot.token = "123456:FAKE_TOKEN"
    mock_bot.set_my_commands = AsyncMock(return_value=True)

    count = await sync_bot_commands(mock_bot)
    assert count == len(PUBLIC_BOT_COMMANDS)
    mock_bot.set_my_commands.assert_awaited_once_with(PUBLIC_BOT_COMMANDS)

    # 2. Non-admin executing /reset_commands is denied
    non_admin_upd = MagicMock(spec=Update)
    user = MagicMock()
    user.id = 112233
    non_admin_upd.effective_user = user
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    non_admin_upd.effective_message = msg
    ctx = MagicMock()
    ctx.bot = mock_bot

    await reset_commands_command(non_admin_upd, ctx)
    msg.reply_text.assert_awaited_once_with("⛔ Comando riservato agli amministratori.")

    # 3. Admin executing /reset_commands succeeds
    admin_id = 998877
    orig_admins = settings.ADMIN_USER_IDS_RAW
    settings.ADMIN_USER_IDS_RAW = str(admin_id)
    try:
        admin_upd = MagicMock(spec=Update)
        admin_user = MagicMock()
        admin_user.id = admin_id
        admin_upd.effective_user = admin_user
        admin_msg = MagicMock()
        admin_msg.reply_text = AsyncMock()
        admin_upd.effective_message = admin_msg

        await reset_commands_command(admin_upd, ctx)
        admin_msg.reply_text.assert_awaited_once()
        assert "aggiornati con successo" in admin_msg.reply_text.call_args[0][0]

        # 4. Admin callback query 'adm_reset_commands'
        cb_upd = MagicMock(spec=Update)
        cb_upd.effective_user = admin_user
        query = MagicMock()
        query.data = "adm_reset_commands"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        cb_upd.callback_query = query

        await admin_callback(cb_upd, ctx)
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        assert "Comandi Bot Aggiornati" in query.edit_message_text.call_args[0][0]
    finally:
        settings.ADMIN_USER_IDS_RAW = orig_admins

    # 5. CLI command reset_commands()
    with patch("circumorari_bot.bot.bot_app.create_bot_application") as mock_create_app:
        fake_app = MagicMock()
        fake_app.initialize = AsyncMock()
        fake_app.shutdown = AsyncMock()
        fake_app.bot.token = "123:TOKEN"
        fake_app.bot.set_my_commands = AsyncMock(return_value=True)
        mock_create_app.return_value = fake_app

        reset_commands()
        fake_app.initialize.assert_awaited_once()
        fake_app.bot.set_my_commands.assert_awaited_once()
        fake_app.shutdown.assert_awaited_once()


@pytest.mark.anyio
async def test_main_menu_keyboard_and_location_keyboards():
    """Verify main menu contains location button and location keyboards are properly configured."""
    from circumorari_bot.bot.keyb import (
        main_menu_keyboard,
        location_request_keyboard,
        nearest_stops_result_keyboard,
    )
    from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup

    # 1. Main menu keyboard
    mm = main_menu_keyboard(is_admin_user=False)
    all_btns = [b for row in mm.inline_keyboard for b in row]
    assert any(b.text == "📍 Fermate Vicine" and b.callback_data == "prompt_location" for b in all_btns)

    # 2. Location request keyboard
    rk = location_request_keyboard()
    assert isinstance(rk, ReplyKeyboardMarkup)
    assert rk.resize_keyboard is True
    assert rk.one_time_keyboard is True
    r_btns = [b for row in rk.keyboard for b in row]
    assert any(getattr(b, "request_location", False) is True for b in r_btns)
    assert any("Annulla" in b.text for b in r_btns)

    # 3. Nearest stops result keyboard
    res_k = nearest_stops_result_keyboard()
    assert isinstance(res_k, InlineKeyboardMarkup)
    res_btns = [b for row in res_k.inline_keyboard for b in row]
    assert any(b.callback_data == "wizard_start" for b in res_btns)
    assert any(b.callback_data == "menu_main" for b in res_btns)


@pytest.mark.anyio
async def test_location_prompt_and_command():
    """Verify callback and slash command prompt user with request_location reply keyboard."""
    from circumorari_bot.bot.handlers.location import prompt_location_callback, prompt_location_command
    from telegram import ReplyKeyboardMarkup

    # 1. Callback query triggered by 'prompt_location'
    upd_cb = MagicMock(spec=Update)
    query = MagicMock()
    query.answer = AsyncMock()
    upd_cb.callback_query = query
    chat = MagicMock()
    chat.id = 12345
    upd_cb.effective_chat = chat
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()

    await prompt_location_callback(upd_cb, ctx)
    query.answer.assert_awaited_once()
    ctx.bot.send_message.assert_awaited_once()
    sent_call = ctx.bot.send_message.call_args[1]
    assert isinstance(sent_call["reply_markup"], ReplyKeyboardMarkup)
    assert "Fermate più Vicine" in sent_call["text"]

    # 2. Command without args (/posizione or /vicino)
    upd_cmd = MagicMock(spec=Update)
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    upd_cmd.effective_message = msg
    ctx_cmd = MagicMock()
    ctx_cmd.args = []

    await prompt_location_command(upd_cmd, ctx_cmd)
    msg.reply_text.assert_awaited_once()
    assert isinstance(msg.reply_text.call_args[1]["reply_markup"], ReplyKeyboardMarkup)

    # 3. Command with explicit coordinates (/posizione 37.505 15.087)
    msg.reply_text.reset_mock()
    ctx_cmd.args = ["37.505", "15.087"]
    await prompt_location_command(upd_cmd, ctx_cmd)
    msg.reply_text.assert_awaited_once()
    text = msg.reply_text.call_args[0][0]
    assert "STESICORO" in text
    assert "google.com/maps" in text


@pytest.mark.anyio
async def test_location_cancel_handler():
    """Verify '❌ Annulla' cancels location retrieval, removes reply keyboard, and displays main menu."""
    from circumorari_bot.bot.handlers.location import location_cancel_handler
    from telegram import ReplyKeyboardRemove, InlineKeyboardMarkup

    upd = MagicMock(spec=Update)
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    upd.effective_message = msg
    chat = MagicMock()
    chat.id = 12345
    upd.effective_chat = chat
    ctx = MagicMock()

    await location_cancel_handler(upd, ctx)
    assert msg.reply_text.await_count == 2
    # First message removes the reply keyboard
    assert isinstance(msg.reply_text.call_args_list[0][1]["reply_markup"], ReplyKeyboardRemove)
    assert "annullata" in msg.reply_text.call_args_list[0][0][0]
    # Second message provides the main menu inline keyboard
    assert isinstance(msg.reply_text.call_args_list[1][1]["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.anyio
async def test_location_handler_catania_and_far_network():
    """Verify incoming user location calculation: tags, Google Maps link, ReplyKeyboardRemove, and >70km note."""
    from circumorari_bot.bot.handlers.location import location_handler
    from telegram import ReplyKeyboardRemove, InlineKeyboardMarkup

    # 1. Location in central Catania
    upd = MagicMock(spec=Update)
    msg = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock(return_value=status_msg)
    loc = MagicMock()
    loc.latitude = 37.505
    loc.longitude = 15.087
    msg.location = loc
    upd.effective_message = msg
    chat = MagicMock()
    chat.id = 12345
    upd.effective_chat = chat
    ctx = MagicMock()

    await location_handler(upd, ctx)
    # Checks immediate ReplyKeyboardRemove
    msg.reply_text.assert_awaited_once()
    assert isinstance(msg.reply_text.call_args[1]["reply_markup"], ReplyKeyboardRemove)

    # Checks edit_text with nearest stops formatting
    status_msg.edit_text.assert_awaited_once()
    result_text = status_msg.edit_text.call_args[0][0]
    assert "STESICORO" in result_text
    assert "https://www.google.com/maps/search/?api=1&query=" in result_text
    assert "🚇 Metro" in result_text
    assert "Nota di prossimità" not in result_text
    assert isinstance(status_msg.edit_text.call_args[1]["reply_markup"], InlineKeyboardMarkup)

    # 2. Location far from Catania network (Rome coordinates, > 70 km)
    msg.reply_text.reset_mock()
    status_msg.edit_text.reset_mock()
    loc.latitude = 41.9028
    loc.longitude = 12.4964

    await location_handler(upd, ctx)
    status_msg.edit_text.assert_awaited_once()
    far_text = status_msg.edit_text.call_args[0][0]
    assert "Nota di prossimità" in far_text
    assert "fuori dall'area" in far_text
    assert "https://www.google.com/maps/search/?api=1&query=" in far_text
    assert "Prossime partenze" not in far_text


def test_format_nearest_stops_message_departures():
    """Verify format_nearest_stops_message shows upcoming departures when near stops and handles night gracefully."""
    from circumorari_bot.bot.handlers.location import format_nearest_stops_message

    # Daytime near Stesicoro (Catania center)
    msg_day = format_nearest_stops_message(37.505, 15.087, current_time="08:00")
    assert "📍 *Fermate più vicine alla tua posizione:*" in msg_day
    assert "STESICORO" in msg_day
    assert "🕒 *Prossime partenze da STESICORO:*" in msg_day
    assert "🚇 Metro per Monte Po" in msg_day
    assert "🚏 *Più vicine per tipologia di mezzo:*" in msg_day

    # Late night near Stesicoro (outside operating hours)
    msg_night = format_nearest_stops_message(37.505, 15.087, current_time="23:59")
    assert "🕒 *Prossime partenze da STESICORO:*" in msg_night
    assert "Nessuna corsa in partenza a breve o servizio terminato per oggi" in msg_night

    # Far away (Rome coordinates) should omit departure board
    msg_far = format_nearest_stops_message(41.9028, 12.4964, current_time="08:00")
    assert "Nota di prossimità" in msg_far
    assert "Prossime partenze" not in msg_far



@pytest.mark.anyio
async def test_ai_cost_and_donation_descriptions():
    """Verify AI model cost justification and open-source donation explanations across help and payment views."""
    from circumorari_bot.bot.handlers.start import HELP_TEXT
    from circumorari_bot.bot.handlers.payments import (
        send_subscription_invoice,
        ai_sub_info_callback,
        abbonamento_command,
    )
    from circumorari_bot.bot.handlers.ai_chat import ai_help_callback
    from circumorari_bot.data.users_repo import UsersRepository

    # 1. HELP_TEXT checks
    assert "costi operativi" in HELP_TEXT or "costi vivi" in HELP_TEXT
    assert "donazione" in HELP_TEXT
    assert "open-source" in HELP_TEXT
    assert "gratuite" in HELP_TEXT

    # 2. send_subscription_invoice invoice description
    upd_inv = MagicMock(spec=Update)
    chat = MagicMock()
    chat.id = 99887711
    upd_inv.effective_chat = chat
    ctx_inv = MagicMock()
    ctx_inv.bot.send_invoice = AsyncMock()

    await send_subscription_invoice(upd_inv, ctx_inv)
    inv_desc = ctx_inv.bot.send_invoice.call_args[1]["description"]
    assert "costi" in inv_desc.lower()
    assert "donazione" in inv_desc.lower()
    assert "gratuit" in inv_desc.lower()

    # 3. ai_sub_info_callback message text
    upd_info = MagicMock(spec=Update)
    upd_info.effective_chat = chat
    q_info = MagicMock()
    q_info.answer = AsyncMock()
    q_info.edit_message_text = AsyncMock()
    upd_info.callback_query = q_info
    ctx_info = MagicMock()

    await ai_sub_info_callback(upd_info, ctx_info)
    info_text = q_info.edit_message_text.call_args[0][0]
    assert "donazione" in info_text.lower()
    assert "costi" in info_text.lower()
    assert "gratuite" in info_text.lower()

    # 4. abbonamento_command prompt text
    upd_abb = MagicMock(spec=Update)
    upd_abb.effective_chat = chat
    msg_abb = MagicMock()
    msg_abb.reply_text = AsyncMock()
    upd_abb.effective_message = msg_abb
    ctx_abb = MagicMock()

    await abbonamento_command(upd_abb, ctx_abb)
    abb_text = msg_abb.reply_text.call_args[0][0]
    assert "donazione" in abb_text.lower()
    assert "costi" in abb_text.lower()

    # 5. ai_help_callback restricted message for unsubscribed user
    repo = UsersRepository()
    test_user_id = 99887722
    repo.delete_user(test_user_id)
    upd_ai = MagicMock(spec=Update)
    chat_ai = MagicMock()
    chat_ai.id = test_user_id
    upd_ai.effective_chat = chat_ai
    user_ai = MagicMock()
    user_ai.id = test_user_id
    upd_ai.effective_user = user_ai
    q_ai = MagicMock()
    q_ai.answer = AsyncMock()
    q_ai.edit_message_text = AsyncMock()
    upd_ai.callback_query = q_ai
    ctx_ai = MagicMock()

    await ai_help_callback(upd_ai, ctx_ai)
    ai_help_text = q_ai.edit_message_text.call_args[0][0]
    assert "donazione" in ai_help_text.lower()
    assert "costi" in ai_help_text.lower()






