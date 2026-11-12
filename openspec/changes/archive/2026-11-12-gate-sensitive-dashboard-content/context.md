## Record provenance

This change record was prepared during review after the implementation in [PR #24](https://github.com/sailresearchco/codex-lb/pull/24), at commit `7845ec86e14f27323de08c00df5f5ee5517607d2`. It does not assert that these artifacts preceded implementation. Its delta requirements reproduce the owning requirement sections at that implementation head.

## Example and constraints

A principal with an effective `ops:write` grant can read automation configuration even without a built-in operator role. A principal without `conversations:read` cannot find a request by a sentinel present only in its error message, including after a privileged request warmed shared caches.

## Existing validation evidence

[Implementation-head CI run 43125210999](https://github.com/sailresearchco/codex-lb/actions/runs/43125210999) completed successfully for `7845ec86e14f27323de08c00df5f5ee5517607d2`; its `CI Required` check passed. This is evidence for the existing implementation, not a claim that the newly added OpenSpec artifacts have already been validated.

Relevant regression coverage in that head:

- `tests/integration/test_guest_sensitive_exposure.py`: operational permission grants, prompt redaction, and error-content/search isolation across permission cache keys.
- `tests/integration/test_dashboard_route_permission_matrix.py`: protected routes declare their effective permission dependencies.
- `tests/integration/test_automations_api.py`: guest access to automation reads and writes remains denied.

Strict validation of this added change is tracked separately in `tasks.md`. No tests were rerun while preparing this retrospective record.

