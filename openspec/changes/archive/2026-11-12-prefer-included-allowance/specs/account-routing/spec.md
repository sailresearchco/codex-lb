## ADDED Requirements

### Requirement: Included allowance precedes discretionary routing preferences

When choosing a new account for foreground traffic from a request's compatible eligible pool, selection SHALL prefer accounts whose applicable included usage windows are not known to be exhausted over accounts whose applicable included usage is exhausted. After existing recovery-probe admission, this preference SHALL precede reset-window buckets, manual routing preferences, health-tier preferences, planner costs, and strategy selection. It MUST NOT broaden authorization, model, account-assignment, explicit single-account, cooldown, or capacity constraints. If no non-exhausted account is eligible, the existing exhausted-account fallback SHALL remain available. This preference SHALL NOT create a new reason to reject, rebind, or revalidate established conversation ownership or existing affinity. Opportunistic traffic SHALL retain its existing pool-dependent admission and burn policies.

Exhaustion SHALL be derived from normalized upstream usage for the request's applicable quota windows before adding local in-flight or leased-token pressure. An applicable window reporting at least 100 percent used SHALL mark the account exhausted. Elapsed windows SHALL follow existing reset normalization. Missing usage SHALL NOT by itself be treated as exhaustion. Additional-quota requests SHALL use their applicable quota rather than unrelated standard quota.

#### Scenario: Later-reset subscription allowance precedes exhausted early-reset account
- **GIVEN** an eligible account has exhausted included allowance and extra credits and resets tomorrow
- **AND** another eligible account has included allowance and resets next week
- **WHEN** capacity-weighted routing prefers earlier weekly resets
- **THEN** it selects the account with included allowance

#### Scenario: Pressure alone does not classify allowance as exhausted
- **GIVEN** reported applicable usage is below 100 percent
- **AND** local in-flight or leased-token pressure raises effective usage to 100 percent
- **WHEN** the selector classifies included allowance
- **THEN** it does not classify that account as exhausted solely because of local pressure

#### Scenario: Ineligible allowance cannot suppress available fallback
- **GIVEN** all non-exhausted accounts are outside the request scope or unavailable
- **AND** an exhausted account remains eligible
- **WHEN** selection runs
- **THEN** it retains the existing eligible exhausted-account fallback

#### Scenario: Unknown usage preserves bootstrap behavior
- **GIVEN** an otherwise eligible account has no usage samples
- **WHEN** selection runs
- **THEN** missing usage alone does not exclude the account

#### Scenario: Included allowance does not bypass recovery-probe admission
- **GIVEN** the pool has an eligible healthy account with exhausted included allowance
- **AND** another account has included allowance but remains in probing state
- **WHEN** the probe is not yet due
- **THEN** the preference does not turn the probing account into an ordinary admission
- **AND** when its probe becomes due, existing probe reservation and admission rules apply
