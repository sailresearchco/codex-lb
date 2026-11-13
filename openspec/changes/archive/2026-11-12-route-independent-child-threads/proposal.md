## Why

New Codex child threads inherit the process account before capacity and included-allowance selection. That soft preference can concentrate independent work on an exhausted, credit-funded account while other accounts retain subscription allowance. The behavior comes from upstream PR #1703 and remains outside the existing subscription-first policy.

## What Changes

- Route explicitly identified, account-neutral collaboration children through ordinary first-account selection instead of inheriting a process preference.
- Retain each child's bounded affinity after its first admission and preserve all existing hard ownership, legacy lookup, and bridge continuity rules.
- Keep process preferences unchanged for root requests, nonportable children, compact/control requests, and clients without a thread identity.

## Impact

- Responses affinity construction shared by HTTP and direct WebSocket requests.
- Responses compatibility and sticky-affinity contracts with focused routing regression tests.
- No schema, settings, ownership migration, or upstream payload changes.
