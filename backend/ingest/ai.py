"""AI model clients: one request of text and JPEG photos in, JSON matching a schema out.

vehicle_analysis.py only calls `ask`, so another provider is one function that turns the same
arguments into a `Reply`, registered in `CLIENTS` and chosen with `AI_PROVIDER`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .config import Settings

TIMEOUT = 300.0  # seconds per attempt; photos plus a long structured answer take a while
MAX_TOKENS = 32000  # thinking and the answer; streamed, so no HTTP timeout at this size


@dataclass(frozen=True)
class Reply:
    data: Any  # the parsed JSON answer
    raw: str  # the answer as returned
    request_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)


class AIError(Exception):
    """The provider gave no usable answer; `raw` keeps whatever it returned, for debugging."""

    def __init__(self, message: str, raw: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.raw, self.request_id = raw, request_id


# A request's content, in order: text, or a JPEG photo as bytes.
Part = str | bytes
Client = Callable[[Settings, str, list[Part], dict[str, Any]], Reply]


def ask_anthropic(settings: Settings, system: str, parts: list[Part],
                  schema: dict[str, Any]) -> Reply:
    content = [
        {"type": "text", "text": p} if isinstance(p, str) else
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(p).decode()}}
        for p in parts
    ]
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, timeout=TIMEOUT)
        with client.beta.messages.stream(
            model=settings.ai_model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "high",
                           "format": {"type": "json_schema",
                                      "schema": anthropic.transform_schema(schema)}},
            # A request a safety classifier declines is re-run on the model Anthropic
            # recommends for that case instead of failing.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        raise AIError(f"the AI provider rejected the request ({exc.status_code}): {exc.message}",
                      request_id=exc.request_id) from exc
    except anthropic.AnthropicError as exc:  # timeout, connection, missing API key
        raise AIError(f"the AI request failed: {type(exc).__name__}: {exc}") from exc
    request_id = message._request_id
    raw = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        raise AIError(f"the model declined the request ({category or 'no category'})",
                      raw, request_id)
    if message.stop_reason == "max_tokens":
        raise AIError("the answer was cut off at the token limit", raw, request_id)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIError(f"the answer is not valid JSON: {exc}", raw, request_id) from exc
    usage = {"model": message.model, "input_tokens": message.usage.input_tokens,
             "output_tokens": message.usage.output_tokens}
    return Reply(data, raw, request_id, usage)


CLIENTS: dict[str, Client] = {"anthropic": ask_anthropic}


def ask(settings: Settings, system: str, parts: list[Part], schema: dict[str, Any]) -> Reply:
    client = CLIENTS.get(settings.ai_provider)
    if client is None:
        raise AIError(f"unknown AI provider {settings.ai_provider!r} "
                      f"(known: {', '.join(sorted(CLIENTS))})")
    return client(settings, system, parts, schema)
