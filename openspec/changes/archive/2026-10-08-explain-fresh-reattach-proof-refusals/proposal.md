## Why

The HTTP bridge logs `fresh_reattach_anchor_injected` when it adds a durable
response anchor to a new upstream connection. The existing event names the
anchor but does not explain why the client's full history could not be
preserved. This prevents operators from distinguishing an anchor or prefix
rejection from a refused context proof when an interrupted response is followed
by an agent message.

## What Changes

- Add bounded classification metadata to both existing anchor-injection events:
  anchor rejection, full-resend shape, stored item count, context retention,
  known tool-manifest count, and same-owner proof presence.
- Reuse the existing classifications without recomputing or broadening replay
  acceptance. Leave more detailed prefix and suffix diagnosis for evidence that
  identifies the refusing proof.
- Record no message content, ciphertext, tool details, or additional identifiers.

## Impact

- Affected spec: `responses-api-compat`.
- Affected code: HTTP bridge fresh-reattach logging in `streaming.py`.
- No routing, replay, client-response, settings, or schema changes.
