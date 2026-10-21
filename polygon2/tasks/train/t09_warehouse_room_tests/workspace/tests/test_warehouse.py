import unittest

from warehouse import fit_order, free_space


class WarehouseTests(unittest.TestCase):
    def test_free_space(self):
        self.assertEqual(free_space(100, 30, 20, 10), 40)

    def test_order_is_cut_to_free_space(self):
        self.assertEqual(fit_order(50, 100, 30, 20, 10), (40, 10))


if __name__ == "__main__":
    unittest.main()
