import sys
import threading
import types
import unittest

pyhive_stub = types.ModuleType("pyhive")
hive_stub = types.ModuleType("pyhive.hive")
hive_stub.Connection = object
pyhive_stub.hive = hive_stub
sys.modules.setdefault("pyhive", pyhive_stub)
sys.modules.setdefault("pyhive.hive", hive_stub)

loguru_stub = types.ModuleType("loguru")
loguru_stub.logger = types.SimpleNamespace(
    remove=lambda *args, **kwargs: None,
    add=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
    exception=lambda *args, **kwargs: None,
    debug=lambda *args, **kwargs: None,
)
sys.modules.setdefault("loguru", loguru_stub)

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda stream: {}
sys.modules.setdefault("yaml", yaml_stub)

from utils.hive_pool import HiveConnectionPool


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class HivePoolTests(unittest.TestCase):
    def _new_pool(self, queue_timeout=0.05):
        created = []

        def factory(**kwargs):
            connection = _Connection()
            created.append(connection)
            return connection

        pool = HiveConnectionPool(
            max_concurrent=2,
            queue_timeout=queue_timeout,
            connection_factory=factory,
        )
        return pool, created

    def test_allows_two_leases_and_times_out_the_third(self):
        pool, _ = self._new_pool()
        entered = threading.Barrier(3)
        release = threading.Event()
        errors = []

        def hold_lease():
            try:
                with pool.get_connection():
                    entered.wait(timeout=1)
                    release.wait(timeout=1)
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=hold_lease) for _ in range(2)]
        for worker in workers:
            worker.start()
        entered.wait(timeout=1)

        with self.assertRaises(TimeoutError):
            with pool.get_connection():
                pass

        release.set()
        for worker in workers:
            worker.join(timeout=1)
        self.assertEqual(errors, [])

    def test_connection_is_closed_and_lease_released_on_failure(self):
        pool, created = self._new_pool()

        with self.assertRaisesRegex(RuntimeError, "query failed"):
            with pool.get_connection() as connection:
                raise RuntimeError("query failed")

        self.assertTrue(created[0].closed)
        with pool.get_connection() as connection:
            self.assertFalse(connection.closed)
        self.assertTrue(created[1].closed)


if __name__ == "__main__":
    unittest.main()
