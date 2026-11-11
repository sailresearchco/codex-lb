## Record provenance

This change record was prepared during review after the implementation in [PR #23](https://github.com/sailresearchco/codex-lb/pull/23), at commit `1870ab923ef2da14efc0d81665fdf12854c7ec18`. It does not assert that these artifacts preceded implementation. Its delta requirements reproduce the owning requirement sections at that implementation head.

## Example and constraints

An external raw peer that supplies a forwarded loopback address cannot authenticate through an identity header or start a drain. A trusted raw identity proxy forwarding an external client retains its identity authority; a direct loopback operator with a local Host can still drain and resume.

## Existing validation evidence

[Implementation-head CI run 43122401111](https://github.com/sailresearchco/codex-lb/actions/runs/43122401111) completed successfully for `1870ab923ef2da14efc0d81665fdf12854c7ec18`; its `CI Required` check passed. This is evidence for the existing implementation, not a claim that the newly added OpenSpec artifacts have already been validated.

Relevant regression coverage in that head:

- `tests/integration/test_auth_socket_provenance.py`: projected clients, trusted identity proxies, forwarded-header families, missing capture, public Host rejection, and direct IPv4/IPv6 drain access.
- `tests/unit/test_health_probes.py`: drain deadlines, cancellation, reversible drain, and committed-shutdown behavior with captured local provenance.

Strict validation of this added change is tracked separately in `tasks.md`. No tests were rerun while preparing this retrospective record.

