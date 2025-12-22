# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from webui_functions_src.anthropic_messages.config import Valves
from webui_functions_src.anthropic_messages.models import _ModelCapabilities


def _auto_thinking_budget_tokens(*, max_tokens: int) -> int:
    """
    extended thinking の budget_tokens を自動決定する.

    NOTE:
    - budget_tokens は max_tokens の枠を消費する.
    - 過剰に大きい budget は遅延/コスト増になりやすい.
    - ここでは「十分に考えられるが暴れにくい」範囲を目指す.
    """

    # max_tokens の 25% を目標にしつつ, 上限を抑える.
    target = int(max_tokens * 0.25)
    budget = max(1024, min(target, 16384))
    # 必ず max_tokens 未満にする.
    return min(budget, max(1024, max_tokens - 1))


def _coerce_user_valves_for_model(
    valves: "Valves", caps: _ModelCapabilities
) -> tuple["Valves", list[str]]:
    """
    UserValves は Function 単位で永続化されるため, モデルを切り替えると
    「前のモデルでは有効だった設定」が残ることがある.

    ここでは, そのような設定を model capabilities に基づいて無効化し,
    呼び出しをエラーで止めずに継続できるようにする（警告は別途表示する）.
    """

    ignored: list[str] = []
    values = valves.model_dump()

    def disable(name: str) -> None:
        current = values.get(name)
        if current is True:
            ignored.append(name)
        values[name] = False

    if values.get("context_1m_enabled") and not caps.supports_context_1m:
        disable("context_1m_enabled")

    if values.get("output_128k_enabled") and (
        not caps.supports_output_128k or caps.max_output_tokens_128k is None
    ):
        disable("output_128k_enabled")

    if values.get("effort_enabled") and not caps.supports_effort:
        disable("effort_enabled")

    if values.get("thinking_enabled") and not caps.supports_extended_thinking:
        disable("thinking_enabled")

    # interleaved thinking は Claude 4 系のみ. さらに thinking が無効なら成立しない.
    if (
        values.get("interleaved_thinking_enabled")
        and not caps.supports_interleaved_thinking
    ):
        disable("interleaved_thinking_enabled")
    if values.get("interleaved_thinking_enabled") and not values.get(
        "thinking_enabled"
    ):
        disable("interleaved_thinking_enabled")

    return Valves(**values), ignored
