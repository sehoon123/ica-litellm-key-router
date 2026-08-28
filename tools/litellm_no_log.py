"""Force ICA's top-level ``no-log`` request-body policy in LiteLLM 1.98.0.

LiteLLM merges ``extra_body`` into Azure Responses and Gemini requests, but its
native Anthropic Messages path allowlists request fields. The small pinned-version
adapter below promotes only this policy field for that path. The callback reapplies
the value after deployment selection so a local client cannot override it.
"""
from __future__ import annotations

from importlib.metadata import version
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.anthropic.experimental_pass_through.messages.utils import (
    AnthropicMessagesRequestUtils,
)

_NO_LOG_FIELD = "no-log"
_PATCH_MARKER = "_ica_no_log_adapter_installed"
_PINNED_LITELLM_VERSION = "1.98.0"

if version("litellm") != _PINNED_LITELLM_VERSION:
    raise RuntimeError(
        f"ICA no-log adapter requires LiteLLM {_PINNED_LITELLM_VERSION}"
    )


def _force_extra_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    extra_body = kwargs.get("extra_body")
    if extra_body is None:
        merged: dict[str, Any] = {}
    elif isinstance(extra_body, dict):
        merged = dict(extra_body)
    else:
        raise TypeError("LiteLLM extra_body must be a JSON object")
    merged[_NO_LOG_FIELD] = True
    kwargs["extra_body"] = merged
    return kwargs


def _install_anthropic_messages_adapter() -> None:
    current = AnthropicMessagesRequestUtils.get_requested_anthropic_messages_optional_param
    if getattr(current, _PATCH_MARKER, False):
        return

    def with_no_log(params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        optional_params = dict(current(params, **kwargs))
        extra_body = params.get("extra_body")
        if isinstance(extra_body, dict) and extra_body.get(_NO_LOG_FIELD) is True:
            optional_params[_NO_LOG_FIELD] = True
        return optional_params

    setattr(with_no_log, _PATCH_MARKER, True)
    AnthropicMessagesRequestUtils.get_requested_anthropic_messages_optional_param = (
        staticmethod(with_no_log)
    )


class IcaNoLogCallback(CustomLogger):
    """Enforce ``{"no-log": true}`` on the proxy's async provider calls.

    LiteLLM Proxy's native routes use async entrypoints. Sync LiteLLM SDK calls
    are outside this local proxy's data path and do not run the deployment hook.
    """

    def __init__(self) -> None:
        _install_anthropic_messages_adapter()

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any
    ) -> dict[str, Any]:
        return _force_extra_body(kwargs)

    async def async_pre_request_hook(
        self, model: str, messages: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # LiteLLM's native Anthropic Messages path invokes this provider-level
        # hook after routing and before its request-field allowlist.
        return _force_extra_body(kwargs)


no_log_callback = IcaNoLogCallback()
