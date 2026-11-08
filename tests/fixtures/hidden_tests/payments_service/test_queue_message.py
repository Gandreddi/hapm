"""Hidden test: never served to the agent; added inside the sandbox only when this function is fixed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from payments import notifications  # noqa: E402


class Hidden(unittest.TestCase):
    def test_queue_message_calls_are_independent(self):
        self.assertEqual(notifications.queue_message("a"), ["a"])
        self.assertEqual(notifications.queue_message("b"), ["b"])


if __name__ == "__main__":
    unittest.main()
