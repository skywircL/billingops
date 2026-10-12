"""Explicit provider capabilities for the pinned Chat Completions configuration.

DeepSeek thinking stays at its provider default (enabled). Its documented
tool-choice restriction is selected before auditing or sending a request;
we never swallow a 400, silently disable thinking, or claim API enforcement.
"""
from __future__ import annotations

from typing import Any, Literal


CAPABILITY_VERSION = 'deepseek-chat-default-thinking-v1'
CAPABILITY_SOURCE = 'https://api-docs.deepseek.com/api/create-chat-completion/'
_DEFAULT_THINKING_MODELS = frozenset({'deepseek-flash', 'deepseek-v4-pro'})


def provider_capability_profile(model_id: str) -> dict[str, Any]:
    if model_id in _DEFAULT_THINKING_MODELS:
        return {'version': CAPABILITY_VERSION, 'model': model_id,
                'api': 'chat_completions', 'thinking': 'provider_default_enabled',
                'required_tool_choice_supported': False,
                'recovery_tool_choice': 'auto',
                'enforcement': 'local_single_tool_schema_and_bounded_repair',
                'source': CAPABILITY_SOURCE, 'verified_on': '2026-10-09'}
    return {'version': CAPABILITY_VERSION, 'model': model_id,
            'required_tool_choice_supported': None,
            'recovery_tool_choice': 'required',
            'source': 'unprofiled_model_preserve_requested_configuration'}


def compatible_tool_choice(model_id: str, requested: Literal['auto', 'required']) -> Literal['auto', 'required']:
    if requested == 'required' and model_id in _DEFAULT_THINKING_MODELS:
        return 'auto'
    return requested
