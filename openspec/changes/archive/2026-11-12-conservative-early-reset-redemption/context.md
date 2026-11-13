# Conservative replenishment

This extends the expiry-only policy introduced by upstream PR #1358. It does not move conversations, change routing, forecast demand, or promise zero paid usage. Weekly-only thresholds intentionally exclude independent short-window and model-specific limits.

An exhausted weekly allowance with four days before natural refresh qualifies; an allowance with 2% remaining or one day before refresh does not. A freshly successful usage fetch that omitted weekly data is not evidence of exhaustion. A manual reset between candidate selection and the serialized recheck must cancel the early attempt.

Each early attempt targets one coupon and remains durably pinned through its expiry, because consume is not safe to retry blindly after an ambiguous response. The existing expiry-date rescue identity is also pinned before an early consume, preventing an immediate expiry rescue of another coupon after refill. Early attempts on a different coupon can proceed only after fresh renewed depletion; they are not limited by that expiry-date rescue identity.

The additive retention column does not protect against old redemption writers: older code purges every pin after 24 hours, including these longer-lived automatic attempts. Complete the replica rollout before relying on extended retention. During rollback, preserve the automatic-attempt ledger and prevent old redemption writes until those attempts have expired or their outcomes have been reconciled; disabling automatic redemption alone does not prevent manual redemption from running the old purge path.
