# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Literal

from pydantic import BaseModel, Field


class Valves(BaseModel):
    api_key_env: str = Field(
        default="ANTHROPIC_API_KEY", description="API key を取得する環境変数名."
    )
    model_allow_regex: str | None = Field(
        default=None, description="モデル一覧の allowlist regex. 未指定なら全許可."
    )
    model_deny_regex: str | None = Field(
        default=None, description="モデル一覧の denylist regex."
    )
    max_tokens: int | None = Field(default=None, description="max_tokens.")
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    top_k: int | None = Field(default=None, description="top_k.")
    stop_sequences: list[str] | None = Field(
        default=None, description="stop_sequences."
    )
    metadata_user_id: str | None = Field(
        default=None,
        description="metadata.user_id. 個人情報（name/email等）は入れず, UUID等の不透明なIDを推奨する.",
    )
    service_tier: Literal["auto", "standard_only"] | None = Field(
        default=None,
        description="service_tier. 'auto'|'standard_only'.",
    )
    tool_choice_disable_parallel_tool_use: bool | None = Field(
        default=None,
        description="tool_choice.disable_parallel_tool_use. True の場合, 並列ツール利用を抑制する.",
    )
    context_1m_enabled: bool = Field(
        default=False,
        description="(Sonnet) 1M context window を有効化する（beta header を付与）.",
    )
    output_128k_enabled: bool = Field(
        default=False,
        description="(Sonnet 3.7) max output 128K を有効化する（beta header を付与）.",
    )
    interleaved_thinking_enabled: bool = Field(
        default=False,
        description="(Claude 4) interleaved thinking を有効化する（beta header を付与）.",
    )
    required_with_thinking: Literal[
        "error", "downgrade_to_auto", "disable_thinking"
    ] = Field(
        default="downgrade_to_auto",
        description=(
            "required web search（forced tool use）と extended thinking が同時に有効な場合の挙動. "
            "'error'|'downgrade_to_auto'|'disable_thinking'."
        ),
    )
    thinking_enabled: bool = Field(
        default=False,
        description="Claude の extended thinking を有効化する（thinking block をストリームで受け取る）.",
    )
    thinking_budget_mode: Literal["auto", "manual"] = Field(
        default="auto",
        description=(
            "extended thinking の budget_tokens の決め方. "
            "'auto' は max_tokens から自動算出し, 'manual' は thinking_budget_tokens を使用する."
        ),
    )
    thinking_budget_tokens: int | None = Field(
        default=None,
        description="extended thinking の予算（budget_tokens）. thinking_enabled=true の場合に使用する.",
    )
    effort_enabled: bool = Field(
        default=False,
        description="(Opus 4.5 only) output_config.effort を有効化する.",
    )
    effort_level: Literal["low", "medium", "high"] = Field(
        default="high",
        description="(Opus 4.5 only) effort. low|medium|high.",
    )
    web_search_tool_type: str = Field(
        default="web_search_20250305",
        description="Anthropic server tool の type（Web Search）.",
    )
    web_search_tool_name: str = Field(
        default="web_search",
        description="Anthropic server tool の name（Web Search）.",
    )
    web_search_max_uses: int = Field(
        default=3,
        description="Web Search tool の最大使用回数（コスト/遅延の暴走防止）.",
    )
    web_search_retry_max_attempts: int = Field(
        default=1,
        description=(
            "Web Search tool が unavailable のときに再試行する回数. "
            "1 の場合, 最大 2 回（初回 + 1 回）試行する."
        ),
    )
    web_search_retry_base_delay_seconds: float = Field(
        default=0.5,
        description="Web Search tool 再試行の base delay（秒）.",
    )
    web_search_retry_max_delay_seconds: float = Field(
        default=5.0,
        description="Web Search tool 再試行の最大 delay（秒）.",
    )
    auto_append_beta_headers: bool = Field(
        default=True,
        description="必要な beta header を自動で付与する（Web Search, effort など）.",
    )
    web_search_beta_header: str = Field(
        default="web-search-2025-03-05",
        description="Web Search 用の beta header.",
    )
    effort_beta_header: str = Field(
        default="effort-2025-11-24",
        description="effort 用の beta header（Opus 4.5）.",
    )
    context_1m_beta_header: str = Field(
        default="context-1m-2025-08-07",
        description="(Sonnet) 1M context window 用 beta header.",
    )
    output_128k_beta_header: str = Field(
        default="output-128k-2025-02-19",
        description="(Sonnet 3.7) max output 128K 用 beta header.",
    )
    interleaved_thinking_beta_header: str = Field(
        default="interleaved-thinking-2025-05-14",
        description="(Claude 4) interleaved thinking 用 beta header.",
    )
    anthropic_beta_header: str | None = Field(
        default=None,
        description="追加の Anthropic beta header（カンマ区切り可）. 必要な場合のみ設定する.",
    )
    files_api_enabled: bool = Field(
        default=True,
        description="Anthropic Files API を利用して PDF を file_id 参照で渡す（失敗時は base64 fallback）.",
    )
    files_api_beta_header: str = Field(
        default="files-api-2025-04-14",
        description="Files API / file_id 参照に必要な beta header.",
    )
    file_inputs_mode: Literal["off", "full", "all"] = Field(
        default="all",
        description=(
            "Open WebUI の添付ファイル（metadata.files / __files__）を upstream へ document として渡す. "
            "'full' は context=='full' のファイルのみ, 'all' は全ファイル, 'off' は無効."
        ),
    )
    file_inputs_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        description="document として渡す 1 ファイルあたりの最大サイズ(bytes). 超える場合はスキップする.",
    )
    image_inputs_enabled: bool = Field(
        default=True,
        description="messages[].content の image_url を upstream へ image block として渡す.",
    )
    extra_json: str | None = Field(
        default=None, description="Messages API payload を上書きする JSON（dict）."
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

    max_tokens: int | None = Field(default=None, description="max_tokens.")
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    top_k: int | None = Field(default=None, description="top_k.")
    stop_sequences: list[str] | None = Field(
        default=None, description="stop_sequences."
    )
    metadata_user_id: str | None = Field(
        default=None,
        description="metadata.user_id. 個人情報（name/email等）は入れず, UUID等の不透明なIDを推奨する.",
    )
    service_tier: Literal["auto", "standard_only"] | None = Field(
        default=None,
        description="service_tier. 'auto'|'standard_only'.",
    )
    tool_choice_disable_parallel_tool_use: bool | None = Field(
        default=None,
        description="tool_choice.disable_parallel_tool_use. True の場合, 並列ツール利用を抑制する.",
    )
    context_1m_enabled: bool = Field(
        default=False,
        description="(Sonnet) 1M context window を有効化する（beta header を付与）.",
    )
    output_128k_enabled: bool = Field(
        default=False,
        description="(Sonnet 3.7) max output 128K を有効化する（beta header を付与）.",
    )
    interleaved_thinking_enabled: bool = Field(
        default=False,
        description="(Claude 4) interleaved thinking を有効化する（beta header を付与）.",
    )
    required_with_thinking: Literal[
        "error", "downgrade_to_auto", "disable_thinking"
    ] = Field(
        default="downgrade_to_auto",
        description=(
            "required web search（forced tool use）と extended thinking が同時に有効な場合の挙動. "
            "'error'|'downgrade_to_auto'|'disable_thinking'."
        ),
    )

    thinking_enabled: bool = Field(
        default=False,
        description="Claude の extended thinking を有効化する（thinking block をストリームで受け取る）.",
    )
    thinking_budget_mode: Literal["auto", "manual"] = Field(
        default="auto",
        description=(
            "extended thinking の budget_tokens の決め方. "
            "'auto' は max_tokens から自動算出し, 'manual' は thinking_budget_tokens を使用する."
        ),
    )
    thinking_budget_tokens: int | None = Field(
        default=None,
        description="extended thinking の予算（budget_tokens）. thinking_enabled=true の場合に使用する.",
    )

    effort_enabled: bool = Field(
        default=False,
        description="(Opus 4.5 only) output_config.effort を有効化する.",
    )
    effort_level: Literal["low", "medium", "high"] = Field(
        default="high",
        description="(Opus 4.5 only) effort. low|medium|high.",
    )
    file_inputs_mode: Literal["off", "full", "all"] | None = Field(
        default=None,
        description="Open WebUI の添付ファイルを upstream へ document として渡すか（上書き）.",
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
