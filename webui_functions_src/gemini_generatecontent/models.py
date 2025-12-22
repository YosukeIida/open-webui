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
