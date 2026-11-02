# Preserve the trace archive contract

Finish two concrete warnings from /tmp/prime-reviewer-b2-review.md. Scope is
state_store.py, migrate_state.py and focused tests in this action repo. Do
not edit action.yml, caller repos, credentials, live services or docs outside
this checkout. Do not use gh or start other agents. Commit your changes.

1. list_traces must follow list_objects_v2 continuation tokens until complete.
   Test a truncated fake response and confirm the next token is used.
2. Migration preflight/reapply/final --refresh-imports must preserve existing
   trace archive bytes and original manifest provenance. Verify existing root
   and child transcripts and structured result content against the source
   before reusing a key; fail if they disagree. Upload only new run archives.
   Also avoid rewriting an existing trace from save_state when a refreshed
   import still has the same latest run ID. Preserve immutable session snapshot
   bytes/metadata where practical; pointer metadata may record the new import
   source. Never overwrite a native B2 review during legacy migration.
   Test repeated import and a later legacy run: old archive bytes remain equal,
   only the new archive is added, latest pointer advances, and a changed source
   transcript at an existing run ID is rejected rather than overwritten.

Keep changes focused and public errors private-safe. Fake S3 clients only;
no live B2 or model calls. Do not introduce subprocess tests that discover or
run their own suite. The earlier OOM came from that exact recursive spawning.
Use only this bounded command, adapting its source path to this worktree:

systemd-run --user --pipe --wait --collect --property=MemoryMax=512M --property=TasksMax=24 --property=RuntimeMaxSec=30s --working-directory=/tmp /tmp/prime-reviewer-b2-venv/bin/python -I -m unittest discover -s /absolute/path/to/this/worktree/tests -v

Report commit hash, changes and capped test evidence via worker_done. Do not
stop for approval. Do not broaden the review or change model/provider routing.
