import unittest

from gr_observer.group_cadence import compute_repost_plan


class GroupCadenceTests(unittest.TestCase):
    def test_recent_activity_has_largest_weight(self):
        recent = compute_repost_plan(40, 0, 0)
        historical = compute_repost_plan(0, 40, 0)
        self.assertGreater(recent.activity_score, historical.activity_score)
        self.assertGreaterEqual(recent.target_messages, historical.target_messages)

    def test_hot_group_uses_more_messages_and_shorter_time_gate(self):
        hot = compute_repost_plan(80, 80, 40)
        slow = compute_repost_plan(1, 1, 1)
        self.assertGreater(hot.target_messages, slow.target_messages)
        self.assertLess(hot.min_interval_seconds, slow.min_interval_seconds)

    def test_bounds_are_conservative(self):
        dead = compute_repost_plan(0, 0, 0)
        extreme = compute_repost_plan(10000, 10000, 10000)
        self.assertGreaterEqual(dead.target_messages, 2)
        self.assertLessEqual(extreme.target_messages, 40)
        self.assertGreaterEqual(extreme.min_interval_seconds, 15 * 60)
        self.assertLessEqual(dead.min_interval_seconds, 4 * 3600)
        self.assertGreaterEqual(dead.validity_seconds, 2 * 3600)
        self.assertLessEqual(dead.validity_seconds, 4 * 3600)


if __name__ == "__main__":
    unittest.main()
