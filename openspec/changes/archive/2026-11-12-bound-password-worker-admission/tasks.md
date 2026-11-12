## 1. Existing implementation

- [x] 1.1 Move HTTP password hashing and verification into bounded process-wide worker admission.
- [x] 1.2 Retain rate-limit ordering, invalid-credential responses, unknown-identity comparison, and synchronous host recovery.
- [x] 1.3 Keep admission occupied across cancellation/loop closure until underlying completion, with failure cleanup.
- [x] 1.4 Cover responsiveness and worker lifecycle through route and unit regressions.

## 2. Verification

- [x] 2.1 Confirm existing implementation-head CI success at `76810bd85071f9be807d72bb04e9dd298d4bfded`: [run 43133472396](https://github.com/sailresearchco/codex-lb/actions/runs/43133472396).
- [x] 2.2 Validate this added artifact with `openspec validate bound-password-worker-admission --strict` before archiving the record.

