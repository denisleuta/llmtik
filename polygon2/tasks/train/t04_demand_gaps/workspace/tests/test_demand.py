import unittest

from demand import daily_totals, moving_average, reorder_qty


def sales(*pairs):
    return [{"day": day, "qty": qty} for day, qty in pairs]


class DemandTests(unittest.TestCase):
    def test_records_of_one_day_are_summed(self):
        history = sales((1, 4), (1, 6), (2, 5))
        self.assertEqual(daily_totals(history), {1: 10, 2: 5})

    def test_average_over_full_window(self):
        history = sales((1, 10), (2, 20), (3, 30), (4, 40))
        self.assertEqual(moving_average(history, day=4, window=3), 30.0)

    def test_window_is_shorter_at_start(self):
        history = sales((1, 6), (2, 9))
        self.assertEqual(moving_average(history, day=2, window=7), 7.5)

    def test_reorder_rounds_need_up(self):
        history = sales((1, 10), (2, 11))
        self.assertEqual(reorder_qty(history, day=2, window=2, stock=5, cover_days=3), 27)


if __name__ == "__main__":
    unittest.main()
