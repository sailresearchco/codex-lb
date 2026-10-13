## ADDED Requirements

### Requirement: SQLite teardown finalizes cancelled statement cursors

File-backed SQLite main and background connections MUST release locks held by
unfinished SQLAlchemy-created cursors when teardown completes, even while
cancellation tracebacks remain referenced. Teardown MUST preserve cancellation
propagation and MUST NOT commit unfinished writes. If a cursor close raises an
ordinary exception, cleanup MUST attempt all remaining cursors and the base
connection close before propagating the first error.

#### Scenario: Cancelled write retains its traceback

- **WHEN** a write is cancelled while its statement is executing
- **AND** connection teardown completes while the traceback remains referenced
- **THEN** an independent writer succeeds without garbage collection
- **AND** the cancelled transaction remains uncommitted

#### Scenario: Cleanup continues after one cursor close fails

- **WHEN** closing one cursor raises an ordinary exception
- **THEN** closing every remaining cursor and the base connection is still attempted
- **AND** the first cleanup error propagates after those attempts

#### Scenario: Cancelled terminal persistence does not strand shared writes

- **WHEN** an owned terminal append is cancelled explicitly or at its delivery bound
- **AND** its database worker and connection teardown finish
- **THEN** subsequent bridge settlement and leader acquisition can write
- **AND** the existing terminal settlement fences remain effective
