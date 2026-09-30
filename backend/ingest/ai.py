"""AI model clients: one request of text and JPEG photos in, JSON matching a schema out.

vehicle_analysis.py only calls `ask`, so another provider is one function that turns the same
arguments into a `Reply`, registered in `CLIENTS`, plus one listing its models for the lot
page's "add a model" form, in `MODEL_LISTS`. Built-in models run on `AI_PROVIDER`; a model a
person added (`ai_models`) on its own provider.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai

from .config import Settings

TIMEOUT = 300.0  # seconds per attempt; photos plus a long structured answer take a while
MAX_TOKENS = 32000  # thinking and the answer; streamed, so no HTTP timeout at this size
EFFORT = "high"  # thinking is adaptive (not set: the default of the offered models)
LIST_TIMEOUT = 15.0  # listing a provider's models, while a person waits in the dialog


@dataclass(frozen=True)
class Model:
    label: str
    note: str
    input_usd: float  # per million tokens (photos count as input)
    output_usd: float  # per million tokens (thinking is billed as output)
    provider: str = "anthropic"  # a key of CLIENTS


# The built-in models a person may choose for an analysis, besides the ones people add
# (`ai_models`): each takes this client's request (effort, structured outputs, server-side
# fallback). Prices as of 2026-09: keep them in step with Anthropic's.
MODELS = {
    "claude-opus-5-5": Model("Claude Opus 5.5", "Anthropic's current Opus model", 4.0, 20.0),
    "claude-sonnet-5-5": Model("Claude Sonnet 5.5", "Faster, half the price of Opus 5.5", 2.0,
                               10.0),
    "claude-fable-5-1": Model("Claude Fable 5.1",
                              "Anthropic's most capable model, 2.5 times the price of Opus 5.5",
                              10.0, 50.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int,
             asked: Model | None = None) -> float | None:
    """Priced at the answering model's rate when it is built in (a server-side fallback
    answers with one), else at `asked`'s, the added model the request named: providers answer
    with a dated snapshot of the id they were asked for."""
    price = MODELS.get(model) or asked
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
    usage = {"model": message.model, "input_tokens": tokens_in, "output_tokens": tokens_out}
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


def ask_openai(settings: Settings, system: str, parts: list[Part],
               schema: dict[str, Any]) -> Reply:
    # The schemas are sent as is: they already are what strict mode needs (closed objects,
    # every property required; a test keeps them so).
    params = {
        "model": settings.ai_model, "max_output_tokens": MAX_TOKENS, "instructions": system,
        "reasoning": {"effort": EFFORT},
        "text": {"format": {"type": "json_schema", "name": schema.get("title", "answer"),
                            "schema": schema, "strict": True}},
    }
    content = [
        {"type": "input_text", "text": p} if isinstance(p, str) else
        {"type": "input_image", "detail": "high",
         "image_url": "data:image/jpeg;base64," + base64.standard_b64encode(p).decode()}
        for p in parts
    ]
    sent = {**params, "timeout_seconds": TIMEOUT, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": p} if isinstance(p, str) else
        {"type": "input_image", "media_type": "image/jpeg", "bytes": len(p), "detail": "high"}
        for p in parts]}]}
    try:
        client = openai.OpenAI(api_key=settings.openai_api_key or None, timeout=TIMEOUT)
        response = client.responses.create(**params,
                                           input=[{"role": "user", "content": content}])
    except openai.APIStatusError as exc:
        raise AIError(f"the AI provider rejected the request ({exc.status_code}): {exc.message}",
                      request_id=exc.request_id, request=sent) from exc
    except openai.OpenAIError as exc:  # timeout, connection, missing API key
        raise AIError(f"the AI request failed: {type(exc).__name__}: {exc}",
                      request=sent) from exc
    request_id = response._request_id
    usage = {"model": response.model, "input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    raw = response.output_text
    refusal = next((c.refusal for item in response.output if item.type == "message"
                    for c in item.content if c.type == "refusal"), None)
    if refusal is not None:
        raise AIError(f"the model declined the request ({refusal})", raw, request_id, usage,
                      sent)
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        raise AIError("the answer was cut off at the token limit"
                      if reason == "max_output_tokens" else
                      f"the answer is incomplete ({reason})", raw, request_id, usage, sent)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIError(f"the answer is not valid JSON: {exc}", raw, request_id, usage,
                      sent) from exc
    return Reply(data, raw, request_id, usage, sent)


CLIENTS: dict[str, Client] = {"anthropic": ask_anthropic, "openai": ask_openai}


def ask(settings: Settings, system: str, parts: list[Part], schema: dict[str, Any],
        asked: Model | None = None) -> Reply:
    """The answer of `settings.ai_model` on `settings.ai_provider`; `asked` is the added
    model it is, if any (for its price)."""
    client = CLIENTS.get(settings.ai_provider)
    if client is None:
        raise AIError(f"unknown AI provider {settings.ai_provider!r} "
                      f"(known: {', '.join(sorted(CLIENTS))})")
    try:
        reply = client(settings, system, parts, schema)
    except AIError as exc:
        _price(exc.usage, asked)
        raise
    _price(reply.usage, asked)
    return reply


def _price(usage: dict[str, Any] | None, asked: Model | None) -> None:
    # ponytail: priced at the answering model's rate; after a server-side fallback the declined
    # attempt (usage.iterations) is not counted. Sum the iterations if fallbacks become common.
    if usage:
        usage["cost_usd"] = cost_usd(usage["model"], usage["input_tokens"],
                                     usage["output_tokens"], asked)


# --- the providers' models, offered when a person adds one ------------------------------------

def _key(value: str, variable: str) -> str:
    if not value:
        raise AIError(f"set {variable} in .env to list the provider's models")
    return value


def list_anthropic(settings: Settings) -> list[dict[str, str]]:
    """Anthropic's models that take this client's request: photos, structured outputs and
    high effort (a model whose capabilities are not reported is offered)."""
    key = _key(settings.anthropic_api_key, "ANTHROPIC_API_KEY")
    try:
        client = anthropic.Anthropic(api_key=key, timeout=LIST_TIMEOUT)
        models = list(client.models.list(limit=1000))  # newest first
    except anthropic.AnthropicError as exc:
        raise AIError(f"{type(exc).__name__}: {exc}") from exc
    return [{"id": m.id, "label": m.display_name} for m in models
            if m.capabilities is None or (m.capabilities.image_input.supported
                                          and m.capabilities.structured_outputs.supported
                                          and m.capabilities.effort.high.supported)]


# ponytail: OpenAI's list says nothing about what a model accepts, so its names are the guess:
# reasoning models (GPT-5 and later, o3 and later: they take `reasoning.effort`) that are not
# audio, image, search or chat variants. Any other id can still be typed in.
OPENAI_MODEL = re.compile(r"^(gpt-([5-9]|[1-9]\d)|o[3-9])(?!.*(audio|realtime|transcribe|tts|"
                          r"image|search|research|chat))")


def list_openai(settings: Settings) -> list[dict[str, str]]:
    key = _key(settings.openai_api_key, "OPENAI_API_KEY")
    try:
        models = list(openai.OpenAI(api_key=key, timeout=LIST_TIMEOUT).models.list())
    except openai.OpenAIError as exc:
        raise AIError(f"{type(exc).__name__}: {exc}") from exc
    return [{"id": m.id, "label": m.id}
            for m in sorted(models, key=lambda m: m.created, reverse=True)
            if OPENAI_MODEL.match(m.id)]


MODEL_LISTS: dict[str, Callable[[Settings], list[dict[str, str]]]] = {
    "anthropic": list_anthropic, "openai": list_openai}
