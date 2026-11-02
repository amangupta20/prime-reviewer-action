# Review B2 persistence before release

Read-only review of the B2 changes in this action (since tag v1), the caller
workflow and docs in /home/aman/projects/homelab, and the model config in
/home/aman/src/prime-reviewer. Follow homelab/repo/review.md. Do not use gh,
access credentials, call external services, edit source, or start other agents.
Write /tmp/prime-reviewer-b2-review.md and report worker_done.

Check session continuation, repository isolation, upload ordering and errors,
Git compatibility while the caller PR remains unmerged, private log handling,
raw trace provenance for later import, and migration overwrite protection.
No viewer deployment is claimed. The selected Muse Spark model was smoke-tested.

Important: a previous worker added tests that recursively launched their own
suite and caused a workstation OOM. Those tests were removed before this
review. Inspect subprocess calls before running anything. If tests are needed,
use the capped process below ONLY; never launch a suite from inside a test:

systemd-run --user --pipe --wait --collect --property=MemoryMax=512M --property=TasksMax=24 --property=RuntimeMaxSec=30s --working-directory=/tmp /tmp/prime-reviewer-b2-venv/bin/python -I -m unittest discover -s /home/aman/src/prime-reviewer-action/tests -v

The coordinator ran 22 tests successfully from an unrelated working directory,
with peak memory below 90 MiB. Do not reproduce an OOM. Report concrete findings
and your final verdict, no source edits. Do not stop for approval.
