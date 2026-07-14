import asyncio
import unittest

from utils.timeouts import OperationTimeout, get_remaining_timeout, with_timeout


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_operation_raises_operation_timeout(self):
        @with_timeout(0.01, operation="slow-test")
        async def slow_operation():
            await asyncio.sleep(0.05)

        with self.assertRaisesRegex(OperationTimeout, "slow-test"):
            await slow_operation()

    async def test_nested_operation_keeps_shorter_deadline(self):
        observed = None

        @with_timeout(0.05, operation="outer")
        async def outer_operation():
            nonlocal observed
            observed = get_remaining_timeout()
            await asyncio.sleep(0)

        await outer_operation()
        self.assertIsNotNone(observed)
        self.assertLessEqual(observed, 0.05)


if __name__ == "__main__":
    unittest.main()
