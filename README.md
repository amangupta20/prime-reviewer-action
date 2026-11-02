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

## B2 runtime storage

Set `state-backend: b2` and provide `b2-endpoint-url`, `b2-bucket`, `b2-region`,
`b2-access-key-id` and `b2-secret-access-key` using caller secrets. The key is
scoped to the private reviewer bucket, with read, list and write access.
The action installs boto3 1.42.4. Its model subprocess never receives those
credentials. Older callers default to `auto`, retaining Git state when no B2
inputs are supplied; partial B2 input is an error.

B2 keys are namespaced by owner, repository and PR. A completed run uploads
its raw trace bundle and session snapshot before updating its latest pointer.
Only this PR's latest state is fetched for review/update/carry mode. Missing
saved state on an instruction is an error. Storage failures fail the job.
No retention or deletion runs automatically. Bucket names and credentials
are omitted from public summaries.

`observability.md` describes how a future importer can replay these private
raw archives into a chosen viewer. No exporter or viewer is deployed yet.

`migrate_state.py` imports a clean, frozen Git state checkout. Default mode
checks source and destination without writing; `--apply` uploads additive
objects, then downloads sessions and root/child transcripts and compares
contents. It rejects a destination with different existing state. Old Git
state remains intact. The coordinator must finish legacy runs and use their
last state snapshot before switching the caller to B2.

Example (replace paths; UNTESTED until deployment verification):

```sh
python3 -I migrate_state.py --source /path/to/state-checkout --repo owner/repo
python3 -I migrate_state.py --source /path/to/state-checkout --repo owner/repo --apply
```

Before the first B2 use of an imported conversation, the wrapper compares
its recorded legacy source commit with the private Git state branch. A newer
legacy write fails explicitly and requires a migration refresh. Native B2
reviews no longer depend on that Git branch. This protects replies added
between the final import and the caller merge.
