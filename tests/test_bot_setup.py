from __future__ import annotations

import unittest
from pathlib import Path

from bot import build_application
from config import BotConfig, ConfigurationError, load_bot_config


class BotSetupTests(unittest.TestCase):
    def test_application_contains_admin_id_and_sequential_updates(self):
        config = BotConfig("123456:TEST_TOKEN", 42, Path("test.db"))
        application = build_application(config)
        self.assertEqual(application.bot_data["admin_id"], 42)
        self.assertEqual(application.concurrent_updates, 1)
        self.assertGreaterEqual(len(application.handlers[0]), 1)

    def test_bot_config_validation(self):
        with self.assertRaises(ConfigurationError):
            load_bot_config({"TELEGRAM_ADMIN_ID": "1"})
        with self.assertRaises(ConfigurationError):
            load_bot_config({"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ADMIN_ID": "nope"})


if __name__ == "__main__":
    unittest.main()

