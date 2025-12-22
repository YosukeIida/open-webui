# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Literal

from pydantic import BaseModel, Field


class Valves(BaseModel):
    api_key_env: str = Field(
        default="GEMINI_API_KEY", description="API key を取得する環境変数名."
    )
    model_allow_regex: str | None = Field(
        default=None, description="モデル一覧の allowlist regex. 未指定なら全許可."
    )
    model_deny_regex: str | None = Field(
        default=None, description="モデル一覧の denylist regex."
    )

    include_thoughts: bool = Field(
        default=False,
        description="thought summary の取得を試みる（Gemini 側の対応モデルのみ有効）.",
    )
    max_output_tokens: int | None = Field(
        default=64000, description="max_output_tokens."
    )
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    file_inputs_mode: Literal["off", "full", "all"] = Field(
        default="all",
        description=(
            "Open WebUI の添付ファイル（metadata.files / __files__）を Gemini の inline_data として渡す. "
            "'full' は context=='full' のファイルのみ, 'all' は全ファイル, 'off' は無効."
        ),
    )
    file_inputs_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        description="inline_data として渡す 1 ファイルあたりの最大サイズ(bytes). 超える場合はスキップする.",
    )
    image_inputs_enabled: bool = Field(
        default=True,
        description="messages[].content の image_url を Gemini の inline_data として渡す（data URLのみ）.",
    )
    extra_json: str | None = Field(
        default=None, description="generateContent payload を上書きする JSON（dict）."
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

    include_thoughts: bool = Field(
        default=False,
        description="thought summary の取得を試みる（Gemini 側の対応モデルのみ有効）.",
    )
    max_output_tokens: int | None = Field(
        default=64000, description="max_output_tokens."
    )
    temperature: float | None = Field(default=None, description="temperature.")
    top_p: float | None = Field(default=None, description="top_p.")
    file_inputs_mode: Literal["off", "full", "all"] | None = Field(
        default=None,
        description="Open WebUI の添付ファイルを Gemini の inline_data として渡すか（上書き）.",
    )
    image_inputs_enabled: bool | None = Field(
        default=None,
        description="messages[].content の image_url を Gemini に渡すか（上書き）.",
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
