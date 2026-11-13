## Why

Automatic banked-reset redemption currently waits until the final five minutes before coupon expiry. An account with exhausted weekly allowance can therefore spend credits for days even when it holds a usable reset. Preserve the expiry fallback and add a conservative threshold without forecasting or new settings.

## What Changes

- When existing automatic redemption is enabled, redeem one earliest-expiring coupon if fresh weekly allowance is at most 1% remaining and its next natural refresh is at least 72 hours away.
- Recheck upstream weekly allowance inside the existing per-account redemption serializer, so stale samples and concurrent manual resets cannot spend another coupon.
- Keep automatic coupon attempts pinned through coupon expiry, including ambiguous failures, while preserving the existing 24-hour lifetime for manual request IDs.
- Preserve the final-five-minute fallback and prevent it from immediately spending another coupon after an early redemption.

## Impact

- Reset-credit scheduler, shared redemption helper, fresh usage result, and durable request retention.
- One nullable ledger retention deadline; no new behavior setting, routing changes, or account migration.
