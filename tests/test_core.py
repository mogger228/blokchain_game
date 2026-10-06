from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import service
import storage
from blockchain import create_genesis_block, create_new_block
from config import ConfigurationError, resolve_db_path
from formatters import split_chain


PROJECT_DIR = Path(__file__).resolve().parents[1]


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chain.db"
        storage.configure_db_path(self.db_path)

    def tearDown(self):
        storage.configure_db_path(None)
        self.temp_dir.cleanup()

    def initialize(self):
        service.initialize()


class StorageTests(TemporaryDatabaseTest):
    def test_missing_database_check_does_not_create_file(self):
        with self.assertRaises(service.DatabaseNotInitializedError):
            service.require_existing_database()
        self.assertFalse(self.db_path.exists())

    def test_init_is_idempotent_and_creates_all_tables(self):
        self.assertTrue(service.initialize())
        self.assertFalse(service.initialize())
        self.assertEqual(len(storage.get_all_blocks()), 1)
        conn = storage.get_connection()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertTrue({"blocks", "players", "processed_actions"} <= tables)

    def test_upgrade_old_database_preserves_genesis(self):
        genesis = create_genesis_block()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE blocks ("index" INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, '
            "sender TEXT NOT NULL, recipient TEXT NOT NULL, amount REAL NOT NULL, "
            "previous_hash TEXT NOT NULL, hash TEXT NOT NULL)"
        )
        conn.execute(
            'INSERT INTO blocks ("index", timestamp, sender, recipient, amount, previous_hash, hash) '
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                genesis.index,
                genesis.timestamp,
                genesis.sender,
                genesis.recipient,
                genesis.amount,
                genesis.previous_hash,
                genesis.hash,
            ),
        )
        conn.commit()
        conn.close()

        self.assertFalse(service.initialize())
        blocks = storage.get_all_blocks()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].hash, genesis.hash)

    def test_concurrent_init_has_one_genesis(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: service.initialize(), range(2)))
        self.assertEqual(len(storage.get_all_blocks()), 1)

    def test_legacy_save_block_with_passed_connection_commits(self):
        self.initialize()
        block = create_new_block(storage.get_last_block(), "system", "Legacy", 5)
        conn = storage.get_connection()
        storage.save_block(block, conn)
        conn.close()
        self.assertEqual(storage.get_last_block().hash, block.hash)

    def test_upgrade_old_code_and_reupgrade_preserve_state(self):
        self.initialize()
        service.register_player(1, "Alice_1")
        service.register_player(2, "Bob__22")
        original = service.create_player_transaction(1, "Bob__22", 10, "kept-receipt")
        conn = storage.get_connection()
        before_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        before_actions = conn.execute("SELECT COUNT(*) FROM processed_actions").fetchone()[0]
        old_rows = conn.execute('SELECT "index", hash FROM blocks ORDER BY "index"').fetchall()
        legacy_block = create_new_block(storage._get_last_block(conn), "system", "Legacy", 5)
        storage.save_block(legacy_block, conn)
        conn.close()

        self.assertGreater(len(old_rows), 0)
        self.assertFalse(service.initialize())
        repeated_registration = service.register_player(1, None)
        repeated_action = service.create_player_transaction(1, "Bob__22", 10, "kept-receipt")
        self.assertFalse(repeated_registration.created)
        self.assertEqual(repeated_action.index, original.index)
        conn = storage.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM players").fetchone()[0], before_players)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM processed_actions").fetchone()[0],
                before_actions,
            )
        finally:
            conn.close()


