## 1. Existing implementation

- [x] 1.1 Add bounded history coverage checks for API-key lifetime and dashboard comparison reads.
- [x] 1.2 Align API-key probes and aggregates with the countable partial timestamp index and migration.
- [x] 1.3 Separate Codex limit reads from lifetime aggregation and budget self-service lifetime reads after reset commits.
- [x] 1.4 Preserve earliest-activity and historical-total semantics with route, query-bound, and migration regressions.

## 2. Verification

- [x] 2.1 Confirm existing implementation-head CI success at `43cb0e07dd5e1c5e8261cc32ca7a1cfb428b8e18`: [run 43133470096](https://github.com/sailresearchco/codex-lb/actions/runs/43133470096).
- [x] 2.2 Validate this added artifact with `openspec validate bound-dashboard-and-key-history-reads --strict` before archiving the record.

