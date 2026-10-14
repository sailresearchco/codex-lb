## ADDED Requirements

### Requirement: Recognize Pro Max account eligibility

The proxy MUST preserve `promax` as a recognized account plan. A Pro Max account
MAY satisfy Pro-family model eligibility, subject to existing authoritative
account and model restrictions. Pro and Pro Lite accounts MUST NOT satisfy a
Pro Max-only plan restriction through this equivalence.

#### Scenario: Pro Max satisfies a Pro-family model restriction

- **GIVEN** an account reports `promax`
- **WHEN** a model permits Pro accounts and no authoritative restriction excludes the account
- **THEN** the account satisfies the model's plan restriction
- **AND** its plan identity remains `promax`

### Requirement: Pro Max participates in weighted routing

The balancer MUST use the Pro scheduling reference for Pro Max capacity weights
and lease pressure without adding an absolute Pro Max allowance to usage
accounting. Existing explicit upstream exhaustion and ownership constraints
MUST continue to apply.

#### Scenario: Available Pro Max account competes in a mixed pool

- **GIVEN** available Pro and Pro Max accounts have comparable remaining percentages and reset times
- **WHEN** capacity-weighted or relative-availability selection ranks the pool
- **THEN** both accounts have positive selection weight
- **AND** Pro Max absolute subscription capacity remains unknown in usage accounting

#### Scenario: Explicitly exhausted Pro Max account remains excluded

- **GIVEN** a Pro Max account has an explicit upstream quota-exhausted state with an unexpired reset
- **AND** no recovery evidence or spendable-credit override clears that state
- **WHEN** the balancer selects an account
- **THEN** the Pro Max scheduling reference does not make the exhausted account eligible

### Requirement: Percentage-only Pro Max availability

Proxy rate-limit reporting MUST consider observed Pro Max usage percentages
without requiring an absolute allowance. The account MUST be considered
available only when at least one applicable window is observed and all observed
applicable windows are below 100 percent. A mixed Pro and Pro Max pool MUST
report the Pro Max plan in preference to the Pro plan.

#### Scenario: One observed window is exhausted

- **GIVEN** a Pro Max account has an available short window and an exhausted long window
- **WHEN** proxy rate-limit reporting evaluates that account
- **THEN** the account does not make the pool report available quota

#### Scenario: Pro Max remains available in a mixed pool

- **GIVEN** the Pro account is exhausted and the Pro Max account's observed windows are below 100 percent
- **WHEN** proxy rate-limit reporting evaluates the pool
- **THEN** it reports available quota and the `promax` plan

#### Scenario: Historical Free monthly exhaustion does not block Pro Max

- **GIVEN** an account upgrades from Free to Pro Max with a still-unexpired exhausted monthly row
- **AND** its fresh Pro Max weekly usage is below 100 percent
- **WHEN** proxy rate-limit reporting evaluates the account
- **THEN** it ignores the inapplicable historical monthly row and reports available quota
