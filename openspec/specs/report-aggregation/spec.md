# report-aggregation Specification

## Purpose
Governs how dashboard reports are assembled from permanent time-bucketed aggregates plus the not-yet-folded raw complement, so historical totals, filter dimensions, and distinct conversation counts survive raw request-log retention and stay correct for any requested timezone. Long report windows previously scanned raw rows until they timed out and every filter change re-ran the full report; this capability bounds that work by preferring folded history, keeping filter options and report caching lightweight, and limiting exact speed metrics to short windows with explicit disclosure.
## Requirements

### Requirement: Historical raw fallbacks require bounded read coverage

Account lifetime usage reads, report cost reads, and report model, User-Agent,
and API-key catalogs SHALL check for populated unfolded raw history before
running their aggregates or distinct scans. Account reads SHALL reject countable
live raw rows between their lifetime watermark and four hours before the current
time. The account probe and lifetime aggregate SHALL use the same countable
predicate as a partial covering timestamp index, excluding warmup, deleted, and
account-less history without scanning that excluded prefix. Unknown non-warmup
kinds SHALL remain countable. History that the lifetime folder cannot count SHALL NOT cause a
permanent building response when that folder leaves its watermark unchanged.
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

### Requirement: Permanent report aggregates
The system SHALL preserve request counts, error and cancellation counts, token totals, cost, first activity, active accounts and distinct normalized conversations in permanent time buckets, including account, API key, model and User-Agent filter dimensions. Report days SHALL respect the requested timezone. Folded history and raw complement SHALL be read in one snapshot without overlap. Partial storage buckets SHALL use raw data; retained history at sub-hour boundaries has the same bounded edge limitation as existing hourly statistics.

#### Scenario: Historical and live data coexist
- **GIVEN** a fold watermark inside the report window
- **WHEN** a filtered report is requested
- **THEN** folded rows and raw rows contribute exactly once and totals match the unfurled raw history

#### Scenario: Retention and lifecycle changes
- **WHEN** retention prunes raw logs
- **THEN** it SHALL wait for report fold coverage and report totals SHALL remain available
- **AND** account soft delete, hard delete and consolidation SHALL mirror report aggregates under the shared fold lock

### Requirement: Lightweight filter options
`GET /api/reports/options` SHALL apply the report date, timezone, account and API-key scope and return distinct models and nonblank User-Agent groups without running speed or full-report aggregates. It SHALL use dashboard authentication and the same date-range validation as reports. The response SHALL also include API-key choices for principals with `api_keys:read`, combining current key metadata and key IDs present in the selected report period's folded history and bounded raw complement. Deleted IDs SHALL retain ID fallback labels. The key catalog SHALL NOT self-filter on selected API keys or run full-history raw-log discovery, speed calculations, measure aggregation, or conversation grouping. Other principals SHALL receive an empty API-key list, including after an authorized response populated shared caches. The browser SHALL obtain key choices from this endpoint rather than request-log options. Browser catalog caches SHALL be separated by principal and API-key read scope, evicted when either changes, and redact key metadata immediately when that permission is absent.

#### Scenario: Filter catalog loads
- **WHEN** the report page loads or its scope changes
- **THEN** it SHALL request options from the dedicated endpoint instead of a second full report

### Requirement: Indexed first activity lookup
Reports SHALL determine first activity from the minimum timestamp in the earliest matching folded hour and an indexed raw-tail minimum, in one snapshot. This lookup SHALL preserve report scope and internal-traffic exclusions without calculating unrelated measures or grouping all historical request rows.

#### Scenario: First activity spans retained and live history
- **GIVEN** filtered history exists before and after the report fold watermark
- **WHEN** a report requests its first activity timestamp
- **THEN** the earliest matching timestamp SHALL remain exact before and after covered raw history is pruned

### Requirement: Bounded speed work
Exact speed medians SHALL only run for date windows of at most seven days. Longer windows SHALL retain zero-valued numeric speed fields for compatibility, return `speedMetricsAvailable=false` and the maximum supported speed-window size and SHALL NOT execute median SQL. The dashboard SHALL explain the omission and hide unavailable speed charts.

#### Scenario: Ninety-day report
- **WHEN** an operator requests a 90-day report
- **THEN** the report SHALL return aggregate totals without running raw median calculations
- **AND** the page SHALL state that speed metrics require a window of seven days or less

### Requirement: Bounded report caching
Successful reports and options SHALL be cached for at most 60 seconds per normalized filter key within an application process, with bounded entry count and coalesced concurrent requests. Failures and cancelled loads SHALL NOT be cached. Browser report queries SHALL have a five-minute freshness interval, no periodic polling and no automatic retry. An explicit refresh control SHALL remain available. Reports SHALL expose their server generation time and the page SHALL show that time so an open, unpolled report does not imply live data.

#### Scenario: Concurrent identical reports
- **WHEN** concurrent requests use the same report filters
- **THEN** at most one aggregate computation SHALL populate their shared cache result

#### Scenario: Cached result during an unrelated slow computation
- **GIVEN** an unexpired cached result and an unrelated report computation in progress
- **WHEN** the cached report is requested
- **THEN** it SHALL return without waiting for the unrelated computation
- **AND** uncached computations SHALL remain bounded across requests by a cache instance initialized before requests are accepted

#### Scenario: Failed computation
- **WHEN** a computation fails or is cancelled
- **THEN** the next request SHALL be able to compute a fresh result

### Requirement: SQLite dashboard read execution budget
Dashboard overview, projections, request-log listing and options, conversation listing and details, reports, account listing, account trends, cached account reset-credit reads, API-key listing, API-key trends, and API-key seven-day usage SHALL share a fifteen-second SQLite query budget within each request after acquiring their private file-backed connection. SQLite execution and row fetching SHALL stop when that budget expires, including synchronous usage-history reads. Lock waits SHALL use the remaining budget. Expiration SHALL return HTTP 503 with dashboard error code `dashboard_query_timeout`. Cleanup SHALL remove connection-local deadlines before ordinary reuse. This budget SHALL NOT change account or API-key mutations, upstream usage-reset-credit fetches and credential refresh, proxy writes, migration connections, PostgreSQL deadlines, or shared in-memory SQLite connections.

#### Scenario: A query exceeds the read budget
- **WHEN** a selected dashboard read exceeds its SQLite execution budget
- **THEN** SQLite SHALL stop the query and the request SHALL return `dashboard_query_timeout`
- **AND** subsequent reads and writes SHALL remain usable after cleanup

#### Scenario: Synchronous usage history consumes the remaining budget
- **GIVEN** a dashboard request has already spent part of its query budget
- **WHEN** it reads usage history on a worker thread
- **THEN** that read and its row fetching SHALL use the same remaining deadline
