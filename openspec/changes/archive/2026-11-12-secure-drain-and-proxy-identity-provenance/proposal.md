## Why

Proxy-header projection can replace the request's apparent client address. Trusting that projected address for identity headers or internal drain access can grant an untrusted peer authority or remove a trusted proxy's authority.

## What Changes

- Use captured raw socket provenance for trusted-header identity admission and identity-header sanitization.
- Require captured loopback provenance, a local Host, and no forwarded client-IP hints for internal drain start, stop, and status.
- Preserve direct IPv4/IPv6 loopback operator access and the existing dashboard bootstrap locality policy.

## Impact

Capabilities: `admin-auth`, `graceful-shutdown`.

Implementation: dashboard identity resolution and proxy-header middleware, shared request-locality checks, and internal health/drain routes. No new configuration or schema.

This record completes the missing OpenSpec artifacts for [PR #23](https://github.com/sailresearchco/codex-lb/pull/23). It documents already implemented behavior and introduces no additional runtime change.

