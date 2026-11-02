# Prime reviewer action

A composite GitHub Action that runs Prime Agent on the repository owner's
pull requests and `/prime` comments. It pushes checked mechanical fixes,
posts suggestions and design questions, and saves private sessions and traces.

The caller keeps its job name (`ai review` for the required check). The action
contains machinery; prompts, model settings, playbooks and state live in the
owner's private `prime-reviewer` repository. Every repository has a folder in
that private repository, sharing one core and common lessons.

Implemented 2026-11-03. Offline regression tests cover repository isolation,
legacy session restore, gate configuration, protected paths and lesson routing.
A live GitHub run of this composite action is UNTESTED until a caller merges.

## Caller

Copy `examples/prime-review.yml` into the target repository's workflows.
Install the reviewer App on the target and private config repositories, both
under the same owner. Set variable `PRIME_APP_ID` and secrets
`PRIME_APP_PRIVATE_KEY` and `OPENCODE_API_KEY` on each caller repository.
Personal repositories configure these separately.

The action must run on `ubuntu-latest` with Python 3.11+, Git and `gh`.
It installs the pinned Prime version; add toolchain setup before the action if
trusted repository checks require it. Pull-request runs use
`pull_request_target`; keep the owner guard first inside the action.

Use `@v1` for the shared release or a commit SHA for an immutable version.
`config-repo` defaults to `<owner>/prime-reviewer`, `config-ref` to `main`,
`prime-version` to `0.10.2`. `model` overrides the repository's configured model.

## Private configuration

Required `repos/<repo>/config.toml` fields:

```toml
model = "deepseek-v4.1-flash"
rules = ["AGENTS.md"]
playbook = "repos/example/playbook"
protected = ['^iac/', '^AGENTS\.md$']
fix_check = 'python3 -I "$RULES_DIR/check.py"'
blocking_sections = [1, 5]
```

This is an UNTESTED example: replace paths and section numbers with your
repository's rules. The playbook must contain `SKILL.md` and live in the
matching repository folder. Rules and checks come from the target's default
branch. `fix_check` is a trusted shell command run in the pull-request working
directory; never use it to execute scripts added or changed by the pull request.
Empty check and blocking-section values explicitly opt out; missing fields fail.

Sessions use `sessions/<repo>/pr-<n>/` on the private `state` branch. Historical
homelab sessions have a read fallback. Trace metadata includes the full repo,
config ref, run, usage, result and head commits. Proposed lesson pull requests
can stage only the current playbook, common-review and harness lessons.

## Validation

Run `python3 -I -m unittest discover -s tests -v` (run locally 2026-11-03).
The test suite uses temporary local repositories and fake API calls, never
model keys or GitHub writes. A live model review is separate validation.

## Icon

`assets/prime-reviewer-icon.png` is the generated App avatar. Created with
the built-in image generation tool on 2026-11-03. Prompt: a friendly robot
owl with a check-mark face and prime accent, bold flat geometry on a dark
square background, legible at avatar size, no text or existing brand logos.
