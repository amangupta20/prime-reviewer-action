"""Tests for ci_evidence module."""

import unittest
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ci_evidence


class CIEvidenceTests(unittest.TestCase):
    def test_state_normalization(self):
        # Check run statuses and conclusions
        self.assertEqual(ci_evidence.normalize_check_run_state("queued", None), "pending")
        self.assertEqual(ci_evidence.normalize_check_run_state("in_progress", None), "pending")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "success"), "pass")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "failure"), "fail")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "timed_out"), "fail")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "cancelled"), "fail")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "skipped"), "skipped")
        self.assertEqual(ci_evidence.normalize_check_run_state("completed", "neutral"), "skipped")
        self.assertEqual(ci_evidence.normalize_check_run_state("unknown_status", None), "unavailable")

        # Commit status states
        self.assertEqual(ci_evidence.normalize_commit_status_state("pending"), "pending")
        self.assertEqual(ci_evidence.normalize_commit_status_state("success"), "pass")
        self.assertEqual(ci_evidence.normalize_commit_status_state("failure"), "fail")
        self.assertEqual(ci_evidence.normalize_commit_status_state("error"), "fail")
        self.assertEqual(ci_evidence.normalize_commit_status_state("cancelled"), "fail")
        self.assertEqual(ci_evidence.normalize_commit_status_state("other"), "unavailable")

    def test_exclude_own_reviewer_checks(self):
        # Should exclude checks matching reviewer app slug or job names
        self.assertTrue(ci_evidence.is_own_reviewer_check("ai review", "prime-reviewer"))
        self.assertTrue(ci_evidence.is_own_reviewer_check("prime", "prime-reviewer"))
        self.assertTrue(ci_evidence.is_own_reviewer_check("Prime Review", "prime-reviewer"))
        self.assertTrue(ci_evidence.is_own_reviewer_check("prime-reviewer-check", "prime-reviewer"))
        self.assertTrue(ci_evidence.is_own_reviewer_check("any-check", "prime-reviewer", "prime-reviewer"))
        self.assertTrue(ci_evidence.is_own_reviewer_check("prime-reviewer[bot]", "prime-reviewer"))

        # Should NOT exclude other checks
        self.assertFalse(ci_evidence.is_own_reviewer_check("unit-tests", "prime-reviewer", "github-actions"))
        self.assertFalse(ci_evidence.is_own_reviewer_check("lint", "prime-reviewer"))
        self.assertFalse(ci_evidence.is_own_reviewer_check("ci/circleci: build", "prime-reviewer"))

    def test_fetch_ci_snapshot_complete_pass(self):
        head = "1111222233334444555566667777888899990000"
        def mock_api(path, check=True):
            if "check-runs" in path:
                return {
                    "total_count": 2,
                    "check_runs": [
                        {"name": "lint", "status": "completed", "conclusion": "success",
                         "app": {"slug": "github-actions"}},
                        {"name": "ai review", "status": "in_progress", "conclusion": None,
                         "app": {"slug": "prime-reviewer"}},  # self-check, must be excluded
                    ]
                }
            if "status" in path:
                return {
                    "total_count": 1,
                    "statuses": [
                        {"context": "ci/build", "state": "success"}
                    ]
                }
            return None

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        self.assertEqual(data["head_sha"], head)
        self.assertEqual(data["overall_state"], "pass")
        self.assertEqual(data["summary"]["total"], 2)  # lint + ci/build (ai review excluded)
        self.assertEqual(data["summary"]["pass"], 2)
        self.assertEqual(data["summary"]["fail"], 0)
        self.assertEqual(data["summary"]["pending"], 0)
        self.assertFalse(data["incomplete"])
        self.assertFalse(data["truncated"])
        self.assertFalse(data["partial_failure"])
        self.assertFalse(data["unavailable"])

    def test_fetch_ci_snapshot_partial_endpoint_failure(self):
        head = "abc1234567890"
        def mock_api(path, check=True):
            if "check-runs" in path:
                return {"total_count": 1, "check_runs": [
                    {"name": "lint", "status": "completed", "conclusion": "success"}
                ]}
            if "status" in path:
                raise RuntimeError("API status endpoint timeout")
            return None

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        self.assertTrue(data["partial_failure"])
        self.assertTrue(data["incomplete"])
        # Overall must NOT be pass when incomplete!
        self.assertNotEqual(data["overall_state"], "pass")
        self.assertEqual(data["overall_state"], "incomplete")

    def test_fetch_ci_snapshot_both_endpoints_fail_unavailable(self):
        head = "abc1234567890"
        def mock_api(path, check=True):
            raise RuntimeError("Network down")

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        self.assertTrue(data["unavailable"])
        self.assertTrue(data["incomplete"])
        self.assertEqual(data["overall_state"], "unavailable")

    def test_fetch_ci_snapshot_unreturned_rows_marks_incomplete(self):
        head = "abc1234567890"
        def mock_api(path, check=True):
            if "check-runs" in path:
                # 150 total checks reported, but only 2 returned in this page
                return {
                    "total_count": 150,
                    "check_runs": [
                        {"name": "test-1", "status": "completed", "conclusion": "success"},
                        {"name": "test-2", "status": "completed", "conclusion": "success"},
                    ]
                }
            if "status" in path:
                return {"total_count": 0, "statuses": []}
            return None

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        self.assertTrue(data["unreturned_rows"])
        self.assertTrue(data["incomplete"])
        # Must NOT be pass on incomplete coverage!
        self.assertNotEqual(data["overall_state"], "pass")
        self.assertEqual(data["overall_state"], "incomplete")

    def test_aggregate_computed_over_all_fetched_catches_hidden_failure(self):
        # 35 checks returned: first 30 pass, 35th fails.
        # MAX_CHECKS is 30, so display is truncated to 30, but aggregate must catch the failure!
        head = "abc1234567890"
        runs = [
            {"name": f"test-{i}", "status": "completed", "conclusion": "success"}
            for i in range(34)
        ]
        runs.append({"name": "test-34-late", "status": "completed", "conclusion": "failure"})

        def mock_api(path, check=True):
            if "check-runs" in path:
                return {"total_count": len(runs), "check_runs": runs}
            return {"total_count": 0, "statuses": []}

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["summary"]["total"], 35)
        self.assertEqual(data["summary"]["fail"], 1)
        self.assertEqual(data["summary"]["pass"], 34)
        # Must be fail!
        self.assertEqual(data["overall_state"], "fail")
        # Display list truncated to 30
        self.assertEqual(len(data["checks"]), ci_evidence.MAX_CHECKS)

    def test_no_output_summaries_or_logs_ingested(self):
        head = "abc1234567890"
        def mock_api(path, check=True):
            if "check-runs" in path:
                return {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "success",
                            "output": {
                                "title": "Build succeeded",
                                "summary": "CRITICAL_SECRET=ghp_secret_key_1234567890\nBuild logs...",
                                "text": "More secret logs...",
                            }
                        }
                    ]
                }
            return {"total_count": 0, "statuses": []}

        data = ci_evidence.fetch_ci_snapshot("owner/repo", head, "prime-reviewer", mock_api)
        # Output summary and text must NOT be present in checks
        check = data["checks"][0]
        self.assertNotIn("summary", check)
        self.assertNotIn("text", check)
        self.assertNotIn("CRITICAL_SECRET", str(data))

    def test_prompt_formatting(self):
        data = {
            "head_sha": "abc1234567890",
            "overall_state": "fail",
            "summary": {"total": 2, "pass": 1, "fail": 1, "pending": 0, "skipped": 0, "unavailable": 0},
            "total_checks_found": 2,
            "total_count_reported": 2,
            "incomplete": False,
            "truncated": False,
            "unreturned_rows": False,
            "partial_failure": False,
            "unavailable": False,
            "checks": [
                {"name": "lint", "type": "check_run", "state": "pass"},
                {"name": "test", "type": "check_run", "state": "fail"},
            ],
        }
        text = ci_evidence.format_ci_summary_for_prompt(data)
        self.assertIn("[CI Evidence (untrusted)]", text)
        self.assertIn("FAIL", text)
        self.assertIn("lint [check_run]: PASS", text)
        self.assertIn("test [check_run]: FAIL", text)
        self.assertIn("untrusted external data", text)


if __name__ == "__main__":
    unittest.main()
