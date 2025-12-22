# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _ModelFilter:
    allow: re.Pattern[str] | None
    deny: re.Pattern[str] | None


def _compile_filter(
    allow_pattern: str | None, deny_pattern: str | None
) -> _ModelFilter:
    try:
        allow = re.compile(allow_pattern) if allow_pattern else None
    except re.error:
        allow = None
    try:
        deny = re.compile(deny_pattern) if deny_pattern else None
    except re.error:
        deny = None
    return _ModelFilter(allow=allow, deny=deny)


def _passes_filter(model_id: str, model_filter: _ModelFilter) -> bool:
    if model_filter.deny and model_filter.deny.search(model_id):
        return False
    if model_filter.allow and not model_filter.allow.search(model_id):
        return False
    return True


@dataclass(frozen=True)
class _ModelCapabilities:
    supports_extended_thinking: bool
    supports_effort: bool
    supports_context_1m: bool
    supports_output_128k: bool
    supports_interleaved_thinking: bool
    max_output_tokens: int
    max_output_tokens_128k: int | None


_MODEL_CAPABILITIES_BY_ID: dict[str, _ModelCapabilities] = {
    # Opus 4.5: effort + extended thinking
    "claude-opus-4-5-20251101": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=True,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    "claude-opus-4-5": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=True,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    # Sonnet 4.5: extended thinking + 1M context (beta)
    "claude-sonnet-4-5-20250929": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=True,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    "claude-sonnet-4-5": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=True,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    # Sonnet 4.0: extended thinking (+ 1M context beta)
    "claude-sonnet-4-20250514": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=True,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    "claude-sonnet-4-0": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=True,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    # Haiku 4.5
    "claude-haiku-4-5-20251001": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    "claude-haiku-4-5": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=64000,
        max_output_tokens_128k=None,
    ),
    # Opus 4.1/4.0: max output 32K
    "claude-opus-4-1-20250805": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=32000,
        max_output_tokens_128k=None,
    ),
    "claude-opus-4-1": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=32000,
        max_output_tokens_128k=None,
    ),
    "claude-opus-4-20250514": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=32000,
        max_output_tokens_128k=None,
    ),
    "claude-opus-4-0": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=True,
        max_output_tokens=32000,
        max_output_tokens_128k=None,
    ),
    # Sonnet 3.7: output 128K beta
    "claude-3-7-sonnet-20250219": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=True,
        supports_interleaved_thinking=False,
        max_output_tokens=64000,
        max_output_tokens_128k=128000,
    ),
    "claude-3-7-sonnet-latest": _ModelCapabilities(
        supports_extended_thinking=True,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=True,
        supports_interleaved_thinking=False,
        max_output_tokens=64000,
        max_output_tokens_128k=128000,
    ),
    # Haiku legacy
    "claude-3-5-haiku-20241022": _ModelCapabilities(
        supports_extended_thinking=False,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=False,
        max_output_tokens=8000,
        max_output_tokens_128k=None,
    ),
    "claude-3-5-haiku-latest": _ModelCapabilities(
        supports_extended_thinking=False,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=False,
        max_output_tokens=8000,
        max_output_tokens_128k=None,
    ),
    "claude-3-haiku-20240307": _ModelCapabilities(
        supports_extended_thinking=False,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=False,
        max_output_tokens=4000,
        max_output_tokens_128k=None,
    ),
}

# Anthropic Web Search tool の対応モデル（docs掲載のAPI ID）.
# NOTE: ここにないモデルでは Web Search tool が動かない可能性がある.
_WEB_SEARCH_SUPPORTED_MODEL_IDS: set[str] = {
    # Sonnet 4.5
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5",
    # Haiku 4.5
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    # Opus 4.5
    "claude-opus-4-5-20251101",
    "claude-opus-4-5",
    # Sonnet 4
    "claude-sonnet-4-20250514",
    "claude-sonnet-4-0",
    # Opus 4 / 4.1
    "claude-opus-4-20250514",
    "claude-opus-4-0",
    "claude-opus-4-1-20250805",
    "claude-opus-4-1",
    # Sonnet 3.7
    "claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-latest",
    # Haiku 3.5
    "claude-3-5-haiku-20241022",
    "claude-3-5-haiku-latest",
}


def _capabilities_for_model(model_id: str) -> _ModelCapabilities:
    caps = _MODEL_CAPABILITIES_BY_ID.get(model_id)
    if caps is not None:
        return caps
    return _ModelCapabilities(
        supports_extended_thinking=False,
        supports_effort=False,
        supports_context_1m=False,
        supports_output_128k=False,
        supports_interleaved_thinking=False,
        max_output_tokens=8192,
        max_output_tokens_128k=None,
    )
