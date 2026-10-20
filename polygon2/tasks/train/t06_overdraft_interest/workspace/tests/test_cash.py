import unittest
from decimal import Decimal

from cash import OverdraftError, apply, close_day


class CashTests(unittest.TestCase):
    def test_payment_within_limit(self):
        self.assertEqual(apply(Decimal("100.00"), "-250.00", "500"), Decimal("-150.00"))

    def test_payment_over_limit_is_rejected(self):
        with self.assertRaises(OverdraftError):
            apply(Decimal("100.00"), "-700.00", "500")

    def test_interest_on_negative_balance(self):
        self.assertEqual(close_day("-2000.00", "0.0005"), (Decimal("-2001.00"), Decimal("1.00")))

    def test_no_interest_on_positive_balance(self):
        self.assertEqual(close_day("10.00", "0.001"), (Decimal("10.00"), Decimal("0.00")))


if __name__ == "__main__":
    unittest.main()
