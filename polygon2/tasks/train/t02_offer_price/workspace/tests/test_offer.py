import unittest
from decimal import Decimal

from offer import order_cost


class OfferTests(unittest.TestCase):
    def test_without_discount(self):
        self.assertEqual(order_cost(10, 45.5, 300, 0.1), Decimal("455.00"))

    def test_with_discount(self):
        self.assertEqual(order_cost(400, 40, 300, 0.1), Decimal("14400.00"))


if __name__ == "__main__":
    unittest.main()
