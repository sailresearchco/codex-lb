## MODIFIED Requirements

### Requirement: Trusted-proxy locality requires trusted socket provenance

When proxy-header trust is enabled, the system MUST classify a forwarded loopback client as local only when the raw socket peer belongs to a configured trusted-proxy CIDR and forwarded client resolution succeeds. The mere presence of a forwarded client-IP header from an untrusted socket peer MUST NOT establish locality or bypass remote dashboard bootstrap requirements.

Trusted-header identity admission and identity-header sanitization MUST check the captured raw socket peer against the configured trusted-proxy CIDRs. Uvicorn client-address projection MUST NOT establish identity-header trust. Missing captured socket provenance MUST fail closed. A trusted raw proxy peer MAY authenticate its asserted identity when its forwarded client address is external.

#### Scenario: Projected loopback cannot authenticate an untrusted peer

- **WHEN** an untrusted raw peer supplies an identity header and a forwarded loopback address
- **AND** proxy projection accepts the forwarded address
- **THEN** the identity header is removed and cannot authenticate a dashboard principal

#### Scenario: Trusted identity proxy forwards an external client

- **WHEN** the raw peer belongs to a configured trusted-proxy CIDR
- **AND** it forwards an external client address and one valid identity header
- **THEN** client-address projection does not remove the proxy's identity authority

#### Scenario: Untrusted loopback proxy cannot bypass remote bootstrap

- **WHEN** proxy-header trust is enabled
- **AND** the raw loopback socket peer is outside every configured trusted-proxy CIDR
- **AND** the request supplies a local Host header and a forwarded client-IP header
- **THEN** the request is classified as remote
- **AND** first-run password setup requires the configured bootstrap token

#### Scenario: Trusted proxy may forward a loopback client

- **WHEN** proxy-header trust is enabled
- **AND** the raw socket peer belongs to a configured trusted-proxy CIDR
- **AND** valid forwarded client resolution yields a loopback address
- **AND** the Host header is local
- **THEN** the request is classified as local
