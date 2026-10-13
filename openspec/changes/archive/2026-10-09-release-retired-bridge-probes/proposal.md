# Return recovery probes after completed bridge retirement

## Why

A cancelled HTTP bridge request can retain its half-open retry-circuit reservation after retirement removes it from the pending queue. Subsequent requests then remain suppressed until the reservation expires, even though its upstream work has ended.

## What Changes

- Preserve owned probe requests while retirement transfers them out of the pending queue.
- Return those probes only after the upstream reader has stopped and socket closure succeeds.
- Apply the existing claim and circuit-generation fences, preserving newer probes, cooldowns, failure counts, and poison evidence.
- Preserve cleanup ownership when the caller is cancelled or its bounded close wait expires.

## Impact

The change affects HTTP bridge resource retirement and retry admission. It adds no settings, migrations, account changes, or replay permissions. Incomplete transport cleanup remains fail-closed.
