#!/usr/bin/env python3
"""Run Prime Agent as the pull-request reviewer and act on its result.

Run by the prime-reviewer composite action (action.yml next to this file)
from a repository's own workflow. Per-repository settings come from the
private config repo: repos/<repo>/config.toml and repos/<repo>/playbook/.
Steps:

1. Work out the mode: `review` (PR opened, or `/prime` / `/prime review ...`),
   `update` (the owner pushed to a reviewed PR), `carry` (the reviewer's own
   fix commits arrived: reuse its verdict, no model call), or `instruct`
   (`/prime <anything else>`).
2. Check out the PR head, restore this PR's saved session from the private
   repo's `state` branch.
3. Run Prime as its own Linux user on fresh copies of the PR, rules,
   config and session: it gets the model key and its inputs, never the
   GitHub token or storage keys, and cannot change files this script trusts.
4. Take its commits back as a bundle, check them (allowed paths, the
   configured check from the default branch, run as Prime's user) and push
   them with the App token.
5. Post a review: suggestions and design comments on diff lines, the rest in
   the summary. In instruct mode, reply to the comment. With `approve-clean`,
   approve a commit that reviewed clean and withdraw that approval otherwise.
6. Save the session and a per-run trace to the `state` branch, and propose
   any change it made to its own playbook as a PR on the private repo.
7. On pull_request_target runs (the required check), exit non-zero when a
   blocker in one of the config's `blocking_sections` is open.

The run log is public: it shows one line per step (tool, what it read or ran,
tokens, cost), never the model's reasoning or its prompts.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# Add script directory to sys.path so python3 -I isolation finds sibling modules
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# action.yml installs boto3 with pip --target; -I ignores PYTHONPATH and the
# user's site-packages, so the folder is added here.
if os.environ.get("PRIME_PYLIB"):
    sys.path.insert(0, os.environ["PRIME_PYLIB"])

import ci_evidence
import command_routing
import review_state
import state_store

WORK = Path.cwd()
RULES = WORK / "rules"
AGENT = WORK / "agent"
PR_DIR = WORK / "pr"
STATE = WORK / "state"
TMP = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
SESSION = TMP / "prime-session"

# Prime runs as its own Linux user (installed by action.yml), so it cannot
# read this process's credentials or change any file this wrapper later
# trusts. Everything it may read or change is a fresh copy under JAIL.
PRIME_USER = os.environ.get("PRIME_USER", "prime-runner")
PRIME_HOME = Path(f"/home/{PRIME_USER}")
PRIME = PRIME_HOME / ".local/bin/prime-agent"
PRIME_PATH = f"{PRIME_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
JAIL = Path(os.environ.get("PRIME_JAIL", "/srv/prime-review"))
OUT = JAIL / "out"

REPO = os.environ["REPO"]
REPO_NAME = REPO.split("/", 1)[1]
OWNER = os.environ["OWNER"]
CONFIG_REPO = os.environ.get("CONFIG_REPO") or f"{OWNER}/prime-reviewer"
CONFIG_REF = os.environ.get("CONFIG_REF") or "main"
PR = os.environ["PR_NUMBER"]
RUN_ID = os.environ["RUN_ID"]
RUN_URL = os.environ["RUN_URL"]
EVENT = os.environ["EVENT_NAME"]
TOKEN = os.environ["GH_TOKEN"]

STATE_BACKEND = os.environ.get("STATE_BACKEND", "auto").strip().lower()
if STATE_BACKEND not in ("auto", "b2", "git"):
    raise state_store.StorageError(
        f"Unknown STATE_BACKEND '{STATE_BACKEND}': must be 'auto', 'b2', or 'git'"
    )

if STATE_BACKEND == "b2":
    state_store.validate_b2_config()
    B2_MODE = True
elif STATE_BACKEND == "git":
    B2_MODE = False
else:  # auto
    if state_store.any_b2_configured():
        state_store.validate_b2_config()
        B2_MODE = True
    else:
        B2_MODE = False

# Credentials leave the process environment once read, so no command this
# wrapper starts inherits them; each call that needs one is given it.
SECRETS = {k: os.environ.pop(k) for k in ("GH_TOKEN", "OPENCODE_API_KEY", *state_store.B2_ENV_VARS)
           if k in os.environ}

B2_STORE: state_store.B2StateStore | None = None


def get_b2_store() -> state_store.B2StateStore | None:
    global B2_STORE
    if B2_STORE is not None:
        return B2_STORE
    if B2_MODE:
        B2_STORE = state_store.B2StateStore.from_env({**os.environ, **SECRETS})
    return B2_STORE


def load_config() -> dict:
    """repos/<repo>/config.toml from the private config repo."""
    f = AGENT / "repos" / REPO_NAME / "config.toml"
    if not f.exists():
        raise SystemExit(f"no reviewer config for {REPO_NAME}: add repos/{REPO_NAME}/config.toml "
                         f"to {CONFIG_REPO}")
    config = tomllib.loads(f.read_text())
    for key in ("rules", "protected", "blocking_sections", "fix_check"):
        if key not in config:
            raise SystemExit(f"reviewer config is missing {key}")
    if not isinstance(config["blocking_sections"], list) or not all(
            type(n) is int for n in config["blocking_sections"]):
        raise SystemExit("blocking_sections must be a list of integers")
    for key in ("rules", "protected"):
        if not isinstance(config[key], list) or not all(isinstance(x, str) for x in config[key]):
            raise SystemExit(f"{key} must be a list of strings")
    if not isinstance(config["fix_check"], str):
        raise SystemExit("fix_check must be a string")
    return config


CONFIG = load_config()
PLAYBOOK = AGENT / CONFIG.get("playbook", f"repos/{REPO_NAME}/playbook")
if not PLAYBOOK.is_dir() or not (PLAYBOOK / "SKILL.md").is_file():
    raise SystemExit(f"missing repository playbook: {PLAYBOOK}")
if not PLAYBOOK.resolve().is_relative_to((AGENT / "repos" / REPO_NAME).resolve()):
    raise SystemExit("playbook must live inside this repository's config folder")
MODEL = os.environ.get("PRIME_MODEL") or CONFIG.get("model", "deepseek-v4.1-flash")
ACTION = os.environ.get("EVENT_ACTION", "")
SENDER = os.environ.get("SENDER", "")
GATE = EVENT == "pull_request_target"
APPROVE_CLEAN = os.environ.get("APPROVE_CLEAN") == "true"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY")
BLOCKING_SECTIONS = set(CONFIG.get("blocking_sections", []))

# Workflows are the gate itself: no run may change them.
ALWAYS_DENY = re.compile("^\\.github/")
# Paths the reviewer may never change by itself; always includes workflows.
# An owner's fix or instruction may change the configured ones (check_and_push).
DENY = re.compile("|".join(["^\\.github/"] + list(CONFIG.get("protected", []))))


def log(msg: str) -> None:
    print(f"prime-review: {msg}", flush=True)


def run(cmd, cwd=None, check=True, env=None, capture=True, input_=None):
    r = subprocess.run(
        cmd, cwd=cwd, env=env, text=True, input=input_,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and r.returncode != 0:
        # Never echo the command line: it may hold an auth header.
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: "
                           f"{(r.stderr or '')[-800:]}")
    return r


def gh_env() -> dict:
    """The environment for `gh`: the only commands given the App token."""
    return {**os.environ, "GH_TOKEN": TOKEN}


def gh_api(path: str, method: str = "GET", body: dict | None = None, check=True):
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    r = run(cmd, check=check, env=gh_env(),
            input_=json.dumps(body) if body is not None else None)
    return json.loads(r.stdout) if r.stdout.strip() else None


def sudo(args: list[str], check=True):
    return run(["sudo", "-n", *args], check=check)


def as_prime(args: list[str], chdir: Path | None = None, env: dict | None = None,
             check=True):
    """Run a command as PRIME_USER with a minimal environment and no credentials."""
    base = {"PATH": PRIME_PATH, "HOME": str(PRIME_HOME), "LANG": "C.UTF-8",
            "TMPDIR": str(JAIL / "tmp"), "DO_NOT_TRACK": "1"}
    pairs = [f"{k}={v}" for k, v in {**base, **(env or {})}.items()]
    where = [f"--chdir={chdir}"] if chdir else []
    return sudo(["-u", PRIME_USER, "--", "/usr/bin/env", "-i", *where, *pairs, *args],
                check=check)


def stop_prime_processes() -> None:
    """Kill everything PRIME_USER still runs, so nothing changes its files or
    watches this wrapper while it collects them and pushes."""
    for _ in range(20):
        sudo(["pkill", "-KILL", "-u", PRIME_USER], check=False)
        if sudo(["pgrep", "-u", PRIME_USER], check=False).returncode == 1:
            return
        time.sleep(0.25)
    raise RuntimeError("the reviewer's processes did not stop")


def give_to_prime(*paths: Path) -> None:
    for p in paths:
        sudo(["chown", "-R", "-h", f"{PRIME_USER}:{PRIME_USER}", str(p)])


def take_from_prime() -> None:
    """Own the jail again (-h: change symlinks themselves, never their targets)."""
    sudo(["chown", "-R", "-h", f"{os.getuid()}:{os.getgid()}", str(JAIL)])


def copy_regular(src: Path, dst: Path) -> None:
    """Copy only regular files: Prime's output may hold symlinks that point at
    this user's files."""
    if src.is_symlink() or not src.is_dir():
        return
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            p = Path(root) / name
            if p.is_symlink() or not p.is_file():
                continue
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def line_counts(session_dir: Path) -> dict[str, int]:
    """Lines per root session file, to tell this run's records from older ones."""
    counts = {}
    for f in session_dir.glob("*.jsonl"):
        if f.is_file() and not f.is_symlink():
            with open(f, errors="replace") as fh:
                counts[f.name] = sum(1 for _ in fh)
    return counts


