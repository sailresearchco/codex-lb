## MODIFIED Requirements

### Requirement: Bare process-session cap spillover is non-mutating

The system MUST parse process-session and thread headers independently. A bare
process-session mapping and a bounded thread-local mapping MUST use distinct,
header-inaccessible storage identities, so a client-supplied hard turn-state
value cannot alias either derived soft row. A current replica MUST consult a
legacy raw Codex-session key independently even when a namespaced process or
thread row exists. Any raw hit MUST take precedence as hard ownership. If a
resolved file, response, bridge, or other exact owner conflicts with that raw
legacy owner, the request MUST fail closed without creating or rewriting any
of those rows.

A missing thread row MAY use an eligible process-session soft row as its
initial placement preference, except for qualifying independent Codex children
whose Responses affinity omits that seed. If a request uses process preference
and that process row is missing, the first admitted thread MUST initialize it
with insert-if-absent and MUST persist its own bounded thread row. A concurrent or later thread MUST NOT overwrite that
first-writer process preference. Account-cap spillover or later thread movement
MUST NOT rewrite or delete the process-session mapping or a sibling's thread
mapping. A provisional recovery-probe reservation MUST NOT initialize a
missing process preference, because its thread mapping may still require
rollback and a probing account is not a stable process default. A later normal
admission MAY initialize the missing process preference.

When the mapped account for a bare process-session key is locally capped and
another eligible account is selected, the spillover MUST apply only to that
request. Selection MUST NOT update or delete the stored process-session mapping
because of account-cap spillover. If the mapped account is below cap, normal
sticky selection MUST retain it.

#### Scenario: Capped bare-session owner spills without rebinding

- **GIVEN** a bare process-session mapping points to account A
- **AND** account A is locally capped
- **AND** account B is eligible and below cap
- **WHEN** a self-contained pre-visible request is selected
- **THEN** the request uses account B
- **AND** the stored process-session mapping still points to account A

#### Scenario: Unsaturated bare-session owner retains locality

- **GIVEN** a bare process-session mapping points to eligible account A below its local caps
- **WHEN** a self-contained request is selected
- **THEN** the request uses account A
- **AND** the mapping remains unchanged

#### Scenario: Equal session and turn-state values remain isolated

- **GIVEN** a process-session header and an explicit turn-state header have equal text values
- **WHEN** their affinity mappings are resolved
- **THEN** the process-session mapping uses a source-separated opaque key
- **AND** the explicit turn-state mapping continues to use the legacy raw key as hard ownership

#### Scenario: Derived soft key cannot be reused as raw hard turn state

- **GIVEN** a process-session or thread value has a derived internal storage key
- **WHEN** a client submits the visible representation of that key as a turn-state header
- **THEN** header normalization cannot reproduce the internal storage identity
- **AND** hard turn-state selection cannot read or rewrite the soft row

#### Scenario: Legacy raw mapping remains hard

- **GIVEN** a legacy replica persisted a raw Codex-session mapping
- **WHEN** a current replica receives the matching process or legacy thread header
- **THEN** it does not reinterpret or mutate the legacy raw row as spillable affinity
- **AND** mixed-version operation remains fail-closed for that row

#### Scenario: Coexisting legacy and namespaced rows prefer hard ownership

- **GIVEN** mixed-version replicas created a raw row and a namespaced process or thread row for the same request identity
- **AND** the rows point to different accounts
- **WHEN** a current replica selects the request
- **THEN** the raw row's account is treated as the hard owner
- **AND** neither row is deleted or rewritten by account-cap spillover

#### Scenario: Legacy hard owner conflicts with resolved owner

- **GIVEN** a raw legacy session row points to account A
- **AND** a file, previous response, or bridge resolves to account B
- **WHEN** the request is routed
- **THEN** the service fails with `continuity_owner_conflict`
- **AND** it neither bypasses nor rewrites the raw row

#### Scenario: Process preference seeds only the new thread

- **GIVEN** a process-session soft row points to account A
- **AND** no bounded row exists for thread T
- **WHEN** T is admitted on account A or a safely selected alternate
- **THEN** the admitted account is persisted under T's bounded key
- **AND** the process-session row remains unchanged

#### Scenario: Missing process preference is initialized once

- **GIVEN** no process-session mapping exists
- **WHEN** the first thread is admitted on account A and a concurrent or later thread is admitted on account B
- **THEN** insert-if-absent preserves the first persisted process owner
- **AND** each thread persists only its own bounded locality after that initialization

#### Scenario: Provisional probe placement does not escape rollback

- **GIVEN** neither process nor thread has a bounded mapping
- **WHEN** a probing-account placement persists provisionally and then loses its runtime commit
- **THEN** its thread mutation is restored
- **AND** no immutable process preference is left behind

#### Scenario: Legacy raw owner wins over thread locality

- **GIVEN** a raw legacy Codex row points to account A
- **AND** a bounded thread row points to account B
- **WHEN** the request is routed
- **THEN** the raw row remains hard ownership evidence and account A wins
- **AND** neither mapping is rewritten to reconcile the disagreement

### Requirement: Included allowance preference preserves established affinity

Included-allowance preference SHALL apply when choosing a new account without overriding an established soft-affinity owner or retained initial session preference. Qualifying independent Codex children SHALL omit the process preference before selection as defined by the Responses compatibility contract. Included-allowance exhaustion alone SHALL NOT add a new affinity-reallocation trigger, owner admission error, or transport-reuse check. Existing budget, health, overload, and continuity rules SHALL continue to govern established affinity. Hard ownership for files, response state, and Codex continuity SHALL remain account-bound.

#### Scenario: Exhausted sticky owner retains established affinity
- **GIVEN** a soft sticky owner has exhausted included allowance and extra credits
- **AND** another compatible eligible account has included allowance
- **AND** no existing reallocation condition applies
- **WHEN** the next request arrives
- **THEN** it retains the owner and may use its extra credits
- **AND** included-allowance preference does not rebind the mapping or reject the request

#### Scenario: Remaining included allowance preserves a sticky owner
- **GIVEN** the soft sticky owner still has included allowance
- **AND** another eligible account has more allowance
- **WHEN** no existing reallocation condition applies
- **THEN** the request retains its existing owner

#### Scenario: Exhaustion without an eligible replacement preserves fallback
- **GIVEN** the soft sticky owner has exhausted included allowance
- **AND** every compatible alternative is exhausted or unavailable
- **WHEN** the owner remains eligible
- **THEN** the request may retain that owner without unnecessary affinity churn

#### Scenario: A new conversation selects included allowance
- **GIVEN** a request has no existing owner or initial session preference
- **AND** the pool contains an exhausted account with extra credits and an eligible account with included allowance
- **WHEN** new affinity is selected
- **THEN** it selects the account with included allowance
- **AND** subsequent requests retain that new affinity under the existing rules

#### Scenario: File ownership is preserved despite pool allowance
- **GIVEN** a request refers to a file owned by an exhausted eligible account
- **AND** another account has included allowance
- **WHEN** the request is dispatched
- **THEN** it remains on the file owner without a new allowance-based rejection
