"""Hidden test: never served to the agent; added inside the sandbox only when this function is fixed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from payments import ledger  # noqa: E402


class Hidden(unittest.TestCase):
    def test_add_entry_calls_are_independent(self):
        self.assertEqual(ledger.add_entry({"amount": 1}), [{"amount": 1}])
        self.assertEqual(ledger.add_entry({"amount": 2}), [{"amount": 2}])


if __name__ == "__main__":
    unittest.main()
