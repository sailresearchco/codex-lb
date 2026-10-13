## Why

The HTTP bridge's retry-circuit and stale-generation submission guards refuse requests before dispatch, but native Codex error shaping treats their timeout code as an upstream transport failure. This turns a useful refusal and retry delay into an empty HTTP 200 stream, spending client retries without explaining the block.

## What Changes

- Mark the two proven pre-dispatch guards as local refusals so native clients receive the existing HTTP error or terminal event.
- Preserve the refusal's retry delay through internal owner forwarding.
- Retain existing admission, replay, account, and ambiguous upstream transport policies.

## Impact

HTTP bridge submission, internal forwarding, and externally visible error delivery. Related to Soju06/codex-lb#2388. No account authentication or schema changes.
