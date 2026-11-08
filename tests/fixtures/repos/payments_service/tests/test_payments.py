import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from payments import api, currency, ledger, refunds, utils  # noqa: E402


class PaymentsTests(unittest.TestCase):
    def test_refund_amount(self):
        self.assertEqual(refunds.refund_amount({"total": 10.0, "coupon": 2.5}), (7.5, "customer_request"))

    def test_parse_refund(self):
        self.assertEqual(refunds.parse_refund({"order_id": "o1", "amount": "3"}), {"order_id": "o1", "amount": 3.0})
        self.assertIsNone(refunds.parse_refund({}))

    def test_convert(self):
        self.assertEqual(currency.convert(10, "USD", "EUR"), 9.2)

    def test_add_entry(self):
        self.assertEqual(ledger.add_entry({"amount": 1}, []), [{"amount": 1}])

    def test_handle_refund(self):
        self.assertEqual(api.handle_refund({"order_id": "o1"})["status"], "ok")

    def test_first_or_none(self):
        self.assertIsNone(utils.first_or_none([]))
        self.assertEqual(utils.first_or_none([3]), 3)


if __name__ == "__main__":
    unittest.main()
