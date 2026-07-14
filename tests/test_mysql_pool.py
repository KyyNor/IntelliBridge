import threading
import time
import sys
import types
import unittest

# Keep the focused unit test runnable in the lightweight review environment;
# production installs provide these modules from requirements.txt.
pymysql_stub = types.ModuleType("pymysql")
pymysql_stub.cursors = types.SimpleNamespace(DictCursor=object)
dbutils_stub = types.ModuleType("dbutils")
pooled_db_stub = types.ModuleType("dbutils.pooled_db")
pooled_db_stub.PooledDB = object
sys.modules.setdefault("pymysql", pymysql_stub)
sys.modules.setdefault("dbutils", dbutils_stub)
sys.modules.setdefault("dbutils.pooled_db", pooled_db_stub)
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

from utils.mysql_pool import MySQLConnectionPool


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        return None


class _Connection:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return _Cursor()

    def close(self):
        self.closed = True


class _Pool:
    def __init__(self):
        self.connections = []

    def connection(self):
        conn = _Connection()
        self.connections.append(conn)
        return conn


def _pool_for_test(acquire_timeout=0.05):
    pool = MySQLConnectionPool.__new__(MySQLConnectionPool)
    pool.nodes_config = [{"name": "node", "pool_acquire_timeout": acquire_timeout}]
    pool._db_to_node_map = {
        "node_db": {"node": "node", "database": "db", "description": ""}
    }
    pool._pools = {"node": _Pool()}
    pool._pool_slots = {"node": threading.BoundedSemaphore(1)}
    pool._pool_acquire_timeouts = {"node": acquire_timeout}
    return pool


class MySQLPoolTests(unittest.TestCase):
    def test_connection_slot_is_released_after_context_exit(self):
        pool = _pool_for_test()

        with pool.get_connection("node_db") as connection:
            self.assertFalse(connection.closed)

        self.assertTrue(connection.closed)
        acquired = pool._pool_slots["node"].acquire(timeout=0.01)
        self.assertTrue(acquired)
        pool._pool_slots["node"].release()

    def test_second_connection_times_out_instead_of_waiting_forever(self):
        pool = _pool_for_test(acquire_timeout=0.05)
        entered = threading.Event()
        release = threading.Event()

        def hold_connection():
            with pool.get_connection("node_db"):
                entered.set()
                release.wait(timeout=1)

        worker = threading.Thread(target=hold_connection)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            with pool.get_connection("node_db"):
                pass
        elapsed = time.monotonic() - started

        release.set()
        worker.join(timeout=1)
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