def repoint_session_cwd(session_dir: Path, cwd: Path) -> None:
    """Make `-c` find the saved conversation from Prime's working folder.

    Prime continues only the newest session whose header (the file's first
    line) records the current folder; otherwise it starts a new one (Prime
    0.10.2, `find_most_recent_session_for_cwd` in print_runtime.rs). Sessions
    saved before Prime ran in the jail record another folder, so the newest
    one is pointed here, unless a session for this folder already exists.
    """
    headers = {}
    for f in session_dir.glob("*.jsonl"):
        if not f.is_file() or f.is_symlink():
            continue
        try:
            with open(f) as fh:
                header = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        if isinstance(header, dict) and isinstance(header.get("cwd"), str):
            headers[f] = header
    if not headers or any(h["cwd"] == str(cwd) for h in headers.values()):
        return
    newest = state_store.newest_session_file(session_dir)
    if newest not in headers:
        return
    stat = newest.stat()
    lines = newest.read_text().splitlines(keepends=True)
    lines[0] = json.dumps({**headers[newest], "cwd": str(cwd)}, separators=(",", ":")) + "\n"
    newest.write_text("".join(lines))
    os.utime(newest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    # Prime caches per-session scan results next to the file; drop the stale one.
    newest.with_name(newest.stem + ".info-cache.json").unlink(missing_ok=True)
    log("saved session now continues from the reviewer's working folder")


def regular_file(p: Path) -> bool:
    return not p.is_symlink() and p.is_file()


def auth_env() -> dict:
    """The App token for git, in the environment: a command line is readable
    by every local user, including the one Prime runs as."""
    b = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    return {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b}"}


def git(args, cwd, auth=False, check=True):
    return run(["git"] + args, cwd=cwd, check=check, env=auth_env() if auth else None)


def react(content: str) -> None:
    cid = os.environ.get("COMMENT_ID")
    if not cid:
        return
    kind = "pulls/comments" if EVENT == "pull_request_review_comment" else "issues/comments"
    gh_api(f"repos/{REPO}/{kind}/{cid}/reactions", "POST", {"content": content}, check=False)


# ---------------------------------------------------------------- mode -----

def saved_session() -> Path:
    """Read old homelab sessions until the first namespaced save."""
    path = STATE / "sessions" / REPO_NAME / f"pr-{PR}"
    legacy = STATE / "sessions" / f"pr-{PR}"
    if not path.exists() and REPO == f"{OWNER}/homelab" and legacy.exists():
        return legacy
    return path


def parse_mode(has_saved: bool | None = None) -> tuple[str, str, str, str]:
    """Return (mode, instruction, focus, command)."""
    if has_saved is None:
        has_saved = saved_session().exists() and any(saved_session().glob("*.jsonl"))
    if GATE:
        if SENDER == f"{os.environ['APP_SLUG']}[bot]":
            return "carry", "", "", "carry"
        if ACTION == "opened" or not has_saved:
            raw_focus = "\n".join(
                l[len("/prime"):].strip() for l in os.environ.get("PR_BODY", "").splitlines()
                if l.startswith("/prime")
            )
            cmd, _, focus_parsed = command_routing.parse_command(raw_focus)
            effective_focus = focus_parsed or raw_focus
            return "review", "", effective_focus, "review"
        return "update", "", "", "update"
    body = os.environ.get("COMMENT_BODY", "")
    cmd, text, focus = command_routing.parse_command(body)
    if cmd == "unknown":
        raise ValueError(f"Unrecognized command: '{body.strip()}' is not a valid /prime command")
    mode = command_routing.command_to_mode(cmd)
    if mode == "review":
        return "review", "", focus, "review"
    if EVENT == "pull_request_review_comment":
        ctx = f"(On {os.environ.get('COMMENT_PATH')} line {os.environ.get('COMMENT_LINE')}"
        reply_to = os.environ.get("COMMENT_REPLY_TO")
        if reply_to:
            parent = gh_api(f"repos/{REPO}/pulls/comments/{reply_to}", check=False) or {}
            ctx += f", replying to this comment:\n{parent.get('body', '')[:3000]}\n"
        text = f"{ctx})\n\n{text}" if text else f"{ctx})"
    if not has_saved:
        raise state_store.SessionNotFoundError(
            f"Instruct mode requires an existing saved session for PR #{PR}, but none was found."
        )
    return mode, text, "", cmd


# ------------------------------------------------------------- checkout ----

def checkout_pr() -> dict:
    pr = gh_api(f"repos/{REPO}/pulls/{PR}")
    if pr["head"]["repo"]["full_name"] != REPO:
        raise SystemExit("pull request from a fork: not reviewed")
    if pr["state"] != "open":
        log("pull request is not open: fixes will not be pushed")
    if PR_DIR.exists():
        shutil.rmtree(PR_DIR)
    PR_DIR.mkdir(parents=True)
    git(["init", "--quiet"], PR_DIR)
    git(["remote", "add", "origin", f"https://github.com/{REPO}.git"], PR_DIR)
    # PR refs survive deletion of the source branch after merge.
    git(["fetch", "--quiet", "--no-tags", "origin", f"refs/pull/{PR}/head"], PR_DIR, auth=True)
    git(["checkout", "--quiet", "-b", "prime-work", "FETCH_HEAD"], PR_DIR)
    git(["fetch", "--quiet", "origin",
         f"refs/heads/{pr['base']['ref']}:refs/remotes/origin/{pr['base']['ref']}"], PR_DIR, auth=True)
    base = git(["merge-base", "HEAD", f"origin/{pr['base']['ref']}"], PR_DIR).stdout.strip()
    head = git(["rev-parse", "HEAD"], PR_DIR).stdout.strip()
    bot = gh_api(f"users/{os.environ['APP_SLUG']}%5Bbot%5D")
    git(["config", "user.name", f"{os.environ['APP_SLUG']}[bot]"], PR_DIR)
    git(["config", "user.email",
         f"{bot['id']}+{os.environ['APP_SLUG']}[bot]@users.noreply.github.com"], PR_DIR)
    return {"pr": pr, "base": base, "head": head}


def checkout_state() -> None:
    if B2_MODE:
        log("using B2 storage backend; skipping git state clone")
        if (AGENT / ".git").exists():
            git(["config", "user.name", f"{os.environ['APP_SLUG']}[bot]"], AGENT)
            git(["config", "user.email", f"{os.environ['APP_SLUG']}[bot]@users.noreply.github.com"], AGENT)
        return
    url = f"https://github.com/{CONFIG_REPO}.git"
    r = run(["git", "clone", "--quiet", "--depth", "1",
             "--branch", "state", url, str(STATE)], check=False, env=auth_env())
    if r.returncode != 0:
        log("no state branch yet: starting one")
        STATE.mkdir(parents=True, exist_ok=True)
        git(["init", "--quiet", "-b", "state"], STATE)
        git(["remote", "add", "origin", url], STATE)
        (STATE / "README.md").write_text(STATE_README)
    for d in (STATE, AGENT):
        git(["config", "user.name", f"{os.environ['APP_SLUG']}[bot]"], d)
        git(["config", "user.email", f"{os.environ['APP_SLUG']}[bot]@users.noreply.github.com"], d)


STATE_README = """# Reviewer state

Written only by the prime-reviewer action (`amangupta20/prime-reviewer-action`), for every repository that uses it.

- `sessions/<repo>/pr-<n>/`: Prime Agent's session folder for that pull request.
  Restored before every run on the PR, so `/prime <instruction>` continues
  the same conversation.
- `traces/<run-id>/`: one folder per run, kept forever:
  - `meta.json`: run id, PR, mode, model, Prime version, start time,
    duration, head commit before and after, verdict, counts, token usage
    and cost summed over this run's model turns.
  - `result.json`: the reviewer's structured result.
  - `session.jsonl`: the session file as it was after this run (every
    message, tool call and usage record). The run's own records are the
    lines after `meta.json`'s `session_lines_before`.
  - `subagents/<child-id>/*.jsonl`: transcripts of sub-agents the run
    started (same record format). `meta.json`'s usage includes them.
  - A run that failed after Prime finished still gets its folder;
    `meta.json`'s `error` says what failed.

`meta.json` and `session.jsonl` are the input for a future trace importer
(OpenTelemetry to Phoenix, Langfuse or OpenObserve).
"""


# ------------------------------------------------------------------ run ----

FINISH_PROMPT = ("Your last message ended the run, but $PRIME_OUT/result.json does not exist, so "
                 "the run will fail. Do whatever you said you would do next, then write that "
                 "file now. Do not end your turn until it is written.")

def run_prime(ctx: dict, mode: str, instruction: str, focus: str,
              command: str = "review", ci_data: dict | None = None) -> dict:
    if not B2_MODE:
        if SESSION.exists():
            shutil.rmtree(SESSION)
        saved = saved_session()
        resume = saved.exists() and any(saved.glob("*.jsonl"))
        if resume:
            shutil.copytree(saved, SESSION)
        else:
            SESSION.mkdir(parents=True)
    else:
        resume = SESSION.is_dir() and any(SESSION.glob("*.jsonl"))
        if not SESSION.exists():
            SESSION.mkdir(parents=True)
    before = line_counts(SESSION)

    prepare_jail()
    playbook = JAIL / "agent" / PLAYBOOK.relative_to(AGENT)
    ci_context_str = json.dumps(ci_data) if ci_data else "{}"
    env = {
        "USER": PRIME_USER,
        "TERM": "dumb",
        "PRIME_AGENT_CODING_AGENT_DIR": str(JAIL / "agent"),
        "BASE": ctx["base"],
        "HEAD": ctx["head"],
        "RULES_DIR": str(JAIL / "rules"),
        "PRIME_OUT": str(OUT),
        "PRIME_MODE": mode,
        "PRIME_COMMAND": command,
        "PRIME_CI_CONTEXT": ci_context_str,
        "PRIME_INSTRUCTION": instruction,
        "PRIME_FOCUS": focus,
        "LAST": ctx.get("last") or ctx["base"],
        "PRIME_RULES": " ".join(CONFIG.get("rules", ["AGENTS.md"])),
        "PRIME_PROTECTED": " ".join(CONFIG.get("protected", [])),
        "PRIME_OWNER_MAY_OPEN_PROTECTED": (
            "true" if command_routing.owner_asked_for_changes(command) else "false"),
        "PRIME_FIX_CHECK": CONFIG.get("fix_check", ""),
        "PRIME_PLAYBOOK": str(playbook),
        "PRIME_REPO": REPO,
    }
    if command == "ask":
        prompt = (f"The owner asked a question on pull request #{PR}: read it from "
                  "$PRIME_INSTRUCTION. Answer it as a read-only response under your core's "
                  "rules without making code changes or commits, and write "
                  "$PRIME_OUT/result.json with the reply filled in.")
    elif command == "fix":
        prompt = (f"The owner requested a fix on pull request #{PR}: read it from "
                  "$PRIME_INSTRUCTION. Implement the requested fix under your core's rules, "
                  "and write $PRIME_OUT/result.json with the reply filled in.")
    elif mode == "review":
        prompt = (f"Review pull request #{PR} (diff $BASE..$HEAD). Follow your core and "
                  "your playbook skill, fix level 1 findings as commits, and write "
                  "$PRIME_OUT/result.json.")
        if focus:
            prompt += " The owner asked for this focus: see $PRIME_FOCUS."
    elif mode == "update":
        prompt = (f"The owner pushed new commits to pull request #{PR} since your last run "
                  "($LAST..$HEAD; the whole change is $BASE..$HEAD). Re-review in update "
                  "mode as your core describes, fix level 1 findings as commits, and write "
                  "$PRIME_OUT/result.json.")
    else:
        prompt = (f"The owner left an instruction on pull request #{PR}: read it from "
                  "$PRIME_INSTRUCTION. Do it under your core's rules (the PR's state may "
                  "have changed since your last run: re-read what you need), and write "
                  "$PRIME_OUT/result.json with the reply filled in.")
    if ci_data:
        ci_summary_text = ci_evidence.format_ci_summary_for_prompt(ci_data)
        if ci_summary_text:
            prompt += f"\n\n{ci_summary_text}"
    cmd = [str(PRIME), "-p", "--mode", "json", "--session-dir", str(JAIL / "session"),
           "--provider", "opencode-go", "--model", MODEL, "--thinking", "high",
           "--skill", str(playbook)]
    # The model key is the one credential Prime gets. It travels through
    # sudo's preserved environment, never the command line, which any local
    # user can read; the other values are not secret.
    base = {"PATH": PRIME_PATH, "HOME": str(PRIME_HOME), "LANG": "C.UTF-8",
            "TMPDIR": str(JAIL / "tmp"), "DO_NOT_TRACK": "1"}
    prefix = ["sudo", "-n", "-u", PRIME_USER, "--preserve-env=OPENCODE_API_KEY", "--",
              "/usr/bin/env", f"--chdir={JAIL / 'work'}",
              *[f"{k}={v}" for k, v in {**base, **env}.items()], *cmd]

    def attempt(message: str, continue_session: bool) -> tuple[int, str]:
        proc = subprocess.Popen([*prefix, *(["-c"] if continue_session else []), message],
                                text=True, stdin=subprocess.DEVNULL,
                                env={"PATH": "/usr/bin:/bin",
                                     "OPENCODE_API_KEY": SECRETS.get("OPENCODE_API_KEY", "")},
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1)
        text = stream_progress(proc, started)
        try:
            return proc.wait(timeout=60), text
        except subprocess.TimeoutExpired:
            proc.kill()
            return -9, text

    started = time.time()
    log(f"running Prime ({mode}, resume={resume}, model {MODEL})")
    code, final_text = attempt(prompt, resume)
    # The model sometimes ends its turn announcing its next step instead of taking it, which
    # exits 0 with no result. Its session is intact, so ask once; a second miss fails the run.
    if code == 0 and not regular_file(OUT / "result.json"):
        log("Prime stopped without a result; asking it to finish")
        code, more = attempt(FINISH_PROMPT, True)
        final_text = more or final_text
    as_prime([str(PRIME), "shutdown"], check=False)
    duration = round(time.time() - started)
    log(f"Prime exited {code} after {duration}s")

    prime_head = collect_from_jail(ctx, command=command)
    root = state_store.newest_session_file(SESSION)
    lines_before = before.get(root.name, 0) if root else 0
    result = None
    try:
        if not regular_file(OUT / "result.json"):
            raise OSError("missing")
        result = json.loads((OUT / "result.json").read_text())
        if not isinstance(result, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        result = None
        log(f"no usable result.json ({e.__class__.__name__})")
    return {"result": result, "exit": code, "duration": duration, "prime_head": prime_head,
            "started": started, "lines_before": lines_before, "stdout": final_text}


def prepare_jail() -> None:
    """Fresh copies of what Prime may read or change. The rules stay owned by
    this user, so Prime can read them but not change them."""
    stop_prime_processes()
    sudo(["rm", "-rf", "--one-file-system", str(JAIL)])
    sudo(["install", "-d", "-m", "755", "-o", str(os.getuid()), "-g", str(os.getgid()),
          str(JAIL)])
    no_git = shutil.ignore_patterns(".git")
    shutil.copytree(RULES, JAIL / "rules", ignore=no_git, symlinks=True)
    shutil.copytree(AGENT, JAIL / "agent", ignore=no_git, symlinks=True)
    shutil.copytree(SESSION, JAIL / "session", symlinks=True)
    repoint_session_cwd(JAIL / "session", JAIL / "work")
    # --no-hardlinks: Prime will own these objects, and they must not be the
    # same files as the checkout this wrapper pushes from.
    git(["clone", "--quiet", "--no-hardlinks", str(PR_DIR), str(JAIL / "work")], WORK)
    for key in ("user.name", "user.email"):
        git(["config", key, git(["config", key], PR_DIR).stdout.strip()], JAIL / "work")
    for d in ("out", "tmp"):
        (JAIL / d).mkdir()
    give_to_prime(*(JAIL / d for d in ("agent", "session", "work", "out", "tmp")))


def collect_from_jail(ctx: dict, command: str = "") -> str:
    """Bring back the session, sub-agent transcripts, lesson edits and commits.

    Prime's git repository is only read by git running as PRIME_USER: its
    config and hooks may have been changed. Its commits come back as a bundle,
    which git here reads as plain data. Returns Prime's head commit, or "".
    """
    stop_prime_processes()
    work = JAIL / "work"
    r = as_prime(["git", "-C", str(work), "rev-parse", "HEAD"], check=False)
    prime_head = r.stdout.strip() if r.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", prime_head):
        prime_head = ""
    if prime_head and prime_head != ctx["head"]:
        as_prime(["git", "-C", str(work), "bundle", "create", "--quiet",
                  str(OUT / "commits.bundle"), "HEAD", f"^{ctx['head']}"], check=False)
    stop_prime_processes()
    take_from_prime()

    if SESSION.exists():
        shutil.rmtree(SESSION)
    SESSION.mkdir(parents=True)
    copy_regular(JAIL / "session", SESSION)
    root = state_store.newest_session_file(SESSION)
    if root:
        artifacts = Path("session-artifacts") / root.stem
        copy_regular(JAIL / "agent" / artifacts, AGENT / artifacts)
    if command_routing.can_propose_learning(command):
        for rel in lesson_paths():
            src, dst = JAIL / "agent" / rel, AGENT / rel
            if regular_file(src):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        copy_regular(JAIL / "agent" / "harness", AGENT / "harness")
    return prime_head


def lesson_paths() -> list[Path]:
    """The files Prime may propose changes to (APPEND_SYSTEM.md's rules)."""
    return [PLAYBOOK.relative_to(AGENT) / "SKILL.md", Path("skills/common-review/SKILL.md")]


def describe_code(code: str) -> str:
    """One public-safe line for a tool call: the shell command or first statement."""
    m = re.search(r"""bash\(\s*(?:f?["']{1,3})(.+?)["']""", code, re.S)
    if m:
        return "$ " + " ".join(m.group(1).split())[:150]
    paths = re.findall(r"""["']([\w./-]+\.(?:md|ya?ml|json|py|toml|sh|txt))["']""", code)
    if paths:
        return "read " + ", ".join(dict.fromkeys(paths))[:150]
    for line in code.splitlines():
        line = line.strip()
        if line and not line.startswith(("import ", "from ", "#")):
            return line[:150]
    return "(code)"


def stream_progress(proc, started: float, deadline: int = 2400) -> str:
    """Print one line per tool call and per model turn; return the final answer."""
    step = turn = 0
    tokens_in = tokens_out = 0
    cost = 0.0
    final = ""
    for raw in proc.stdout:
        if time.time() - started > deadline:
            log("time limit reached, stopping Prime")
            proc.kill()
            break
        if raw.startswith('{"type":"message_update"') or '"message_update"' in raw[:40]:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        t = e.get("type")
        el = f"{round(time.time() - started):>4}s"
        if t == "tool_execution_start":
            step += 1
            args = e.get("args") or {}
            what = describe_code(args.get("code", "")) if "code" in args else e.get("toolName", "?")
            print(f"{el}  step {step:>2}  {what}", flush=True)
        elif t == "tool_execution_end":
            d = (e.get("result") or {}).get("details") or {}
            if e.get("isError") or d.get("status") not in (None, "ok"):
                print(f"{el}          ↳ error ({d.get('status', 'failed')})", flush=True)
        elif t == "turn_end":
            turn += 1
            m = e.get("message") or {}
            u = m.get("usage") or {}
            tokens_in += u.get("input", 0) or 0
            tokens_out += u.get("output", 0) or 0
            c = u.get("cost")
            cost += c.get("total", 0) if isinstance(c, dict) else 0
            texts = [x.get("text", "") for x in m.get("content", []) if x.get("type") == "text"]
            if texts and any(x.strip() for x in texts):
                final = "\n".join(texts)
            print(f"{el}  turn {turn:>2}  tokens in {tokens_in:,} · out {tokens_out:,} · "
                  f"cost ${cost:.4f}", flush=True)
    if final:
        print(f"Prime's last words: {final.strip().splitlines()[-1][:200]}", flush=True)
    return final


# -------------------------------------------------------------- commits ----

def check_and_push(ctx: dict, prime_head: str, command: str = "") -> tuple[str, list[str], str]:
    """Return (new head, list of pushed commit lines, note)."""
    if not prime_head or prime_head == ctx["head"]:
        return ctx["head"], [], ""
    if not command_routing.can_push_commits(command):
        return ctx["head"], [], "ask command is read-only; commits not pushed"
    bundle = OUT / "commits.bundle"
    if not regular_file(bundle) or git(["fetch", "--quiet", "--no-tags", str(bundle), "HEAD"],
                                       PR_DIR, check=False).returncode != 0:
        return ctx["head"], [], "its commits could not be collected; not pushed"
    new_head = git(["rev-parse", "FETCH_HEAD"], PR_DIR).stdout.strip()
    if git(["merge-base", "--is-ancestor", ctx["head"], new_head], PR_DIR,
           check=False).returncode != 0:
        return ctx["head"], [], "the reviewer rewrote history; its commits were dropped"
    git(["reset", "--quiet", "--hard", new_head], PR_DIR)
    # --no-renames lists both sides of a move, so moving a protected file out
    # of its folder is still caught; -z keeps unusual names unquoted.
    changed = [p for p in git(["diff", "--no-renames", "--name-only", "-z",
                               f"{ctx['head']}..{new_head}"], PR_DIR).stdout.split("\0") if p]
    # The configured list guards against fixes nobody asked for. A fix or
    # instruction is the owner's own request, so only workflows stay closed.
    deny = ALWAYS_DENY if command_routing.owner_asked_for_changes(command) else DENY
    bad = [p for p in changed if deny.match(p)]
    if bad:
        git(["reset", "--hard", ctx["head"]], PR_DIR)
        return ctx["head"], [], f"its commits touched protected paths ({', '.join(bad)}); not pushed"
    if CONFIG.get("fix_check") and not run_fix_check():
        git(["reset", "--hard", ctx["head"]], PR_DIR)
        return ctx["head"], [], "the repo's check failed after its commits; not pushed"
    if ctx["pr"]["state"] != "open":
        git(["reset", "--hard", ctx["head"]], PR_DIR)
        return ctx["head"], [], "the pull request is closed; commits not pushed"
    git(["push", "--quiet", "origin", f"HEAD:refs/heads/{ctx['pr']['head']['ref']}"],
        PR_DIR, auth=True)
    commits = git(["log", "--format=%h %s", f"{ctx['head']}..{new_head}"], PR_DIR).stdout.splitlines()
    return new_head, commits, ""


def run_fix_check() -> bool:
    """The configured check, as PRIME_USER, on a fresh copy of exactly the
    commits about to be pushed. It gets no credentials."""
    check = JAIL / "check"
    sudo(["rm", "-rf", "--one-file-system", str(check)])
    git(["clone", "--quiet", "--no-hardlinks", str(PR_DIR), str(check)], WORK)
    give_to_prime(check, JAIL / "tmp")
    # The job's PATH, so toolchains set up before the action are found.
    r = as_prime(["bash", "-c", CONFIG["fix_check"]], chdir=check, check=False,
                 env={"RULES_DIR": str(JAIL / "rules"), "PATH": os.environ.get("PATH", PRIME_PATH)})
    stop_prime_processes()
    take_from_prime()
    return r.returncode == 0


# --------------------------------------------------------------- review ----

def diff_lines(base: str, head: str) -> dict[str, set[int]]:
    """Right-side line numbers GitHub accepts comments on, per file."""
    out: dict[str, set[int]] = {}
    diff = git(["diff", "--unified=3", "--no-color", f"{base}..{head}"], PR_DIR).stdout
    path, n = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if path is None or line.startswith(("---", "diff ", "index ")):
            continue
        if line.startswith("+") or line.startswith(" "):
            out.setdefault(path, set()).add(n)
            n += 1
    return out


LABELS = {1: "🔧 Fixed", 2: "✏️ Suggested fix", 3: "💡 Design question"}


def finding_body(f: dict) -> str:
    """Plain label first, the short explanation, then the one-click fix."""
    label = "🛑 Blocker" if f.get("blocker") else LABELS.get(f.get("level"), "Note")
    b = f"{label} — **{f.get('title', '').strip()}**\n\n{f.get('body', '').strip()}"
    if f.get("level") == 2 and f.get("suggestion") is not None:
        b += "\n\n```suggestion\n" + f["suggestion"].rstrip("\n") + "\n```"
    b += f"\n\n<sub>level {f.get('level')} · rules section {f.get('section')}</sub>"
    return b


def post_review(ctx: dict, new_head: str, res: dict, commits: list[str], note: str,
                meta: dict, learn_url: str) -> None:
    result = res["result"]
    if result is not None and res.get("mode") == "update":
        result = dict(result)
        result["findings"] = [f for f in result.get("findings") or [] if not f.get("carried")]
        if not result["findings"] and not commits and not note and not learn_url:
            log("update review: nothing new to say, no comment posted")
            return
    footer = (f"\n\n<sub>run `{RUN_ID}` · {meta['duration_s']}s · "
              f"${meta['usage']['cost']:.3f} · [log]({RUN_URL})</sub>")
    if result is None:
        body = ("Prime finished without a result file. Last lines of its answer:\n\n"
                + "\n".join("> " + l for l in res["stdout"].strip().splitlines()[-15:]))
        gh_api(f"repos/{REPO}/issues/{PR}/comments", "POST", {"body": body + footer})
        return

    parts = []
    if res.get("mode") == "instruct":
        parts.append(result.get("reply", "").strip() or "(no reply)")
    else:
        icon = {"approve": "✅", "request_changes": "🛑", "needs_owner": "👤"}.get(result.get("verdict"), "❔")
        parts.append(f"{icon} **{result.get('verdict', '?').replace('_', ' ')}** — {result.get('summary', '').strip()}")
    if commits:
        parts.append("🔧 **Fixed for you**\n" + "\n".join(f"- {c}" for c in commits))
    if note:
        parts.append(f"**Not pushed:** {note}")

    lines = diff_lines(ctx["base"], new_head)
    inline, rest = [], []
    for f in result.get("findings", []) or []:
        path, line = f.get("path"), f.get("line")
        start = f.get("start_line") or line
        ok = (isinstance(line, int) and isinstance(start, int) and start <= line
              and path in lines and all(n in lines[path] for n in range(start, line + 1)))
        if ok:
            c = {"path": path, "line": line, "side": "RIGHT", "body": finding_body(f)}
            if start != line:
                c.update(start_line=start, start_side="RIGHT")
            inline.append(c)
        else:
            rest.append(f)
    if rest:
        parts.append("**Elsewhere in the repo**\n\n" + "\n\n---\n\n".join(
            f"`{f.get('path')}:{f.get('line')}`\n" + finding_body(f) for f in rest))
    if learn_url:
        parts.append(f"I proposed a change to my playbook: {learn_url}")
    body = "\n\n".join(parts) + footer

    if res.get("mode") == "instruct" and not inline:
        if EVENT == "pull_request_review_comment":
            r = gh_api(f"repos/{REPO}/pulls/{PR}/comments/{os.environ['COMMENT_ID']}/replies",
                       "POST", {"body": body}, check=False)
            if r:
                return
        gh_api(f"repos/{REPO}/issues/{PR}/comments", "POST", {"body": body})
        return
    review = {"commit_id": new_head, "event": "COMMENT", "body": body, "comments": inline}
    r = run(["gh", "api", "-X", "POST", f"repos/{REPO}/pulls/{PR}/reviews", "--input", "-"],
            check=False, env=gh_env(), input_=json.dumps(review))
    if r.returncode != 0:
        log("inline review rejected; posting everything as one comment")
        allf = "\n\n".join(f"`{c['path']}:{c['line']}` — {c['body']}" for c in inline)
        gh_api(f"repos/{REPO}/issues/{PR}/comments", "POST",
               {"body": body.replace(footer, "") + ("\n\n" + allf if allf else "") + footer})


def review_is_clean(res: dict, blockers: list, note: str, head: str, new_head: str) -> bool:
    """True when this run may vouch for the commit it reviewed. A commit the
    reviewer just pushed gets its own follow-up review, which decides for it."""
    return (res["result"] is not None and res["exit"] == 0
            and res["result"].get("verdict") == "approve"
            and not blockers and not note and new_head == head)


def sync_approval(head: str, clean: bool) -> None:
    """Make the App's approval match the review that just finished.

    The approval is a separate review so that a rejected approval cannot lose
    the findings, and a failure here never fails the check: the pull request
    just stays unapproved, or the log says an approval could not be withdrawn.
    """
    if not APPROVE_CLEAN:
        return
    try:
        bot = f"{os.environ['APP_SLUG']}[bot]"
        mine = [r for r in gh_api(f"repos/{REPO}/pulls/{PR}/reviews?per_page=100")
                if (r.get("user") or {}).get("login") == bot and r.get("state") == "APPROVED"]
        if not clean:
            for r in mine:
                gh_api(f"repos/{REPO}/pulls/{PR}/reviews/{r['id']}/dismissals", "PUT",
                       {"message": "A later review did not end in approve."})
                log(f"withdrew approval {r['id']}")
            return
        if any(r.get("commit_id") == head for r in mine):
            log(f"commit {head[:7]} is already approved")
            return
        # A push during the run is reviewed by the next run; do not vouch for it.
        pr = gh_api(f"repos/{REPO}/pulls/{PR}")
        if pr["state"] != "open" or pr["head"]["sha"] != head:
            log(f"not approving {head[:7]}: the pull request is closed or has moved on")
            return
        gh_api(f"repos/{REPO}/pulls/{PR}/reviews", "POST", {
            "commit_id": head, "event": "APPROVE",
            "body": f"Clean review at this commit (run `{RUN_ID}`). Merging is the owner's call.",
        })
        log(f"approved {head[:7]}")
    except Exception as e:
        log(f"warning: could not update the approval: {e.__class__.__name__}: {str(e)[:300]}")


# ---------------------------------------------------------------- state ----

def child_dirs() -> list[Path]:
    """Sub-agent transcripts of this run: Prime writes them under the agent
    folder, session-artifacts/<root session id>/<child id>/*.jsonl."""
    root = state_store.newest_session_file(SESSION)
    if not root:
        return []
    d = AGENT / "session-artifacts" / root.stem
    return [d] if d.is_dir() else []


def add_usage(u: dict, path: Path, skip: int = 0) -> None:
    for i, line in enumerate(open(path, errors="replace")):
        if i < skip:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "tool_started":
            u["tool_calls"] += 1
        m = e.get("message") or {}
        if m.get("role") == "assistant":
            u["turns"] += 1
            x = m.get("usage") or {}
            u["input"] += x.get("input", 0) or 0
            u["output"] += x.get("output", 0) or 0
            u["cache_read"] += x.get("cacheRead", 0) or 0
            c = x.get("cost")
            u["cost"] += c.get("total", 0) if isinstance(c, dict) else 0


def usage_since(lines_before: int) -> dict:
    """Usage of this run: the root session's new lines plus every sub-agent."""
    u = {"turns": 0, "tool_calls": 0, "input": 0, "output": 0, "cache_read": 0,
         "cost": 0.0, "subagent_transcripts": 0}
    root = state_store.newest_session_file(SESSION)
    if root:
        add_usage(u, root, lines_before)
    for d in child_dirs():
        for f in sorted(d.rglob("*.jsonl")):
            u["subagent_transcripts"] += 1
            add_usage(u, f)
    u["cost"] = round(u["cost"], 5)
    return u


def save_state(meta: dict, res: dict) -> None:
    if B2_MODE:
        store = get_b2_store()
        if not store:
            raise state_store.StorageError("B2 store uninitialized")
        store.save_state(
            run_id=RUN_ID,
            meta=meta,
            result=res.get("result"),
            session_dir=SESSION,
            child_dirs=child_dirs(),
            config_commit=meta.get("config_commit", ""),
            session_lines_before=res.get("lines_before", 0),
        )
        return

    saved = STATE / "sessions" / REPO_NAME / f"pr-{PR}"
    if saved.exists():
        shutil.rmtree(saved)
    shutil.copytree(SESSION, saved)
    trace = STATE / "traces" / RUN_ID
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    if res["result"] is not None:
        (trace / "result.json").write_text(json.dumps(res["result"], indent=2) + "\n")
    f = state_store.newest_session_file(SESSION)
    if f:
        shutil.copy(f, trace / "session.jsonl")
    for d in child_dirs():
        dest = trace / "subagents"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(d, dest)
    (STATE / "README.md").write_text(STATE_README)
    git(["add", "-A"], STATE)
    git(["commit", "--quiet", "-m", f"Run {RUN_ID} on {REPO}#{PR} ({meta['mode']})"], STATE)
    for attempt in range(4):
        if git(["push", "--quiet", "origin", "HEAD:refs/heads/state"], STATE, auth=True,
               check=False).returncode == 0:
            return
        git(["pull", "--quiet", "--rebase", "origin", "state"], STATE, auth=True, check=False)
        time.sleep(2 + attempt * 3)
    raise RuntimeError("could not save state after retries")


def propose_learning(command: str = "") -> str:
    if not command_routing.can_propose_learning(command):
        return ""
    allowed = [str(PLAYBOOK.relative_to(AGENT) / "SKILL.md"),
               "skills/common-review/SKILL.md", "harness"]
    allowed = [p for p in allowed if (AGENT / p).exists()
               or git(["ls-files", "--", p], AGENT).stdout.strip()]
    git(["reset", "--quiet"], AGENT)
    git(["add", "-A", "--", *allowed], AGENT)
    if not git(["diff", "--cached", "--name-only"], AGENT).stdout.strip():
        return ""
    branch = f"learn/{REPO_NAME}-pr-{PR}-{RUN_ID}"
    git(["checkout", "--quiet", "-b", branch], AGENT)
    git(["commit", "--quiet", "-m", f"Lesson from {REPO}#{PR} (run {RUN_ID})"], AGENT)
    git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], AGENT, auth=True)
    diff = git(["show", "--stat", "--patch", "HEAD"], AGENT).stdout[:60000]
    pr = gh_api(f"repos/{CONFIG_REPO}/pulls", "POST", {
        "title": f"Lesson from {REPO}#{PR}",
        "head": branch, "base": CONFIG_REF,
        "body": (f"The reviewer changed its own playbook during run `{RUN_ID}` on "
                 f"{REPO}#{PR}. Merge to keep it, close to drop it.\n\n```diff\n{diff}\n```"),
    }, check=False)
    return (pr or {}).get("html_url", "")


