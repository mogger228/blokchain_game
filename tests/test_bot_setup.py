from __future__ import annotations

import unittest
import os
import tempfile
from pathlib import Path
from unittest import mock

from bot import build_application
from notifications import start_notifications, stop_notifications
from config import BotConfig, ConfigurationError, load_bot_config


class BotSetupTests(unittest.TestCase):
    def test_config_reads_project_dotenv_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".env").write_text(
                '# Telegram settings\nTELEGRAM_BOT_TOKEN="123456:FILE_TOKEN"\n'
                'TELEGRAM_ADMIN_ID=42\n',
                encoding="utf-8-sig",
            )
            with mock.patch("config.PROJECT_DIR", project), mock.patch.dict(
                os.environ, {}, clear=True
            ), mock.patch("config.Path.cwd", return_value=project.parent):
                config = load_bot_config()
            self.assertEqual(config.token, "123456:FILE_TOKEN")
            self.assertEqual(config.admin_id, 42)
            self.assertEqual(config.db_path, (project / "chain.db").resolve())

    def test_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=file_token\nTELEGRAM_ADMIN_ID=42\n",
                encoding="utf-8",
            )
            with mock.patch("config.PROJECT_DIR", project), mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "env_token", "TELEGRAM_ADMIN_ID": "99"},
                clear=True,
            ), mock.patch("config.Path.cwd", return_value=project):
                config = load_bot_config()
            self.assertEqual(config.token, "env_token")
            self.assertEqual(config.admin_id, 99)

    def test_explicit_environment_does_not_read_dotenv(self):
        with mock.patch("config.dotenv_values") as read_dotenv:
            config = load_bot_config(
                {"TELEGRAM_BOT_TOKEN": "explicit_token", "TELEGRAM_ADMIN_ID": "7"}
            )
        read_dotenv.assert_not_called()
        self.assertEqual(config.admin_id, 7)

    def test_application_contains_admin_id_and_sequential_updates(self):
        config = BotConfig("123456:TEST_TOKEN", 42, Path("test.db"))
        application = build_application(config)
        self.assertEqual(application.bot_data["admin_id"], 42)
        self.assertEqual(application.concurrent_updates, 1)
        self.assertGreaterEqual(len(application.handlers[0]), 1)
        self.assertIs(application.post_init, start_notifications)
        self.assertIs(application.post_stop, stop_notifications)
        self.assertIs(application.post_shutdown, stop_notifications)

    def test_bot_config_validation(self):
        with self.assertRaises(ConfigurationError):
            load_bot_config({"TELEGRAM_ADMIN_ID": "1"})
        with self.assertRaises(ConfigurationError):
            load_bot_config({"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ADMIN_ID": "nope"})


if __name__ == "__main__":
    unittest.main()
