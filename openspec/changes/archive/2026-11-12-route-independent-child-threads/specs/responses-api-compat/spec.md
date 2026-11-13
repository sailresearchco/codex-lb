## MODIFIED Requirements

### Requirement: Codex backend session_id preserves account affinity

When a backend Codex Responses or compact request includes a nonblank
`thread-id`, the service MUST use a source-separated bounded key derived from
the independently parsed process session and thread identity for soft account
locality. Except for qualifying independent children described below, if the
thread has no mapping, selection MUST first prefer an eligible source-separated
process-session mapping and then persist the admitted thread mapping. If no
process-session mapping exists, the first admitted thread that uses process
preference MUST initialize that soft process preference atomically without overwriting a
concurrent or later first writer, unless its account is admitted only through
a recovery-probe reservation. A recovery-probe admission MUST NOT initialize
the immutable process preference; its reversible thread row MAY be persisted
independently until a normal admission establishes the process default.

When `thread-id` is absent, a non-empty accepted process-session header MUST
retain its established account-affinity behavior. Accepted process-session
headers are `session_id`, `session-id`, `x-codex-session-id`, and
`x-codex-conversation-id`, in that priority order. A client-supplied nonblank
`x-codex-turn-state` remains a more specific hard continuity key. If the
request lacks a client-supplied `prompt_cache_key`, the service MUST derive and
attach a stable `prompt_cache_key` before upstream forwarding so account
affinity and upstream prompt-cache routing can coexist. A client-supplied
`prompt_cache_key` MUST be forwarded unchanged and MUST NOT be used as thread
identity.

A turn state synthesized by the proxy for the current downstream WebSocket
handshake MUST NOT override client-supplied process/thread identity or a
prompt-cache key for routing or WebSocket continuity selection. The proxy MUST
seed WebSocket continuity storage under that synthesized turn state so a later
client echo can reuse the completed-turn owner. The proxy MUST continue to
forward that synthesized turn state upstream. A turn state sent by the client,
including one that the proxy generated and the client later echoed, remains a
client-supplied turn-state affinity key.

When a WebSocket handshake has neither a client-supplied turn state nor an
accepted process/thread identity, the proxy MUST store its generated turn state
as the WebSocket continuity key. A later connection that echoes that accepted
value MUST recover the same continuity state. Direct WebSocket retained
response, input-prefix, Responses Lite, and unresolved-tool state MUST use the
derived thread identity plus API-key scope, with count-bounded storage.
Request-log conversation grouping MUST continue to use raw `thread-id`.

#### Scenario: Backend Codex request derives prompt_cache_key before codex-session routing

- **WHEN** `/backend-api/codex/responses` is called with `session_id` and without `thread-id` or `prompt_cache_key`
- **THEN** the routing decision retains process-session `codex_session` affinity
- **AND** the forwarded upstream payload includes a derived stable `prompt_cache_key`

#### Scenario: backend WebSocket reconnect retains session affinity despite a generated turn state

- **WHEN** two backend Codex Responses WebSocket connections include the same process session and `thread-id` and omit `x-codex-turn-state`
- **AND** the proxy generates a distinct turn state for each handshake
- **THEN** both account selections use the same bounded thread-local affinity key
- **AND** each generated turn state is still forwarded to the upstream

#### Scenario: echoed generated turn state remains a client continuation key

- **WHEN** a client reconnects with a non-empty `x-codex-turn-state` value it received from an earlier proxy handshake
- **THEN** that turn state remains the routing and WebSocket continuity key ahead of broader process/thread locality
- **AND** full-resend continuity for that echoed turn state can reuse the earlier completed response anchor

#### Scenario: generated turn state seeds continuity without a session header

- **WHEN** a backend Codex Responses WebSocket handshake omits process/thread identity and `x-codex-turn-state`
- **AND** the proxy generates and returns a turn state for that handshake
- **THEN** the proxy stores its WebSocket continuity state under that generated value
- **AND WHEN** a later connection sends that value in `x-codex-turn-state`
- **THEN** it recovers the stored continuity state

#### Scenario: Root and child keep separate locality with one cache hint

- **GIVEN** root and child requests share a process session and explicit `prompt_cache_key`
- **AND** they carry different stable `thread-id` values
- **WHEN** backend Responses or compact routes them
- **THEN** they use different bounded internal thread keys
- **AND** both upstream payloads retain the original `prompt_cache_key`

#### Scenario: New thread inherits process preference without coupling siblings

