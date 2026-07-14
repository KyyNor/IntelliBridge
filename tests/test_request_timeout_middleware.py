import asyncio
import unittest

from utils.timeouts import get_remaining_timeout
from utils.middleware import RequestTimeoutMiddleware


class RequestTimeoutMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_http_request_gets_504(self):
        async def slow_app(scope, receive, send):
            await asyncio.sleep(0.05)

        messages = []

        async def send(message):
            messages.append(message)

        middleware = RequestTimeoutMiddleware(slow_app, timeout_seconds=0.01)
        await middleware({"type": "http"}, lambda: None, send)

        self.assertEqual(messages[0]["status"], 504)
        self.assertIn("超时", messages[1]["body"].decode("utf-8"))

    async def test_deadline_is_visible_to_downstream_app(self):
        observed = None

        async def app(scope, receive, send):
            nonlocal observed
            observed = get_remaining_timeout()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"ok"})

        messages = []
        middleware = RequestTimeoutMiddleware(app, timeout_seconds=0.2)

        async def send(message):
            messages.append(message)

        await middleware({"type": "http"}, lambda: None, send)

        self.assertEqual(messages[0]["status"], 200)
        self.assertIsNotNone(observed)
        self.assertLessEqual(observed, 0.2)


if __name__ == "__main__":
    unittest.main()
