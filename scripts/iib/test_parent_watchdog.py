import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from scripts.iib.parent_watchdog import _wait_macos, _watch


class ParentWatchdogExitTest(unittest.TestCase):
    def test_closed_desktop_log_pipe_does_not_prevent_exit(self):
        with patch("sys.platform", "darwin"), \
             patch("scripts.iib.parent_watchdog._wait_macos"), \
             patch("scripts.iib.parent_watchdog.tlog", side_effect=BrokenPipeError), \
             patch("os._exit", side_effect=SystemExit) as exit_process:
            with self.assertRaises(SystemExit):
                _watch(123)
            exit_process.assert_called_once_with(0)


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS process events")
class MacParentWatchdogTest(unittest.TestCase):
    def test_tracks_a_process_that_is_not_our_direct_parent(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        finished = threading.Event()
        errors = []

        def wait():
            try:
                _wait_macos(process.pid)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=wait, daemon=True)
        worker.start()
        try:
            self.assertFalse(finished.wait(0.3), "A live process was considered dead")
            process.terminate()
            self.assertTrue(finished.wait(5), "The process exit was not observed")
            self.assertEqual(errors, [])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            worker.join(timeout=5)

    def test_process_already_exited_before_registration(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        started = time.monotonic()
        _wait_macos(process.pid)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
