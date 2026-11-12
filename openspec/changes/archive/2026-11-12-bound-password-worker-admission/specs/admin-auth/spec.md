## ADDED Requirements

### Requirement: Password work does not block request processing

HTTP password hashing and verification SHALL run outside the server event loop. Each process SHALL admit at most one password job with no waiting queue; overlapping password work SHALL return HTTP 429 with code `password_work_busy` and `Retry-After: 1`. Existing per-address and per-identity login admission SHALL occur before password-worker admission, and invalid admitted credentials SHALL retain the existing HTTP 401 response. Unknown identities SHALL perform the same single password comparison as known identities, including generating their cached dummy hash outside the event loop. Cancelling a request or closing its event loop SHALL NOT release worker capacity before its submitted job finishes. Standalone host-recovery password hashing SHALL remain synchronous.

#### Scenario: Password verification permits unrelated requests

- **GIVEN** an admitted password comparison is still running
- **WHEN** a health request and another password attempt arrive
- **THEN** the health request can complete before that comparison finishes
- **AND** the second password attempt receives `password_work_busy` without enqueuing password work

#### Scenario: Cancelled password request retains capacity

- **GIVEN** a password job is running and its request is cancelled
- **WHEN** another password attempt arrives before that job finishes
- **THEN** the new attempt receives `password_work_busy`
- **AND** a later attempt can be admitted after the job finishes
