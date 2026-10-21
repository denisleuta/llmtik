import unittest

from capacity import allocate


class AllocateTests(unittest.TestCase):
    def test_enough_capacity(self):
        self.assertEqual(allocate(100, {"bread": 30, "cake": 20}), {"bread": 30, "cake": 20})

    def test_proportional_without_remainder(self):
        self.assertEqual(allocate(60, {"bread": 80, "cake": 40}), {"bread": 40, "cake": 20})


if __name__ == "__main__":
    unittest.main()
