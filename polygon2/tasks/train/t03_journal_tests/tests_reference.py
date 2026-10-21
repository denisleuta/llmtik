import unittest

from journal import active, add, state


def v(event_id, revision, operation, **fields):
    return {"event_id": event_id, "revision": revision, "operation": operation, "fields": fields}


class JournalReference(unittest.TestCase):
    def test_added_duplicate_conflict(self):
        j, s = add([], v("e", 1, "create", start=5, length=3))
        self.assertEqual(s, "added")
        j2, s = add(j, v("e", 1, "create", start=5, length=3))
        self.assertEqual((s, len(j2)), ("duplicate", 1))
        j3, s = add(j, v("e", 1, "create", start=6, length=3))
        self.assertEqual((s, j3), ("conflict", j))

    def test_add_does_not_mutate(self):
        j = [v("e", 1, "create", start=5, length=3)]
        before = [dict(x) for x in j]
        add(j, v("e", 2, "update", start=7))
        self.assertEqual(j, before)

    def test_out_of_order_and_update_wins(self):
        j, _ = add([], v("e", 2, "update", start=7))
        self.assertIsNone(state(j, "e"))
        j, _ = add(j, v("e", 1, "create", start=5, length=3, factor=4.0))
        self.assertEqual(state(j, "e"), {"start": 7, "length": 3, "factor": 4.0})

    def test_create_must_be_revision_one(self):
        j, _ = add([], v("e", 2, "create", start=5, length=3))
        self.assertIsNone(state(j, "e"))

    def test_cancel_is_final(self):
        j = []
        for x in (v("e", 1, "create", start=5, length=3), v("e", 2, "cancel"),
                  v("e", 3, "update", start=9)):
            j, _ = add(j, x)
        self.assertIsNone(state(j, "e"))

    def test_unknown_event(self):
        self.assertIsNone(state([], "nope"))

    def test_active_window_and_order(self):
        j = []
        for x in (v("b", 1, "create", start=4, length=2), v("a", 1, "create", start=4, length=1),
                  v("c", 1, "create", start=1, length=2), v("d", 1, "create", start=9)):
            j, _ = add(j, x)
        self.assertEqual(active(j, 2), ["c", "a", "b"])
        self.assertEqual(active(j, 3), ["a", "b"])
        self.assertEqual(active(j, 4), ["a", "b"])
        self.assertEqual(active(j, 5), ["b"])
        self.assertEqual(active(j, 6), [])


if __name__ == "__main__":
    unittest.main()
