"""Уведомления, атомарность и обновление старой схемы на временных БД."""

import asyncio
import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

import notifications
import service
import storage


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / 'test.db'
        storage.configure_db_path(self.db_path)
        service.initialize()
        service.register_player(1, 'Alice_1')
        service.register_player(2, 'Bob__22')
        self.bot = SimpleNamespace(send_message=AsyncMock())

    def tearDown(self):
        storage.configure_db_path(None)
        self.directory.cleanup()

    def rows(self):
        conn = storage.get_connection()
        try:
            return conn.execute(
                'SELECT block_index, message, status, attempts, next_attempt_at '
                'FROM money_notifications ORDER BY block_index'
            ).fetchall()
        finally:
            conn.close()

    async def test_transfer_notifies_recipient_with_message_once(self):
        message = 'Спасибо! 💛\n<b>Текст без HTML</b>'
        block = service.create_player_transaction(1, 'Bob__22', 25, 'transfer', message)
        repeated = service.create_player_transaction(1, 'Bob__22', 25, 'transfer', message)
        self.assertEqual(block.index, repeated.index)
        self.assertEqual(storage.get_transaction_message(block.index), message)
        self.assertTrue(await notifications.deliver_next_notification(self.bot))
        self.assertFalse(await notifications.deliver_next_notification(self.bot))
        args = self.bot.send_message.call_args.kwargs
        self.assertEqual(args['chat_id'], 2)
        self.assertIn('@alice_1', args['text'])
        self.assertIn('$25', args['text'])
        self.assertIn(message, args['text'])
        self.assertIn(f'#{block.index}', args['text'])
        self.assertIsNone(args['parse_mode'])
        self.assertTrue(args['link_preview_options'].is_disabled)
        self.assertEqual(self.rows()[0][2], 'sent')
        self.assertEqual(service.get_player_balance(1).balance, 75)
        self.assertEqual(service.get_player_balance(2).balance, 125)
        self.assertEqual(service.check_chain(), (True, None))

    async def test_admin_grant_notification(self):
        service.register_player(9, 'Admin_9')
        block = service.create_admin_grant(9, 'Bob__22', 50, 'grant', 9)
        service.create_admin_grant(9, 'Bob__22', 50, 'grant', 9)
        await notifications.deliver_next_notification(self.bot)
        args = self.bot.send_message.call_args.kwargs
        self.assertEqual(args['chat_id'], 2)
        self.assertIn('Администратор @admin_9', args['text'])
        self.assertIn('$50', args['text'])
        self.assertEqual(self.rows()[0][0], block.index)
        self.assertEqual(len(self.rows()), 1)

    async def test_message_changes_conflict_and_invalid_message_has_no_effect(self):
        original = service.create_player_transaction(1, 'Bob__22', 10, 'same', 'Первое')
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_player_transaction(1, 'Bob__22', 10, 'same', 'Другое')
        for invalid in ('x' * 1001, 123):
            with self.assertRaises(service.ValidationError):
                service.create_player_transaction(1, 'Bob__22', 10, 'invalid', invalid)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(storage.get_transaction_message(original.index), 'Первое')
        self.assertEqual(service.get_player_balance(1).balance, 90)

    async def test_empty_message_remains_compatible_with_old_receipt(self):
        block = service.create_player_transaction(1, 'Bob__22', 10, 'same')
        self.assertEqual(
            service.create_player_transaction(1, 'Bob__22', 10, 'same', '  ').index,
            block.index,
        )
        self.assertEqual(len(self.rows()), 1)

    async def test_notice_failure_rolls_back_block_receipt_and_balances(self):
        before = storage.get_last_block()
        with patch('storage._insert_money_notification', side_effect=RuntimeError('test insert')):
            with self.assertRaises(RuntimeError):
                service.create_player_transaction(1, 'Bob__22', 25, 'broken', 'Test')
        self.assertEqual(storage.get_last_block().hash, before.hash)
        self.assertEqual(service.get_player_balance(1).balance, 100)
        self.assertEqual(service.get_player_balance(2).balance, 100)
        self.assertEqual(self.rows(), [])
        # Откат должен позволять использовать тот же ключ при новой попытке.
        service.create_player_transaction(1, 'Bob__22', 25, 'broken', 'Test')
        self.assertEqual(len(self.rows()), 1)

    async def test_insufficient_balance_does_not_queue_notification(self):
        with self.assertRaises(service.InsufficientFundsError):
            service.create_player_transaction(1, 'Bob__22', 101, 'too-much', 'Test')
        self.assertEqual(self.rows(), [])
        self.bot.send_message.assert_not_awaited()

    async def test_network_failure_retries_later_and_survives_restart(self):
        block = service.create_player_transaction(1, 'Bob__22', 10, 'retry', 'Hello')
        self.bot.send_message.side_effect = TimedOut()
        await notifications.deliver_next_notification(self.bot)
        self.assertEqual(self.rows()[0][2], 'pending')
        self.assertFalse(await notifications.deliver_next_notification(self.bot))
        self.assertEqual(service.get_player_balance(2).balance, 110)
        storage.configure_db_path(self.db_path)
        service.initialize()
        self.bot.send_message.side_effect = None
        with patch('storage.time.time', return_value=time.time() + 10):
            await notifications.deliver_next_notification(self.bot)
        self.assertEqual(self.rows()[0][2], 'sent')
        self.assertEqual(storage.get_transaction_message(block.index), 'Hello')
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_forbidden_and_bad_request_do_not_undo_credit_or_retry(self):
        for error in (Forbidden('blocked'), BadRequest('chat not found')):
            service.create_player_transaction(1, 'Bob__22', 10, str(type(error)))
            self.bot.send_message.side_effect = error
            await notifications.deliver_next_notification(self.bot)
            self.assertFalse(await notifications.deliver_next_notification(self.bot))
        self.assertEqual([row[2] for row in self.rows()], ['failed', 'failed'])
        self.assertEqual(service.get_player_balance(2).balance, 120)

    async def test_rate_limit_schedules_retry(self):
        service.create_player_transaction(1, 'Bob__22', 10, 'rate')
        self.bot.send_message.side_effect = RetryAfter(30)
        before = time.time()
        with patch('notifications.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await notifications.deliver_next_notification(self.bot)
        row = self.rows()[0]
        self.assertEqual(row[2], 'pending')
        self.assertGreaterEqual(row[4], before + 31)
        sleep.assert_awaited_once_with(31)

    async def test_expired_claim_is_recovered_and_old_attempt_cannot_finish_it(self):
        service.create_player_transaction(1, 'Bob__22', 10, 'lease')
        first = storage.claim_money_notification(now=100)
        self.assertIsNone(storage.claim_money_notification(now=399))
        second = storage.claim_money_notification(now=400)
        self.assertEqual(second.attempts, 2)
        storage.finish_money_notification(first, 'sent')
        self.assertEqual(self.rows()[0][2], 'processing')
        storage.finish_money_notification(second, 'sent')
        self.assertEqual(self.rows()[0][2], 'sent')

    async def test_concurrent_same_key_creates_one_notice_and_only_one_claim(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            blocks = list(pool.map(
                lambda _: service.create_player_transaction(1, 'Bob__22', 10, 'parallel', 'Text'),
                range(2),
            ))
            claims = list(pool.map(lambda _: storage.claim_money_notification(), range(2)))
        self.assertEqual(blocks[0].index, blocks[1].index)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    async def test_upgrade_keeps_hashes_receipts_and_does_not_notify_old_transfers(self):
        block = service.create_player_transaction(1, 'Bob__22', 10, 'old')
        conn = storage.get_connection()
        try:
            conn.execute('DROP TABLE money_notifications')
            conn.commit()
            hashes = conn.execute('SELECT "index", hash FROM blocks').fetchall()
        finally:
            conn.close()
        self.assertFalse(service.initialize())
        self.assertFalse(service.initialize())
        self.assertEqual(service.create_player_transaction(1, 'Bob__22', 10, 'old').index, block.index)
        self.assertEqual([(b.index, b.hash) for b in storage.get_all_blocks()], hashes)
        self.assertEqual(self.rows(), [])
        service.create_player_transaction(1, 'Bob__22', 5, 'new')
        self.assertEqual(len(self.rows()), 1)

    async def test_cli_credit_to_registered_account_and_legacy_database(self):
        block = service.create_trusted_transaction('system', '@bob__22', 7)
        self.assertEqual(self.rows()[0][0], block.index)
        conn = storage.get_connection()
        try:
            conn.execute('DROP TABLE money_notifications')
            conn.commit()
        finally:
            conn.close()
        service.create_trusted_transaction('system', '@bob__22', 3)
        self.assertEqual(service.get_player_balance(2).balance, 110)

    async def test_cli_case_mismatch_keeps_block_without_credit_or_notification(self):
        for recipient in ('@BOB__22', '@Bob__22'):
            with self.subTest(recipient=recipient):
                before = storage.get_last_block()
                block = service.create_trusted_transaction('system', recipient, 7)
                self.assertEqual(block.index, before.index + 1)
                self.assertEqual(storage.get_last_block().hash, block.hash)
                self.assertEqual(storage.get_last_block().recipient, recipient)
                self.assertEqual(service.get_player_balance(2).balance, 100)
                self.assertEqual(self.rows(), [])
                self.assertFalse(await notifications.deliver_next_notification(self.bot))
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(service.check_chain(), (True, None))

    async def test_worker_lifecycle(self):
        application = SimpleNamespace(bot=self.bot, bot_data={})
        await notifications.start_notifications(application)
        task = application.bot_data[notifications.TASK_KEY]
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await notifications.stop_notifications(application)
        await notifications.stop_notifications(application)
        self.assertTrue(task.cancelled())
        self.assertNotIn(notifications.TASK_KEY, application.bot_data)
