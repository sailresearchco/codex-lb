## Why

HTTP password hashing and comparison ran on the server event loop, delaying unrelated requests. Moving this work off the event loop also requires bounded admission so overlapping requests and cancellation cannot accumulate password work.

## What Changes

- Run HTTP password hashes and comparisons in one process-wide worker with one admitted job and no waiting queue.
- Return HTTP 429 `password_work_busy` with `Retry-After: 1` for overlapping work, after existing login rate admission.
- Keep capacity occupied until the submitted job actually completes, including request cancellation or event-loop closure.
- Generate the cached unknown-identity dummy hash inside the admitted worker and retain one comparison per verification.
- Keep standalone host-recovery password hashing synchronous and preserve invalid-credential behavior.

## Impact

Capability: `admin-auth`.

Implementation: shared password worker and dashboard password setup, login, guest password, rotation/removal, invite, and step-up call sites. No authentication-method, schema, or configuration change.

This record completes the missing OpenSpec artifacts for [PR #26](https://github.com/sailresearchco/codex-lb/pull/26). It documents already implemented behavior and introduces no additional runtime change.