# ----------------------------------------------------------------- main ----

def pr_traces() -> list[dict]:
    out = []
    for f in (STATE / "traces").glob("*/meta.json"):
        try:
            m = json.loads(f.read_text())
        except ValueError:
            continue
        if str(m.get("pr")) == str(PR) and m.get("repo") == REPO:
            out.append(m)
    return sorted(out, key=lambda m: m.get("started", ""))


def gate_blockers(result: dict | None) -> list[dict]:
    if result is None:
        return [{"section": 0, "title": "the reviewer produced no result"}]
    return [
        {"section": f.get("section"), "path": f.get("path"), "line": f.get("line"),
         "title": f.get("title", "")}
        for f in result.get("findings") or []
        if f.get("blocker") and f.get("section") in BLOCKING_SECTIONS
    ]


def write_summary(meta: dict) -> None:
    if not SUMMARY:
        return
    u = meta.get("usage") or {}
    store = get_b2_store() if B2_MODE else None
    trace_label = (
        f"b2:{store.trace_key(RUN_ID)}"
        if B2_MODE and store
        else f"state:traces/{RUN_ID}/"
    )
    rows = [
        ("Mode", meta.get("mode")), ("Verdict", meta.get("verdict")),
        ("Blocking findings", len(meta.get("gate_blockers") or [])),
        ("Findings", meta.get("findings")), ("Fixes pushed", meta.get("fixes_pushed")),
        ("Duration", f"{meta.get('duration_s', 0)} s"),
        ("Model turns / tool calls", f"{u.get('turns', 0)} / {u.get('tool_calls', 0)}"),
        ("Tokens in / out / cache read",
         f"{u.get('input', 0):,} / {u.get('output', 0):,} / {u.get('cache_read', 0):,}"),
        ("Cost (Prime's estimate)", f"${u.get('cost', 0):.4f}"),
        ("Model", meta.get("model")), ("Trace", f"`{trace_label}`"),
    ]
    if meta.get("ci_summary"):
        rows.append(("CI status", meta["ci_summary"]))
    if meta.get("push_note"):
        rows.append(("Not pushed", meta["push_note"]))
    if meta.get("playbook_pr"):
        rows.append(("Playbook change", meta["playbook_pr"]))
    with open(SUMMARY, "a") as f:
        f.write(f"### Prime reviewer · PR #{PR}\n\n| | |\n|---|---|\n")
        f.writelines(f"| {k} | {v} |\n" for k, v in rows)


