import unittest

from journal import add, state


class JournalTests(unittest.TestCase):
    def test_create_then_state(self):
        journal, status = add([], {"event_id": "e1", "revision": 1, "operation": "create",
                                   "fields": {"start": 5, "length": 3}})
        self.assertEqual(status, "added")
        self.assertEqual(state(journal, "e1"), {"start": 5, "length": 3})


if __name__ == "__main__":
    unittest.main()
