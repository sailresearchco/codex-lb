## 1. Existing implementation

- [x] 1.1 Bind identity admission and sanitization to the captured raw peer.
- [x] 1.2 Gate all internal drain routes on direct local provenance without changing dashboard bootstrap locality.
- [x] 1.3 Retain route and drain lifecycle regression coverage.

## 2. Verification

- [x] 2.1 Confirm existing implementation-head CI success at `1870ab923ef2da14efc0d81665fdf12854c7ec18`: [run 43122401111](https://github.com/sailresearchco/codex-lb/actions/runs/43122401111).
- [x] 2.2 Validate this added artifact with `openspec validate secure-drain-and-proxy-identity-provenance --strict` before archiving the record.

