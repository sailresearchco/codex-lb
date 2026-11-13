## ADDED Requirements

### Requirement: Conservative early automatic weekly redemption

When `auto_redeem_reset_credits_before_expiry` is enabled, the scheduler SHALL also consider an early redemption when weekly allowance has at most 1% remaining and its next natural weekly refresh is at least 72 hours away. It SHALL select one earliest-expiring available coupon, require an unexpired known coupon expiry, and reuse the existing per-account serialization and redemption helper. The selected coupon and redemption reason SHALL be evaluated at one clock instant and remain bound to that exact coupon; its expiration SHALL NOT retarget an expiry rescue to another coupon. Before pinning or consuming, the helper SHALL successfully fetch upstream usage inside that serialization and re-evaluate the thresholds using a weekly measurement returned by that fetch. Missing, failed, unknown-duration, nonfinite, or nonweekly usage SHALL NOT authorize early redemption. A seven-day primary window SHALL qualify as weekly usage.

The scheduler SHALL retain the existing final-five-minute expiry fallback. Early redemption SHALL durably pin the existing account-plus-expiry-date fallback identity before consume with a deadline at least 24 hours after the attempt, extending an existing earlier deadline, so that fallback cannot immediately spend another coupon. An early attempt on a different coupon MAY proceed after newly confirmed depletion even when the fallback identity is pinned. Each early coupon attempt SHALL have a stable account-and-coupon request identity retained through coupon expiry; an already-pinned automatic attempt SHALL NOT call consume again, including after an ambiguous failure. The expiry fallback SHALL also honor that coupon pin. Manual request identities without an explicit retention deadline SHALL retain their existing 24-hour lifetime.

#### Scenario: Exact conservative thresholds qualify
- **GIVEN** automatic redemption is enabled and an account has an available unexpired coupon
- **WHEN** fresh weekly allowance reports exactly 1% remaining and exactly 72 hours until its natural refresh
- **THEN** one earliest-expiring coupon is redeemed

#### Scenario: Near natural refresh or usable weekly allowance waits
- **WHEN** weekly remaining allowance exceeds 1% or its next natural refresh is less than 72 hours away
- **THEN** no early redemption occurs
- **AND** the final-five-minute fallback remains available

#### Scenario: A manual reset invalidates the early candidate
- **GIVEN** a stored measurement selected an account for early redemption
- **AND** a manual reset restores allowance before automatic redemption obtains serialization
- **WHEN** automatic redemption fetches usage under serialization
- **THEN** no coupon is pinned or consumed by the early attempt

#### Scenario: Missing fresh weekly usage fails closed
- **GIVEN** an old stored weekly measurement qualifies
- **WHEN** the forced usage fetch fails or returns no known weekly measurement
- **THEN** the old measurement does not authorize consumption

#### Scenario: Separate coupons require renewed depletion
- **GIVEN** an early redemption has restored an account's allowance and another coupon shares its expiry date
- **AND** the prior early attempt was less than 24 hours ago
- **WHEN** the scheduler runs again before renewed depletion
- **THEN** neither early redemption nor the expiry fallback consumes that second coupon
- **WHEN** fresh weekly allowance later reaches at most 1% with at least 72 hours until refresh
- **THEN** the second coupon can be redeemed early

#### Scenario: An ambiguous consume is not retried automatically
- **GIVEN** an early coupon attempt was durably pinned before an upstream consume whose outcome is unknown
- **WHEN** a later early or expiry-fallback cycle sees that same coupon
- **THEN** it does not issue another upstream consume through the coupon's expiry


## MODIFIED Requirements

### Requirement: Reset credits are polled per account on a fixed cadence

The system SHALL poll upstream `GET /wham/rate-limit-reset-credits` for each eligible account on a fixed 60-second cadence, using that account's stored OAuth bearer token and `chatgpt-account-id`. The scheduler SHALL start with the application lifespan when reset-credit polling is enabled. Because snapshots are kept in process-local memory, every running replica SHALL refresh its own snapshot cache instead of relying on leader election, and the scheduler SHALL NOT be leader-gated while snapshots remain process-local. Each replica SHALL apply a randomized startup delay of up to one full interval and randomized per-tick jitter of +/-10% so replica ticks are desynchronized. The aggregate upstream fetch rate scales with the number of running replicas (one fetch per eligible account per 60 seconds per replica); the cadence is a fixed application constant and MUST NOT be operator-configurable. The poll SHALL skip any account that is paused, requires reauthentication, deactivated, or lacks a usable `chatgpt-account-id`.

