import unittest
from decimal import Decimal

from tiers import discount_for, next_tier, order_total, validate_scale

SCALE = [(100, 5), (500, 10), (1000, 15)]


class DiscountTests(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        self.assertEqual(discount_for(500, SCALE), 10)
        self.assertEqual(discount_for(499, SCALE), 5)
        self.assertEqual(discount_for(100, SCALE), 5)

    def test_largest_reached_step_wins(self):
        self.assertEqual(discount_for(600, SCALE), 10)
        self.assertEqual(discount_for(5000, SCALE), 15)

    def test_below_all_thresholds(self):
        self.assertEqual(discount_for(99, SCALE), 0)
        self.assertEqual(discount_for(0, SCALE), 0)
        self.assertEqual(discount_for(10, []), 0)

    def test_scale_order_does_not_matter(self):
        shuffled = [(1000, 15), (100, 5), (500, 10)]
        self.assertEqual(discount_for(600, shuffled), 10)
        self.assertEqual(validate_scale(shuffled), SCALE)

    def test_scale_not_changed(self):
        shuffled = [(1000, 15), (100, 5), (500, 10)]
        discount_for(600, shuffled)
        self.assertEqual(shuffled, [(1000, 15), (100, 5), (500, 10)])

    def test_negative_qty(self):
        with self.assertRaises(ValueError):
            discount_for(-1, SCALE)
        with self.assertRaises(ValueError):
            next_tier(-1, SCALE)


class ScaleRulesTests(unittest.TestCase):
    def test_equal_discount_allowed(self):
        self.assertEqual(validate_scale([(200, 5), (100, 5)]), [(100, 5), (200, 5)])

    def test_bad_scales(self):
        for scale in ([(100, 5), (100, 7)], [(100, 10), (500, 5)], [(100, 100)],
                      [(100, -1)], [(0, 5)]):
            with self.assertRaises(ValueError, msg=str(scale)):
                validate_scale(scale)

    def test_limits_allowed(self):
        self.assertEqual(validate_scale([(1, 0), (2, 99)]), [(1, 0), (2, 99)])


class TotalTests(unittest.TestCase):
    def test_discount_on_whole_volume(self):
        self.assertEqual(order_total(500, "2.00", SCALE), Decimal("900.00"))
        self.assertEqual(order_total(50, Decimal("2.00"), SCALE), Decimal("100.00"))

    def test_half_kopeck_up(self):
        self.assertEqual(order_total(1, "0.25", [(1, 10)]), Decimal("0.23"))

    def test_rounded_once(self):
        self.assertEqual(order_total(3, "0.25", [(1, 10)]), Decimal("0.68"))


class NextTierTests(unittest.TestCase):
    def test_next_tier(self):
        self.assertEqual(next_tier(450, SCALE), (500, 50))
        self.assertEqual(next_tier(0, SCALE), (100, 100))

    def test_on_threshold_points_to_next(self):
        self.assertEqual(next_tier(500, SCALE), (1000, 500))

    def test_top_or_empty(self):
        self.assertIsNone(next_tier(1000, SCALE))
        self.assertIsNone(next_tier(3, []))


if __name__ == "__main__":
    unittest.main()
