# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any


def _get_message_content_blocks(message: Any) -> list[dict[str, Any]]:
    """
    Anthropic Messages API のレスポンスから content blocks を取り出す.
    SDK object / dict の両方に対応する.
    """

    if isinstance(message, dict):
        blocks = message.get("content", [])
        return blocks if isinstance(blocks, list) else []

    blocks = getattr(message, "content", None)
    return blocks if isinstance(blocks, list) else []


def _get_message_usage_tokens(message: Any) -> tuple[int, int]:
    """
    (input_tokens, output_tokens) を返す. SDK object / dict の両方に対応する.
    """

    if isinstance(message, dict):
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return (0, 0)
        return (
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )

    usage_obj = getattr(message, "usage", None)
    if usage_obj is None:
        return (0, 0)
    return (
        int(getattr(usage_obj, "input_tokens", 0) or 0),
        int(getattr(usage_obj, "output_tokens", 0) or 0),
    )


def _get_message_stop_reason(message: Any) -> str | None:
    if isinstance(message, dict):
        value = message.get("stop_reason")
        return value if isinstance(value, str) and value.strip() else None

    value = getattr(message, "stop_reason", None)
    return value if isinstance(value, str) and value.strip() else None


def _stop_reason_note(stop_reason: str) -> str:
    if stop_reason == "pause_turn":
        return (
            "stop_reason=pause_turn. 応答が一時停止しました. 続行が必要な場合は, 直前の assistant content を含めた messages を維持したまま, "
            "同じ model で続きの指示を送ってください."
        )
    if stop_reason == "refusal":
        return "stop_reason=refusal. 安全上の理由で拒否されました. 入力や指示を見直してください."
    return f"stop_reason={stop_reason}."
