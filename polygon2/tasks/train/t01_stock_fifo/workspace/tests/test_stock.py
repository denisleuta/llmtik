import unittest

from stock import add_lot, consume, expire, total


class StockTests(unittest.TestCase):
    def test_add_lot_sets_last_day(self):
        lots = add_lot([], day=3, qty=10, shelf_life=2)
        self.assertEqual(lots, [{"day": 3, "qty": 10, "expires": 4}])

    def test_consume_sells_requested_amount(self):
        lots = add_lot(add_lot([], 1, 10, 5), 2, 10, 5)
        rest, sold = consume(lots, 15)
        self.assertEqual(sold, 15)
        self.assertEqual(total(rest), 5)

    def test_expire_removes_old_lots(self):
        lots = add_lot([], day=1, qty=7, shelf_life=1)
        rest, spoiled = expire(lots, today=2)
        self.assertEqual((rest, spoiled), ([], 7))


if __name__ == "__main__":
    unittest.main()
