"""Точка входа Telegram-бота учебного блокчейна."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

import service
import storage
from bot_handlers import register_handlers
from config import PROJECT_DIR, BotConfig, ConfigurationError, load_bot_config
from telegram_request import ResilientTelegramRequest
from notifications import start_notifications, stop_notifications


LOGGER = logging.getLogger(__name__)
LOG_PATH = PROJECT_DIR / "logs" / "bot.log"


def configure_logging(log_path: Path = LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
    # HTTP-клиенты библиотеки не должны печатать URL с bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError) and not isinstance(context.error, BadRequest):
        LOGGER.warning(
            "Сбой связи с Telegram (%s). Если бот не ответил, "
            "повторите последнее действие или отправьте /cancel для выхода из диалога.",
            type(context.error).__name__,
        )
        LOGGER.debug("Подробности сетевого сбоя", exc_info=context.error)
        return
    LOGGER.error("Необработанная ошибка Telegram update", exc_info=context.error)


def build_application(config: BotConfig) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.token)
        .request(ResilientTelegramRequest(
            connect_timeout=15, read_timeout=30, write_timeout=30, pool_timeout=5,
        ))
        .get_updates_request(ResilientTelegramRequest(
            connection_pool_size=1,
            connect_timeout=15, read_timeout=30, write_timeout=30, pool_timeout=5,
        ))
        .concurrent_updates(False)
        .post_init(start_notifications)
        .post_stop(stop_notifications)
        .post_shutdown(stop_notifications)
        .build()
    )
    application.bot_data["admin_id"] = config.admin_id
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application


def main() -> int:
    configure_logging()
    try:
        LOGGER.info("Запуск бота")
        config = load_bot_config()
        storage.configure_db_path(config.db_path)
        service.initialize()
        application = build_application(config)
        application.run_polling(drop_pending_updates=False)
        LOGGER.info("Бот остановлен")
        return 0
    except ConfigurationError as exc:
        LOGGER.error("Ошибка конфигурации: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("Ошибка работы бота")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