class ServiceTests(TemporaryDatabaseTest):
    def setUp(self):
        super().setUp()
        self.initialize()

    def register_pair(self):
        alice = service.register_player(1, "Alice_1")
        bob = service.register_player(2, "Bob__22")
        return alice, bob

    def test_registration_is_idempotent_and_starts_at_100(self):
        first = service.register_player(1, "Alice_1")
        second = service.register_player(1, None)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.player, second.player)
        self.assertEqual(service.get_player_balance(1).balance, 100.0)
        self.assertEqual(len(service.get_chain()), 2)

    def test_username_is_unique_case_insensitively(self):
        service.register_player(1, "Alice_1")
        with self.assertRaises(service.ValidationError):
            service.register_player(2, "ALICE_1")

    def test_transfer_and_idempotent_retry(self):
        self.register_pair()
        block = service.create_player_transaction(1, "@bob__22", 25, "same-key")
        retry = service.create_player_transaction(1, "BOB__22", 25.0, "same-key")
        self.assertEqual(block.index, retry.index)
        self.assertEqual(service.get_player_balance(1).balance, 75.0)
        self.assertEqual(service.get_player_balance(2).balance, 125.0)
        self.assertEqual(len(service.get_chain()), 4)

    def test_reused_key_with_changed_request_is_rejected(self):
        self.register_pair()
        service.register_player(3, "Carol_3")
        service.create_player_transaction(1, "Bob__22", 10, "reused")
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_player_transaction(1, "Carol_3", 10, "reused")

    def test_receipt_conflict_is_checked_before_current_recipient(self):
        self.register_pair()
        original = service.create_player_transaction(1, "Bob__22", 10, "receipt-order")
        conn = storage.get_connection()
        conn.execute("DELETE FROM players WHERE telegram_id = 2")
        conn.commit()
        conn.close()
        self.assertEqual(
            service.create_player_transaction(1, "Bob__22", 10, "receipt-order").index,
            original.index,
        )
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_player_transaction(1, "Nobody_9", 10, "receipt-order")

    def test_invalid_amounts_and_self_transfer(self):
        self.register_pair()
        for value in (0, -1, float("nan"), float("inf"), "not-a-number"):
            with self.subTest(value=value), self.assertRaises(service.ValidationError):
                service.create_player_transaction(1, "Bob__22", value, f"key-{value}")
        with self.assertRaises(service.ValidationError):
            service.create_player_transaction(1, "Alice_1", 1, "self")

    def test_insufficient_funds(self):
        self.register_pair()
        with self.assertRaises(service.InsufficientFundsError):
            service.create_player_transaction(1, "Bob__22", 101, "too-much")

    def test_admin_grant_requires_configured_id_and_is_idempotent(self):
        self.register_pair()
        with self.assertRaises(service.ForbiddenError):
            service.create_admin_grant(8, "Alice_1", 50, "grant-x", 9)
        block = service.create_admin_grant(9, "Alice_1", 50, "grant-ok", 9)
        retry = service.create_admin_grant(9, "Alice_1", 50, "grant-ok", 9)
        self.assertEqual(block.index, retry.index)
        self.assertEqual(service.get_player_balance(1).balance, 150.0)

    def test_action_key_is_bound_to_actor_type_and_amount(self):
        self.register_pair()
        service.create_player_transaction(1, "Bob__22", 10, "bound-key")
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_player_transaction(2, "Alice_1", 10, "bound-key")
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_player_transaction(1, "Bob__22", 11, "bound-key")
        with self.assertRaises(service.IdempotencyConflictError):
            service.create_admin_grant(9, "Bob__22", 10, "bound-key", 9)

    def test_player_page_uses_balances(self):
        self.register_pair()
        service.create_player_transaction(1, "Bob__22", 25, "page-transfer")
        page = service.list_players_with_balances(0, 10)
        self.assertEqual(page.total, 2)
        self.assertEqual(
            {item.player.username: item.balance for item in page.items},
            {"alice_1": 75.0, "bob__22": 125.0},
        )

    def test_chain_damage_blocks_new_write(self):
        self.register_pair()
        conn = storage.get_connection()
        conn.execute('UPDATE blocks SET amount = 999 WHERE "index" = 1')
        conn.commit()
        conn.close()
        with self.assertRaises(service.ChainIntegrityError):
            service.create_player_transaction(1, "Bob__22", 1, "damaged")

    def test_registration_rolls_back_when_player_insert_fails(self):
        baseline = len(service.get_chain())
        with mock.patch("storage._insert_player", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                service.register_player(1, "Alice_1")
        self.assertEqual(len(service.get_chain()), baseline)
        self.assertIsNone(service.get_player(1))

    def test_transfer_rolls_back_when_receipt_insert_fails(self):
        self.register_pair()
        baseline = len(service.get_chain())
        with mock.patch("storage._insert_action", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                service.create_player_transaction(1, "Bob__22", 25, "broken-receipt")
        self.assertEqual(len(service.get_chain()), baseline)
        self.assertEqual(service.get_player_balance(1).balance, 100.0)

    def test_concurrent_overspend_allows_only_one_transfer(self):
        self.register_pair()
        service.register_player(3, "Carol_3")

        def send(recipient, key):
            return service.create_player_transaction(1, recipient, 80, key)

        outcomes = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(send, "Bob__22", "parallel-1"),
                executor.submit(send, "Carol_3", "parallel-2"),
            ]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except service.InsufficientFundsError as exc:
                    outcomes.append(exc)
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        self.assertEqual(service.get_player_balance(1).balance, 20.0)

    def test_concurrent_registration_conflicts_are_serialized(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(service.register_player, 1, "Same_name"),
                executor.submit(service.register_player, 2, "SAME_NAME"),
            ]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except service.ValidationError as exc:
                    results.append(exc)
        self.assertEqual(sum(isinstance(item, service.RegistrationResult) for item in results), 1)
        self.assertEqual(service.list_players_with_balances().total, 1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            same_id = list(
                executor.map(
                    lambda name: service.register_player(3, name),
                    ("Third_3", "Other_4"),
                )
            )
        self.assertEqual(same_id[0].player, same_id[1].player)
        self.assertEqual(sum(item.created for item in same_id), 1)

    def test_concurrent_same_action_key_returns_one_block(self):
        self.register_pair()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            blocks = list(
                executor.map(
                    lambda _: service.create_player_transaction(
                        1, "Bob__22", 10, "parallel-same-key"
                    ),
                    range(2),
                )
            )
        self.assertEqual(blocks[0].index, blocks[1].index)
        self.assertEqual(service.get_player_balance(1).balance, 90.0)

    def test_concurrent_transfer_grant_and_cli_append(self):
        self.register_pair()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(
                    service.create_player_transaction, 1, "Bob__22", 10, "mixed-transfer"
                ),
                executor.submit(
                    service.create_admin_grant, 9, "Alice_1", 20, "mixed-grant", 9
                ),
                executor.submit(service.create_trusted_transaction, "system", "Legacy", 7),
            ]
            blocks = [future.result() for future in futures]
        self.assertEqual(len({block.index for block in blocks}), 3)
        self.assertEqual(service.get_player_balance(1).balance, 110.0)
        self.assertEqual(service.get_player_balance(2).balance, 110.0)
        self.assertEqual(service.check_chain(), (True, None))

    def test_player_page_uses_one_read_snapshot(self):
        self.register_pair()
        conn = storage.get_connection()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.close()
        original_list = storage._list_players
        triggered = False

        def interleaved_list(conn, limit, offset):
            nonlocal triggered
            rows = original_list(conn, limit, offset)
            if not triggered:
                triggered = True
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(
                        service.create_admin_grant,
                        9,
                        "Alice_1",
                        50,
                        "snapshot-grant",
                        9,
                    ).result()
            return rows

        with mock.patch("storage._list_players", side_effect=interleaved_list):
            page = service.list_players_with_balances()
        balances = {item.player.username: item.balance for item in page.items}
        self.assertEqual(balances["alice_1"], 100.0)
        self.assertEqual(service.get_player_balance(1).balance, 150.0)

    def test_trusted_cli_semantics_and_transaction_lookup(self):
        block = service.create_trusted_transaction("system", "Legacy Alice", 40)
        service.create_trusted_transaction("Legacy Alice", "Legacy Bob", 15)
        self.assertEqual(service.get_named_balance("Legacy Alice"), 25.0)
        self.assertEqual(service.get_transaction(block.index).hash, block.hash)
        self.assertIsNone(service.get_transaction(9999))
        self.assertIsNone(service.get_transaction(2**70))
        self.assertEqual(service.check_chain(), (True, None))

    def test_chain_splitting_keeps_every_block(self):
        self.register_pair()
        chunks = split_chain(service.get_chain(), limit=300)
        combined = "\n".join(chunks)
        for block in service.get_chain():
            self.assertIn(f"Блок #{block.index}", combined)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))


