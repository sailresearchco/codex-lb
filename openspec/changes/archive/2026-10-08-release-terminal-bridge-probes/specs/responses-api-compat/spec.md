## ADDED Requirements

### Requirement: Matched terminal failures return their own bridge recovery probe

When an admitted HTTP bridge recovery probe receives its matched upstream terminal failure, the proxy MUST return that request's recovery reservation if the existing circuit outcome policy does not otherwise settle it. The proxy MUST NOT keep treating a completed probe as active until its lease expires.

Returning the reservation MUST be fenced to the circuit episode and reservation claimed by that request. It MUST NOT release a newer probe or erase newer failure counts, cooldowns, or anchor quarantine. An unmatched terminal MUST NOT settle a pending request's reservation. The next admission MUST permit at most one replacement probe.

The proxy MUST preserve existing upstream failure classification and account ownership. It MUST NOT classify every upstream `server_error` as a transport failure or anchor rejection merely to release the reservation.

#### Scenario: A server error does not hold the conversation for ten minutes

- **GIVEN** a conversation's recovery probe has been admitted and sent upstream
- **WHEN** that probe receives its matched `server_error` terminal
- **THEN** its recovery reservation is returned without waiting for lease expiry
- **AND** the next eligible retry can compete for a single replacement probe

#### Scenario: A late terminal cannot release a successor

- **GIVEN** a newer circuit episode or probe supersedes the failed request's reservation
- **WHEN** the old request's terminal is settled
- **THEN** the newer episode and reservation remain unchanged

#### Scenario: Concurrent retries preserve single-probe admission

- **GIVEN** a completed probe has returned its reservation
- **WHEN** multiple retries attempt recovery concurrently
- **THEN** at most one is admitted to send upstream
- **AND** the other retries retain a visible bounded refusal
