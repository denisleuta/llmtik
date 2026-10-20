import unittest

from arrivals import arrival_day, in_transit, schedule, simulate


def order(day, qty):
    return {"day": day, "qty": qty}


class ArrivalsTests(unittest.TestCase):
    def test_arrival_day(self):
        self.assertEqual(arrival_day(3, lag=2), 5)

    def test_schedule_groups_by_arrival_day(self):
        self.assertEqual(schedule([order(1, 10), order(2, 5)], lag=2), {3: 10, 4: 5})

    def test_order_is_in_transit_until_arrival(self):
        orders = [order(2, 7)]
        self.assertEqual(in_transit(orders, lag=3, day=4), 7)
        self.assertEqual(in_transit(orders, lag=3, day=5), 0)

    def test_simulate_with_enough_stock(self):
        report = simulate(20, [order(1, 5)], lag=2, demand=[4, 4, 4])
        self.assertEqual([row["sold"] for row in report], [4, 4, 4])
        self.assertEqual(report[-1]["stock"], 13)


if __name__ == "__main__":
    unittest.main()