class ConfigurationTests(unittest.TestCase):
    def test_default_path_is_project_path(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as cwd:
            result = resolve_db_path({}, Path(cwd), Path(project))
            self.assertEqual(result, (Path(project) / "chain.db").resolve())

    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as cwd:
            result = resolve_db_path(
                {"BLOCKCHAIN_DB_PATH": "custom.db"}, Path(cwd), Path(project)
            )
            self.assertEqual(result, (Path(cwd) / "custom.db").resolve())

    def test_legacy_cwd_file_requires_explicit_choice(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "chain.db").touch()
            with self.assertRaises(ConfigurationError):
                resolve_db_path({}, Path(cwd), Path(project))


class CliTests(unittest.TestCase):
    def run_cli(self, db_path: Path, *args: str):
        env = os.environ.copy()
        env["BLOCKCHAIN_DB_PATH"] = str(db_path)
        return subprocess.run(
            [sys.executable, str(PROJECT_DIR / "main.py"), *args],
            cwd=PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_commands_except_init_do_not_create_database(self):
        commands = [
            ("send", "--from", "system", "--to", "Alice", "--amount", "1"),
            ("balance", "--user", "Alice"),
            ("chain",),
            ("validate",),
            ("transaction", "--id", "0"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, command in enumerate(commands):
                db_path = Path(directory) / f"missing-{index}.db"
                result = self.run_cli(db_path, *command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Сначала выполните", result.stderr)
                self.assertFalse(db_path.exists())

    def test_old_cli_flow_and_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cli.db"
            self.assertEqual(self.run_cli(db_path, "init").returncode, 0)
            first = self.run_cli(
                db_path, "send", "--from", "system", "--to", "Alice", "--amount", "100"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cli(
                db_path, "send", "--from", "Alice", "--to", "Bob", "--amount", "25"
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("75.0", self.run_cli(db_path, "balance", "--user", "Alice").stdout)
            self.assertIn("Цепочка валидна", self.run_cli(db_path, "validate").stdout)
            self.assertIn("Блок #2", self.run_cli(db_path, "transaction", "--id", "2").stdout)


if __name__ == "__main__":
    unittest.main()
