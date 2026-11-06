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
The first live run was a review on homelab pull request 31 (2026-11-03,
release `v1.1.0`, B2 storage, approve). A live `/prime` reply is UNTESTED.

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

`approve-clean: "true"` (default `"false"`) makes a review or update run approve
its commit as the App, only when the verdict is `approve`, no blocker is open
and the run pushed nothing. A later review or update of the pull request that
is not clean dismisses the App's approvals. Replies never change an approval.
A required approval needs the App to have write access to the repository, and
the ruleset's last-push approval off, because the App may push the last commit.
A live approval is UNTESTED.

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

With `state-backend: b2`, sessions and traces live in B2 (see "B2 runtime
storage"). Without B2 inputs, sessions use `sessions/<repo>/pr-<n>/` on the
private `state` branch, with a read fallback for historical homelab sessions.
Trace metadata includes the full repo,
config ref, run, usage, result and head commits. Proposed lesson pull requests
can stage only the current playbook, common-review and harness lessons.

## Commands

The action parses `/prime` comments on pull requests and review threads:

- `/prime review`: triggers a full review.
- `/prime review <focus>`: triggers a full review focused on the requested topic (e.g. `/prime review security`).
- `/prime ask <question>`: read-only question answering. Prime answers without making code changes or pushing commits, and without proposing playbook changes.
- `/prime fix <instruction>`: implements a requested code fix and pushes checked commits.
- `/prime <natural instruction>`: backwards-compatible natural instruction.

A strict `/prime` token boundary is enforced: commands like `/primer` are rejected.
Instruction runs (`ask`, `fix`, `instruct`) continue the PR's saved session and preserve
existing gate blockers from earlier review runs.

## CI evidence

At run start, the reviewer captures a single exact-head snapshot of GitHub check
runs and commit statuses for the PR head commit and provides it to Prime as
bounded, untrusted context:

- Excludes the reviewer action's own checks (`ai review`, `prime`, and App slug).
- Distinguishes `pass`, `fail`, `pending`, `skipped`, `incomplete`, and `unavailable` states.
- Aggregate status is computed across all fetched checks before display bounds,
  ensuring failures are not hidden behind truncation limits.
- Evaluates endpoint and pagination completeness: partial endpoint failures or unreturned
  rows mark the snapshot as incomplete and prevent false-pass verdicts.
- Only check names, types, and states are ingested; arbitrary log output or step summaries
  are never fetched, avoiding credential exposure.
- No automatic workflow reruns or polling are triggered.

## Interaction status and review coverage

The action maintains one marked PR comment showing Working, Completed,
Blocked or Failed, the run link, PR commit, last reviewed commit and CI state
at run start. A rejected push fails the command run and shows Blocked.
Completed means execution completed, not that GitHub can merge the PR.
Hard runner cancellation can leave Working visible; follow its run link.

Conversation snapshots and review coverage are separate. Questions and fixes
preserve the last reviewed commit and existing blockers; only a successful
review can advance coverage and clear blockers. Reviewer-generated commits
receive a follow-up review unless that exact commit already has coverage.
Historical instruction snapshots with unknown coverage cause a full review.

CI evidence requires the App to read checks and commit statuses. Missing
access is reported as unavailable, never passing. Live interaction validation
of this update is pending rollout of the caller pin.

## Isolation

Prime runs as a separate Linux user, `prime-runner`, created by the action.
It works on fresh copies of the pull request, rules, private config and
session under `/srv/prime-review`; the rules copy is read-only to it. The
wrapper keeps the App token and storage keys out of its own environment,
passes the model key through `sudo`'s preserved environment, and gives git
the token through the environment rather than the command line. After Prime
exits, all its processes are killed, its commits come back as a git bundle,
and only regular files are copied back (session, sub-agent transcripts and
the lesson files it may edit). The configured check also runs as
`prime-runner`, on a fresh copy of the commits about to be pushed. Prime is
installed fresh for that user on every run instead of from a cache.
UNTESTED on GitHub until the first run of a commit that contains it.

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
