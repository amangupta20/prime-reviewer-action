# Implement B2 persistence for Prime reviewer

Owner approved moving PR sessions and raw traces from Git to a private B2
bucket, retaining prompts/lessons in Git, and using Muse Spark 1.3 Contributor.
This task owns only the storage implementation and tests in this public action
repository. The coordinator owns credentials, private config, homelab workflow,
model activation, docs outside this repo, release, and GitHub operations.

Implement using boto3 (pinned dependency installed by action), but do not edit
action.yml; tell the coordinator the required environment and install command.
Create state_store.py and integrate prime-review.py. Secrets must never reach
Prime's scrubbed subprocess environment or public logs.

Requirements:
- Preserve existing Git state mode when B2 inputs are absent so published v1
  remains compatible while the caller PR awaits merge. B2 mode requires all
  B2_ENDPOINT_URL, B2_BUCKET, B2_REGION, B2_ACCESS_KEY_ID, B2_SECRET_ACCESS_KEY.
- In B2 mode, restore only this PR's session and latest metadata; do not clone
  the Git state branch. Use owner/repo/PR namespacing for all mutable keys.
- Compress the whole session directory into a tar.gz; safe extraction must
  reject traversal, absolute paths, symlinks and hard links. Preserve full
  Prime JSONL required for -c continuation. Store metadata alongside it.
- Upload each run's raw root transcript, subagent transcripts, result and meta
  as a compressed immutable bundle. Include schema_version and source fields
  in a manifest so a future importer can replay into Phoenix/Langfuse/OpenObserve.
  Do not implement a viewer or claim the raw files are already OTLP.
- Commit the per-PR latest pointer only AFTER its referenced immutable session
  snapshot/run bundle has uploaded. One writer per PR is supplied by caller
  concurrency. Different repos and PRs must not contend on a global object.
- B2 read/auth/network errors fail the run; only NoSuchKey/404 means absent.
  In instruct mode a missing saved session must fail explicitly rather than
  silently start a fresh conversation. Persistence failure must fail the job.
- Preserve current gate carry/update behavior via this PR's latest metadata;
  no global listing/scan of all old traces. Keep immutable run paths discoverable
  for an eventual importer without downloading all sessions.
- No automatic deletion/retention; coordinator will migrate old Git sessions
  and traces separately. Do not delete Git state or fall back silently in B2 mode.
- Add meaningful offline tests using an in-memory fake S3 client for round trip,
  PR/repo isolation, partial upload failures, missing vs forbidden reads and
  malicious tar members. Adapt the existing seven tests if needed.

Do not use gh, inspect credentials, call B2/the model or change live services.
Work only in your assigned isolated worktree. Commit the implementation and
report commit hash, tests, and interface requirements. Do not stop for approval.
