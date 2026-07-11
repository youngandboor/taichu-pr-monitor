import pathlib
import tempfile
import unittest

from monitor.instance_lock import InstanceAlreadyRunning, InstanceLock


class InstanceLockTest(unittest.TestCase):
    def test_second_lock_for_same_state_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = pathlib.Path(temp_dir) / "monitor.sqlite3"
            first = InstanceLock(state_path)
            second = InstanceLock(state_path)
            first.acquire()
            try:
                with self.assertRaises(InstanceAlreadyRunning):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_lock_file_records_current_process_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = pathlib.Path(temp_dir) / "monitor.sqlite3"
            with InstanceLock(state_path) as lock:
                self.assertTrue(lock.path.read_text(encoding="ascii").strip().isdigit())


if __name__ == "__main__":
    unittest.main()
