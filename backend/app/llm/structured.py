"""Structured output: strict JSON schema, request building, defensive parsing."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from importlib import resources
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.types import ChatMessage, ChatRequest, OutputMode

# Keywords strict mode (and many providers) reject; Pydantic still enforces them.
UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "examples",
    }
)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_LEADING_THINK = re.compile(r"^.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
M = TypeVar("M", bound=BaseModel)


def _prompt(name: str) -> str:
    return resources.files("app.llm.prompts").joinpath(name).read_text(encoding="utf-8")


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema made strict-mode compatible: every property required,
    additionalProperties false, unsupported keywords removed (validated locally)."""
    schema = deepcopy(model.model_json_schema())

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in UNSUPPORTED_SCHEMA_KEYWORDS & node.keys():
                del node[key]
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema)
    return schema


def build_request(
    *,
    model: str,
    messages: list[ChatMessage],
    mode: OutputMode,
    schema: dict[str, Any],
    schema_name: str,
    reasoning_effort: str | None,
    max_tokens: int,
) -> ChatRequest:
    """Same conversation, structure requested the way `mode` allows."""
    extra_body: dict[str, Any] = {}
    if reasoning_effort is not None:
        extra_body["reasoning"] = {"effort": reasoning_effort, "exclude": True}
    if mode is OutputMode.JSON_SCHEMA:
        # require_parameters: never let OpenRouter route to a provider that drops the schema
        extra_body["provider"] = {"require_parameters": True}
        response_format: dict[str, Any] | None = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
        final_messages = messages
    else:
        response_format = {"type": "json_object"} if mode is OutputMode.JSON_OBJECT else None
        final_messages = with_schema_instruction(messages, schema)
    return ChatRequest(
        model=model,
        messages=final_messages,
        max_tokens=max_tokens,
        response_format=response_format,
        extra_body=extra_body,
    )


def with_schema_instruction(messages: list[ChatMessage], schema: dict[str, Any]) -> list[ChatMessage]:
    instruction = _prompt("schema_instruction.md").replace("{schema}", json.dumps(schema))
    if messages and messages[0].role == "system":
        return [ChatMessage("system", f"{messages[0].content}\n\n{instruction}"), *messages[1:]]
    return [ChatMessage("system", instruction), *messages]


def repair_messages(previous: list[ChatMessage], raw_text: str, errors: str) -> list[ChatMessage]:
    repair = _prompt("repair.md").replace("{errors}", errors)
    return [*previous, ChatMessage("assistant", raw_text), ChatMessage("user", repair)]


def clean_model_text(text: str) -> str:
    """Strip reasoning leakage and markdown wrappers some models add anyway."""
    cleaned = _THINK_BLOCK.sub("", text)
    if "</think>" in cleaned.lower():  # opening tag swallowed by the provider
        cleaned = _LEADING_THINK.sub("", cleaned)
    if fence := _FENCE.search(cleaned):
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return cleaned[start : end + 1] if 0 <= start < end else cleaned.strip()


def parse_structured(text: str, model: type[M]) -> M:
    """Raises ValueError (with a readable message) when the text doesn't validate."""
    cleaned = clean_model_text(text)
    try:
        return model.model_validate_json(cleaned)
    except ValidationError as exc:
        raise ValueError(_summarize(exc)) from exc


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()[:20]
    )
