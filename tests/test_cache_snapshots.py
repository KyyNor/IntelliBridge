import threading
import unittest

from utils.cache_snapshot import AtomicSnapshot


class AtomicSnapshotTests(unittest.TestCase):
    def test_replace_publishes_one_complete_value(self):
        snapshot = AtomicSnapshot(("old-cache", "old-index"))
        observed = []
        start = threading.Event()

        def reader():
            start.wait(timeout=1)
            for _ in range(1000):
                observed.append(snapshot.get())

        thread = threading.Thread(target=reader)
        thread.start()
        start.set()
        snapshot.replace(("new-cache", "new-index"))
        thread.join(timeout=1)

        self.assertTrue(observed)
        self.assertTrue(all(value in {
            ("old-cache", "old-index"),
            ("new-cache", "new-index"),
        } for value in observed))


if __name__ == "__main__":
    unittest.main()
