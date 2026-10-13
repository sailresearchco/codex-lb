The observed client sent complete cumulative input after a subagent message
interrupted generation. The HTTP bridge's fresh-context checks rejected the
new `agent_message` suffix, so the bridge injected a response ID from an old
WebSocket and trimmed the history. Since the request used `store=false`, the
replacement socket could not resolve that anchor. Recovery subsequently hit
the local denied-anchor fence.

The existing verified full-resend and same-owner replacement paths already
preserve the original request. Extend their context classification, rather than
allowing arbitrary cumulative payloads, removing the denied-anchor fence, or
treating encrypted messages as portable between accounts. An agent message is
new input, never proof that omitted assistant output or parallel tool calls are
present.

The unavailable-owner regression also exposed implicit fallback during fresh
bridge selection and owner retirement. A same-owner proof cannot authorize
either path to move the original opaque body. Keep its owner required and leave
any cross-account projection to the separate account-neutral check.

For example, `[stored user input, custom call, matching output, agent message]`
with the exact durable call manifest can open a replacement socket on its
original account. Its encrypted reasoning and message content are retained.
The same input with a missing output remains anchored; a paused owner yields
the existing owner-unavailable error rather than dispatching to another account.

## Backport provenance

This local candidate backports AmeenP's upstream PR #2581, commit
`73ff014c75fe6cdd448facd786c1484775ea0094`, onto release `v1.25.0-beta.9`.
The downloaded upstream patch SHA256 is
`965a7aea5b1c021b7583a2c0bac46c3fb99e81214096cb24f67f91969d780a30`.
Source: https://github.com/Soju06/codex-lb/pull/2581.

Upstream's archived test claims are not validation of this backport. Local
reproduction, candidate validation, independent review, and proof limits are
recorded in `verification.md`. This change was archived on 2026-10-07 after that
local validation; no live deployment is included.
