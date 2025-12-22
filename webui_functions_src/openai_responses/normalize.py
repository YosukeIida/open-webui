# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any

def _looks_like_background_task_prompt(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("### Task:"):
        return True
    if "<chat_history>" in stripped and "### Output:" in stripped:
        return True
    return False


def _format_query_for_status(text: str, max_len: int = 160) -> str:
    query = " ".join((text or "").strip().splitlines()).strip()
    if not query:
        return ""
    if len(query) <= max_len:
        return query
    return query[: max(0, max_len - 1)] + "…"


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    parts.append(part["text"])
            return "\n".join(parts)
        if isinstance(content, str):
            return content
        return str(content)
    return ""


def _extract_openai_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Open WebUI messages(Chat Completions) から Responses の (instructions, input messages) を作る.

    注意: Responses API の input items に system/developer を直接入れると互換が壊れやすいので,
    system/developer は `instructions` にまとめる.
    """
    instruction_parts: list[str] = []
    filtered: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")

        if role in ("system", "developer"):
            # system/developer は instructions に集約する.
            if isinstance(content, list):
                text_parts: list[str] = []
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        text_parts.append(part["text"])
                content_text = "\n".join(text_parts).strip()
            elif isinstance(content, str):
                content_text = content.strip()
            else:
                content_text = str(content).strip()
            if content_text:
                instruction_parts.append(content_text)
            continue

        if role in ("user", "assistant"):
            # OpenAI chat形式の content を維持して, 後段で Responses input に変換する.
            if isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text" and isinstance(part.get("text"), str):
                        parts.append({"type": "text", "text": part["text"]})
                        continue
                    if part_type == "image_url":
                        image_url = part.get("image_url")
                        url: str | None = None
                        if isinstance(image_url, dict):
                            url = image_url.get("url")
                        elif isinstance(image_url, str):
                            url = image_url
                        if isinstance(url, str) and url.strip():
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": url.strip()},
                                }
                            )
                        continue
                if not parts:
                    parts = [{"type": "text", "text": str(content)}]
                filtered.append({"role": role, "content": parts})
            else:
                filtered.append({"role": role, "content": str(content)})

    return "\n\n".join(instruction_parts), filtered


def _to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            items: list[dict[str, Any]] = []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text" and isinstance(part.get("text"), str):
                        items.append({"type": "input_text", "text": part["text"]})
                        continue
                    if part_type == "image_url":
                        image_url = part.get("image_url")
                        url: str | None = None
                        if isinstance(image_url, dict):
                            url = image_url.get("url")
                        elif isinstance(image_url, str):
                            url = image_url
                        if isinstance(url, str) and url.strip():
                            items.append(
                                {"type": "input_image", "image_url": url.strip()}
                            )
                        continue
            else:
                items.append({"type": "input_text", "text": str(content)})
            converted.append(
                {"role": "user", "content": items or [{"type": "input_text", "text": ""}]}
            )
        elif role == "assistant":
            if isinstance(content, list):
                text = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            else:
                text = str(content)
            converted.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
    return converted
