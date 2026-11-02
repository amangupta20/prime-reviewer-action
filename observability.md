# Importing reviewer traces into a future viewer

The reviewer stores raw Prime records in private B2 object storage. A viewer
is optional: reviews and PR replies do not depend on one being reachable.
This document records the intended import boundary; no viewer or live exporter
has been deployed by the B2 migration (2026-11-03).

## What is preserved

Each run keeps its structured result and metadata, the root Prime JSONL
transcript and any subagent transcripts. Metadata identifies repository, PR,
run and model, configured and resolved private config revision, Prime version,
start time, duration, result, usage and the number of root-session lines that
already existed before this run. A schema version identifies the archive
format. Older imported runs retain unknown provenance as missing fields;
we do not invent the config revision that produced them. The storage implementation and its tests define the object layout.

Full raw records remain the source for replay. They are not OTLP messages.
Session archives support continuation; immutable per-run archives support
inspection and later import. Neither is a public workflow artifact.

## Future data path

1. A separate importer running beside the chosen viewer reads completed run
   archives from B2 using its own read-only credential.
2. It converts the records into OpenTelemetry spans: one run parent, model
   calls with model/token/cost attributes, tool spans, and child-agent spans
   where the source has enough timing and parent information.
3. It sends those spans to the viewer's OTLP/HTTP endpoint using that
   viewer's authentication and attribute mapping.
4. It records successful imports, scoped to the destination and adapter
   version. A deterministic trace ID derived from repo and run ID allows
   replay to preserve identity; each backend's duplicate-ingestion behavior
   must be tested before automatic retries are enabled.

Import only root records after `session_lines_before` for the current run.
A resumed session includes previous runs. Do not infer child-span parentage
or precise timing where Prime did not record it: retain the raw records and
mark the missing information. Check whether saved child transcripts contain
prior-run records before ingesting their usage or spans again.

## Viewer adapters

All three expose OpenTelemetry ingestion, but their useful LLM views have
additional conventions. Supporting OTLP does not make the adapters identical.

| Viewer | Integration to verify when selected |
|---|---|
| Phoenix | OTLP collector plus OpenInference attributes for useful model/tool views |
| Langfuse | Its OTLP endpoint, authentication, and generation/session metadata mapping; scores use its score API |
| OpenObserve | Organization-specific OTLP endpoint and headers, plus attributes used for trace search |

Sources checked 2026-11-03:
- Phoenix: https://arize.com/docs/phoenix/learn/tracing/how-tracing-works
- Langfuse: https://langfuse.com/docs/api-and-data-platform/features/public-api
- OpenObserve: https://openobserve.ai/docs/user-guide/data-exploration/traces/traces/

Those are ingestion interfaces, not evidence of an end-to-end integration
with these Prime records. Once a viewer is selected, import one successful,
one failed and one resumed review, verify the hierarchy and usage totals,
then replay them to check duplicate handling. Credentials and raw prompt or
response content remain private throughout.
