import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_state


class CheckpointTests(unittest.TestCase):
    def test_question_after_push_does_not_review_new_head(self):
        old = {"reviewed_sha": "old", "gate_blockers": [{"title": "original"}]}
        sha, blockers = review_state.checkpoint(old, "instruct", "new", [{"title": "new issue"}], True)
        self.assertEqual(sha, "old")
        self.assertEqual(len(blockers), 2)

    def test_full_review_can_clear_blockers(self):
        old = {"reviewed_sha": "old", "gate_blockers": [{"title": "original"}]}
        self.assertEqual(review_state.checkpoint(old, "review", "new", [], True), ("new", []))

    def test_failed_review_does_not_advance_or_clear(self):
        old = {"reviewed_sha": "old", "gate_blockers": [{"title": "original"}]}
        self.assertEqual(review_state.checkpoint(old, "update", "new", [], False),
                         ("old", old["gate_blockers"]))

    def test_legacy_question_has_no_known_review_coverage(self):
        self.assertIsNone(review_state.reviewed_sha({"mode": "instruct", "head_after": "new"}))

    def test_legacy_review_only_covers_input_head(self):
        self.assertEqual(review_state.reviewed_sha(
            {"mode": "review", "head_before": "old", "head_after": "fix"}), "old")

    def test_status_exposes_blocked_outcome_and_reviewed_commit(self):
        body = review_state.status_body("Blocked", "https://example.com/run", "new", "old",
                                       "pending", "Changes were not pushed.")
        self.assertTrue(body.startswith(review_state.MARKER))
        self.assertIn("Prime: Blocked", body)
        self.assertIn("Reviewed commit: `old`", body)
        self.assertIn("Changes were not pushed", body)
