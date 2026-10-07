"""Проверки Telegram-сценариев без сетевых запросов."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler
from telegram.error import TimedOut

import bot_handlers as handlers
import service
import storage


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        storage.configure_db_path(Path(self.directory.name) / "test.db")
        service.initialize()
        self.context = SimpleNamespace(
            user_data={}, application=SimpleNamespace(bot_data={"admin_id": 9})
        )

    def tearDown(self):
        storage.configure_db_path(None)
        self.directory.cleanup()

    def update(self, user_id=1, username="Alice_1", text="", callback=None, chat_type="private"):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        query = None if callback is None else SimpleNamespace(
            data=callback, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message
        )
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, username=username),
            effective_chat=SimpleNamespace(type=chat_type),
            effective_message=message, callback_query=query,
        )

    async def test_registration_repeat_and_conflict(self):
        await handlers.start(self.update(), self.context)
        await handlers.start(self.update(username=None), self.context)
        self.assertEqual(service.get_player_balance(1).balance, 100)
        conflict = self.update(user_id=2)
        await handlers.start(conflict, self.context)
        self.assertIn("зарегистрирован", conflict.effective_message.reply_text.call_args.args[0])
        self.assertIsNone(service.get_player(2))

    async def test_missing_username_and_group_do_not_register(self):
        for update in (self.update(user_id=9, username=None), self.update(chat_type="group")):
            await handlers.start(update, self.context)
            self.assertIsNone(service.get_player(update.effective_user.id))
            self.assertNotIn("reply_markup", update.effective_message.reply_text.call_args.kwargs)

    async def test_transfer_flow_and_stale_callback(self):
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        await handlers.begin_transfer(self.update(callback="menu:transfer"), self.context)
        await handlers.receive_transfer_recipient(self.update(text="Bob__22"), self.context)
        await handlers.receive_transfer_amount(self.update(text="25"), self.context)
        await handlers.skip_transfer_message(self.update(callback="tx:skip_message"), self.context)
        key = self.context.user_data["active_intent"]["key"]
        confirmed = self.update(callback=f"tx:confirm:{key}")
        await handlers.confirm_transfer(confirmed, self.context)
        self.assertEqual(service.get_player_balance(1).balance, 75)
        self.assertIn("Hash:", confirmed.callback_query.edit_message_text.call_args.args[0])
        await handlers.confirm_transfer(confirmed, self.context)
        self.assertEqual(service.get_player_balance(1).balance, 75)

    async def test_transfer_message_confirmation_and_search(self):
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        await handlers.receive_transfer_recipient(self.update(text="Bob__22"), self.context)
        self.assertEqual(
            await handlers.receive_transfer_amount(self.update(text="25"), self.context),
            handlers.TRANSFER_MESSAGE,
        )
        for text in ("   ", "x" * 1001):
            self.assertEqual(
                await handlers.receive_transfer_message(self.update(text=text), self.context),
                handlers.TRANSFER_MESSAGE,
            )
        message_update = self.update(text="  Спасибо! 💛\nЗа помощь  ")
        self.assertEqual(
            await handlers.receive_transfer_message(message_update, self.context),
            handlers.TRANSFER_CONFIRM,
        )
        self.assertIn("Спасибо! 💛\nЗа помощь", message_update.effective_message.reply_text.call_args.args[0])
        key = self.context.user_data["active_intent"]["key"]
        await handlers.confirm_transfer(self.update(callback=f"tx:confirm:{key}"), self.context)
        block = storage.get_last_block()
        searched = self.update(user_id=9, text=str(block.index))
        await handlers.receive_search_id(searched, self.context)
        self.assertIn("Спасибо! 💛\nЗа помощь", searched.effective_message.reply_text.call_args.args[0])
        self.assertEqual(service.get_player_balance(2).balance, 125)

    async def test_search_hides_message_from_non_admin_users(self):
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        block = service.create_player_transaction(1, "Bob__22", 25, "private-note", "Личный текст 💛")
        for user_id in (1, 2, 3):
            with self.subTest(user_id=user_id):
                searched = self.update(user_id=user_id, username="Admin_9", text=str(block.index))
                with patch("storage.get_transaction_message") as read_message:
                    result = await handlers.receive_search_id(searched, self.context)
                read_message.assert_not_called()
                self.assertEqual(result, ConversationHandler.END)
                text = searched.effective_message.reply_text.call_args.args[0]
                self.assertIn(f"Блок #{block.index}", text)
                self.assertIn("$25", text)
                self.assertNotIn("Личный текст", text)
                self.assertNotIn("Сообщение отправителя", text)

    async def test_search_does_not_expose_admin_message_in_group(self):
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        block = service.create_player_transaction(1, "Bob__22", 25, "group-note", "Личный текст")
        searched = self.update(user_id=9, text=str(block.index), chat_type="group")
        with patch("storage.get_transaction_message") as read_message:
            await handlers.receive_search_id(searched, self.context)
        read_message.assert_not_called()
        self.assertNotIn("Личный текст", searched.effective_message.reply_text.call_args.args[0])

    async def test_sender_reply_failure_keeps_credit_and_notification(self):
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        await handlers.receive_transfer_recipient(self.update(text="Bob__22"), self.context)
        await handlers.receive_transfer_amount(self.update(text="25"), self.context)
        await handlers.skip_transfer_message(self.update(callback="tx:skip_message"), self.context)
        key = self.context.user_data["active_intent"]["key"]
        confirmation = self.update(callback=f"tx:confirm:{key}")
        confirmation.callback_query.edit_message_text.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await handlers.confirm_transfer(confirmation, self.context)
        self.assertEqual(service.get_player_balance(2).balance, 125)
        notice = storage.claim_money_notification()
        self.assertEqual(notice.amount, 25)
        self.assertEqual(notice.recipient_id, 2)
        self.assertNotIn("active_intent", self.context.user_data)

    async def test_cancel_and_new_flow_invalidate_intent(self):
        service.register_player(1, "Alice_1")
        self.context.user_data["active_intent"] = {"key": "old", "kind": "transfer"}
        await handlers.begin_search(self.update(callback="menu:search"), self.context)
        self.assertNotIn("active_intent", self.context.user_data)
        self.context.user_data["active_intent"] = {"key": "old", "kind": "transfer"}
        await handlers.cancel(self.update(), self.context)
        stale = self.update(callback="tx:confirm:old")
        await handlers.confirm_transfer(stale, self.context)
        self.assertIn("устарело", stale.callback_query.edit_message_text.call_args.args[0])

    async def test_search_invalid_missing_genesis_and_large_id(self):
        for text in ("abc", "-1"):
            self.assertEqual(
                await handlers.receive_search_id(self.update(text=text), self.context),
                handlers.SEARCH_ID,
            )
        for text in ("9999", str(2**70)):
            update = self.update(text=text)
            await handlers.receive_search_id(update, self.context)
            self.assertIn("не найдена", update.effective_message.reply_text.call_args.args[0])
        genesis = self.update(text="0")
        await handlers.receive_search_id(genesis, self.context)
        self.assertIn("генезис", genesis.effective_message.reply_text.call_args.args[0])

    async def test_admin_grant_flow_and_denied_callbacks(self):
        service.register_player(1, "Alice_1")
        await handlers.begin_admin_grant(self.update(9, callback="admin:grant"), self.context)
        await handlers.select_admin_recipient(
            self.update(9, callback="grant:user:alice_1"), self.context
        )
        await handlers.choose_admin_amount(self.update(9, callback="grant:amount:50"), self.context)
        key = self.context.user_data["active_intent"]["key"]
        await handlers.confirm_admin_grant(self.update(9, callback=f"grant:confirm:{key}"), self.context)
        self.assertEqual(service.get_player_balance(1).balance, 150)
        for handler, callback in (
            (handlers.admin_panel, "admin:panel"),
            (handlers.show_admin_users, "admin:users:0"),
            (handlers.begin_admin_grant, "admin:grant"),
            (handlers.admin_grant_page, "grant:page:0"),
            (handlers.choose_admin_amount, "grant:amount:10"),
            (handlers.confirm_admin_grant, "grant:confirm:old"),
            (handlers.expired_action, "grant:confirm:old"),
        ):
            update = self.update(callback=callback)
            await handler(update, self.context)
            self.assertIn("прав", update.callback_query.edit_message_text.call_args.args[0])
