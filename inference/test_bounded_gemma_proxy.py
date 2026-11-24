import asyncio
import unittest
from unittest.mock import patch
import httpx
from inference import bounded_gemma_proxy as gate


class GateTest(unittest.IsolatedAsyncioTestCase):
    async def test_limits_requests_and_releases_after_stream(self):
        count = 0
        peak = 0
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'ok'
        async def respond(request):
            nonlocal count, peak
            count += 1
            peak = max(peak, count)
            await asyncio.sleep(0.02)
            count -= 1
            return httpx.Response(200, stream=Bytes())
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
            upstream.base_url = 'http://test'
            with patch.object(gate, 'upstream', upstream), patch.object(gate, 'slots', asyncio.Semaphore(7)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate.app), base_url='http://test') as client:
                    responses = await asyncio.gather(*[client.post('/v1/chat/completions', json={}) for _ in range(20)])
                self.assertTrue(all(r.content == b'ok' for r in responses))
                self.assertEqual(peak, 7)
                self.assertEqual(gate.active, 0)
