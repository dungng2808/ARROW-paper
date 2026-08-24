from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LlmRequest:
    model: str
    messages: list[dict[str, str]]
    temperature: float = 0.0
    api_base: str | None = None
    api_key_env: str | None = None
    num_ctx: int | None = None
    max_tokens: int | None = None
    stream: bool = False
    thinking: dict[str, Any] | None = None
    reasoning_effort: str | None = None


@dataclass
class LlmResponse:
    content: str
    metadata: dict[str, Any]


class LlmClient(Protocol):
    def complete(self, request: LlmRequest) -> LlmResponse:
        ...


class LiteLlmClient:
    def complete(self, request: LlmRequest) -> LlmResponse:
        try:
            from litellm import completion
        except ImportError as exc:
            raise RuntimeError("litellm is required for model calls") from exc

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
        }
        if request.api_base:
            kwargs["api_base"] = request.api_base
        if request.api_key_env:
            api_key = os.environ.get(request.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"API key environment variable '{request.api_key_env}' is not set for model '{request.model}'"
                )
            kwargs["api_key"] = api_key
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.num_ctx is not None:
            kwargs["num_ctx"] = request.num_ctx
        if request.thinking or request.reasoning_effort:
            # DeepSeek V4 exposes reasoning controls through the OpenAI
            # compatible request body's extra_body. Keeping this generic means
            # non-DeepSeek agents can leave both fields unset.
            extra_body: dict[str, Any] = {}
            if request.thinking:
                extra_body["thinking"] = request.thinking
            if request.reasoning_effort:
                extra_body["reasoning_effort"] = request.reasoning_effort
            kwargs["extra_body"] = extra_body
        if request.stream:
            # Keep the HTTP response active while a reasoning model prepares a
            # long answer; Java validation starts only after all chunks arrive.
            kwargs["stream"] = True
            stream = completion(**kwargs)
            content_parts: list[str] = []
            last_chunk: Any = {}
            for chunk in stream:
                last_chunk = chunk
                choices = _get_value(chunk, "choices") or []
                if not choices:
                    continue
                delta = _get_value(choices[0], "delta") or {}
                content = _get_value(delta, "content")
                if content:
                    content_parts.append(str(content))
            return LlmResponse(content="".join(content_parts), metadata=_safe_litellm_metadata(last_chunk))
        result = completion(**kwargs)
        message = result["choices"][0]["message"]
        return LlmResponse(content=message.get("content", ""), metadata=_safe_litellm_metadata(result))


class StaticLlmClient:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        if not self.responses:
            return LlmResponse("", {"static": True})
        return LlmResponse(self.responses.pop(0), {"static": True})


def _safe_litellm_metadata(result: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("id", "created", "model", "object", "system_fingerprint"):
        value = _get_value(result, key)
        if value is not None:
            metadata[key] = value
    usage = _get_value(result, "usage")
    if usage is not None:
        metadata["usage"] = _json_safe(usage)
    choices = _get_value(result, "choices")
    if choices is not None:
        metadata["choices_count"] = len(choices) if hasattr(choices, "__len__") else ""
        try:
            first = choices[0]
            metadata["finish_reason"] = _get_value(first, "finish_reason")
        except Exception:
            pass
    return _json_safe(metadata)


def token_usage_from_metadata(metadata: dict[str, Any]) -> dict[str, int] | None:
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens") or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "calls": 1,
    }


def record_token_usage(bucket: dict[str, dict[str, int]], prompt_name: str, metadata: dict[str, Any]) -> dict[str, int] | None:
    usage = token_usage_from_metadata(metadata)
    if usage is None:
        return None
    current = bucket.setdefault(
        prompt_name,
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0},
    )
    for key in ("input_tokens", "output_tokens", "total_tokens", "calls"):
        current[key] = int(current.get(key, 0)) + usage[key]
    return usage


def token_usage_report(bucket: dict[str, dict[str, int]]) -> dict[str, Any]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
    normalized: dict[str, dict[str, int]] = {}
    for prompt_name, usage in bucket.items():
        normalized[prompt_name] = {}
        for key in totals:
            value = int(usage.get(key, 0))
            normalized[prompt_name][key] = value
            totals[key] += value
    return {
        "llm_input_tokens": totals["input_tokens"],
        "llm_output_tokens": totals["output_tokens"],
        "llm_total_tokens": totals["total_tokens"],
        "llm_call_count": totals["calls"],
        "token_usage_by_prompt": normalized,
    }


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if value in {None, ""}:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _get_value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "dict"):
        return _json_safe(value.dict())
    return str(value)
