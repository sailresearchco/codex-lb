## ADDED Requirements

### Requirement: Bridge admission refusals remain visible to native clients

The HTTP bridge's submit retry-circuit and stale replay-generation guards MUST record refusals as local pre-dispatch failures. The guards MUST retain their existing status, error code, retry delay, and prevention of upstream dispatch.

Before downstream response commitment, the proxy MUST return the refusal's HTTP status, error envelope, and `Retry-After`. After commitment, it MUST emit the existing unmarked terminal `response.failed` and retry hint, followed by `[DONE]`. A local refusal MUST NOT become an empty successful stream.

Internal owner forwarding MUST preserve local refusal provenance and a valid bounded retry delay for both HTTP and committed-stream delivery. Unmarked ambiguous upstream transport failures MUST retain their existing delivery policy.

#### Scenario: A refused recovery retry is visible before commitment

- **GIVEN** a native Codex request is blocked by the retry-circuit submission guard
- **WHEN** downstream headers have not committed
- **THEN** it receives HTTP 503, the original error, and `Retry-After`
- **AND** no upstream frame is sent for that refused request

#### Scenario: A refusal after commitment terminates the stream explicitly

- **GIVEN** a native request has already received HTTP 200 headers
- **WHEN** either local submission guard refuses dispatch
- **THEN** the client receives one unmarked `response.failed`, the retry hint, and `[DONE]`
- **AND** no upstream frame is sent for that refused request

#### Scenario: Owner forwarding retains the retry delay

- **GIVEN** the owning instance returns a local pre-dispatch refusal with `Retry-After`
- **WHEN** the origin reconstructs the failure
- **THEN** it retains both refusal provenance and the validated retry delay
- **AND** the client receives the same refusal policy as a locally owned request
