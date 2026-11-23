import unittest

from orders.service import OrderService


class OrderServiceTest(unittest.TestCase):
    def test_service_exists(self) -> None:
        self.assertTrue(OrderService())
