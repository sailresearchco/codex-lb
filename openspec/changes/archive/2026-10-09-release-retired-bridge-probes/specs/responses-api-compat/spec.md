## ADDED Requirements

### Requirement: Completed bridge retirement returns owned recovery probes

When bridge retirement removes a dispatched request without an upstream terminal event, the service MUST preserve its owned half-open probe until the actual upstream reader has stopped and socket closure has succeeded. Completed retirement MUST return that probe only when its claim and circuit episode still match. The return MUST preserve failure counts, cooldowns, poison evidence, and newer probe ownership, and MUST permit at most one replacement probe.

#### Scenario: Cancelled probe completes resource retirement
- **GIVEN** a dispatched half-open probe whose downstream request is cancelled
- **WHEN** no active sibling or admission waiter remains and its reader and socket finish closing
- **THEN** its still-owned probe SHALL be returned
- **AND** concurrent replacement requests SHALL admit at most one probe

#### Scenario: Bounded close wait ends before resource cleanup
- **GIVEN** retirement owns a probe whose reader or socket has not finished closing
- **WHEN** a bounded caller wait expires or is cancelled
- **THEN** the service SHALL retain cleanup ownership
- **AND** the probe SHALL NOT be returned before actual successful transport closure

#### Scenario: Active sibling prevents retirement
- **GIVEN** a cancelled probe shares a session with an active request or admission waiter
- **WHEN** retirement checks session ownership
- **THEN** the cancelled probe SHALL remain reserved until resource retirement can complete

#### Scenario: The upstream reader initiates retirement
- **GIVEN** a cancelled probe remains reserved while a sibling is active
- **WHEN** the reader processes the sibling's terminal event and initiates successful socket closure
- **THEN** the probe SHALL remain reserved until that reader exits
- **AND** reader completion SHALL schedule owned cleanup that returns only the captured still-owned probe

#### Scenario: Incomplete cleanup or a newer circuit episode
- **GIVEN** resource cleanup leaves a reader running, socket closure fails, or a newer circuit episode or probe supersedes the retired request
- **WHEN** retirement attempts to settle the old probe
- **THEN** the service SHALL NOT clear the live or newer reservation
