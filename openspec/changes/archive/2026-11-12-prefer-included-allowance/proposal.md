## Why

An exhausted account can remain eligible through its extra-credit balance. Earlier-reset filtering and sticky-owner reuse can then select it while another compatible account has included allowance. The pool should consume included allowance before falling back to extra credits.

## What Changes

- Prefer eligible accounts whose applicable included usage windows are not exhausted before reset timing, manual routing preferences, and strategy weighting.
- Apply the preference when choosing a new account; preserve established sticky affinity, initial session preferences, file ownership, response continuity, and reusable transport ownership.
- Do not add any exhaustion-triggered affinity reallocation or owner admission error. Established conversations may continue using their owner's extra credits.
- Keep existing fallback when no eligible included-allowance account remains. Do not redeem resets or purchase credits.

## Impact

- Fresh account selection and normalized usage classification.
- Account-routing and sticky-session-operations contracts and regression coverage.
- No schema changes, new settings, or financial actions.