When dashboard setting `auto_redeem_reset_credits_before_expiry` is enabled, the refresh loop SHALL evaluate refreshed snapshots and attempt to redeem the soonest-expiring available reset credit when it expires within five minutes by reusing the existing reset-credit redemption function, serialization, idempotency ledger, cache invalidation, and usage-refresh path. Before invoking the redemption function, automatic redemption SHALL re-read the target account in the redemption session and abort without consuming upstream when the account is missing, paused, requires reauthentication, deactivated, or no longer has a usable `chatgpt-account-id`. Automatic redemption SHALL constrain the redemption helper to the credit id and expiry that triggered the five-minute window, and SHALL abort without consuming upstream if the helper's fresh pre-consume fetch no longer reports that same credit with the same expiry as available. Automatic redemption SHALL use a stable automatic redeem request id for the account and UTC expiry date, and SHALL NOT issue another upstream consume when that automatic request is already durably pinned.

#### Scenario: Default cadence polls every 60 seconds
- **WHEN** the application starts
- **THEN** each eligible account's credits are fetched from upstream at most once per 60 seconds plus the jitter bound
- **AND** a `CODEX_LB_RATE_LIMIT_RESET_CREDITS_REFRESH_INTERVAL_SECONDS` value in the environment is ignored with the removed-setting warning

#### Scenario: Every replica refreshes its local cache
- **WHEN** the application is deployed with multiple running replicas
- **THEN** each replica refreshes its own in-memory reset-credit snapshots on the fixed cadence
- **AND** dashboard reads served by any replica can observe populated reset-credit data after that replica's refresh tick

#### Scenario: Two replicas do not fetch in lockstep
- **GIVEN** two replicas start
- **WHEN** their refresh loops run
- **THEN** their startup delays are independent uniform draws over the full interval and each tick interval carries independent +/-10% jitter, so the replicas' tick times are not synchronized

#### Scenario: Ineligible accounts are skipped
- **WHEN** an account is persisted as `paused`, `reauth_required`, or `deactivated`
- **THEN** the scheduler performs no upstream reset-credits fetch for that account
- **AND** the cached snapshot for that account (if any) is left untouched by the skip

#### Scenario: Automatic redemption is disabled by default
- **WHEN** the dashboard settings row is created for the first time
- **THEN** `auto_redeem_reset_credits_before_expiry` is `false`
- **AND** the reset-credit refresh scheduler only refreshes snapshots and does not redeem credits automatically

#### Scenario: Automatic redemption reuses the existing redeem path
- **GIVEN** `auto_redeem_reset_credits_before_expiry` is enabled
- **AND** a refreshed eligible account snapshot includes an available credit whose expiry is within the automatic redemption window
- **WHEN** the reset-credit refresh loop processes that account
- **THEN** the system redeems the soonest-expiring available credit through the same redemption function used by the dashboard consume endpoint
- **AND** the redemption uses the existing per-account serializer, durable idempotency ledger, cache invalidation, and usage refresh behavior
- **AND** duplicate automatic attempts for an already pinned automatic request do not issue another upstream consume

#### Scenario: Automatic redemption ignores non-expiring snapshots
- **GIVEN** `auto_redeem_reset_credits_before_expiry` is enabled
- **AND** a refreshed eligible account snapshot has no available credit with `expires_at`
- **WHEN** the reset-credit refresh loop processes that account
- **THEN** the system does not attempt automatic redemption for that snapshot

#### Scenario: Automatic redemption waits when neither policy qualifies
- **GIVEN** `auto_redeem_reset_credits_before_expiry` is enabled
- **AND** a refreshed eligible account snapshot's soonest available credit expires more than five minutes in the future
- **AND** the account does not qualify for conservative early weekly redemption
- **WHEN** the reset-credit refresh loop processes that account
- **THEN** the system refreshes the snapshot but does not attempt automatic redemption

### Requirement: Reset credit redemption is serialized and idempotent across replicas

