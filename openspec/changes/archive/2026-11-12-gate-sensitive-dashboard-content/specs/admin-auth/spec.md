## ADDED Requirements

### Requirement: Sensitive dashboard content follows effective grants

Automation job and run inventories, their filters and run details, and model-source configuration SHALL require `ops:write`. Settings responses SHALL redact `limitWarmupPrompt` to `[redacted]` without `ops:write`, preserving the existing nonempty string schema. Request-log responses SHALL return null for `errorMessage` and `failureDetail` without `conversations:read`; searches by such principals SHALL exclude error-message content. Aggregate counts, status and code enums, tokens, costs and latency SHALL remain available under the existing read permissions. Authorization SHALL use effective grants rather than role names, and cached privileged results SHALL NOT disclose these fields to later unprivileged requests.

#### Scenario: Guest cannot enumerate automation or model-source configuration

- **WHEN** a principal without `ops:write` requests an automation inventory, filter, run detail, or model-source configuration
- **THEN** the system returns HTTP 403 without the protected payload

#### Scenario: Custom operational grant permits configuration reads

- **WHEN** a principal with an effective `ops:write` grant requests those configuration routes
- **THEN** the existing successful response contract is preserved regardless of its role name

#### Scenario: Error content cannot be discovered through search

- **GIVEN** a request log contains a unique sentinel only in its error message
- **WHEN** a principal without `conversations:read` searches for that sentinel
- **THEN** that error content does not produce a match and its free-text error fields remain null
- **AND** a principal with `conversations:read` can search and read the error content
