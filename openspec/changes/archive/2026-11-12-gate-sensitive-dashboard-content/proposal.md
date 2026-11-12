## Why

Dashboard read access exposed operational configuration and free-text request diagnostics beyond the caller's effective grants. Searching error-message content could also reveal information hidden from an unprivileged response.

## What Changes

- Require `ops:write` for automation inventories, filters, run details, and model-source configuration reads.
- Redact the warmup prompt without `ops:write`, preserving its nonempty string response schema.
- Return null free-text request error fields and exclude error-message search matches without `conversations:read`; preserve existing aggregate and enum metadata access.
- Apply effective permission grants and retain redaction across privileged and unprivileged cache use.

## Impact

Capability: `admin-auth`.

Implementation: automation/model-source read dependencies, settings response mapping, and request-log response/search mapping. Existing mutation and step-up rules remain unchanged; no schema or permission-vocabulary change.

This record completes the missing OpenSpec artifacts for [PR #24](https://github.com/sailresearchco/codex-lb/pull/24). It documents already implemented behavior and introduces no additional runtime change.

