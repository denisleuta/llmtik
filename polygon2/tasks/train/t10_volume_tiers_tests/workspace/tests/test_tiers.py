import unittest
from decimal import Decimal

from tiers import discount_for, order_total


class TiersTests(unittest.TestCase):
    def test_discount_of_reached_step(self):
        self.assertEqual(discount_for(600, [(100, 5), (500, 10)]), 10)

    def test_total_without_discount(self):
        self.assertEqual(order_total(10, "2.00", [(100, 5)]), Decimal("20.00"))


if __name__ == "__main__":
    unittest.main()
