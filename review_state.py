"""Review coverage is independent of the latest conversation checkpoint."""


def reviewed_sha(meta):
    meta = meta or {}
    if "reviewed_sha" in meta:
        return meta["reviewed_sha"]
    # Historical instruction runs did not prove review coverage.
    if meta.get("mode") in ("review", "update") and not meta.get("error"):
        return meta.get("head_before")
    return None


def merge_blockers(previous, current):
    result = list(previous or [])
    for finding in current or []:
        if finding not in result:
            result.append(finding)
    return result


def checkpoint(previous, mode, head, blockers, successful):
    if mode in ("review", "update") and successful:
        return head, blockers
    return reviewed_sha(previous), merge_blockers(
        (previous or {}).get("gate_blockers"), blockers)


MARKER = "<!-- prime-reviewer-status -->"


def status_body(state, run_url, head=None, reviewed=None, ci=None, note=None):
    lines = [MARKER, f"### Prime: {state}", f"[View run]({run_url})"]
    if head:
        lines.append(f"PR commit: `{head}`")
    lines.append(f"Reviewed commit: `{reviewed}`" if reviewed else "Reviewed commit: not established")
    if ci:
        lines.append(f"CI at run start: {ci}")
    if note:
        lines.append(note)
    lines.append("Commands: `/prime review`, `/prime ask <question>`, `/prime fix <request>`")
    return "\n\n".join(lines)
