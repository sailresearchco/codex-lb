## ADDED Requirements

### Requirement: Included allowance preference preserves established affinity

Included-allowance preference SHALL apply when choosing a new account without overriding an established soft-affinity owner or initial session preference. Included-allowance exhaustion alone SHALL NOT add a new affinity-reallocation trigger, owner admission error, or transport-reuse check. Existing budget, health, overload, and continuity rules SHALL continue to govern established affinity. Hard ownership for files, response state, and Codex continuity SHALL remain account-bound.

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
