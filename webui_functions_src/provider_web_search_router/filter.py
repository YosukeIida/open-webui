import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from starlette.requests import Request


class Valves(BaseModel):
    """
    Web Search の実行バックエンドを制御します.

    注意: Open WebUI の標準 Web Search は Pipe/Manifold ルートでは実行されないため,
    本 Filter を有効にして初めて Pipe/Manifold でも Web Search が動作します.
    """

    force_web_search_when_filter_enabled: bool = Field(
        default=True,
        description="本 Filter が有効なら, Pipe 側の検索を強制的に有効化する.",
    )
    default_web_search_policy: Literal["auto", "required"] = Field(
        default="auto",
        description="Filter 有効時の既定検索ポリシー. 'auto' は必要時のみ検索, 'required' は常に検索を要求する.",
    )
    required_prompt_regex: str = Field(
        default=r"(検索して|調べて|出典|引用|sources?|citations?)",
        description="ユーザープロンプトがこの regex に一致する場合, 検索ポリシーを 'required' に昇格する.",
    )
    disable_prompt_regex: str = Field(
        default=r"(検索しない|調べない|web検索不要|ウェブ検索不要|no\\s*search)",
        description="ユーザープロンプトがこの regex に一致する場合, 検索ポリシーを 'off' にする.",
    )
    target_model_id_prefix_regex: str = Field(
        default=r"^(openai_responses|anthropic_messages|gemini_generatecontent)\.",
        description="本 Filter を適用する model id の regex.",
    )
    openai_model_id_prefix_regex: str = Field(
        default=r"^openai_responses\.",
        description="OpenAI provider 検索(tool)の対象にする model id の regex.",
    )
    openai_web_search_backend: str = Field(
        default="provider",
        description="OpenAI の Web Search バックエンド. 'provider'|'webui'.",
    )


class Filter:
    valves: Valves

    # Toggleable filter として提供する（UIでON/OFF可能）.
    toggle: bool = True

    def __init__(self) -> None:
        self.valves = Valves()

    def _last_user_text(self, messages: Any) -> str:
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                return "\n".join(parts)
            if isinstance(content, str):
                return content
            return str(content)
        return ""

    def _looks_like_background_task_prompt(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if stripped.startswith("### Task:"):
            return True
        if "<chat_history>" in stripped and "### Output:" in stripped:
            return True
        if "Response must be a JSON" in stripped or "Output must be a JSON" in stripped:
            return True
        return False

    async def inlet(
        self,
        body: dict[str, Any],
        __user__: dict | None = None,
        __request__: Request | None = None,
        __event_emitter__: Any = None,
    ) -> dict[str, Any]:
        model_id = body.get("model", "")
        if not isinstance(model_id, str):
            return body

        try:
            if not re.search(self.valves.target_model_id_prefix_regex, model_id):
                return body
        except re.error:
            return body

        features = body.get("features")
        if not isinstance(features, dict):
            features = {}
            body["features"] = features

        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata

        # follow_up_generation/tags_generation 等のタスク実行では, 検索を強制しない.
        # Open WebUI の背景タスクは prompt 形式でも判別できるため, 両方でガードする.
        if metadata.get("task") or self._looks_like_background_task_prompt(
            self._last_user_text(body.get("messages"))
        ):
            features["web_search"] = False
            return body

        is_openai_responses = False
        try:
            is_openai_responses = bool(
                re.search(self.valves.openai_model_id_prefix_regex, model_id)
            )
        except re.error:
            is_openai_responses = False

        if not self.valves.force_web_search_when_filter_enabled:
            return body

        # Filter を有効にした場合は Open WebUI 側の Web Search を通さず,
        # Pipe 側で provider 検索toolを実行する.
        metadata["pipe_web_search_enabled"] = True
        features["web_search"] = False

        user_text = self._last_user_text(body.get("messages"))
        policy: str = self.valves.default_web_search_policy
        try:
            if re.search(
                self.valves.disable_prompt_regex, user_text, flags=re.IGNORECASE
            ):
                policy = "off"
            elif re.search(
                self.valves.required_prompt_regex, user_text, flags=re.IGNORECASE
            ):
                policy = "required"
        except re.error:
            policy = self.valves.default_web_search_policy

        metadata["pipe_web_search_policy"] = policy

        if is_openai_responses:
            metadata["pipe_web_search_backend"] = (
                (self.valves.openai_web_search_backend or "provider").strip().lower()
            )
        else:
            # Claude/Gemini は provider 内蔵検索toolを使う.
            metadata["pipe_web_search_backend"] = "provider"

        body["features"] = features
        body["metadata"] = metadata
        return body
