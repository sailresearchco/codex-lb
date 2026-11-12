## Record provenance

This change record was prepared during review after the implementation in [PR #25](https://github.com/sailresearchco/codex-lb/pull/25), at commit `43cb0e07dd5e1c5e8261cc32ca7a1cfb428b8e18`. It does not assert that these artifacts preceded implementation. Its delta requirements reproduce the owning requirement sections at that implementation head.

## Example and constraints

A key with countable raw history older than the allowed unfolded tail receives `usage_history_building` instead of a partial lifetime total. After folding covers that history, the same read returns complete totals. The Codex quota endpoint can still report current limits without reading lifetime request history.

## Existing validation evidence

[Implementation-head CI run 43133470096](https://github.com/sailresearchco/codex-lb/actions/runs/43133470096) completed successfully for `43cb0e07dd5e1c5e8261cc32ca7a1cfb428b8e18`; its `CI Required` check passed. This is evidence for the existing implementation, not a claim that the newly added OpenSpec artifacts have already been validated.

Relevant regression coverage in that head:

- `tests/integration/test_api_key_history_coverage.py`: cold-history deferral/recovery, sparse/recent history, empty inventory, excluded prefixes, scoped totals, and late pre-epoch inserts.
- `tests/integration/test_dashboard_history_coverage.py`: both projections, previous comparison coverage, exact partial-hour boundaries, and empty/recent history.
- `tests/integration/test_self_usage_read_budget.py`: interrupted reads recover while committed resets survive; Codex usage avoids history, including the trailing-slash route.
- `tests/integration/test_earliest_activity.py`: excluded prefixes, unknown kinds, exact timestamps, and retention fallback.
- `tests/integration/test_api_key_countable_index_migration.py`: upgrade/downgrade/retry preserve history and PostgreSQL invalid-index repair.

Strict validation of this added change is tracked separately in `tasks.md`. No tests were rerun while preparing this retrospective record.

