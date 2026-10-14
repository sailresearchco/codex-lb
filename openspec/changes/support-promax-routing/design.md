## Context

The balancer uses absolute capacity to weight remaining usage. Pro Max provides
usage percentages but has no verified absolute allowance in the local capacity
table. Converting its unknown allowance into zero makes it lose to every
positive-weight Pro account in the same pool.

## Decision

Keep `promax` as the account's identity. It may match Pro model eligibility,
while Pro and Pro Lite must not acquire eligibility reserved for Pro Max.
Use the existing Pro capacity only inside scheduling and lease-pressure
calculation. Usage totals continue to treat the absolute Pro Max allowance as
unknown. For example, a Pro Max account at 20 percent weekly usage can compete
with a Pro account at the same percentage, but this does not assert that the
two subscriptions have equal absolute quotas.

Availability reporting uses observed Pro Max percentages across the reported
windows. An exhausted window cannot be ignored merely because its absolute
allowance is unknown. Explicit upstream quota and rate-limit blocks retain
their existing recovery rules. Account ownership and authoritative model
restrictions are evaluated by the existing selection pipeline.

## Tradeoffs

The Pro scheduling reference is a conservative heuristic, not a measurement of
Pro Max entitlement. It restores participation without claiming a larger
allowance or changing routing strategy. Accurate pooled absolute quota coverage
and dashboard presentation remain outside this backport.
