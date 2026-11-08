"""Vision backends: local Ollama (free) or the Claude API.

Both expose the same call shape, hand them images plus a pydantic schema and
get a validated object back, so the pipeline can mix and match: a free local
model for the high-volume gate, a stronger model only for the rare final
verdict.
"""
from __future__ import annotations

import base64
import json
import time
import logging
from pathlib import Path
from typing import Any, Sequence, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from .config import Config, config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class VisionError(RuntimeError):
    pass


class ModelRefusal(VisionError):
    """The model declined to answer (Claude `stop_reason == "refusal"`)."""


def encode(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


# Full-size camera frames cost ~1500 tokens each, and Ollama's default context
# is 4096 tokens, so a handful of frames is rejected with HTTP 400. Downscaling
# keeps the detail the posture check needs at a fraction of the cost.
OLLAMA_MAX_EDGE = 640
OLLAMA_NUM_CTX = 16384


def encode_small(path: Path, max_edge: int = OLLAMA_MAX_EDGE) -> str:
    from io import BytesIO
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


# --------------------------------------------------------------------- ollama
_ollama_cache: dict = {}
_OLLAMA_CACHE_SECONDS = 30


def ollama_available(cfg: Config | None = None) -> tuple[bool, str]:
    """Cached for 30s: the dashboard polls the status endpoint every few seconds,
    and each uncached check can take several seconds on Windows."""
    cfg = cfg or config
    key = cfg.ollama_url
    cached = _ollama_cache.get(key)
    if cached and time.time() - cached[0] < _OLLAMA_CACHE_SECONDS:
        return cached[1]
    result = _check_ollama(cfg)
    _ollama_cache[key] = (time.time(), result)
    return result


def _check_ollama(cfg: Config) -> tuple[bool, str]:
    try:
        r = requests.get(cfg.ollama_url + "/api/version", timeout=4)
        r.raise_for_status()
    except requests.RequestException as e:
        return False, "ollama not reachable at " + cfg.ollama_url + " (" + str(e) + ")"

    try:
        tags = requests.get(cfg.ollama_url + "/api/tags", timeout=8).json()
        names = {m.get("name", "") for m in tags.get("models", [])}
    except (requests.RequestException, ValueError):
        return True, "ollama up, but model list unavailable"

    if cfg.ollama_model in names or any(n.startswith(cfg.ollama_model) for n in names):
        return True, "ollama ready with " + cfg.ollama_model
    return False, (
        "ollama is running but '" + cfg.ollama_model + "' is not pulled. Run: "
        "ollama pull " + cfg.ollama_model
    )


def ask_ollama(images: Sequence[Path], prompt: str, schema: type[T],
               cfg: Config | None = None, timeout: int = 300) -> T:
    """Query a local vision model with JSON-schema-constrained output."""
    cfg = cfg or config
    payload: dict[str, Any] = {
        "model": cfg.ollama_model,
        "prompt": prompt,
        "images": [encode_small(p) for p in images],
        "format": schema.model_json_schema(),
        "stream": False,
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
    }
    try:
        r = requests.post(cfg.ollama_url + "/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except requests.RequestException as e:
        raise VisionError("ollama request failed: " + str(e)) from e
    except ValueError as e:
        raise VisionError("ollama returned non-JSON: " + str(e)) from e

    raw = (body.get("response") or "").strip()
    if not raw:
        raise VisionError("ollama returned an empty response")
    try:
        return schema.model_validate_json(raw)
    except ValidationError as e:
        # Small models occasionally wrap JSON in prose despite `format`.
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return schema.model_validate_json(raw[start:end + 1])
            except ValidationError:
                pass
        raise VisionError(
            "ollama output did not match schema: " + raw[:300]
        ) from e


# --------------------------------------------------------------------- claude
_client = None


def claude_client():
    global _client
    if _client is None:
        import anthropic
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
        # `ant auth login` profile, never hardcode a key.
        _client = anthropic.Anthropic()
    return _client


def ask_claude(images: Sequence[Path], prompt: str, schema: type[T],
               model: str, *, thinking: bool = True,
               effort: str | None = None, max_tokens: int = 8000) -> T:
    """Query Claude with images and a pydantic output schema."""
    import anthropic

    content: list[dict[str, Any]] = []
    for p in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": encode(p),
            },
        })
    content.append({"type": "text", "text": prompt})

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
        "output_format": schema,
    }
    # Haiku 4.5 takes neither adaptive thinking nor `effort`; the Opus/Sonnet 5
    # family takes both and benefits on the temporal reasoning.
    if thinking and not model.startswith("claude-haiku"):
        kwargs["thinking"] = {"type": "adaptive"}
        if effort:
            kwargs["output_config"] = {"effort": effort}

    try:
        response = claude_client().messages.parse(**kwargs)
    except anthropic.BadRequestError as e:
        raise VisionError("claude rejected the request: " + str(e.message)) from e
    except anthropic.AuthenticationError as e:
        raise VisionError("ANTHROPIC_API_KEY is missing or invalid") from e
    except anthropic.RateLimitError as e:
        retry_after = e.response.headers.get("retry-after", "60")
        raise VisionError("rate limited; retry after " + str(retry_after) + "s") from e
    except anthropic.APIStatusError as e:
        raise VisionError("claude API error " + str(e.status_code) + ": " + str(e.message)) from e
    except anthropic.APIConnectionError as e:
        raise VisionError("network error reaching the Claude API") from e

    # Safety classifiers can decline a request: HTTP 200, no usable content.
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ModelRefusal("claude declined this request (category=" + str(category) + ")")

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = schema.model_validate(json.loads(text))
        except (ValueError, ValidationError) as e:
            raise VisionError("could not parse structured output: " + text[:300]) from e
    return parsed


def describe_backend(backend: str, cfg: Config | None = None) -> str:
    cfg = cfg or config
    if backend == "ollama":
        return "ollama/" + cfg.ollama_model + " (local, free)"
    if backend == "off":
        return "disabled"
    return backend
