"""Tests for command_routing module."""

import unittest
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import command_routing


class CommandRoutingTests(unittest.TestCase):
    def test_full_review_commands(self):
        cases = [
            ("", ("review", "", "")),
            ("/prime", ("review", "", "")),
            ("/prime ", ("review", "", "")),
            ("/prime review", ("review", "", "")),
            ("/prime: review", ("review", "", "")),
            ("review", ("review", "", "")),
            ("   /prime   review   ", ("review", "", "")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(command_routing.parse_command(text), expected)

    def test_focused_review_commands(self):
        cases = [
            ("/prime review security implications", ("review", "", "security implications")),
            ("/prime review: error handling", ("review", "", "error handling")),
            ("/prime review   concurrency and locks  ", ("review", "", "concurrency and locks")),
            ("review edge cases", ("review", "", "edge cases")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(command_routing.parse_command(text), expected)

    def test_ask_commands(self):
        cases = [
            ("/prime ask why is this mutex necessary?", ("ask", "why is this mutex necessary?", "")),
            ("/prime ask: what does this function return?", ("ask", "what does this function return?", "")),
            ("/prime ask   how is state stored?  ", ("ask", "how is state stored?", "")),
            ("ask what is this line doing", ("ask", "what is this line doing", "")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(command_routing.parse_command(text), expected)

    def test_fix_commands(self):
        cases = [
            ("/prime fix handle None when auth token is missing",
             ("fix", "handle None when auth token is missing", "")),
            ("/prime fix: rename old_var to new_var",
             ("fix", "rename old_var to new_var", "")),
            ("fix typo in line 42", ("fix", "typo in line 42", "")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(command_routing.parse_command(text), expected)

    def test_natural_instructions(self):
        cases = [
            ("/prime please explain the algorithm",
             ("instruct", "please explain the algorithm", "")),
            ("/prime can you add unit tests for the parser?",
             ("instruct", "can you add unit tests for the parser?", "")),
            ("please optimize this query", ("instruct", "please optimize this query", "")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(command_routing.parse_command(text), expected)

    def test_strict_token_boundary(self):
        # Commands like /primer or /prime_reviewer must NOT be accepted as /prime
        cases = [
            "/primer review",
            "/primer ask something",
            "/prime_runner fix it",
            "/other_command review",
        ]
        for text in cases:
            with self.subTest(text=text):
                cmd, raw, _ = command_routing.parse_command(text)
                self.assertEqual(cmd, "unknown")
                self.assertEqual(raw, text)

    def test_command_to_mode_mapping(self):
        self.assertEqual(command_routing.command_to_mode("review"), "review")
        self.assertEqual(command_routing.command_to_mode("update"), "update")
        self.assertEqual(command_routing.command_to_mode("carry"), "carry")
        self.assertEqual(command_routing.command_to_mode("ask"), "instruct")
        self.assertEqual(command_routing.command_to_mode("fix"), "instruct")
        self.assertEqual(command_routing.command_to_mode("instruct"), "instruct")

    def test_ask_is_read_only(self):
        self.assertFalse(command_routing.can_push_commits("ask"))
        self.assertFalse(command_routing.can_propose_learning("ask"))

        for cmd in ("review", "update", "fix", "instruct"):
            self.assertTrue(command_routing.can_push_commits(cmd))
            self.assertTrue(command_routing.can_propose_learning(cmd))


if __name__ == "__main__":
    unittest.main()
