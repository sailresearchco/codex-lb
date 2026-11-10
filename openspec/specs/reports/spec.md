# reports Specification

## Purpose
Operators need the Reports dashboard to be narrowable to specific API-key traffic, matching the Request Logs filter, so summary, daily trend, model and account metrics can be inspected per key. This capability covers the Reports API and dashboard filtering contract.
## Requirements

### Requirement: Report speed medians use mergeable approximate histograms

Speed reports SHALL merge sample-count histograms for TTFT, output-token speed,
and queue time across the selected account, API-key, model, and user-agent
filters. They SHALL retain the existing metric sample predicates and include
soft-deleted requests. Histogram bins SHALL use a fixed schema with at most
507 bins per metric and SHALL omit the conversation dimension.

Each median SHALL disclose its sample count, lower and upper bounds, and
whether either central sample falls in an underflow or overflow bin. Missing
samples SHALL remain distinguishable from a measured zero. Positive in-range
estimates SHALL use harmonic bin representatives with relative error at most
`(2^(1/8)-1)/(2^(1/8)+1)`; mixed-sign central samples and clipped bins SHALL
not claim a finite relative error bound. Even-sized samples SHALL average
the two central estimates and their corresponding bounds.

Background folds SHALL publish at most one UTC hour per transaction, with
counts and their own watermark committed atomically under the shared fold
lock. Migration SHALL create empty storage without scanning historical logs.
Account detachment, hard deletion, and merging SHALL mirror folded counts;
retention SHALL preserve histograms and wait for their fold watermark.

Reads SHALL combine covered whole hours with exact-window raw contributions
for timezone partial hours and at most four hours of unfolded live tail.
Coverage checks and counts SHALL use one database snapshot. Missing or lagged
coverage SHALL return an explicit building state without a historical raw
fallback. Historical partial hours whose raw samples have been pruned SHALL
return unavailable rather than silently apportioning a full-hour histogram.
The initial surviving-history boundary SHALL be persisted as `speed_history_from`
and exposed with the fold watermark. A requested day beginning before that
boundary SHALL be unavailable rather than silently reporting a partial day.
Backlog checks SHALL stop at the observation time for the current day; future
portions of a requested window SHALL NOT require future fold progress.
Explicit backdated request-log inserts older than the fold lag SHALL acquire
the fold-state lock before insertion and add any already-folded speed samples
in that same transaction. A failed append SHALL roll back both the raw row
and histogram delta. An insertion before `speed_history_from` SHALL NOT move
the proven coverage boundary backwards or imply that pruned history was recovered.
Retention SHALL atomically persist a monotonic, inclusive maximum timestamp
actually deleted in each batch. A late insert SHALL NOT erase that pruning
frontier. Partial-hour reads at or before the frontier with matching folded
samples SHALL remain unavailable; covered whole hours and filtered hours
without any matching samples SHALL remain available. An interrupted retention
transaction SHALL roll back its raw deletions and frontier together.

#### Scenario: A backdated insert follows raw retention

- **GIVEN** retention has removed samples from a folded hour
- **WHEN** an older request is inserted and a partial window of that hour is requested
- **THEN** the partial window remains unavailable even if the oldest retained request is now earlier
- **AND** a complete folded-hour report includes the late sample without losing the retained histogram counts

#### Scenario: A filtered timezone day spans folded and live data

- **GIVEN** a day whose UTC boundaries fall within hours and a current histogram watermark
- **WHEN** speed metrics are requested with account, key, model, or user-agent filters
- **THEN** every eligible sample in that exact day contributes once to its metric
- **AND** each returned median interval contains the corresponding exact median

#### Scenario: Backfill has not reached the requested history

- **GIVEN** a missing or lagged histogram watermark requiring more than four hours of raw tail
- **WHEN** a speed report is requested
- **THEN** it returns building without aggregating the historical raw backlog

#### Scenario: A fold is interrupted after writing some metrics

- **WHEN** a histogram fold is cancelled before committing its hour
- **THEN** every metric count and the watermark roll back together
- **AND** rerunning the fold produces each sample exactly once
### Requirement: Independent speed calculations

The cost-report response SHALL load without calculating raw-request speed medians. Speed charts SHALL use an independently cached, cancellable read with the same date, timezone, account, API-key, model, and user-agent filters. A speed calculation failure SHALL leave cost and usage totals available and expose a retry action for speed metrics. Hidden speed charts SHALL NOT issue speed requests.

