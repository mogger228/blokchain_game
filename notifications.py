"""Фоновая доставка уведомлений о зачислениях из SQLite."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from telegram import LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

import storage
from formatters import format_amount


LOGGER = logging.getLogger(__name__)
TASK_KEY = "money_notification_task"


def notification_text(notification: storage.MoneyNotification) -> str:
    text = (
        "💰 Деньги поступили на баланс!\n"
        f"От кого: {notification.sender_label[:128]}\n"
        f"Сумма: ${format_amount(notification.amount)}\n"
        f"Транзакция #{notification.block_index}"
    )
    if notification.message:
        text += f"\n\nСообщение отправителя:\n{notification.message}"
    return text


async def deliver_next_notification(bot) -> bool:
    notification = storage.claim_money_notification()
    if notification is None:
        return False
    try:
        await bot.send_message(
            chat_id=notification.recipient_id,
            text=notification_text(notification),
            parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except (Forbidden, BadRequest) as exc:
        storage.finish_money_notification(notification, "failed")
        LOGGER.warning(
            "Уведомление о транзакции #%s не доставлено: %s",
            notification.block_index, type(exc).__name__,
        )
    except RetryAfter as exc:
        delay = exc.retry_after
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        storage.finish_money_notification(notification, "pending", retry_after=seconds + 1)
        # Ограничение Telegram может действовать на весь бот, а не только на один чат.
        await asyncio.sleep(seconds + 1)
    except TelegramError as exc:
        delay = min(300, 5 * 2 ** min(notification.attempts - 1, 6))
        storage.finish_money_notification(notification, "pending", retry_after=delay)
        LOGGER.warning(
            "Сбой доставки уведомления #%s (%s); повтор через %s с.",
            notification.block_index, type(exc).__name__, delay,
        )
    else:
        storage.finish_money_notification(notification, "sent")
        LOGGER.info("Уведомление о транзакции #%s доставлено", notification.block_index)
    return True


async def notification_worker(bot) -> None:
    while True:
        try:
            delivered = await deliver_next_notification(bot)
        except Exception:
            LOGGER.exception("Ошибка очереди уведомлений")
            delivered = False
        if not delivered:
            await asyncio.sleep(2)
        else:
            # Уступаем цикл обработки другим задачам даже при большой очереди.
            await asyncio.sleep(0)


async def start_notifications(application) -> None:
    application.bot_data[TASK_KEY] = asyncio.create_task(
        notification_worker(application.bot), name="money-notifications",
    )


async def stop_notifications(application) -> None:
    task = application.bot_data.pop(TASK_KEY, None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
