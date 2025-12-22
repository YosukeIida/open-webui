# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any

from pydantic import ValidationError

from webui_functions_src.gemini_generatecontent.config import Valves


def _build_generation_config(valves: Valves, generation_config: dict[str, Any]) -> Any:
    """
    GenerateContentConfig を構築する.
    include_thoughts のフィールド名は SDK バージョン差分があり得るため, 複数候補を試す.
    """
    from google.genai import types

    if not generation_config and not valves.include_thoughts:
        return None

    candidates: list[dict[str, Any]] = []
    if valves.include_thoughts:
        candidates.append({**generation_config, "includeThoughts": True})
        candidates.append({**generation_config, "include_thoughts": True})
    candidates.append(generation_config)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return types.GenerateContentConfig(**candidate) if candidate else None
        except (TypeError, ValidationError) as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    return None
