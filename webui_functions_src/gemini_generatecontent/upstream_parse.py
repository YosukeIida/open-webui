# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any


def _iter_genai_thought_and_text(response: Any) -> list[tuple[str, str]]:
    """
    google-genai のレスポンスから (kind, text) を抽出する.
    kind は "thought" or "text".
    """
    chunks: list[tuple[str, str]] = []

    candidates = getattr(response, "candidates", None) or (
        response.get("candidates") if isinstance(response, dict) else None
    )
    if not isinstance(candidates, list):
        return chunks

    for candidate in candidates:
        content = getattr(candidate, "content", None) or (
            candidate.get("content") if isinstance(candidate, dict) else None
        )
        parts = getattr(content, "parts", None) or (
            content.get("parts") if isinstance(content, dict) else None
        )
        if not isinstance(parts, list):
            continue

        for part in parts:
            thought = (
                getattr(part, "thought", None)
                or getattr(part, "thinking", None)
                or (part.get("thought") if isinstance(part, dict) else None)
                or (part.get("thinking") if isinstance(part, dict) else None)
            )
            text = getattr(part, "text", None) or (
                part.get("text") if isinstance(part, dict) else None
            )

            if isinstance(thought, str) and thought:
                chunks.append(("thought", thought))
            if isinstance(text, str) and text:
                chunks.append(("text", text))

    return chunks
