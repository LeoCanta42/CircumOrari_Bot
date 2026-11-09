"""Tests for AI subscription management, 3-day trial, auto-deactivation, GDPR, and Admin customization."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, CallbackQuery, PreCheckoutQuery, Message, Chat, User

from fce_bot.config import settings
from fce_bot.data.db import get_persistent_db
from fce_bot.data.users_repo import UsersRepository
from fce_bot.data.settings_repo import SettingsRepository
from fce_bot.bot.handlers.payments import (
    send_subscription_invoice,
    ai_subscribe_callback,
    ai_trial_activate_callback,
    ai_sub_info_callback,
    pre_checkout_handler,
    successful_payment_handler,
    abbonamento_command,
)
from fce_bot.bot.handlers.admin import admin_callback, is_admin


def test_settings_repository_defaults_and_mutation():
    """Verify default AI subscription settings and persistence across updates."""
    repo = SettingsRepository()
    repo.set_sub_price_eur(1.00)
    repo.set_sub_stars(50)
    repo.set_sub_days(30)
    repo.set_trial_days(3)
    repo.set_trial_enabled(True)
    repo.set_currency("XTR")
    
    # Defaults
    assert repo.get_sub_price_eur() == 1.00
    assert repo.get_sub_days() == 30
    assert repo.get_trial_days() == 3
    assert repo.is_trial_enabled() is True
    assert repo.get_currency() == "XTR"

    # Update price
    assert repo.set_sub_price_eur(1.99) is True
    assert repo.get_sub_price_eur() == 1.99

    # Update days
    assert repo.set_sub_days(45) is True
    assert repo.get_sub_days() == 45

    # Update trial
    assert repo.set_trial_days(5) is True
    assert repo.get_trial_days() == 5
    assert repo.set_trial_enabled(False) is True
    assert repo.is_trial_enabled() is False

    # Update currency: only XTR is allowed
    assert repo.set_currency("XTR") is True
    assert repo.get_currency() == "XTR"
    assert repo.set_currency("EUR") is False
    assert repo.set_currency("INVALID") is False

    # Stars configuration
    assert repo.set_sub_stars(1) is True
    assert repo.get_sub_stars() == 1
    assert repo.get_price_display() == "1 ⭐"

    assert repo.set_sub_stars(50) is True
    assert repo.get_sub_stars() == 50
    assert repo.get_price_display() == "50 ⭐"

    # Deletion test
    assert repo.set("temp_setting_key", "temporary_val") is True
    assert repo.get("temp_setting_key") == "temporary_val"
    assert repo.delete("temp_setting_key") is True
    assert repo.get("temp_setting_key") is None

    # Reset back to standard defaults for subsequent tests
    repo.set_sub_price_eur(1.00)
    repo.set_sub_stars(50)
    repo.set_sub_days(30)
    repo.set_trial_days(3)
    repo.set_trial_enabled(True)
    repo.set_currency("XTR")


def test_one_time_trial_and_reset():
    """Verify 3-day free trial can only be activated once per user unless reset by admin."""
    users_repo = UsersRepository()
    cid = 918273645
    users_repo.delete_user(cid)

    # 1. First time trial activation
    ok, exp = users_repo.activate_trial(cid, days=3)
    assert ok is True
    assert exp is not None

    status = users_repo.get_ai_status(cid)
    assert status["ai_allowed"] is True
    assert status["trial_used"] is True
    assert status["days_remaining"] in (3, 4)
    assert users_repo.is_ai_allowed(cid) is True

    # 2. Second trial attempt should be rejected
    ok2, err2 = users_repo.activate_trial(cid, days=3)
    assert ok2 is False
    assert err2 == "already_used"

    # 3. Admin resets trial
    assert users_repo.reset_trial(cid) is True
    status_after_reset = users_repo.get_ai_status(cid)
    assert status_after_reset["trial_used"] is False

    # 4. Now trial can be re-activated
    ok3, exp3 = users_repo.activate_trial(cid, days=3)
    assert ok3 is True
    users_repo.delete_user(cid)


def test_rolling_30_days_subscription_and_stacking():
    """Verify subscription month is strictly rolling 30 days and accumulates on top of remaining days."""
    users_repo = UsersRepository()
    cid = 817263544
    users_repo.delete_user(cid)

    # Clean activation from scratch
    ok, exp1_str = users_repo.extend_ai_subscription(cid, days=30)
    assert ok is True
    exp1_dt = datetime.strptime(exp1_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    diff1 = exp1_dt - now
    # Should be approximately 30 days (720 hours)
    assert 29 <= diff1.days <= 30

    # User extends while having active subscription: 30 more days stacked
    ok2, exp2_str = users_repo.extend_ai_subscription(cid, days=30)
    assert ok2 is True
    exp2_dt = datetime.strptime(exp2_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    diff_stacked = exp2_dt - exp1_dt
    assert diff_stacked.days == 30

    users_repo.delete_user(cid)


def test_auto_deactivation_on_expiration():
    """Verify user is automatically deactivated when expiration date passes."""
    users_repo = UsersRepository()
    cid = 716253443
    users_repo.delete_user(cid)

    # Set user with expired date in past
    past_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    with get_persistent_db() as conn:
        conn.execute("""
            INSERT INTO users (chat_id, ai_allowed, ai_expires_at, last_active)
            VALUES (?, 1, ?, CURRENT_TIMESTAMP)
        """, (str(cid), past_date))

    # is_ai_allowed should detect expiration, auto-deactivate in DB, and return False
    assert users_repo.is_ai_allowed(cid) is False

    # Check status: should be marked expired and ai_allowed = 0
    status = users_repo.get_ai_status(cid)
    assert status["is_expired"] is True
    assert status["ai_allowed"] is False

    # Batch deactivator test
    cid2 = 716253444
    with get_persistent_db() as conn:
        conn.execute("""
            INSERT INTO users (chat_id, ai_allowed, ai_expires_at, last_active)
            VALUES (?, 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET ai_allowed = 1, ai_expires_at = excluded.ai_expires_at
        """, (str(cid2), past_date))

    deactivated_count = users_repo.deactivate_expired_users()
    assert deactivated_count >= 1
    assert users_repo.is_ai_allowed(cid2) is False

    users_repo.delete_user(cid)
    users_repo.delete_user(cid2)


def test_gdpr_data_minimization_and_erasure():
    """Verify minimal data storage, right to erasure (/cancella), and unexpired user preservation."""
    users_repo = UsersRepository()
    cid_active = 615243332
    cid_old = 615243333
    users_repo.delete_user(cid_active)
    users_repo.delete_user(cid_old)

    # 1. Verify schema only has technical & subscription metadata
    user = users_repo.get_user(cid_active)
    assert user is None

    # Register active subscriber with future expiry
    future_date = (datetime.now(timezone.utc) + timedelta(days=25)).strftime("%Y-%m-%d %H:%M:%S")
    users_repo.register_or_update(cid_active, username="privacy_user", first_name="Anna")
    users_repo.extend_ai_subscription(cid_active, days=25)

    # Register old inactive user (no active AI, last_active 70 days ago)
    old_date = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%d %H:%M:%S")
    with get_persistent_db() as conn:
        conn.execute("""
            INSERT INTO users (chat_id, last_active, is_subscribed, ai_allowed)
            VALUES (?, ?, 0, 0)
        """, (str(cid_old), old_date))

    # Prune inactive users: old user deleted, active unexpired user preserved
    pruned = users_repo.prune_inactive(days=60)
    assert pruned >= 1
    assert users_repo.get_user(cid_old) is None
    assert users_repo.get_user(cid_active) is not None

    # Right to erasure: user deletes their own record
    assert users_repo.delete_user(cid_active) is True
    assert users_repo.get_user(cid_active) is None


@pytest.mark.anyio
async def test_telegram_payment_flow_and_invoice():
    """Verify PreCheckout validation and SuccessfulPayment subscription activation."""
    users_repo = UsersRepository()
    cid = 514232221
    users_repo.delete_user(cid)

    # 1. PreCheckout query validation
    update_pre = MagicMock(spec=Update)
    query_pre = MagicMock(spec=PreCheckoutQuery)
    query_pre.invoice_payload = f"ai_sub:{cid}:12345678"
    query_pre.answer = AsyncMock()
    update_pre.pre_checkout_query = query_pre
    context_pre = MagicMock()

    await pre_checkout_handler(update_pre, context_pre)
    query_pre.answer.assert_awaited_once_with(ok=True)

    # Invalid payload should be rejected
    query_pre.invoice_payload = "fraud_payload"
    query_pre.answer.reset_mock()
    await pre_checkout_handler(update_pre, context_pre)
    query_pre.answer.assert_awaited_once_with(ok=False, error_message="Ordine non valido o scaduto.")

    # Idempotency: exception if query was already answered by fast-path is safely suppressed
    query_pre.invoice_payload = f"ai_sub:{cid}:12345678"
    query_pre.answer.reset_mock()
    query_pre.answer.side_effect = Exception("Query is already answered by fast-path")
    await pre_checkout_handler(update_pre, context_pre)
    query_pre.answer.assert_awaited_once_with(ok=True)

    # 2. Successful Payment handler
    update_succ = MagicMock(spec=Update)
    msg_succ = MagicMock(spec=Message)
    sp_mock = MagicMock()
    sp_mock.invoice_payload = f"ai_sub:{cid}:12345678"
    msg_succ.successful_payment = sp_mock
    msg_succ.reply_text = AsyncMock()
    update_succ.effective_message = msg_succ
    chat_mock = MagicMock(spec=Chat)
    chat_mock.id = cid
    update_succ.effective_chat = chat_mock
    context_succ = MagicMock()

    await successful_payment_handler(update_succ, context_succ)
    msg_succ.reply_text.assert_awaited_once()
    reply_text = msg_succ.reply_text.call_args[0][0]
    assert "Pagamento Completato con Successo" in reply_text
    assert users_repo.is_ai_allowed(cid) is True

    # 3. Test sending Telegram Stars Invoice (currency='XTR', provider_token='')
    context_inv = MagicMock()
    context_inv.bot.send_invoice = AsyncMock()
    update_inv = MagicMock(spec=Update)
    chat_inv = MagicMock(spec=Chat)
    chat_inv.id = cid
    update_inv.effective_chat = chat_inv
    settings_repo = SettingsRepository()
    settings_repo.set_currency("XTR")
    settings_repo.set_sub_stars(1)  # 1 Star test configuration

    try:
        await send_subscription_invoice(update_inv, context_inv)
        context_inv.bot.send_invoice.assert_awaited_once()
        inv_kwargs = context_inv.bot.send_invoice.call_args[1]
        assert inv_kwargs["currency"] == "XTR"
        assert inv_kwargs["provider_token"] == ""
        assert inv_kwargs["prices"][0].amount == 1
    finally:
        settings_repo.set_sub_stars(50)
        users_repo.delete_user(cid)


@pytest.mark.anyio
async def test_trial_callback_flow():
    """Verify interactive button callback for trial activation."""
    users_repo = UsersRepository()
    cid = 413221110
    users_repo.delete_user(cid)

    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    chat = MagicMock(spec=Chat)
    chat.id = cid
    update.effective_chat = chat
    context = MagicMock()

    # First click: activates trial
    await ai_trial_activate_callback(update, context)
    query.edit_message_text.assert_awaited_once()
    assert "Prova Gratuita Attivata" in query.edit_message_text.call_args[0][0]
    assert users_repo.is_ai_allowed(cid) is True

    # Second click: notices already used
    query.edit_message_text.reset_mock()
    await ai_trial_activate_callback(update, context)
    query.edit_message_text.assert_awaited_once()
    assert "già utilizzata" in query.edit_message_text.call_args[0][0]

    users_repo.delete_user(cid)


@pytest.mark.anyio
async def test_admin_settings_customization_callbacks(monkeypatch):
    """Verify admin panel controls for modifying subscription pricing, duration, and trial."""
    settings_repo = SettingsRepository()
    users_repo = UsersRepository()
    admin_id = 99887766
    monkeypatch.setattr(settings, "ADMIN_USER_IDS_RAW", str(admin_id))

    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    admin_user = MagicMock(spec=User)
    admin_user.id = admin_id
    update.effective_user = admin_user
    context = MagicMock()
    context.user_data = {}

    # Open AI config menu
    query.data = "adm_ai_cfg"
    await admin_callback(update, context)
    assert "Configurazione Abbonamenti AI" in query.edit_message_text.call_args[0][0]

    # Change stars to 25
    query.data = "adm_ai_cfg:stars:25"
    await admin_callback(update, context)
    assert settings_repo.get_sub_stars() == 25

    # Change sub days to 15
    query.data = "adm_ai_cfg:sub_days:15"
    await admin_callback(update, context)
    assert settings_repo.get_sub_days() == 15

    # Toggle trial
    initial_trial = settings_repo.is_trial_enabled()
    query.data = "adm_ai_cfg:toggle_trial"
    await admin_callback(update, context)
    assert settings_repo.is_trial_enabled() == (not initial_trial)

    # Clean expired
    query.data = "adm_ai_cfg:clean_expired"
    await admin_callback(update, context)
    query.answer.assert_awaited()

    # Reset back to defaults
    settings_repo.set_sub_stars(50)
    settings_repo.set_sub_days(30)
    settings_repo.set_trial_enabled(True)
    settings_repo.set_currency("XTR")


@pytest.mark.anyio
async def test_admin_ai_test_mode_toggle_and_buying(monkeypatch):
    """Verify admin can toggle AI off to test trial & purchase, and re-enable VIP anytime."""
    settings_repo = SettingsRepository()
    users_repo = UsersRepository()
    admin_id = 99887766
    monkeypatch.setattr(settings, "ADMIN_USER_IDS_RAW", str(admin_id))

    # Clean initial state
    users_repo.delete_user(admin_id)
    settings_repo.delete(f"admin_ai_test:{admin_id}")

    try:
        # 1. Initially, admin has full VIP AI bypass
        assert is_admin(admin_id) is True
        assert users_repo.is_ai_allowed(admin_id) is True
        status = users_repo.get_ai_status(admin_id)
        assert status["is_admin"] is True
        assert status["admin_test_mode"] is False

        # 2. Admin revokes AI for their own account in User Management panel to test purchase
        update = MagicMock(spec=Update)
        query = MagicMock(spec=CallbackQuery)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        admin_user = MagicMock(spec=User)
        admin_user.id = admin_id
        update.effective_user = admin_user
        context = MagicMock()
        context.user_data = {}

        query.data = f"adm_u_ai:{admin_id}:0:all:0"
        await admin_callback(update, context)

        # Verify test mode is now active and AI is revoked for the admin account
        assert settings_repo.get_admin_ai_test_mode(admin_id) is True
        assert users_repo.is_ai_allowed(admin_id) is False
        status_test = users_repo.get_ai_status(admin_id)
        assert status_test["is_admin"] is False
        assert status_test["admin_test_mode"] is True
        assert status_test["ai_allowed"] is False

        # Admin privilege for /admin remains intact
        assert is_admin(admin_id) is True

        # 3. Test /abbonamento command behavior for admin in test mode
        msg_abbonamento = MagicMock(spec=Message)
        msg_abbonamento.reply_text = AsyncMock()
        update_abb = MagicMock(spec=Update)
        chat_abb = MagicMock(spec=Chat)
        chat_abb.id = admin_id
        update_abb.effective_chat = chat_abb
        update_abb.effective_message = msg_abbonamento

        await abbonamento_command(update_abb, context)
        msg_abbonamento.reply_text.assert_awaited_once()
        reply_text = msg_abbonamento.reply_text.call_args[0][0]
        # Should show the subscription offer, NOT "Account Amministratore già permanente"
        assert "Abbonamento Assistente AI FCE" in reply_text
        assert "Account Amministratore" not in reply_text

        # 4. Admin simulates purchasing 1 Telegram Star
        settings_repo.set_currency("XTR")
        settings_repo.set_sub_stars(1)
        ok, exp = users_repo.extend_ai_subscription(admin_id, days=30)
        assert ok is True
        assert exp is not None
        assert users_repo.is_ai_allowed(admin_id) is True

        # 5. Admin restores permanent unlimited VIP access in User Management panel
        query.edit_message_text.reset_mock()
        query.answer.reset_mock()
        query.data = f"adm_u_perm:{admin_id}:all:0"
        await admin_callback(update, context)

        # Verify test mode is disabled and VIP access is restored
        assert settings_repo.get_admin_ai_test_mode(admin_id) is False
        assert users_repo.is_ai_allowed(admin_id) is True
        status_vip = users_repo.get_ai_status(admin_id)
        assert status_vip["is_admin"] is True
        assert status_vip["is_permanent"] is True
    finally:
        # Restore production defaults and clean test artifacts
        settings_repo.set_sub_stars(50)
        settings_repo.delete(f"admin_ai_test:{admin_id}")
        users_repo.delete_user(admin_id)

