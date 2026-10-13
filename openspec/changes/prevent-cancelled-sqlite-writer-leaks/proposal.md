## Why

Cancelling a write can leave an unfinished SQLite cursor
reachable through a traceback after connection close, retaining the database
writer lock and blocking unrelated requests and scheduler writes.

## What Changes

- Backport upstream #2600: finalize SQLAlchemy-created cursors when file-backed
  SQLite connections close, without relying on traceback collection.
- Preserve terminal settlement fences, transaction semantics, and cancellation
  propagation. No migration, dependency, credential, or configuration changes.

## Impact

The shared file-backed SQLite engine factory and real SQLite regression tests.
Terminal append cancellation behavior, readiness detection, and scheduler
shutdown grace remain outside this change.
