import unittest

from warehouse import fit_order, fit_orders, fits, free_space, occupied


class OccupiedTests(unittest.TestCase):
    def test_all_three_parts_count(self):
        self.assertEqual(occupied(30, 20, 10), 60)
        self.assertEqual(occupied(0, 0, 7), 7)

    def test_zero_is_allowed(self):
        self.assertEqual(occupied(0, 0, 0), 0)
        self.assertEqual(free_space(0, 0, 0, 0), 0)
        self.assertEqual(fit_order(0, 10, 0, 0, 0), (0, 0))

    def test_negative_values_rejected(self):
        for args in ((-1, 0, 0), (0, -1, 0), (0, 0, -1)):
            with self.assertRaises(ValueError):
                occupied(*args)


class FreeSpaceTests(unittest.TestCase):
    def test_free_space(self):
        self.assertEqual(free_space(100, 30, 20, 10), 40)

    def test_overfull_store_has_no_space(self):
        self.assertEqual(free_space(50, 40, 20, 5), 0)
        self.assertEqual(fit_order(10, 50, 40, 20, 5), (0, 10))
        self.assertFalse(fits(1, 50, 40, 20, 5))

    def test_negative_capacity_rejected(self):
        with self.assertRaises(ValueError):
            free_space(-1, 0, 0, 0)


class FitTests(unittest.TestCase):
    def test_fits_boundary(self):
        self.assertTrue(fits(40, 100, 30, 20, 10))
        self.assertFalse(fits(41, 100, 30, 20, 10))
        self.assertTrue(fits(0, 60, 30, 20, 10))

    def test_fit_order_whole_and_cut(self):
        self.assertEqual(fit_order(15, 100, 30, 20, 10), (15, 0))
        self.assertEqual(fit_order(50, 100, 30, 20, 10), (40, 10))

    def test_negative_order_rejected(self):
        with self.assertRaises(ValueError):
            fit_order(-5, 100, 0, 0, 0)
        with self.assertRaises(ValueError):
            fits(-5, 100, 0, 0, 0)


class FitOrdersTests(unittest.TestCase):
    def test_orders_take_space_in_list_order(self):
        self.assertEqual(fit_orders([8, 3], 10, 0, 0, 0), [8, 2])

    def test_every_order_gets_a_result(self):
        self.assertEqual(fit_orders([6, 6, 4, 0], 10, 0, 0, 0), [6, 4, 0, 0])

    def test_accepted_part_takes_space(self):
        self.assertEqual(fit_orders([5, 5, 5], 12, 0, 0, 0), [5, 5, 2])

    def test_orders_not_changed(self):
        orders = [7, 5]
        fit_orders(orders, 10, 0, 0, 0)
        self.assertEqual(orders, [7, 5])


if __name__ == "__main__":
    unittest.main()
