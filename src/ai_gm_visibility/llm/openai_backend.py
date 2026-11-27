"""Stateless, text-only Responses API and development cost accounting."""

import json
import os
from decimal import Decimal
from importlib.metadata import version

from ..schemas import require

MODEL = "gpt-6-luna"
BASE_URL = "https://api.openai.com/v1"
PRICING = {
    "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "checked_on": "2026-11-25",
    "tier": "standard_short_context",
    "input": "0.10",
    "cached_input": "0.01",
    "cache_write_12h": "0.25",
    "output": "0.50",
}
INPUT_PADDING_TOKENS = 1024
SHORT_CONTEXT_LIMIT = 272_000
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cache_write_tokens")


def response_parameters(settings):
    return {
        "reasoning": {"effort": settings["reasoning_effort"]},
        "max_output_tokens": settings["max_output_tokens"],
        "store": settings["store"],
        "service_tier": "default",
        "prompt_cache_options": {"mode": "explicit"},
        "truncation": "disabled",
    }


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _cost(input_tokens, output_tokens, cached_tokens=0, cache_write_tokens=0):
    return (
        (input_tokens - cached_tokens - cache_write_tokens) * Decimal(PRICING["input"])
        + cached_tokens * Decimal(PRICING["cached_input"])
        + cache_write_tokens * Decimal(PRICING["cache_write_12h"])
        + output_tokens * Decimal(PRICING["output"])
    ) / 1_000_000


class OpenAIBudget:
    def __init__(self, limit, max_output_tokens):
        self.limit = Decimal(str(limit))
        self.max_output_tokens = max_output_tokens
        self.estimated = Decimal(0)
        self.reserved = Decimal(0)
        self.unknown_attempts = 0
        self.tokens = dict.fromkeys(TOKEN_FIELDS, 0)

    def quote(self, messages):
        # Text-only input. Count each serialized UTF-8 byte as a token and pad framing.
        input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + INPUT_PADDING_TOKENS
        if input_bound <= SHORT_CONTEXT_LIMIT:
            return _cost(input_bound, self.max_output_tokens)
        return None

    def account(self, record, quote):
        input_tokens = record["input_tokens"]
        output_tokens = record["output_tokens"]
        cached = record["cached_tokens"] or 0
        writes = record["cache_write_tokens"] or 0
        observed = record["provider_parameters"]
        cost = None
        if (
            input_tokens is not None and output_tokens is not None
            and cached + writes <= input_tokens <= SHORT_CONTEXT_LIMIT
            and observed.get("service_tier") in {None, "default"}
        ):
            cost = _cost(input_tokens, output_tokens, cached, writes)
        for field in TOKEN_FIELDS:
            self.tokens[field] += record[field] or 0
        if cost is None:
            self.unknown_attempts += 1
            self.reserved += quote
        else:
            self.estimated += cost
        record.update(
            estimated_cost=float(cost) if cost is not None else None,
            preflight_estimate_usd=float(quote),
            cumulative_estimated_cost_usd=float(self.estimated),
            cumulative_reserved_cost_usd=float(self.reserved),
        )
        if cost is not None and cost > quote:
            return "estimate_exceeded"
        if (
            (input_tokens is not None and input_tokens > SHORT_CONTEXT_LIMIT)
            or (output_tokens is not None and output_tokens > self.max_output_tokens)
            or observed.get("service_tier") not in {None, "default"}
            or cached or writes
        ):
            return "unexpected_billing_or_usage"
        if cost is None and record["status"] == "ok":
            record.update(status="error", error_type="missing_or_invalid_usage")
        return None

    def summary(self):
        return {
            "budget_usd": float(self.limit),
            "estimated_cost_usd": float(self.estimated),
            "reserved_cost_usd": float(self.reserved),
            "attempts_with_unknown_cost": self.unknown_attempts,
            "observed_tokens": self.tokens,
        }


class OpenAIModel:
    model_id = MODEL

    def __init__(self, settings, client=None):
        from openai import DefaultHttpxClient, OpenAI

        self.sdk_version = version("openai")
        self.parameters = response_parameters(settings)
        self.client_options = {"timeout": settings["timeout_seconds"], "max_retries": settings["max_retries"]}
        self._owns_client = client is None
        if self._owns_client:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            require(bool(key), "Set OPENAI_API_KEY in the environment before running OpenAI")
            client = OpenAI(api_key=key, base_url=BASE_URL, http_client=DefaultHttpxClient(follow_redirects=False),
                            **self.client_options)
        self.client = client

    def close(self):
        if self._owns_client:
            self.client.close()

    def generate(self, request):
        return self.generate_payload({"model": self.model_id, "input": request["messages"], **self.parameters})

    def generate_payload(self, payload):
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        try:
            response = self.client.responses.create(**payload)
        except APITimeoutError:
            return {"error_type": "api_timeout"}
        except APIConnectionError:
            return {"error_type": "api_connection_error"}
        except APIStatusError as exc:
            return {
                "error_type": "api_http_error",
                "http_status": exc.status_code,
                "request_id": exc.request_id,
                "stop_reason": "api_http_error" if exc.status_code in {400, 401, 403, 404, 422, 429} else None,
            }

        body = response.model_dump(mode="json", warnings=False)
        usage = _object(body.get("usage"))
        input_details = _object(usage.get("input_tokens_details"))
        output_details = _object(usage.get("output_tokens_details"))
        tokens = {
            "input_tokens": _count(usage.get("input_tokens")),
            "output_tokens": _count(usage.get("output_tokens")),
            "cached_tokens": _count(input_details.get("cached_tokens")),
            "reasoning_tokens": _count(output_details.get("reasoning_tokens")),
            "cache_write_tokens": _count(input_details.get("cache_write_tokens")),
        }
        answer_parts = []
        output = body.get("output")
        malformed = not isinstance(output, list)
        for item in output if isinstance(output, list) else ():
            if isinstance(item, dict) and item.get("type") == "reasoning":
                continue
            if not (
                isinstance(item, dict) and item.get("type") == "message"
                and item.get("role") == "assistant" and isinstance(item.get("content"), list)
            ):
                malformed = True
                continue
            for part in item["content"]:
                part = _object(part)
                part_type = part.get("type")
                field = "text" if part_type == "output_text" else "refusal" if part_type == "refusal" else None
                if field and isinstance(part.get(field), str):
                    answer_parts.append({"type": part_type, "text": part[field]})
                else:
                    malformed = True

        status = body.get("status")
        model = body.get("model")
        text = "".join(p["text"] for p in answer_parts)
        if status == "incomplete":
            error = "incomplete_response"
        elif status in ("failed", "cancelled", "queued", "in_progress"):
            error = "response_" + status
        elif status != "completed" or malformed or not isinstance(model, str) or not model:
            error = "malformed_response"
        elif any(p["type"] == "refusal" for p in answer_parts):
            error = "model_refusal"
        else:
            error = None if text.strip() else "empty_response"

        return {
            "response_text": text, "returned_model": model, "usage": tokens,
            "response_id": body.get("id"), "request_id": response._request_id,
            "finish_reason": "stop" if error is None else None,
            "provider_status": status, "answer_parts": answer_parts,
            "incomplete_details": body.get("incomplete_details"),
            "provider_error_code": _object(body.get("error")).get("code"),
            "provider_parameters": {
                key: body[key]
                for key in (*self.parameters, "temperature", "top_p", "text")
                if body.get(key) is not None
            },
            "error_type": error, "synthetic": False,
        }
