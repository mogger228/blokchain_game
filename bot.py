"""Точка входа Telegram-бота учебного блокчейна."""

from __future__ import annotations

import logging
import sys

from telegram.ext import Application, ApplicationBuilder, ContextTypes

import service
import storage
from bot_handlers import register_handlers
from config import BotConfig, ConfigurationError, load_bot_config


LOGGER = logging.getLogger(__name__)


async def error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Необработанная ошибка Telegram update", exc_info=context.error)


def build_application(config: BotConfig) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.token)
        .concurrent_updates(False)
        .build()
    )
    application.bot_data["admin_id"] = config.admin_id
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTP-клиенты библиотеки не должны печатать URL с bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = load_bot_config()
        storage.configure_db_path(config.db_path)
        service.initialize()
        application = build_application(config)
        application.run_polling(drop_pending_updates=False)
        return 0
    except ConfigurationError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