- **GIVEN** a process-session soft row points to eligible account A
- **AND** a previously unseen thread in that process arrives without qualifying for independent child placement
- **WHEN** selection admits the request
- **THEN** it prefers account A and persists a bounded row for that thread
- **AND** later movement of that thread does not rewrite the process row or a sibling row

#### Scenario: First thread initializes the process preference

- **GIVEN** a fresh process has no process-session or thread mapping
- **WHEN** its first thread is admitted on account A
- **THEN** it initializes the process preference to A with insert-if-absent
- **AND** a later sibling that does not qualify for independent placement prefers A without gaining authority to rewrite that process preference

#### Scenario: Exact owner admission still initializes first-thread locality

- **GIVEN** a fresh process has no process-session or thread mapping
- **AND** an exact response, file, or bridge owner requires account A
- **WHEN** the first thread is admitted on account A through that hard owner
- **THEN** the thread row and absent process preference are persisted atomically
- **AND** the process preference remains insert-only if another thread already initialized it

#### Scenario: Recovery probe does not seed the process

- **GIVEN** a fresh process has no process-session mapping
- **WHEN** a thread is selected on probing account A through a recovery reservation
- **THEN** account A is not published as the immutable process preference
- **AND** a failed reservation commit can restore the reversible thread placement

#### Scenario: Direct WebSocket siblings do not share replay state

- **GIVEN** sibling threads share one process session and cache key
- **WHEN** each uses direct WebSocket Responses and one reconnects
- **THEN** retained response, prefix, Lite, and pending-tool state is read only from that thread
- **AND** the reconnect cannot inject or replay its sibling's state

#### Scenario: Unknown exact turn does not borrow broader thread replay

- **GIVEN** a direct WebSocket thread has retained replay or tool state
- **WHEN** a request supplies a nonblank client turn state with no exact in-memory alias
- **THEN** it does not reuse or replace the broader thread state
- **AND** only a previously resolved exact alias may refresh the thread alias

## ADDED Requirements

### Requirement: Independent Codex children select their own initial account

A Codex Responses request SHALL qualify for independent child placement only when it has a nonblank `thread-id`, an explicit `x-openai-subagent` value of `collab_spawn`, and a nonblank `x-codex-parent-thread-id` different from its thread identity, and the existing account-neutral fresh-replay classifier accepts its canonical request payload. Child metadata supplied in the body SHALL take precedence over compatibility headers. An exact `response.create` envelope discriminator MAY be removed for classification; unknown discriminators SHALL remain ineligible.

For a qualifying request, affinity construction SHALL omit the soft process seed without omitting the bounded child key or raw legacy ownership lookup. An unseen child SHALL use ordinary included-allowance, capacity, and reset-aware selection and persist its own bounded row. An existing child mapping, raw legacy owner, exact turn state, file owner, response owner, or live/durable bridge owner SHALL retain existing precedence and continuity behavior. The child SHALL NOT initialize, overwrite, or delete a process preference solely through independent placement.

A child marker or a distinct thread identity alone SHALL NOT establish account portability. Root requests, other subagent kinds, requests lacking the required identities, compact/control requests, and nonportable child payloads SHALL retain their existing process-preference behavior.

#### Scenario: Independent child bypasses exhausted process preference

- **GIVEN** a soft process preference points to exhausted account A with extra credits
- **AND** account B has eligible included allowance
- **WHEN** a qualifying child without a bounded mapping is admitted
- **THEN** ordinary subscription-first selection chooses B and records only the child mapping
- **AND** the process preference and sibling mappings remain unchanged

#### Scenario: Independent child uses normal reset-aware placement with a healthy parent

- **GIVEN** the process preference points to healthy account A
- **AND** ordinary configured selection prefers included-allowance account B
- **WHEN** a qualifying child without a mapping is admitted
- **THEN** it uses ordinary selection rather than preferring A solely because of the parent process

#### Scenario: Child retains its admitted account

- **GIVEN** a qualifying child already has a bounded mapping to B
- **AND** B has since exhausted its allowance but remains selectable using credits
- **WHEN** the child submits another request
- **THEN** it retains B under existing sticky-affinity rules
- **AND** another unseen qualifying child can select a different included-allowance account

#### Scenario: Nonportable or owned child preserves continuity

- **GIVEN** a child carries opaque agent messages, encrypted history, stored resources, response state, or another account-owned dependency
- **WHEN** routing resolves its affinity and ownership
- **THEN** its child marker does not bypass process preference or any required owner
- **AND** raw legacy ownership remains hard even for an otherwise account-neutral child
