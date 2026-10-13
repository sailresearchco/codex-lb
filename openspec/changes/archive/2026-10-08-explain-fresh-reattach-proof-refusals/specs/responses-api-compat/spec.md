## ADDED Requirements

### Requirement: Fresh-reattach anchor injection exposes bounded proof diagnostics

Both `fresh_reattach_anchor_injected` branches SHALL include the existing anchor
rejection classification, whether the original payload is full-resend shaped,
the durable stored input-item count, the computed context-retention result,
the known pending-tool-manifest count, and whether a same-owner full-resend proof
is present. The diagnostics SHALL distinguish an unknown tool manifest from an
empty manifest and SHALL identify context evaluation skipped by an earlier
anchor rejection.

The diagnostics MUST reuse the current classification results without changing
replay eligibility, account ownership, anchor injection, or request dispatch.
They MUST NOT add message content, ciphertext, tool names or arguments, item
identifiers, fingerprints, or identifiers beyond the existing event fields.

#### Scenario: Stored anchor metadata rejects a full resend
- **GIVEN** an original full-resend-shaped payload whose existing anchor check fails
- **WHEN** a fresh bridge injects the durable anchor
- **THEN** the event reports that anchor rejection and the stored item count
- **AND** context retention is reported as not checked

#### Scenario: Context or sealed same-owner proof refuses preservation
- **GIVEN** the existing anchor check passes
- **WHEN** a fresh bridge injects the anchor instead of preserving the original full resend
- **THEN** the event reports the computed context-retention result and proof presence
- **AND** a missing pending-tool manifest is distinguishable from a known empty manifest
