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
EFFORT = "high"  # thinking is adaptive (not set: the default of the offered models)



@dataclass(frozen=True)
class Model:
    label: str
    note: str
    input_usd: float  # per million tokens (photos count as input)
    output_usd: float  # per million tokens (thinking is billed as output)


# The models a person may choose for an analysis: each takes this client's request (effort,
# structured outputs, server-side fallback). Prices as of 2026-09: keep them in step with
# Anthropic's. A model answering that is missing here (a fallback) has no cost.
MODELS = {
    "claude-opus-5-5": Model("Claude Opus 5.5", "Anthropic's current Opus model", 4.0, 20.0),
    "claude-sonnet-5-5": Model("Claude Sonnet 5.5", "Faster, half the price of Opus 5.5", 2.0,
                               10.0),
    "claude-fable-5-1": Model("Claude Fable 5.1",
                              "Anthropic's most capable model, 2.5 times the price of Opus 5.5",
                              10.0, 50.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = MODELS.get(model)
    if price is None:
        return None
    return round((input_tokens * price.input_usd + output_tokens * price.output_usd)
                 / 1_000_000, 6)


@dataclass(frozen=True)
class Reply:
    data: Any  # the parsed JSON answer
    raw: str  # the answer as returned
    request_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)  # model, tokens in and out, cost_usd
    request: dict[str, Any] = field(default_factory=dict)  # what was sent, photos by size


class AIError(Exception):
    """The provider gave no usable answer; `raw` keeps whatever it returned, for debugging,
    `usage` what an answer that came back but could not be used cost, and `request` what was
    sent."""

    def __init__(self, message: str, raw: str | None = None, request_id: str | None = None,
                 usage: dict[str, Any] | None = None, request: dict[str, Any] | None = None):
        super().__init__(message)
        self.raw, self.request_id, self.usage, self.request = raw, request_id, usage, request


# A request's content, in order: text, or a JPEG photo as bytes.
Part = str | bytes
Client = Callable[[Settings, str, list[Part], dict[str, Any]], Reply]


def ask_anthropic(settings: Settings, system: str, parts: list[Part],
                  schema: dict[str, Any]) -> Reply:
    params = {
        "model": settings.ai_model, "max_tokens": MAX_TOKENS, "system": system,
        "output_config": {"effort": EFFORT,
                          "format": {"type": "json_schema",
                                     "schema": anthropic.transform_schema(schema)}},
        # A request a safety classifier declines is re-run on the model Anthropic recommends
        # for that case instead of failing.
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default",
    }
    content = [
        {"type": "text", "text": p} if isinstance(p, str) else
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(p).decode()}}
        for p in parts
    ]
    sent = {**params, "timeout_seconds": TIMEOUT, "messages": [{"role": "user", "content": [
        {"type": "text", "text": p} if isinstance(p, str) else
        {"type": "image", "media_type": "image/jpeg", "bytes": len(p)} for p in parts]}]}
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, timeout=TIMEOUT)
        with client.beta.messages.stream(
            **params, messages=[{"role": "user", "content": content}],
        ) as stream:
            message = stream.get_final_message()
            request_id = stream.request_id  # a streamed message does not carry it
    except anthropic.APIStatusError as exc:
        raise AIError(f"the AI provider rejected the request ({exc.status_code}): {exc.message}",
                      request_id=exc.request_id, request=sent) from exc
    except anthropic.AnthropicError as exc:  # timeout, connection, missing API key
        raise AIError(f"the AI request failed: {type(exc).__name__}: {exc}",
                      request=sent) from exc
    tokens_in, tokens_out = message.usage.input_tokens, message.usage.output_tokens
    # ponytail: priced at the answering model's rate; after a server-side fallback the declined
    # attempt (usage.iterations) is not counted. Sum the iterations if fallbacks become common.
    usage = {"model": message.model, "input_tokens": tokens_in, "output_tokens": tokens_out,
             "cost_usd": cost_usd(message.model, tokens_in, tokens_out)}
    raw = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        raise AIError(f"the model declined the request ({category or 'no category'})",
                      raw, request_id, usage, sent)
    if message.stop_reason == "max_tokens":
        raise AIError("the answer was cut off at the token limit", raw, request_id, usage, sent)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIError(f"the answer is not valid JSON: {exc}", raw, request_id, usage,
                      sent) from exc
    return Reply(data, raw, request_id, usage, sent)


CLIENTS: dict[str, Client] = {"anthropic": ask_anthropic}


def ask(settings: Settings, system: str, parts: list[Part], schema: dict[str, Any]) -> Reply:
    client = CLIENTS.get(settings.ai_provider)
    if client is None:
        raise AIError(f"unknown AI provider {settings.ai_provider!r} "
                      f"(known: {', '.join(sorted(CLIENTS))})")
    return client(settings, system, parts, schema)
