## ADDED Requirements

### Requirement: Internal drain routes require proven local access

The internal drain start, stop, and status routes MUST require a captured loopback raw socket peer, a local Host, and no forwarded client-IP hints. A projected loopback client address alone MUST NOT authorize drain access. Missing raw socket provenance, a public Host, or a forwarded client-IP hint MUST fail closed. Direct loopback requests using a local Host MUST retain drain access regardless of proxy-trust configuration. Dashboard bootstrap locality policy SHALL remain unchanged.

#### Scenario: External request projects a loopback client

- **WHEN** an external raw peer supplies a forwarded loopback address accepted by proxy projection
- **THEN** every internal drain route returns HTTP 403
- **AND** process drain state remains unchanged

#### Scenario: Direct local operator drains and resumes

- **WHEN** a direct loopback client uses a local Host without forwarded client hints
- **THEN** it can start, inspect, and stop a reversible drain