Per-account redemption serialization MUST hold across all replicas and processes sharing one database. On PostgreSQL the system SHALL use `pg_advisory_xact_lock` keyed by the account id on the caller's session. On SQLite the system SHALL acquire a durable claim row via a single atomic conditional upsert (`INSERT ... ON CONFLICT(account_id) DO UPDATE ... WHERE expires_at < now`) with a 30-second lease, a bounded retry loop that surfaces a client-facing conflict on timeout, release on completion, and takeover of expired claims. While the redeem section runs, the claim holder SHALL renew its lease on a heartbeat cadence shorter than the lease (10 seconds) so a redemption that legitimately outlives one lease (e.g. slow upstream fetch/consume) is NOT taken over by a concurrent process; lease expiry without renewal remains the crash-recovery path. A claim-acquisition timeout SHALL surface in the caller surface's native error envelope: the dashboard error envelope on the dashboard consume endpoint and the `/v1/*` OpenAI error envelope (HTTP 409) on `POST /v1/reset-credit`. The system SHALL persist the `(account_id, redeem_request_id) -> credit_id` mapping in the shared database, committed inside the serialized section BEFORE the upstream consume call; a retry carrying the same `redeem_request_id`, served by ANY replica, MUST resolve to the originally selected `credit_id` and MUST NOT consume a different credit. Ledger rows without an explicit retention deadline SHALL retain the existing 24-hour lifetime, including after a failed consume. Rows with an explicit `expires_at` SHALL remain pinned through that deadline regardless of `created_at`; explicit deadlines SHALL be normalized to UTC before persistence. Both lookup and opportunistic purge SHALL apply the same expiration rule. Expired rows for an account SHALL be purged BEFORE a new pin is inserted, so an expired `redeem_request_id` can durably pin its newly selected credit instead of colliding with a stale row. Extending a fallback guard SHALL only increase its deadline and SHALL preserve its first-writer credit identity. Both the dashboard consume endpoint and `POST /v1/reset-credit` SHALL redeem inside this cross-replica serialized section.

#### Scenario: Retry lands on a second replica and reuses the pinned credit
- **GIVEN** replica A redeemed the soonest credit for `redeem_request_id` R but the client never saw the response
- **WHEN** the client retries the consume with the same R and the request is served by replica B
- **THEN** replica B forwards the originally pinned `credit_id` to upstream
- **AND** no second credit is consumed for that account

#### Scenario: Two processes on one SQLite file redeem concurrently
- **GIVEN** two processes sharing one SQLite database each receive a consume request for the same account at nearly the same time
- **WHEN** the first process holds the durable redeem claim
- **THEN** the second process waits on (or conflicts out of) the claim instead of redeeming in parallel
- **AND** at most one upstream consume is sent per selected credit

#### Scenario: Claim holder crashes and the lease recovers
- **GIVEN** a process crashed while holding the redeem claim for an account
- **WHEN** a later consume request arrives after the claim lease has expired
- **THEN** the request takes over the expired claim and proceeds without operator intervention

#### Scenario: Slow redemption keeps its claim past the original lease
- **GIVEN** a process holds the redeem claim and its redeem section (upstream fetch/consume, usage refresh) runs longer than one 30-second lease
- **WHEN** a second process attempts to acquire the claim after the original lease would have expired
- **THEN** the heartbeat-renewed lease rejects the takeover and the second process keeps waiting (or conflicts out)
- **AND** at most one upstream consume is sent per selected credit

#### Scenario: Reused redeem_request_id after TTL re-pins the new credit
- **GIVEN** an account has a ledger row for `redeem_request_id` R pinned to credit C1 without an explicit retention deadline whose `created_at` is older than the 24h TTL
- **WHEN** a new redemption reuses R and selects a different credit C2
- **THEN** the expired row is purged before the new insert so the ledger persists `(R -> C2)`
- **AND** a same-R retry served by any replica retargets C2, not the discarded C1

#### Scenario: Expired pin is ignored on read
- **GIVEN** an account has a ledger row for `redeem_request_id` R without an explicit retention deadline whose `created_at` is older than the 24h TTL
- **WHEN** the pin lookup for `(account_id, R)` runs before any purge write
- **THEN** the lookup returns no durable pin (the expired row reads as absent)
- **AND** the redemption re-selects against the fresh fetch and re-pins the newly selected credit rather than forwarding the stale expired `credit_id`

#### Scenario: Claim contention on the v1 surface uses the OpenAI envelope
- **GIVEN** another process holds the redeem claim for the whole acquisition timeout
- **WHEN** a client calls `POST /v1/reset-credit` for that account
- **THEN** the endpoint returns 409 in the `/v1/*` OpenAI error envelope, not the dashboard envelope
