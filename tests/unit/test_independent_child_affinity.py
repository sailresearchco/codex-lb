from __future__ import annotations

import pytest

from app.core.openai.requests import ResponsesRequest
from app.db.models import StickySessionKind
from app.modules.proxy.affinity import (
    _codex_backend_identity,
    _codex_session_selection_key,
    _sticky_key_for_responses_request,
)


def _policy(payload_overrides=None, header_overrides=None):
    headers = {
        "session-id": "shared-process",
        "thread-id": "child-thread",
        "x-openai-subagent": "collab_spawn",
        "x-codex-parent-thread-id": "parent-thread",
        **(header_overrides or {}),
    }
    payload = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.1",
            "instructions": "Complete the assigned task.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Independent task"}]}],
            "prompt_cache_key": "shared-process",
            **(payload_overrides or {}),
        }
    )
    policy = _sticky_key_for_responses_request(
        payload,
        headers,
        codex_session_affinity=True,
        openai_cache_affinity=True,
        openai_cache_affinity_max_age_seconds=300,
        sticky_threads_enabled=True,
    )
    return policy, payload, headers


@pytest.mark.parametrize("envelope", [{}, {"type": "response.create"}])
def test_independent_child_omits_only_process_seed(envelope):
    policy, payload, headers = _policy(envelope)

    assert policy.key == _codex_backend_identity(headers).thread_selection_key
    assert policy.kind == StickySessionKind.PROMPT_CACHE
    assert policy.legacy_selection_key == "shared-process"
    assert policy.seed_selection_key is None
    assert policy.seed_selection_kind is None
    assert payload.prompt_cache_key == "shared-process"
    assert policy.selection_kwargs()["legacy_sticky_key"] == "shared-process"


def test_independent_child_accepts_body_metadata_with_body_precedence():
    policy, _, _ = _policy(
        {
            "client_metadata": {
                "x-openai-subagent": "collab_spawn",
                "x-codex-parent-thread-id": "parent-thread",
            }
        },
        {"x-openai-subagent": "review", "x-codex-parent-thread-id": "child-thread"},
    )
    assert policy.seed_selection_key is None


@pytest.mark.parametrize(
    "payload_overrides",
    [
        {"previous_response_id": "resp_owner"},
        {"conversation": "conv_owner"},
        {"input": [{"type": "input_file", "file_id": "file_owner"}]},
        {"input": [{"type": "agent_message", "text": "Opaque inherited state"}]},
        {"input": [{"type": "reasoning", "encrypted_content": "owner-bound"}]},
        {"unknown_account_state": "opaque"},
        {"type": "unknown.envelope"},
        {"client_metadata": {"x-openai-subagent": "review"}},
        {"client_metadata": {"x-openai-subagent": None}},
        {"client_metadata": {"x-codex-parent-thread-id": "child-thread"}},
    ],
)
def test_child_marker_does_not_make_nonportable_payload_independent(payload_overrides):
    policy, _, _ = _policy(payload_overrides)
    assert policy.seed_selection_key == _codex_session_selection_key("shared-process")
    assert policy.seed_selection_kind == StickySessionKind.CODEX_SESSION


@pytest.mark.parametrize(
    "header_overrides",
    [
        {"x-openai-subagent": ""},
        {"x-openai-subagent": "review"},
        {"x-codex-parent-thread-id": ""},
        {"x-codex-parent-thread-id": "child-thread"},
    ],
)
def test_non_child_thread_keeps_process_preference(header_overrides):
    policy, _, _ = _policy(header_overrides=header_overrides)
    assert policy.seed_selection_key == _codex_session_selection_key("shared-process")


def test_child_without_thread_identity_keeps_bare_process_affinity():
    policy, _, _ = _policy(header_overrides={"thread-id": ""})
    assert policy.codex_session_source == "session_header"
    assert policy.selection_key == _codex_session_selection_key("shared-process")


def test_child_exact_turn_state_keeps_hard_affinity():
    policy, _, _ = _policy(header_overrides={"x-codex-turn-state": "owned-turn"})
    assert policy.key == "owned-turn"
    assert policy.kind == StickySessionKind.CODEX_SESSION
    assert policy.codex_session_source == "turn_state"
