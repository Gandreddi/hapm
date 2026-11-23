import unittest

from inventory.stock import available


class StockTest(unittest.TestCase):
    def test_unknown_sku(self) -> None:
        self.assertEqual(available("none"), 0)
