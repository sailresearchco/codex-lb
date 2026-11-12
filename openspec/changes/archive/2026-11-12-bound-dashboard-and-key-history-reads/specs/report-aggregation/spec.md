## MODIFIED Requirements

### Requirement: Historical raw fallbacks require bounded read coverage

Account and API-key lifetime usage reads, dashboard overview reads, report cost reads, and report model, User-Agent,
and API-key catalogs SHALL check for populated unfolded raw history before
running their aggregates or distinct scans. Account reads SHALL reject countable
live raw rows between their lifetime watermark and four hours before the current
time. The account probe and lifetime aggregate SHALL use the same countable
predicate as a partial covering timestamp index, excluding warmup, deleted, and
account-less history without scanning that excluded prefix. Unknown non-warmup
kinds SHALL remain countable. History that the lifetime folder cannot count SHALL NOT cause a
permanent building response when that folder leaves its watermark unchanged.
API-key lifetime reads SHALL apply the same four-hour coverage allowance to their
countable raw tail, including soft-deleted and account-detached requests with a
key ID. Their probe and aggregate SHALL use a partial timestamp index with the
same non-warmup, key-attributed predicate. An empty key inventory SHALL return
an empty list without aggregating unrelated history. Missing coverage SHALL
return `usage_history_building`; it SHALL NOT truncate lifetime totals.
Dashboard overview reads SHALL require both hourly usage and conversation
coverage for the selected and previous comparison windows. They SHALL reject
populated unfolded tails older than four hours before each window end with
`usage_history_building`, using indexed timestamp existence probes before
aggregation. Covered whole hours and exact partial hours SHALL remain readable;
request metrics SHALL continue including soft-deleted and account-less rows
while conversation metrics preserve their existing eligibility rules.
API-key self-service lifetime totals SHALL use the existing 15-second SQLite
read budget after any expired-limit resets have committed. Codex API-key quota
responses SHALL read current limits without aggregating lifetime request
history; missing keys and keys with no configured limits SHALL remain distinct.
Authentication, limit-reset writes, inference, and OAuth usage SHALL retain
their existing execution paths. The overview earliest-activity lookup SHALL
seek by request kind rather than scan excluded raw history, preserving unknown
kinds, exact retained timestamps, and the existing hourly retention fallback.
Report reads SHALL reject raw rows in an unfolded report segment earlier than
four hours before the earlier of that segment's end and the current time.
These checks SHALL use indexed timestamp-range existence probes, not raw
counts, grouping, or dimension-filtered historical scans. Dimension-independent
probes MAY conservatively defer a filtered request until folding catches up.

An absent or stale watermark alone SHALL NOT reject an empty database, a recent
installation, or a sparse interval with no rows in the excess range. Fully
folded historical windows and exact timezone partial hours SHALL remain
readable. Cost-report coverage SHALL include its previous comparison period
and the activity prefix used to determine comparison availability. That
activity lookup SHALL stop at the previous period's inclusive start.

Unavailable account and report reads SHALL return HTTP 503 with dashboard
codes `usage_history_building` and `report_history_building`, respectively.
They SHALL NOT return zero or partial totals as complete results, and failed
reads SHALL NOT populate successful-response caches. Coverage probes SHALL
NOT advance the subsequent aggregate's time bounds: a concurrent late insert
after a successful probe SHALL remain included. The fifteen-second SQLite
execution budget SHALL continue to bound dense intervals and concurrent work.

#### Scenario: Cold history becomes available after folding

- **GIVEN** retained historical rows populate an unfolded range beyond the four-hour allowance
- **WHEN** Accounts, Reports, or report filter options are requested
- **THEN** the read SHALL return its structured history-building error before aggregating that history
- **WHEN** background folding covers that range and the request is retried
- **THEN** the complete totals and filter choices SHALL be returned

#### Scenario: Sparse bootstrap and timezone edges stay exact

- **GIVEN** coverage is absent or stale but the excess raw range is empty
- **WHEN** a report or account usage read is requested
- **THEN** the request SHALL succeed without truncating the requested interval
- **AND** a covered historical report's partial UTC hours SHALL retain the requested timezone boundaries

#### Scenario: Late writes are not lost after the readiness probe

- **GIVEN** a readiness probe finds no raw rows in the excess range
- **WHEN** a raw row is inserted there before the subsequent aggregate
- **THEN** the aggregate SHALL retain its original boundaries and include that row
