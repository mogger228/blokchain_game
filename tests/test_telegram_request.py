"""Проверки повторов сетевых подключений без обращения к Telegram."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, TimedOut

from telegram_request import ResilientTelegramRequest


class TelegramRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_failure_is_retried_then_succeeds(self):
        for error_type in (httpx.ConnectTimeout, httpx.ConnectError):
            with self.subTest(error_type=error_type):
                calls = []

                async def respond(request):
                    calls.append(request)
                    if len(calls) == 1:
                        raise error_type('test connection failure')
                    return httpx.Response(200, content=b'ok')

                request = ResilientTelegramRequest(
                    httpx_kwargs={'transport': httpx.MockTransport(respond)}
                )
                try:
                    with patch('telegram_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                        result = await request.do_request('https://example.test/test', 'POST')
                    self.assertEqual(result, (200, b'ok'))
                    self.assertEqual(len(calls), 2)
                    sleep.assert_awaited_once_with(1)
                finally:
                    await request.shutdown()

    async def test_connection_retries_are_bounded(self):
        respond = AsyncMock(side_effect=httpx.ConnectTimeout('test unavailable'))
        request = ResilientTelegramRequest(
            httpx_kwargs={'transport': httpx.MockTransport(respond)}
        )
        try:
            with patch('telegram_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(TimedOut):
                    await request.do_request('https://example.test/test', 'POST')
            self.assertEqual(respond.await_count, 3)
            self.assertEqual(sleep.await_count, 2)
        finally:
            await request.shutdown()

    async def test_failures_after_connection_are_not_retried(self):
        for error_type in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.PoolTimeout):
            with self.subTest(error_type=error_type):
                respond = AsyncMock(side_effect=error_type('test uncertain response'))
                request = ResilientTelegramRequest(
                    httpx_kwargs={'transport': httpx.MockTransport(respond)}
                )
                try:
                    with patch('telegram_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                        with self.assertRaises(NetworkError):
                            await request.do_request('https://example.test/test', 'POST')
                    self.assertEqual(respond.await_count, 1)
                    sleep.assert_not_awaited()
                finally:
                    await request.shutdown()

    async def test_http_error_response_is_not_retried(self):
        respond = AsyncMock(return_value=httpx.Response(400, content=b'bad request'))
        request = ResilientTelegramRequest(
            httpx_kwargs={'transport': httpx.MockTransport(respond)}
        )
        try:
            self.assertEqual(
                await request.do_request('https://example.test/test', 'POST'),
                (400, b'bad request'),
            )
            self.assertEqual(respond.await_count, 1)
        finally:
            await request.shutdown()
