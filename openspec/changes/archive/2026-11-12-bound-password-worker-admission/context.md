## Record provenance

This change record was prepared during review after the implementation in [PR #26](https://github.com/sailresearchco/codex-lb/pull/26), at commit `76810bd85071f9be807d72bb04e9dd298d4bfded`. It does not assert that these artifacts preceded implementation. Its delta requirements reproduce the owning requirement sections at that implementation head.

## Example and constraints

While one login comparison runs, health requests remain responsive and a second password attempt receives `password_work_busy` immediately. Cancelling the first request does not admit another password job until its original worker finishes.

## Existing validation evidence

[Implementation-head CI run 43133472396](https://github.com/sailresearchco/codex-lb/actions/runs/43133472396) completed successfully for `76810bd85071f9be807d72bb04e9dd298d4bfded`; its `CI Required` check passed. This is evidence for the existing implementation, not a claim that the newly added OpenSpec artifacts have already been validated.

Relevant regression coverage in that head:

- `tests/integration/test_password_worker_api.py`: setup/login remain responsive, concurrent work is rejected, rate admission precedes worker admission, and invalid credentials retain HTTP 401.
- `tests/unit/test_password_worker.py`: cancellation, loop shutdown, worker/submission failure, and first-use dummy hashing preserve capacity and off-loop execution.
- `tests/integration/test_dashboard_password_auth.py`, `tests/unit/test_dashboard_auth_password_service.py`, and `tests/unit/test_admin_cli.py`: existing password and host-recovery contracts.

Strict validation of this added change is tracked separately in `tasks.md`. No tests were rerun while preparing this retrospective record.