def verify_legacy_snapshot(meta: dict | None) -> None:
    """An imported conversation must include the last legacy writer before cutover."""
    expected = (meta or {}).get("legacy_source_commit")
    if not expected:
        return
    current = gh_api(f"repos/{CONFIG_REPO}/git/ref/heads/state")
    if (current or {}).get("object", {}).get("sha") != expected:
        raise state_store.StorageError(
            "Legacy state changed after import; refresh the B2 migration before continuing")


def update_status(state, head=None, reviewed=None, ci=None, note=None):
    """Update only this App's marked overview; never edit a user's comment."""
    body = review_state.status_body(state, RUN_URL, head, reviewed, ci, note)
    for page in range(1, 11):
        comments = gh_api(f"repos/{REPO}/issues/{PR}/comments?per_page=100&page={page}")
        for comment in comments or []:
            if (comment.get("user", {}).get("login") == f"{os.environ['APP_SLUG']}[bot]"
                    and comment.get("body", "").startswith(review_state.MARKER)):
                gh_api(f"repos/{REPO}/issues/comments/{comment['id']}", "PATCH", {"body": body})
                return
        if len(comments or []) < 100:
            break
    gh_api(f"repos/{REPO}/issues/{PR}/comments", "POST", {"body": body})


def main() -> int:
    if GATE and os.environ.get("PR_AUTHOR") != OWNER:
        log("not reviewed: only the owner's pull requests are; the owner reviews this one by hand")
        return 1

    checkout_state()

    latest_meta = None
    if B2_MODE:
        store = get_b2_store()
        if SESSION.exists():
            shutil.rmtree(SESSION)
        latest_meta = store.restore_session(SESSION) if store else None
        verify_legacy_snapshot(latest_meta)

    has_saved = (
        (latest_meta is not None and SESSION.is_dir() and any(SESSION.glob("*.jsonl")))
        if B2_MODE
        else (saved_session().exists() and any(saved_session().glob("*.jsonl")))
    )

    mode, instruction, focus, command = parse_mode(has_saved=has_saved)
    log(f"mode: {mode} (command: {command}, {EVENT} {ACTION})")
    react("eyes")

    if mode == "carry":
        head = os.environ.get("HEAD_SHA", "")
        if B2_MODE:
            if latest_meta and review_state.reviewed_sha(latest_meta) == head:
                blockers = latest_meta.get("gate_blockers") or []
                prior_run = latest_meta.get("run_id", "prior")
                log(f"commit {head[:7]} was pushed by the reviewer in run {prior_run}: "
                    f"reusing its verdict ({len(blockers)} blocking)")
                for b in blockers:
                    log(f"blocking: section {b.get('section')} {b.get('path')}:{b.get('line')} {b.get('title')}")
                return 1 if blockers else 0
            log("no earlier run produced this commit: reviewing the update instead")
            mode = "update"
            command = "update"
        else:
            prior = [m for m in pr_traces() if review_state.reviewed_sha(m) == head]
            if prior:
                blockers = prior[-1].get("gate_blockers") or []
                log(f"commit {head[:7]} was pushed by the reviewer in run {prior[-1]['run_id']}: "
                    f"reusing its verdict ({len(blockers)} blocking)")
                for b in blockers:
                    log(f"blocking: section {b.get('section')} {b.get('path')}:{b.get('line')} {b.get('title')}")
                return 1 if blockers else 0
            log("no earlier run produced this commit: reviewing the update instead")
            mode = "update"
            command = "update"

    ctx = checkout_pr()
    ci_data = ci_evidence.fetch_ci_snapshot(
        repo=REPO,
        head_sha=ctx["head"],
        app_slug=os.environ.get("APP_SLUG", ""),
        gh_api_fn=gh_api,
    )
    log(f"CI evidence: {ci_data.get('overall_state')} ({ci_data.get('summary', {})})")

    if B2_MODE:
        ctx["last"] = review_state.reviewed_sha(latest_meta)
    else:
        traces = pr_traces()
        latest_meta = traces[-1] if traces else None
        ctx["last"] = review_state.reviewed_sha(latest_meta)

    if mode == "update" and not ctx["last"]:
        mode = command = "review"
    update_status("Working", ctx["head"], ctx["last"], ci_data.get("overall_state"))

    if not B2_MODE:
        git(["remote", "set-url", "origin", f"https://github.com/{CONFIG_REPO}.git"], AGENT)

    res = run_prime(ctx, mode, instruction, focus, command=command, ci_data=ci_data)
    res["mode"] = mode
    res["command"] = command
    result = res["result"] or {}

    if res["result"] is None:
        blockers = [{"section": 0, "title": "the reviewer produced no result"}]
    elif mode == "instruct":
        current_blockers = gate_blockers(res["result"])
        prior_blockers = []
        if latest_meta and latest_meta.get("gate_blockers"):
            prior_blockers = latest_meta.get("gate_blockers")
        elif not B2_MODE:
            traces = pr_traces()
            if traces and traces[-1].get("gate_blockers"):
                prior_blockers = traces[-1].get("gate_blockers")
        blockers = review_state.merge_blockers(prior_blockers, current_blockers)
    else:
        blockers = gate_blockers(res["result"])

    reviewed, blockers = review_state.checkpoint(
        latest_meta, mode, ctx["head"], blockers,
        res["result"] is not None and res["exit"] == 0)

    config_commit = ""
    if (AGENT / ".git").exists():
        try:
            config_commit = git(["rev-parse", "HEAD"], AGENT).stdout.strip()
        except Exception:
            config_commit = ""

    meta = {
        "run_id": RUN_ID, "run_url": RUN_URL, "repo": REPO, "pr": int(PR), "mode": mode,
        "command": command,
        "config_ref": CONFIG_REF, "config_commit": config_commit,
        "event": EVENT, "action": ACTION, "model": MODEL,
        "prime_version": os.environ.get("PRIME_AGENT_VERSION"),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(res["started"])),
        "duration_s": res["duration"], "prime_exit": res["exit"],
        "head_before": ctx["head"], "head_after": ctx["head"], "base": ctx["base"],
        "last_reviewed": ctx["last"],
        "reviewed_sha": reviewed,
        "verdict": result.get("verdict"),
        "findings": len(result.get("findings") or []), "fixes_pushed": 0,
        "gate_blockers": blockers, "push_note": "", "playbook_pr": "", "error": "",
        "session_lines_before": res["lines_before"],
        "usage": usage_since(res["lines_before"]),
        "ci_evidence": ci_data,
        "ci_summary": f"{ci_data['overall_state']} ({ci_data['summary']['pass']} pass, {ci_data['summary']['fail']} fail, {ci_data['summary']['pending']} pending)",
    }
    # The trace is saved whatever happens next: a failed run is the one a
    # trace viewer is most needed for.
    try:
        if res["result"] is None or res["exit"] != 0:
            new_head, commits, note = ctx["head"], [], "review execution failed; commits not pushed"
        else:
            new_head, commits, note = check_and_push(ctx, res["prime_head"], command=command)
        meta.update(head_after=new_head, fixes_pushed=len(commits), push_note=note)
        learn_url = (propose_learning(command=command)
                     if res["result"] is not None and res["exit"] == 0 else "")
        meta["playbook_pr"] = learn_url
        post_review(ctx, new_head, res, commits, note, meta, learn_url)
        if mode in ("review", "update"):
            sync_approval(ctx["head"], review_is_clean(res, blockers, note, ctx["head"], new_head))
    except Exception as e:
        meta["error"] = f"{e.__class__.__name__}: {str(e)[:300]}"
        raise
    finally:
        save_state(meta, res)
        write_summary(meta)
    failed = res["result"] is None or res["exit"] != 0
    state = "Failed" if failed else "Blocked" if note or blockers else "Completed"
    update_status(state, meta["head_after"], reviewed, ci_data.get("overall_state"),
                  "The requested changes were not pushed; see the reply for details." if note else
                  "Review has unresolved blockers." if blockers else
                  "Execution completed. This does not establish that the PR is mergeable.")
    react("rocket" if res["result"] is not None else "confused")
    log(f"done: {json.dumps({k: meta[k] for k in ('verdict', 'findings', 'fixes_pushed', 'duration_s')})}")
    if failed or note:
        return 1
    if GATE and blockers:
        for b in blockers:
            log(f"blocking: section {b.get('section')} {b.get('path')}:{b.get('line')} {b.get('title')}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # report, never dump the environment
        try:
            update_status("Failed", note="The run failed. See the run log for details.")
        except Exception:
            pass
        log(f"failed: {e.__class__.__name__}: {str(e)[:500]}")
        react("confused")
        sys.exit(1)
