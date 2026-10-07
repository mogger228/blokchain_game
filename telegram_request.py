"""Повтор подключения к Telegram до отправки HTTP-запроса."""

from __future__ import annotations

import asyncio
import logging

import httpx
from telegram.error import NetworkError
from telegram.request import HTTPXRequest


LOGGER = logging.getLogger(__name__)


class ResilientTelegramRequest(HTTPXRequest):
    """Повторяет только сбои подключения, когда запрос ещё не отправлен."""

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        for attempt in range(3):
            try:
                return await super().do_request(*args, **kwargs)
            except NetworkError as exc:
                if not isinstance(exc.__cause__, (httpx.ConnectTimeout, httpx.ConnectError)):
                    raise
                if attempt == 2:
                    raise
                LOGGER.warning(
                    "Не удалось подключиться к Telegram (%s). Повтор подключения %s/2.",
                    type(exc.__cause__).__name__, attempt + 1,
                )
                await asyncio.sleep(attempt + 1)
        raise AssertionError("Unreachable")
