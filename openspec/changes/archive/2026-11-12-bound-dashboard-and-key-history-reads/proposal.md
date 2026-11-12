## Why

Dashboard overview and API-key lifetime reads could aggregate a large unfolded historical tail before rollups caught up. Codex quota responses also calculated lifetime history unnecessarily, while the earliest-activity lookup could scan excluded request history.

## What Changes

- Check API-key lifetime and both dashboard comparison windows for populated unfolded history beyond the existing four-hour allowance; return `usage_history_building` before expensive aggregation.
- Use a shared countable API-key predicate and partial timestamp index for coverage probes and raw-tail aggregates, preserving soft-deleted and account-detached key history.
- Apply the existing SQLite read budget to self-service lifetime totals after limit-reset writes commit; read only current limits for Codex quota responses.
- Seek earliest activity by request kind while preserving unknown kinds, exact retained timestamps, and the existing hourly fallback.
- Preserve complete totals, sparse/recent history, exact partial-hour boundaries, and empty-key-inventory behavior.

## Impact

Capability: `report-aggregation`.

Implementation: dashboard overview, API-key summaries/self-service limits, usage coverage, earliest activity, and migration `20261112_002527_api_key_usage_countable_index`. The migration adds a partial index; it does not remove request history. Authentication, inference, OAuth usage, and limit-reset writes retain their existing paths.

This record completes the missing OpenSpec artifacts for [PR #25](https://github.com/sailresearchco/codex-lb/pull/25). It documents already implemented behavior and introduces no additional runtime change.

