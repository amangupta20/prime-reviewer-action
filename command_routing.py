"""Command routing for prime-reviewer-action.

Parses /prime comments and pull request triggers into explicit commands:
- review: full review (or focused review if focus is provided)
- ask: read-only question answering (no code push, no lesson updates)
- fix: targeted mechanical or requested code fix
- resolve: merge the base branch into the PR and resolve its conflicts
- instruct: backwards-compatible natural instruction
- update: owner pushed commits to an open PR
- carry: reviewer's own fixes arrived
"""

from __future__ import annotations

import re


def parse_command(body: str) -> tuple[str, str, str]:
    """Parse /prime comment or instruction text into (command, instruction, focus).

    Supported commands:
      /prime review              -> command='review', instruction='', focus=''
      /prime review <focus>      -> command='review', instruction='', focus='<focus>'
      /prime ask <question>      -> command='ask', instruction='<question>', focus=''
      /prime fix <instruction>   -> command='fix', instruction='<instruction>', focus=''
      /prime resolve [<notes>]   -> command='resolve', instruction='<notes>', focus=''
      /prime <natural text>      -> command='instruct', instruction='<natural text>', focus=''
      /prime                     -> command='review', instruction='', focus=''

    Enforces a strict '/prime' token boundary: commands like '/primer' or
    '/prime_runner' are not recognized as '/prime' commands and return ('unknown', body, '').

    Returns:
      (command, instruction, focus)
    """
    if not body:
        return "review", "", ""

    text = body.strip()

    # Enforce strict /prime token boundary
    m_token = re.match(r"(?is)^/prime\b(?:\s*:\s*|\s+)?(.*)$", text)
    if m_token:
        text = m_token.group(1).strip()
    elif text.startswith("/"):
        # Slash command that is not /prime (e.g. /primer, /prime_action, /other)
        return "unknown", body.strip(), ""

    # Strip optional leading ':' if written as '/prime: ...' or '/prime : ...'
    text = text.lstrip(":").strip()
    if not text:
        return "review", "", ""

    m_review = re.match(r"(?is)^review\b(?:\s*:\s*|\s+)?(.*)$", text)
    if m_review:
        focus = m_review.group(1).strip()
        focus = focus.lstrip(":").strip()
        return "review", "", focus

    m_ask = re.match(r"(?is)^ask\b(?:\s*:\s*|\s+)?(.*)$", text)
    if m_ask:
        question = m_ask.group(1).strip()
        question = question.lstrip(":").strip()
        return "ask", question, ""

    m_fix = re.match(r"(?is)^fix\b(?:\s*:\s*|\s+)?(.*)$", text)
    if m_fix:
        instruction = m_fix.group(1).strip()
        instruction = instruction.lstrip(":").strip()
        return "fix", instruction, ""

    m_resolve = re.match(r"(?is)^resolve\b(?:\s*:\s*|\s+)?(.*)$", text)
    if m_resolve:
        notes = m_resolve.group(1).strip()
        notes = notes.lstrip(":").strip()
        return "resolve", notes, ""

    return "instruct", text, ""


def command_to_mode(command: str) -> str:
    """Map explicit command to existing PRIME_MODE semantics.

    PRIME_MODE preserves 'review', 'update', and 'instruct' (or 'carry').
    'ask', 'fix' and 'resolve' run under 'instruct' mode while exposing PRIME_COMMAND.
    """
    if command in ("ask", "fix", "resolve", "instruct"):
        return "instruct"
    if command == "update":
        return "update"
    if command == "carry":
        return "carry"
    return "review"


def can_push_commits(command: str) -> bool:
    """Whether the given command allows pushing code commits.

    'ask' is strictly read-only and must never push commits.
    """
    return command not in ("ask", "unknown")


def owner_asked_for_changes(command: str) -> bool:
    """Whether the commits answer an explicit owner request for a change.

    'fix', 'instruct' and 'resolve' carry the owner's own request, so their
    commits may touch the paths the repository config protects (for
    'resolve', only where its resolution differs from a plain merge). A
    review ('review', 'update', 'carry') commits fixes nobody asked for, so it
    may not.
    """
    return command in ("fix", "instruct", "resolve")


def may_merge_base(command: str) -> bool:
    """Whether the command's commits may include a merge of the base branch.

    Only 'resolve' merges; every other command must add plain commits.
    """
    return command == "resolve"


def can_propose_learning(command: str) -> bool:
    """Whether the given command allows proposing playbook/lesson updates.

    'ask' is strictly read-only and must not propose lesson changes.
    """
    return command not in ("ask", "unknown")
