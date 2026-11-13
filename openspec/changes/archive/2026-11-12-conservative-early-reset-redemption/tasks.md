## 1. Conservative early redemption

- [x] Add fresh weekly measurements to usage refresh results.
- [x] Add the <=1% remaining / >=72-hour early predicate under existing automatic-redemption opt-in.
- [x] Recheck the predicate inside per-account redemption serialization before pinning and consuming.
- [x] Retain automatic coupon pins through expiry and preserve the expiry fallback without duplicate consumption.

## 2. Verification

- [x] Cover exact thresholds, missing/unknown weekly data, weekly-primary measurements, and stale samples.
- [x] Cover manual/concurrent resets, renewed depletion with multiple coupons, ambiguous consume failures, and expiry fallback.
- [x] Cover ledger upgrade/downgrade and unchanged manual request TTL.
- [x] Run focused tests, lint/type checks, and strict OpenSpec validation.
