## Why

A half-open HTTP bridge recovery probe that receives an upstream `server_error` has finished, but the current transport-only circuit accounting leaves its recovery reservation held for ten minutes. Later requests in the conversation are refused despite there being no active probe.

## What Changes

- Return the recovery reservation after a matched terminal failure that does not settle the circuit through the existing transport-failure policy.
- Fence settlement to the request's own circuit episode and reservation, preserving newer failures, cooldowns, anchor quarantine, and sibling ownership.
- Keep admission serialized so at most one replacement probe may be sent.

## Impact

HTTP bridge recovery and its regression coverage. No account authentication, schema, deployment settings, or cross-account replay changes.
