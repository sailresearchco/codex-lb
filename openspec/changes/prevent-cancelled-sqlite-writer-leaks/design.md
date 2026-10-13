## Context

A cancelled INSERT with RETURNING can leave its cursor reachable through an
exception traceback. Closing the SQLite connection does not necessarily finalize
that statement, so the writer lock can survive the session's teardown.

## Decision

Backport upstream PR #2600 at `66aaaa32f423bd28c85cae3950aa7cdedab404c9`.
The shared file-backed engine factory uses a small SQLite connection subclass
that weakly tracks cursors created through `cursor()`. Before closing the raw
connection, it closes every tracked cursor on the existing aiosqlite worker.
Cleanup attempts every cursor and the base close even after an ordinary error,
then propagates the first cleanup exception. Weak tracking does not extend cursor
lifetime. In-memory SQLite and PostgreSQL construction paths remain unchanged.

For example, when the terminal append bound cancels a blocked transcript INSERT,
the worker eventually completes that statement and runs queued close. Finalizing
the retained cursor releases its lock before traceback collection, permitting
fenced fallback settlement and scheduler writes.

## Scope and limits

Cancellation, terminal-append task lifetime, persistence deadlines, and dispatch
fences remain unchanged. This is not an in-process writer-lock detector or a
repair for a connection already stranded before the patched process starts.
Existing teardown and interruption bounds still govern actively stuck workers.
No schema, dependency, settings, or authentication changes are introduced.

## Validation

Real SQLite tests retain cancellation tracebacks and require an independent
connection to write without forced garbage collection. Both engine roles and
both transcript formats are covered. Terminal tests exercise the existing
delivery-bound cancellation, then verify fenced
settlement and leader acquisition. Additional tests protect commit/rollback,
weak cursor lifetime, repeated close, and cleanup after cursor-close errors.
