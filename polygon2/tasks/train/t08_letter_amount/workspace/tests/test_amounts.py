import unittest
from decimal import Decimal

from amounts import parse_amount


class ParseAmountTests(unittest.TestCase):
    def test_comma_decimal(self):
        self.assertEqual(parse_amount("1200,50"), Decimal("1200.50"))

    def test_rub_mark(self):
        self.assertEqual(parse_amount("350 руб."), Decimal("350.00"))


if __name__ == "__main__":
    unittest.main()
