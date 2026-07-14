import threading
import time
import unittest

from utils.call_log_writer import BoundedCallLogWriter


class BoundedCallLogWriterTests(unittest.TestCase):
    def test_queue_is_bounded_and_worker_drains_records(self):
        received = []
        started = threading.Event()
        release = threading.Event()

        def write(record):
            started.set()
            release.wait(timeout=1)
            received.append(record)

        writer = BoundedCallLogWriter(write, max_queue_size=1)
        try:
            self.assertTrue(writer.submit({"id": 1}))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(writer.submit({"id": 2}))
            self.assertFalse(writer.submit({"id": 3}))
            release.set()
            self.assertTrue(writer.flush(timeout=1))
            self.assertEqual(received, [{"id": 1}, {"id": 2}])
        finally:
            writer.stop(timeout=1)

    def test_stop_is_idempotent(self):
        writer = BoundedCallLogWriter(lambda record: None)
        writer.stop(timeout=1)
        writer.stop(timeout=1)


if __name__ == "__main__":
    unittest.main()
