## Backport baseline

The coordinating agent ran the fresh replacement-socket cases against unchanged
release `v1.25.0-beta.9` runtime code. Both HTTP routes failed because the
dispatched payload unexpectedly retained `previous_response_id`, at the
regression assertion in `test_http_bridge_preserves_agent_message_full_resend`.

Result: 2 failed, 196 deselected, 1 warning in 1.61 seconds.
Local raw log: `/private/tmp/codex-lb-validation/baseline.log`.

## Candidate validation

The coordinating agent validated the candidate locally on 2026-10-07. The
combined four-suite run completed with **459 passed and one 45-second timeout**
in 175.35 seconds. All 18 new route cases passed. The existing
`test_v1_responses_http_bridge_idle_recovery_hands_reader_to_replacement` was the
only timeout. Re-running that case unchanged with a 90-second cutoff passed in
60.47 seconds. The independent reviewer confirmed its owner-side watchdog uses
the existing 60-second deadline in
`app/modules/proxy/_service/http_bridge/helpers.py`; the 45-second test cutoff
expired before that intended recovery. The isolated pass resolves this cutoff
failure; it is not represented as a clean single-run pass of the combined suite.

The new matrix exercises both public HTTP routes, replacement sockets,
explicit anchor rejection, successful subsequent turns, unavailable owners,
quarantine, malformed messages, incomplete history, missing operation fences,
and rejection after visible output. Helper cases cover plaintext and encrypted
agent messages, malformed shapes, parallel-call completeness, account-neutral
rejection, and non-mutation. Original opaque input remains on its proven owner.

Ruff checks, Ruff format checks, and targeted `ty` checks passed for all four
edited Python files. Strict validation passed for the active change and all
**65** main specifications. Independent static review reported no blockers.
The normative requirement and rationale were synchronized to the main
`responses-api-compat` specification before validation and archival.

## Exact local invocations

All commands ran from `/private/tmp/codex-lb-fix` through
`python3 /private/tmp/codex-lb-validation/run_bounded.py <overall-seconds> ...`.
The supervisor bounded process lifetime and thread counts, disabled automatic
pytest plugin loading, removed inherited `CODEX_LB_*` variables, and used an
isolated test data directory and encryption key. No live database or live account
credentials were used.

The baseline invocation (overall cap 150 seconds) stopped after the first two
fresh-route failures:

```sh
.venv/bin/python -m pytest -p pytest_asyncio.plugin -p pytest_timeout -p anyio.pytest_plugin tests/integration/test_http_responses_bridge.py -k preserves_agent_message_full_resend --maxfail=2 --timeout=30 --tb=short --show-capture=no -q
```

The candidate invocation (overall cap 240 seconds) was:

```sh
.venv/bin/python -m pytest -p pytest_asyncio.plugin -p pytest_timeout -p anyio.pytest_plugin tests/unit/test_replay_safety.py tests/unit/test_http_bridge_safe_continuity.py tests/unit/test_http_bridge_replay_rejection.py tests/integration/test_http_responses_bridge.py --timeout=45 --tb=short --show-capture=no -q
```

The unchanged existing-test rerun (overall cap 115 seconds) was:

```sh
.venv/bin/python -m pytest -p pytest_asyncio.plugin -p pytest_timeout -p anyio.pytest_plugin tests/integration/test_http_responses_bridge.py::test_v1_responses_http_bridge_idle_recovery_hands_reader_to_replacement --timeout=90 --tb=short --show-capture=no -q
```

Static checks used overall caps of 60, 45, and 90 seconds respectively:

```sh
.venv/bin/ruff check app/modules/proxy/replay_safety.py app/modules/proxy/_service/http_bridge/streaming.py tests/integration/test_http_responses_bridge.py tests/unit/test_replay_safety.py
.venv/bin/ruff format --check app/modules/proxy/replay_safety.py app/modules/proxy/_service/http_bridge/streaming.py tests/integration/test_http_responses_bridge.py tests/unit/test_replay_safety.py
.venv/bin/ty check app/modules/proxy/replay_safety.py app/modules/proxy/_service/http_bridge/streaming.py tests/integration/test_http_responses_bridge.py tests/unit/test_replay_safety.py
```

OpenSpec checks used overall caps of 90 and 60 seconds respectively:

```sh
npx --yes @fission-ai/openspec@1.11.0 validate --type change --strict --no-interactive -- fix-bridge-agent-message-continuity
env OPENSPEC_TELEMETRY=0 npx --yes @fission-ai/openspec@1.11.0 validate --specs --strict
```

Raw local logs are in `/private/tmp/codex-lb-validation/`: `baseline.log`,
`patched.log`, `idle-recovery-recheck.log`, `typecheck.log`, `spec-change.log`,
and `specs.log`. Pytest reported Starlette's existing AnyIO deprecation warning.

## Proof limits

These tests use synthetic accounts, temporary databases, and stubbed upstream
sockets. They prove local recovery and ownership behavior for the exercised
shapes; they do not prove live upstream acceptance or completion of the user's
affected thread. No real model generation, deployment, settings change, database
migration, or authentication change is included. The full repository test
matrix and cloud CI were not run. The separate native-stream terminal-delivery
and SQLite contention findings are outside this patch.

This is a local backport of the attributed upstream commit, not a claim that
upstream's archived validation was reproduced wholesale. The implementation
worker ran no tests or dependency installation; the coordinating agent supplied
the supervised validation evidence above.
