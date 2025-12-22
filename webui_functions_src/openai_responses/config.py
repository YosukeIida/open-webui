# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Literal

from pydantic import BaseModel, Field


class Valves(BaseModel):
    base_url: str | None = Field(
        default=None,
        description="OpenAI互換 base_url. 未指定なら https://api.openai.com を使用する.",
    )
    api_key_env: str = Field(
        default="OPENAI_API_KEY", description="API key を取得する環境変数名."
    )
    model_allow_regex: str | None = Field(
        default=None, description="モデル一覧の allowlist regex. 未指定なら全許可."
    )
    model_deny_regex: str | None = Field(
        default=None, description="モデル一覧の denylist regex."
    )

    max_output_tokens: int | None = Field(
        default=None,
        description="max_output_tokens. 未指定なら送らない（モデル既定に任せる）.",
    )
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    reasoning_summary: str | None = Field(
        default=None,
        description=(
            "reasoning summary の設定. 例: 'auto', 'concise', 'detailed'. "
            "未指定の場合でも既定で 'auto' を送信する（非対応モデルでは自動で外して再試行する）."
        ),
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="reasoning effort の設定. 例: 'low', 'medium', 'high'. 未指定なら送信しない.",
    )

    web_search_backend: str = Field(
        default="provider",
        description="Web Search の実行バックエンド. 'provider' のみ.",
    )
    web_search_allowed_domains: list[str] | None = Field(
        default=None,
        description="web_search.filters.allowed_domains. 指定した場合, 返る sources を特定ドメインに寄せられる可能性がある.",
    )
    web_search_blocked_domains: list[str] | None = Field(
        default=None,
        description="web_search.filters.blocked_domains.",
    )
    web_search_context_size: str | None = Field(
        default=None,
        description="web_search.search_context_size. 例: 'low'|'medium'|'high'. 未指定なら送信しない.",
    )
    include_web_search_sources: bool = Field(
        default=True,
        description="Web検索の sources を取得する（include に web_search_call.action.sources を追加）.",
    )

    file_inputs_mode: Literal["off", "full", "all"] = Field(
        default="all",
        description=(
            "Open WebUI の添付ファイル（metadata.files / __files__）を upstream へ file input として渡す. "
            "'full' は context=='full' のファイルのみ, 'all' は全ファイル, 'off' は無効."
        ),
    )
    file_inputs_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        description="file input として渡す 1 ファイルあたりの最大サイズ(bytes). 超える場合はスキップする.",
    )
    image_inputs_enabled: bool = Field(
        default=True,
        description="messages[].content の image_url を upstream へ渡す.",
    )

    extra_json: str | None = Field(
        default=None, description="Responses API payload を上書きする JSON（dict）."
    )

    debug_enabled: bool = Field(
        default=False,
        description="デバッグ用に request/response を表示する（管理者のみ）.",
    )
    debug_include_request: bool = Field(
        default=True, description="送信した request を表示する."
    )
    debug_include_response: bool = Field(
        default=False, description="受信した response を表示する."
    )
    debug_max_string_length: int = Field(
        default=4000, description="デバッグ表示での文字列の最大長."
    )
    debug_max_depth: int = Field(
        default=8, description="デバッグ表示での最大ネスト深さ."
    )


class UserValves(BaseModel):
    """
    チャット画面からユーザーが調整してよい項目のみを公開する.
    （APIキー環境変数名や extra_json など, 事故りやすい項目は含めない）
    """

    max_output_tokens: int | None = Field(
        default=None,
        description="max_output_tokens. 未指定なら送らない（モデル既定に任せる）.",
    )
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    reasoning_summary: str | None = Field(
        default=None,
        description="reasoning summary. 未指定の場合でも既定で 'auto' を送信する.",
    )
    reasoning_effort: str | None = Field(default=None, description="reasoning effort.")
    web_search_allowed_domains: list[str] | None = Field(
        default=None, description="web_search.filters.allowed_domains."
    )
    web_search_blocked_domains: list[str] | None = Field(
        default=None, description="web_search.filters.blocked_domains."
    )
    web_search_context_size: str | None = Field(
        default=None, description="web_search.search_context_size."
    )
    file_inputs_mode: Literal["off", "full", "all"] | None = Field(
        default=None,
        description="Open WebUI の添付ファイルを upstream へ file input として渡すか（上書き）.",
    )
    image_inputs_enabled: bool | None = Field(
        default=None,
        description="messages[].content の image_url を upstream へ渡すか（上書き）.",
    )

    debug_enabled: bool = Field(
        default=False,
        description="デバッグ用に request/response を表示する（管理者のみ）.",
    )
    debug_include_request: bool = Field(
        default=True, description="送信した request を表示する."
    )
    debug_include_response: bool = Field(
        default=False, description="受信した response を表示する."
    )
    debug_max_string_length: int = Field(
        default=4000, description="デバッグ表示での文字列の最大長."
    )
    debug_max_depth: int = Field(
        default=8, description="デバッグ表示での最大ネスト深さ."
    )