#### Scenario: Slow speed calculation
- **WHEN** the speed calculation exceeds its read budget
- **THEN** the cost report SHALL remain available and the speed charts SHALL show the failure without displaying zero-valued measurements as successful results.

#### Scenario: Speed charts are hidden
- **WHEN** no speed chart is visible
- **THEN** loading or refreshing the cost report SHALL NOT calculate speed metrics.

### Requirement: API Key Filtering

The `GET /api/reports` endpoint MUST support filtering report aggregations by one or more `api_key_id` query parameters.

#### Scenario: Single API Key filter applied
- **WHEN** `GET /api/reports` is requested with `api_key_id=key-1`
- **THEN** all summary, daily breakdown, model distribution, and account distribution metrics SHALL only include request logs generated by `key-1`.

#### Scenario: Multiple API Key filters applied
- **WHEN** `GET /api/reports` is requested with `api_key_id=key-1&api_key_id=key-2`
- **THEN** report metrics SHALL include request logs matching either `key-1` or `key-2`.

#### Scenario: Default all API Keys when unselected
- **WHEN** `GET /api/reports` is requested without any `api_key_id` query parameter
- **THEN** report metrics SHALL aggregate across all API keys without filtering by key ID.

### Requirement: Retained thread identity uses exact hourly caches

Thread identity metrics SHALL retain the existing seven-day window ceiling, pool-wide scope, and keyed/unkeyed facets. The effective thread key SHALL be the normalized conversation identifier, then normalized session identifier, then API key for unkeyed traffic. Conversation and session identifiers SHALL share a namespace; keyed and unkeyed groups SHALL remain separate. Missing effective keys SHALL contribute request/cache counts but no conversation or turn counts. Internal warmup traffic SHALL be excluded and soft-deleted traffic SHALL remain included.

Complete folded UTC hours SHALL use disposable header, account-presence, and directed-pair caches describing retained raw history. Partial hours and the live tail SHALL use exact raw rows; the uncovered live tail SHALL be no longer than four hours, measured through the earlier of the requested end and the current time. Coverage, repair markers, folded data, and raw edges SHALL share one statement snapshot. Incomplete coverage SHALL raise `thread_identity_building`; overlapping dirty hours SHALL raise `thread_identity_repairing`. Requests wider than seven days SHALL retain the existing `available=false` response. Request handling SHALL NOT backfill caches or fall back to a full-history scan.

#### Scenario: Hour boundaries preserve thread semantics
- **GIVEN** one thread spans folded hours and partial or live raw edges
- **WHEN** its metrics are read
- **THEN** consecutive requests SHALL be ordered by timestamp and row id across all segments
- **AND** a turn SHALL require attributed endpoints and a whole-second gap strictly below 600 seconds
- **AND** unkeyed turns SHALL additionally require known, nonshrinking input-token counts, while keyed turns SHALL retain failure-adjacent requests with missing usage
- **AND** distinct accounts and the three-request conversation threshold SHALL be evaluated over the whole selected window
- **AND** cache totals SHALL include only successful requests with more than 5,000 input tokens

#### Scenario: History changes never publish stale repaired metrics
- **WHEN** hard deletion, partial retention, or an explicitly backdated append changes a folded hour
- **THEN** the same mutation transaction SHALL mark that hour dirty before it becomes visible
- **AND** requests touching it SHALL report repairing until an atomic background rebuild completes
- **AND** an earlier successful response SHALL NOT remain cached across that repair
- **AND** fully pruned hour caches SHALL be removed rather than preserved as permanent history

#### Scenario: Account attribution changes preserve adjacency
- **WHEN** an account is detached or merged
- **THEN** account presence and header endpoints SHALL follow the raw attribution change
- **AND** turn counts SHALL remain determined by consecutive surviving requests, with switches computed from the resulting endpoint identities
- **AND** hard deletion of a middle request SHALL rebuild newly adjacent endpoint pairs

#### Scenario: Cache construction is resumable background work
- **WHEN** cache construction or repair runs
- **THEN** each transaction SHALL cover at most one UTC hour and each pass at most eight transactions
- **AND** the migration SHALL create empty caches without scanning raw history
- **AND** interrupted transactions SHALL leave the prior cache and durable repair marker intact
- **AND** concurrent late writes SHALL either be included in the published hour or leave a repair marker after publication
