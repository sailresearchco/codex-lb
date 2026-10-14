## Why

Pro Max accounts report the plan identifier `promax`, which the proxy currently
does not recognize. In a mixed Pro and Pro Max pool, the unknown capacity becomes
zero routing weight, so capacity-weighted routing can leave available Pro Max
accounts unused.

## What Changes

Recognize Pro Max as a distinct account plan with Pro-family model eligibility.
Use the existing Pro capacity as a scheduling reference for Pro Max, including
lease pressure, without treating that reference as a measured subscription
allowance. Honor observed usage percentages when reporting whether a Pro Max
account has available quota. Retain the existing routing strategies and account
ownership and exhaustion rules.

This is a focused backport of the routing and availability portions of upstream
PR #2546. Its broader quota-coverage API and dashboard changes are outside this
change.

## Capabilities

### Modified Capabilities

- `account-routing`: recognize and rank Pro Max accounts without inventing an
  absolute subscription allowance.

## Impact

Plan normalization, bootstrap model eligibility, routing weights, proxy
availability reporting, and the existing Pro-family telemetry bucket change.
There are no database migrations or new settings.
