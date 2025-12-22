# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import asyncio
import base64
import inspect
import json
import os
import random
import re
from collections.abc import AsyncGenerator
from typing import Any, Literal

import httpx
from anthropic import AsyncAnthropic
from open_webui.utils.misc import (
    openai_chat_chunk_message_template,
    openai_chat_completion_message_template,
)
from pydantic import BaseModel
from starlette.requests import Request

from webui_functions_src.anthropic_messages.config import UserValves, Valves
from webui_functions_src.anthropic_messages.emit import (
    _emit_citation,
    _emit_citations_from_items,
    _emit_citations_from_urls,
    _emit_debug_parameters,
    _emit_status,
    _emit_unverified_citations_from_text,
    _is_admin_user,
)
from webui_functions_src.anthropic_messages.header import (
    _ANTHROPIC_STREAMING_REQUIRED_MAX_TOKENS,
)
from webui_functions_src.anthropic_messages.models import (
    _WEB_SEARCH_SUPPORTED_MODEL_IDS,
    _capabilities_for_model,
    _compile_filter,
    _passes_filter,
)
from webui_functions_src.anthropic_messages.normalize import (
    _extract_openai_messages,
    _format_query_for_status,
    _last_user_text,
    _looks_like_background_task_prompt,
)
from webui_functions_src.anthropic_messages.preflight import (
    _coerce_user_valves_for_model,
)
from webui_functions_src.anthropic_messages.upstream_http import (
    _anthropic_api_key_from_env,
    _anthropic_create_via_http,
    _anthropic_files_create_via_http,
    _anthropic_models_list_via_http,
    _anthropic_stream_via_http,
)
from webui_functions_src.anthropic_messages.upstream_parse import (
    _get_message_content_blocks,
    _get_message_stop_reason,
    _get_message_usage_tokens,
    _stop_reason_note,
)
from webui_functions_src.anthropic_messages.web_search import (
    _coalesce_web_search_backend,
    _deep_find_citation_items,
    _deep_find_queries,
    _deep_find_web_search_tool_errors,
    _deep_has_web_search_tool_activity,
    _with_system_note,
)


def _parse_base64_data_url(url: str) -> tuple[str, str] | None:
    """
    data:<mime>;base64,<data> を (mime, base64_data) に分解する.
    """
    if not isinstance(url, str):
        return None
    if not url.startswith("data:"):
        return None
    if ";base64," not in url:
        return None
    header, encoded = url.split(",", 1)
    mime = header[len("data:") :].split(";", 1)[0].strip() or "application/octet-stream"
    encoded = (encoded or "").strip()
    if not encoded:
        return None
    return mime, encoded


def _strip_anthropic_content_block_types(
    request: dict[str, Any], *, remove_types: set[str]
) -> dict[str, Any]:
    if not remove_types:
        return request
    messages = request.get("messages")
    if not isinstance(messages, list):
        return request
    new_messages: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            new_messages.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            new_messages.append(message)
            continue
        new_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in remove_types:
                continue
            new_content.append(block)
        copied = dict(message)
        copied["content"] = new_content
        new_messages.append(copied)
    copied_request = dict(request)
    copied_request["messages"] = new_messages
    return copied_request


def _load_open_webui_file_bytes_for_anthropic(
    file_id: str, *, __user__: dict | None, max_bytes: int
) -> tuple[bytes, str, str]:
    """
    Open WebUI の Files storage から raw bytes を取得する（Anthropic document/image 用）.
    """
    from open_webui.models.files import Files
    from open_webui.models.users import Users
    from open_webui.routers.files import has_access_to_file
    from open_webui.storage.provider import Storage

    file = Files.get_file_by_id(file_id)
    if not file:
        raise RuntimeError(f"File not found: {file_id}")

    user_id = (__user__ or {}).get("id") if isinstance(__user__, dict) else None
    if not isinstance(user_id, str) or not user_id.strip():
        raise RuntimeError("Missing user context for file access.")
    user = Users.get_user_by_id(user_id)
    if not user:
        raise RuntimeError("User not found.")

    if not (file.user_id == user.id or user.role == "admin"):
        allowed = False
        try:
            allowed = bool(has_access_to_file(file_id, "read", user=user))
        except Exception:
            allowed = False
        if not allowed:
            raise RuntimeError(f"Access denied for file: {file_id}")

    meta = file.meta or {}
    filename = (
        (meta.get("name") if isinstance(meta, dict) else None)
        or getattr(file, "filename", None)
        or file_id
    )
    if not isinstance(filename, str) or not filename.strip():
        filename = file_id

    content_type = meta.get("content_type") if isinstance(meta, dict) else None
    if not isinstance(content_type, str) or not content_type.strip():
        content_type = "application/octet-stream"

    size = meta.get("size") if isinstance(meta, dict) else None
    if isinstance(size, int) and size > max_bytes:
        raise RuntimeError(
            f"File too large for upstream document/image input. file_id={file_id} size={size} max_bytes={max_bytes}"
        )

    if not getattr(file, "path", None):
        raise RuntimeError(f"File path is missing. file_id={file_id}")
    disk_path = Storage.get_file(file.path)
    with open(disk_path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(
            f"File too large for upstream document/image input. file_id={file_id} read_bytes>{max_bytes}"
        )
    return data, content_type, filename


class Pipe:
    valves: Valves
    UserValves = UserValves

    def __init__(self) -> None:
        self.valves = Valves()

    @property
    def name(self) -> str:
        return "Anthropic Messages: "

    def _effective_valves(self, __user__: dict | None) -> Valves:
        if not isinstance(__user__, dict):
            return self.valves

        user_valves = __user__.get("valves")
        if user_valves is None:
            return self.valves

        raw_values: dict[str, Any] | None = None
        if isinstance(user_valves, BaseModel):
            raw_values = user_valves.model_dump(exclude_none=True)
        elif isinstance(user_valves, dict):
            raw_values = {k: v for k, v in user_valves.items() if v is not None}
        else:
            return self.valves

        # セキュリティ: dict をそのまま Valves に merge しない（UserValves でホワイトリスト化）
        allowed_keys = set(getattr(UserValves, "model_fields", {}).keys())
        filtered_values = {
            k: v for k, v in (raw_values or {}).items() if k in allowed_keys
        }
        # UserValves のデフォルト値を "ユーザー指定" と誤認しないように exclude_defaults する.
        user_values = UserValves(**filtered_values).model_dump(
            exclude_none=True, exclude_defaults=True
        )

        merged = {
            **self.valves.model_dump(),
            **(user_values or {}),
        }
        return Valves(**merged)

    def _beta_headers(
        self, valves: Valves, *, extra_beta_headers: list[str] | None = None
    ) -> list[str]:
        beta_headers: list[str] = []
        if isinstance(extra_beta_headers, list):
            beta_headers.extend(
                [h for h in extra_beta_headers if isinstance(h, str) and h.strip()]
            )
        if valves.anthropic_beta_header:
            beta_headers.extend(
                [
                    h.strip()
                    for h in valves.anthropic_beta_header.split(",")
                    if h.strip()
                ]
            )
        return list(dict.fromkeys(beta_headers))

    def _async_client(
        self, valves: Valves, *, extra_beta_headers: list[str] | None = None
    ) -> AsyncAnthropic:
        api_key = os.environ.get(valves.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")

        beta_headers = self._beta_headers(valves, extra_beta_headers=extra_beta_headers)
        if beta_headers:
            return AsyncAnthropic(
                api_key=api_key,
                default_headers={"anthropic-beta": ",".join(beta_headers)},
            )
        return AsyncAnthropic(api_key=api_key)

    def pipes(self) -> list[dict[str, str]]:
        model_filter = _compile_filter(
            self.valves.model_allow_regex, self.valves.model_deny_regex
        )

        models: list[dict[str, str]] = []

        # SDK 依存を減らすため, models.list は HTTP で取得する.
        try:
            api_key = _anthropic_api_key_from_env(self.valves)
            beta_headers = self._beta_headers(self.valves)
            for model_id in _anthropic_models_list_via_http(
                api_key=api_key, beta_headers=beta_headers
            ):
                if _passes_filter(model_id, model_filter):
                    models.append({"id": model_id, "name": model_id})
            if models:
                return models
        except Exception:
            pass

        configured = os.environ.get("ANTHROPIC_MODEL_IDS", "").strip()
        for model_id in [m.strip() for m in configured.split(";") if m.strip()]:
            if _passes_filter(model_id, model_filter):
                models.append({"id": model_id, "name": model_id})
        return models

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict | None = None,
        __event_emitter__: Any = None,
        __request__: Request | None = None,
        __metadata__: dict | None = None,
        __files__: list[dict[str, Any]] | None = None,
    ) -> Any:
        valves = self._effective_valves(__user__)

        model_id = body.get("model", "")
        if not isinstance(model_id, str) or "." not in model_id:
            raise ValueError("Invalid model id.")
        _pipe_id, upstream_model = model_id.split(".", 1)
        caps = _capabilities_for_model(upstream_model)

        messages = body.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Invalid messages.")

        system, filtered = _extract_openai_messages(messages)

        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        is_metadata_task = bool(metadata.get("task"))
        # Pipe 側の Web Search は Router（provider_web_search_router）で制御する.
        # Open WebUI の Web Search トグル（features.web_search）は Pipe では使わない.
        if metadata.get("task"):
            web_search_enabled = False
        else:
            web_search_enabled = metadata.get("pipe_web_search_enabled") is True
        web_search_query = _last_user_text(messages)
        is_background_task = is_metadata_task or _looks_like_background_task_prompt(
            web_search_query
        )
        if is_background_task:
            web_search_enabled = False

        valves, ignored_user_valves = _coerce_user_valves_for_model(valves, caps)
        if ignored_user_valves and not is_background_task:
            await _emit_status(
                __event_emitter__,
                {
                    "action": "warning",
                    "description": (
                        "このモデルでは次の設定を無視しました: "
                        + ", ".join(sorted(ignored_user_valves))
                        + f". model={upstream_model}"
                    ),
                    "done": True,
                },
            )

        seen_urls: dict[str, bool] = {}

        web_search_backend = _coalesce_web_search_backend(
            metadata, default_backend="provider"
        )

        policy_raw = metadata.get("pipe_web_search_policy")
        if isinstance(policy_raw, str):
            policy_raw = policy_raw.strip().lower()
        web_search_policy: Literal["auto", "required", "off"]
        if not web_search_enabled:
            web_search_policy = "off"
        elif policy_raw in ("auto", "required", "off"):
            web_search_policy = policy_raw  # type: ignore[assignment]
        else:
            # 後方互換: 古い Router は policy を付与しないため, 従来どおり required とする.
            web_search_policy = "required"

        if (
            web_search_enabled
            and web_search_backend == "provider"
            and web_search_policy != "off"
            and upstream_model not in _WEB_SEARCH_SUPPORTED_MODEL_IDS
        ):
            if web_search_policy == "required":
                raise RuntimeError(
                    "This model does not support the Anthropic Web Search tool. "
                    f"model={upstream_model}. "
                    "Please switch to a supported model (e.g., claude-sonnet-4-20250514) or disable provider web search."
                )
            web_search_policy = "off"
            if not is_background_task:
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "warning",
                        "description": (
                            "このモデルでは Anthropic Web Search tool を利用できないため, "
                            "provider web search を無効化しました. "
                            f"model={upstream_model}"
                        ),
                        "done": True,
                    },
                )

        provider_web_search = (
            web_search_enabled
            and web_search_backend == "provider"
            and web_search_policy != "off"
            and web_search_query.strip() != ""
        )
        provider_web_search_required = (
            provider_web_search and web_search_policy == "required"
        )

        # Anthropic extended thinking は forced tool use (tool_choice=tool/any) と非互換.
        # required のままだと API がエラーを返すため, valves.required_with_thinking に従って扱う.
        if provider_web_search_required and valves.thinking_enabled:
            policy = valves.required_with_thinking
            if policy == "error":
                raise RuntimeError(
                    "extended thinking は required web search（forced tool use）と非互換です. "
                    "thinking_enabled を無効化するか, web search policy を auto/off にしてください."
                )
            if policy == "disable_thinking":
                values = valves.model_dump()
                values["thinking_enabled"] = False
                values["interleaved_thinking_enabled"] = False
                valves = Valves(**values)
                if not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "required web search（forced tool use）を優先するため, "
                                "extended thinking を無効化しました."
                            ),
                            "done": True,
                        },
                    )
            else:
                provider_web_search_required = False
                web_search_policy = "auto"
                if not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "extended thinking は required web search（forced tool use）と非互換です. "
                                "required を維持できないため, web search policy を auto に変更しました（検索しない可能性があります）. "
                                "確実に検索したい場合は thinking_enabled を無効化してください."
                            ),
                            "done": True,
                        },
                    )

        if provider_web_search:
            if provider_web_search_required:
                query_for_status = _format_query_for_status(web_search_query)
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "web_search",
                        "description": "Searching the web",
                        "done": False,
                    },
                )
                if query_for_status:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "web_search_queries_generated",
                            "queries": [query_for_status],
                            "done": False,
                        },
                    )

                system = _with_system_note(
                    system,
                    "\n".join(
                        [
                            "Web検索が有効です. web search tool を使って最新情報を取得してから回答してください.",
                            "可能なら情報源(URL)も含めてください.",
                            "検索できない場合は、その旨を明確に述べてください.",
                        ]
                    ),
                )
            else:
                system = _with_system_note(
                    system,
                    "\n".join(
                        [
                            "必要に応じて web search tool を使って最新情報を確認してから回答してください.",
                            "特に『今日/最新/ニュース/天気』など時事性が強い場合は検索を優先してください.",
                            "可能なら情報源(URL)も含めてください.",
                        ]
                    ),
                )

        extra_beta_headers: list[str] = []
        if valves.auto_append_beta_headers:
            if provider_web_search and valves.web_search_beta_header.strip():
                extra_beta_headers.append(valves.web_search_beta_header.strip())
            if valves.effort_enabled and valves.effort_beta_header.strip():
                extra_beta_headers.append(valves.effort_beta_header.strip())
            if valves.context_1m_enabled and valves.context_1m_beta_header.strip():
                extra_beta_headers.append(valves.context_1m_beta_header.strip())
            if valves.output_128k_enabled and valves.output_128k_beta_header.strip():
                extra_beta_headers.append(valves.output_128k_beta_header.strip())
            if (
                valves.interleaved_thinking_enabled
                and valves.interleaved_thinking_beta_header.strip()
            ):
                extra_beta_headers.append(
                    valves.interleaved_thinking_beta_header.strip()
                )

        file_inputs_mode = (valves.file_inputs_mode or "off").strip().lower()
        if file_inputs_mode not in ("off", "full", "all"):
            file_inputs_mode = "off"

        pdf_file_ids: list[str] = []
        if file_inputs_mode != "off" and isinstance(__files__, list):
            for item in __files__:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "file":
                    continue
                if file_inputs_mode == "full" and item.get("context") != "full":
                    continue
                file_id = item.get("id")
                if isinstance(file_id, str) and file_id.strip():
                    pdf_file_ids.append(file_id.strip())
            pdf_file_ids = list(dict.fromkeys(pdf_file_ids))

        pdf_payloads: list[dict[str, Any]] = []
        if pdf_file_ids and not is_background_task:
            for file_id in pdf_file_ids:
                try:
                    data, content_type, filename = (
                        _load_open_webui_file_bytes_for_anthropic(
                            file_id,
                            __user__=__user__,
                            max_bytes=int(valves.file_inputs_max_bytes),
                        )
                    )
                except Exception as exc:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "添付ファイルを upstream document として渡せませんでした: "
                                f"{file_id}. {exc}"
                            ),
                            "done": True,
                        },
                    )
                    continue

                is_pdf = (
                    content_type.strip().lower() == "application/pdf"
                    or filename.strip().lower().endswith(".pdf")
                )
                if not is_pdf:
                    continue

                pdf_payloads.append(
                    {
                        "openwebui_file_id": file_id,
                        "filename": filename,
                        "content_type": content_type,
                        "data": data,
                    }
                )

        anthropic_messages: list[dict[str, Any]] = []
        for message in filtered:
            role = message.get("role")
            raw_content = message.get("content", "")

            blocks: list[dict[str, Any]] = []
            if isinstance(raw_content, list):
                for part in raw_content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text" and isinstance(part.get("text"), str):
                        blocks.append({"type": "text", "text": part["text"]})
                        continue
                    if part_type == "image_url":
                        if not valves.image_inputs_enabled:
                            continue
                        image_url = part.get("image_url")
                        url: str | None = None
                        if isinstance(image_url, dict):
                            url = image_url.get("url")
                        elif isinstance(image_url, str):
                            url = image_url
                        if not isinstance(url, str) or not url.strip():
                            continue
                        parsed = _parse_base64_data_url(url.strip())
                        if parsed is None:
                            # URL fetch は環境依存・壊れやすいので, ここではテキスト化する.
                            blocks.append(
                                {
                                    "type": "text",
                                    "text": f"[image omitted: {url.strip()}]",
                                }
                            )
                            continue
                        mime, encoded = parsed
                        if not mime.lower().startswith("image/"):
                            blocks.append(
                                {"type": "text", "text": f"[non-image omitted: {mime}]"}
                            )
                            continue
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime,
                                    "data": encoded,
                                },
                            }
                        )
                        continue
            else:
                blocks.append({"type": "text", "text": str(raw_content)})

            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            if role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": blocks})

        request: dict[str, Any] = {
            "model": upstream_model,
            "messages": anthropic_messages,
        }
        if system:
            request["system"] = system

        if (
            pdf_payloads
            and valves.files_api_enabled
            and isinstance(valves.files_api_beta_header, str)
            and valves.files_api_beta_header.strip()
        ):
            # PDF を file_id 参照で渡すには beta header が必要.
            extra_beta_headers.append(valves.files_api_beta_header.strip())

        ui_stream = bool(body.get("stream", False))

        if valves.max_tokens is not None:
            request["max_tokens"] = valves.max_tokens
        if valves.temperature is not None:
            request["temperature"] = valves.temperature
        if valves.top_p is not None:
            request["top_p"] = valves.top_p
        if valves.top_k is not None:
            request["top_k"] = valves.top_k
        if valves.stop_sequences is not None:
            request["stop_sequences"] = valves.stop_sequences

        # generation params は UserValves 経由でのみ受け付ける（body での上書きは許可しない）.

        effective_max_output_tokens = caps.max_output_tokens
        if valves.output_128k_enabled and caps.max_output_tokens_128k is not None:
            effective_max_output_tokens = caps.max_output_tokens_128k

        max_tokens_value = request.get("max_tokens")
        if max_tokens_value is None:
            # 未指定ならモデルcapに合わせて自動決定する.
            max_tokens_value = int(effective_max_output_tokens)
            request["max_tokens"] = max_tokens_value

        if not isinstance(max_tokens_value, int):
            raise RuntimeError("max_tokens must be an integer.")

        if max_tokens_value <= 0:
            raise RuntimeError("max_tokens must be a positive integer.")

        upstream_stream_required = (
            max_tokens_value > _ANTHROPIC_STREAMING_REQUIRED_MAX_TOKENS
        )
        upstream_stream = ui_stream or upstream_stream_required

        if max_tokens_value > effective_max_output_tokens:
            raise RuntimeError(
                f"max_tokens exceeds model limit. model={upstream_model}. "
                f"max_tokens={max_tokens_value}, max_output_tokens={effective_max_output_tokens}."
            )

        if request.get("temperature") is not None and request.get("top_p") is not None:
            raise RuntimeError(
                "temperature and top_p are mutually exclusive. Unset one of them."
            )

        if valves.metadata_user_id is not None and valves.metadata_user_id.strip():
            request["metadata"] = {"user_id": valves.metadata_user_id.strip()}
        if valves.service_tier is not None:
            request["service_tier"] = valves.service_tier

        if valves.interleaved_thinking_enabled:
            # _coerce_user_valves_for_model で model capability と依存関係を満たすよう調整済み.
            if not valves.thinking_enabled:
                raise RuntimeError(
                    "interleaved_thinking_enabled requires thinking_enabled=true."
                )

        if valves.thinking_enabled:
            if request.get("temperature") is not None:
                raise RuntimeError(
                    "extended thinking cannot be combined with temperature. "
                    "Unset temperature or disable thinking_enabled."
                )
            if request.get("top_k") is not None:
                raise RuntimeError(
                    "extended thinking cannot be combined with top_k. "
                    "Unset top_k or disable thinking_enabled."
                )
            if request.get("top_p") is not None:
                try:
                    top_p_value = float(request.get("top_p"))
                except (TypeError, ValueError):
                    raise RuntimeError(
                        "top_p must be a number when provided."
                    ) from None
                if top_p_value < 0.95 or top_p_value > 1.0:
                    raise RuntimeError(
                        "extended thinking requires top_p to be in [0.95, 1.0] when top_p is provided."
                    )
            budget: int | None
            mode = (valves.thinking_budget_mode or "auto").strip().lower()
            if mode not in ("auto", "manual"):
                raise RuntimeError("thinking_budget_mode must be 'auto' or 'manual'.")

            if mode == "auto":
                if valves.thinking_budget_tokens is not None and not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "thinking_budget_mode=auto のため thinking_budget_tokens を無視します. "
                                "手動指定したい場合は thinking_budget_mode=manual に変更してください."
                            ),
                            "done": True,
                        },
                    )
                budget = _auto_thinking_budget_tokens(max_tokens=max_tokens_value)
            else:
                budget = valves.thinking_budget_tokens
                if budget is None:
                    raise RuntimeError(
                        "thinking_budget_mode=manual requires thinking_budget_tokens."
                    )
            if budget < 1024:
                raise RuntimeError(
                    "thinking_budget_tokens must be >= 1024 for extended thinking."
                )
            if max_tokens_value <= 1024:
                raise RuntimeError("extended thinking requires max_tokens > 1024.")
            # budget_tokens は max_tokens の枠を消費する前提なので, 常に budget < max_tokens に丸める.
            # interleaved thinking + web_search の組み合わせでも例外にしない（API仕様差分で壊れやすいため）.
            if budget >= max_tokens_value:
                new_budget = max(1024, max_tokens_value - 1)
                if new_budget != budget and not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                f"thinking_budget_tokens({budget}) >= max_tokens({max_tokens_value}) のため "
                                f"{new_budget} に調整しました."
                            ),
                            "done": True,
                        },
                    )
                budget = new_budget
            if filtered and filtered[-1].get("role") == "assistant":
                raise RuntimeError(
                    "extended thinking cannot be combined with prefill (messages ending with assistant role)."
                )
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}

        if valves.effort_enabled:
            request["output_config"] = {"effort": valves.effort_level}

        if provider_web_search:
            if valves.web_search_max_uses <= 0:
                raise RuntimeError("web_search_max_uses must be a positive integer.")
            request["tools"] = [
                {
                    "type": valves.web_search_tool_type,
                    "name": valves.web_search_tool_name,
                    "max_uses": int(valves.web_search_max_uses),
                }
            ]
            if provider_web_search_required:
                request["tool_choice"] = {
                    "type": "tool",
                    "name": valves.web_search_tool_name,
                }

        if valves.tool_choice_disable_parallel_tool_use is not None:
            disable_parallel = bool(valves.tool_choice_disable_parallel_tool_use)
            tool_choice = request.get("tool_choice")
            if isinstance(tool_choice, dict):
                tool_choice["disable_parallel_tool_use"] = disable_parallel
                request["tool_choice"] = tool_choice
            else:
                request["tool_choice"] = {
                    "type": "auto",
                    "disable_parallel_tool_use": disable_parallel,
                }

        if valves.extra_json:
            try:
                override = json.loads(valves.extra_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "extra_json is not valid JSON: " + (exc.msg or "invalid JSON")
                ) from None
            if not isinstance(override, dict):
                raise ValueError("extra_json must be a JSON object.")
            allowed_top_keys = {
                "metadata",
                "service_tier",
                "output_config",
                "tool_choice",
                "tools",
            }
            unknown = sorted([k for k in override.keys() if k not in allowed_top_keys])
            if unknown:
                raise RuntimeError(
                    "extra_json contains unsupported top-level keys: "
                    + ", ".join(unknown)
                    + ". Allowed keys: "
                    + ", ".join(sorted(allowed_top_keys))
                )

            if "metadata" in override and isinstance(override.get("metadata"), dict):
                merged_meta = dict(request.get("metadata") or {})
                merged_meta.update(override["metadata"])
                request["metadata"] = merged_meta

            if "service_tier" in override and override.get("service_tier") is not None:
                request["service_tier"] = override["service_tier"]

            if "output_config" in override and isinstance(
                override.get("output_config"), dict
            ):
                merged_output_config = dict(request.get("output_config") or {})
                merged_output_config.update(override["output_config"])
                request["output_config"] = merged_output_config

            if "tool_choice" in override and isinstance(
                override.get("tool_choice"), dict
            ):
                merged_tool_choice = dict(request.get("tool_choice") or {})
                merged_tool_choice.update(override["tool_choice"])
                request["tool_choice"] = merged_tool_choice

            if "tools" in override:
                if not provider_web_search or not isinstance(
                    request.get("tools"), list
                ):
                    raise RuntimeError(
                        "extra_json.tools is only supported when provider web search is enabled."
                    )
                tools_override = override.get("tools")
                if not (isinstance(tools_override, list) and tools_override):
                    raise RuntimeError("extra_json.tools must be a non-empty list.")
                first = tools_override[0]
                if not isinstance(first, dict):
                    raise RuntimeError("extra_json.tools[0] must be an object.")
                allowed_tool_keys = {
                    "allowed_domains",
                    "blocked_domains",
                    "user_location",
                    "max_uses",
                }
                unknown_tool = sorted(
                    [k for k in first.keys() if k not in allowed_tool_keys]
                )
                if unknown_tool:
                    raise RuntimeError(
                        "extra_json.tools[0] contains unsupported keys: "
                        + ", ".join(unknown_tool)
                        + ". Allowed keys: "
                        + ", ".join(sorted(allowed_tool_keys))
                    )
                tool0 = dict(request["tools"][0])  # type: ignore[index]
                tool0.update(first)
                request["tools"][0] = tool0  # type: ignore[index]

            tool_choice = request.get("tool_choice")
            if isinstance(tool_choice, dict):
                tool_choice_type = tool_choice.get("type")
                if valves.thinking_enabled and tool_choice_type in ("any", "tool"):
                    raise RuntimeError(
                        "extended thinking is incompatible with forced tool use (tool_choice=any/tool). "
                        "Use tool_choice=auto/none or disable thinking_enabled."
                    )
                if tool_choice_type == "tool":
                    tool_name = tool_choice.get("name")
                    if not isinstance(tool_name, str) or not tool_name.strip():
                        raise RuntimeError(
                            "tool_choice.name must be a non-empty string when tool_choice.type='tool'."
                        )
                    tools = request.get("tools")
                    if not (
                        isinstance(tools, list)
                        and tools
                        and isinstance(tools[0], dict)
                        and isinstance(tools[0].get("name"), str)
                    ):
                        raise RuntimeError(
                            "tool_choice.type='tool' requires tools to be provided."
                        )
                    tool0_name = tools[0].get("name")  # type: ignore[assignment]
                    if tool_name != tool0_name:
                        raise RuntimeError(
                            "tool_choice.name must match tools[0].name. "
                            f"tool_choice.name={tool_name}, tools[0].name={tool0_name}."
                        )
                    if provider_web_search and tool_name != valves.web_search_tool_name:
                        raise RuntimeError(
                            "When provider web search is enabled, tool_choice must target the web_search tool. "
                            f"tool_choice.name={tool_name}, expected={valves.web_search_tool_name}."
                        )

        def _looks_like_unexpected_kwarg_error(exc: BaseException) -> bool:
            text = str(exc)
            return (
                "unexpected keyword argument" in text
                or "got an unexpected keyword argument" in text
            )

        # anthropic SDK と API の引数がズレることがあるため, 必要に応じて HTTP フォールバックする.
        use_http_fallback = False
        http_beta_headers = list(extra_beta_headers)
        if valves.anthropic_beta_header:
            http_beta_headers.extend(
                [
                    h.strip()
                    for h in valves.anthropic_beta_header.split(",")
                    if h.strip()
                ]
            )
        api_key = _anthropic_api_key_from_env(valves)
        async_client: Any | None = None
        if not use_http_fallback:
            async_client = self._async_client(
                valves, extra_beta_headers=extra_beta_headers
            )

        # NOTE: RAGのテンプレ自体も "### Task:" から始まるため, promptパターンではなく metadata.task のみで判定する.
        if pdf_payloads and not is_metadata_task:
            uploaded_ids: dict[str, str] = {}
            document_blocks: list[dict[str, Any]] = []
            for payload in pdf_payloads:
                openwebui_file_id = payload.get("openwebui_file_id")
                filename = payload.get("filename", "")
                content_type = payload.get("content_type", "")
                data = payload.get("data", b"")
                if not isinstance(openwebui_file_id, str) or not openwebui_file_id:
                    continue
                if not isinstance(data, (bytes, bytearray)) or not data:
                    continue

                # 可能なら Files API に upload して file_id 参照にする（失敗時は base64 fallback）.
                upstream_file_id: str | None = None
                if valves.files_api_enabled:
                    try:
                        upstream_file_id = uploaded_ids.get(openwebui_file_id)
                        if upstream_file_id is None:
                            upstream_file_id = await _anthropic_files_create_via_http(
                                api_key=api_key,
                                beta_headers=http_beta_headers,
                                filename=str(filename or "document.pdf"),
                                content_type=str(content_type or "application/pdf"),
                                data=bytes(data),
                            )
                            uploaded_ids[openwebui_file_id] = upstream_file_id
                    except Exception:
                        upstream_file_id = None

                if upstream_file_id:
                    document_blocks.append(
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": upstream_file_id},
                        }
                    )
                    continue

                encoded = base64.b64encode(bytes(data)).decode("ascii")
                document_blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                    }
                )

            if document_blocks:
                messages_obj = request.get("messages")
                if isinstance(messages_obj, list):
                    target: dict[str, Any] | None = None
                    for msg in reversed(messages_obj):
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            target = msg
                            break
                    if target is None:
                        target = {"role": "user", "content": []}
                        messages_obj.append(target)
                    existing = target.get("content")
                    if not isinstance(existing, list):
                        existing = []
                    target["content"] = [*document_blocks, *existing]
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "info",
                            "description": (
                                "添付PDFを視覚入力として使用します（"
                                + str(len(document_blocks))
                                + "件）."
                            ),
                            "done": True,
                        },
                    )

        async def _iter_events_via_async_sdk(
            call_request: dict[str, Any],
        ) -> AsyncGenerator[Any, None]:
            if async_client is None:
                raise RuntimeError("AsyncAnthropic client is not available.")

            stream_call = async_client.messages.stream(**call_request)

            # SDK 実装差分を吸収する:
            # - async context manager を返す
            # - awaitable を返す
            # - async iterator を返す
            if hasattr(stream_call, "__aenter__"):
                async with stream_call as events:
                    async for event in events:
                        yield event
                return

            if inspect.isawaitable(stream_call):
                events = await stream_call
                if hasattr(events, "__aiter__"):
                    async for event in events:
                        yield event
                else:
                    for event in events:
                        yield event
                return

            async for event in stream_call:
                yield event

        if valves.debug_enabled:
            if not _is_admin_user(__user__):
                raise RuntimeError("debug_enabled requires an admin user.")
            if valves.debug_include_request:
                await _emit_debug_parameters(
                    __event_emitter__,
                    provider="anthropic",
                    title="request",
                    parameters={
                        "model_id": model_id,
                        "upstream_model": upstream_model,
                        "request": request,
                    },
                    max_string_length=valves.debug_max_string_length,
                    max_depth=valves.debug_max_depth,
                )

        if upstream_stream:
            stream_result: dict[str, Any] | None = None

            async def stream() -> AsyncGenerator[Any, None]:
                nonlocal stream_result
                content_block_types: dict[int, str] = {}
                text_tail = ""
                reasoning_tail = ""
                capture_full_response = bool(
                    (valves.debug_enabled and valves.debug_include_response)
                    or not ui_stream
                )
                full_text_parts: list[str] | None = (
                    [] if capture_full_response else None
                )
                full_reasoning_parts: list[str] | None = (
                    [] if capture_full_response else None
                )
                url_tail = ""
                provider_items_by_url: dict[str, dict[str, str]] = {}
                seen_queries: set[str] = set()
                web_search_error_codes: list[str] = []
                web_search_started = bool(provider_web_search_required)
                stop_reason: str | None = None
                max_attempts = 1
                if provider_web_search:
                    max_attempts = 1 + max(0, int(valves.web_search_retry_max_attempts))
                attempt_index = 0
                stream_request: dict[str, Any] = request
                retried_without_documents = False
                retried_without_images = False

                class _RetryWebSearchUnavailable(Exception):
                    pass

                def _retry_delay_seconds() -> float:
                    base = float(valves.web_search_retry_base_delay_seconds)
                    max_delay = float(valves.web_search_retry_max_delay_seconds)
                    if base <= 0:
                        base = 0.1
                    if max_delay <= 0:
                        max_delay = base
                    delay = min(max_delay, base * (2**attempt_index))
                    return float(delay + (random.random() * base))

                async def handle_event(
                    event: Any,
                ) -> AsyncGenerator[Any, None]:
                    nonlocal url_tail, web_search_started, stop_reason, text_tail, reasoning_tail

                    event_type = getattr(event, "type", None) or (
                        event.get("type") if isinstance(event, dict) else None
                    )

                    if provider_web_search:
                        for query in _deep_find_queries(event):
                            if query in seen_queries:
                                continue
                            seen_queries.add(query)
                            if not web_search_started:
                                web_search_started = True
                                await _emit_status(
                                    __event_emitter__,
                                    {
                                        "action": "web_search",
                                        "description": "Searching the web",
                                        "done": False,
                                    },
                                )
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "web_search_queries_generated",
                                    "queries": [_format_query_for_status(query)],
                                    "done": False,
                                },
                            )

                        for code in _deep_find_web_search_tool_errors(event):
                            if code not in web_search_error_codes:
                                web_search_error_codes.append(code)
                            if (
                                code == "unavailable"
                                and attempt_index < (max_attempts - 1)
                                and not is_background_task
                            ):
                                await _emit_status(
                                    __event_emitter__,
                                    {
                                        "action": "warning",
                                        "description": (
                                            "Web search tool error: unavailable. "
                                            f"Retrying ({attempt_index + 2}/{max_attempts})."
                                        ),
                                        "done": True,
                                    },
                                )
                                raise _RetryWebSearchUnavailable()

                        items = _deep_find_citation_items(event)
                        # verified citation は Web Search tool の実行痕跡がある場合のみ採用する.
                        if items and _deep_has_web_search_tool_activity(event):
                            for item in items:
                                url = (
                                    item.get("url") or item.get("link") or ""
                                ).strip()
                                if not url or url in provider_items_by_url:
                                    continue
                                provider_items_by_url[url] = item
                            if not is_background_task:
                                await _emit_citations_from_items(
                                    __event_emitter__,
                                    items=list(provider_items_by_url.values()),
                                    seen_urls=seen_urls,
                                )

                    if event_type == "content_block_start":
                        index = getattr(event, "index", None) or (
                            event.get("index") if isinstance(event, dict) else None
                        )
                        block = getattr(event, "content_block", None) or (
                            event.get("content_block")
                            if isinstance(event, dict)
                            else None
                        )
                        block_type = getattr(block, "type", None) or (
                            block.get("type") if isinstance(block, dict) else None
                        )
                        if isinstance(index, int) and isinstance(block_type, str):
                            content_block_types[index] = block_type
                            if block_type == "redacted_thinking":
                                if full_reasoning_parts is not None:
                                    full_reasoning_parts.append("（redacted thinking）")
                                reasoning_tail = (
                                    reasoning_tail + "（redacted thinking）"
                                )[-20000:]
                                yield openai_chat_chunk_message_template(
                                    model_id, reasoning_content="（redacted thinking）"
                                )
                        return

                    if event_type == "content_block_delta":
                        index = getattr(event, "index", None) or (
                            event.get("index") if isinstance(event, dict) else None
                        )
                        delta = getattr(event, "delta", None) or (
                            event.get("delta") if isinstance(event, dict) else None
                        )
                        delta_type = getattr(delta, "type", None) or (
                            delta.get("type") if isinstance(delta, dict) else None
                        )
                        block_type = (
                            content_block_types.get(index, "")
                            if isinstance(index, int)
                            else ""
                        )

                        if delta_type == "text_delta" or block_type == "text":
                            text = getattr(delta, "text", None) or (
                                delta.get("text") if isinstance(delta, dict) else None
                            )
                            if isinstance(text, str) and text:
                                if full_text_parts is not None:
                                    full_text_parts.append(text)
                                text_tail = (text_tail + text)[-20000:]
                                url_tail = (url_tail + text)[-2000:]
                                if not is_background_task and not provider_web_search:
                                    await _emit_unverified_citations_from_text(
                                        __event_emitter__,
                                        text=url_tail,
                                        seen_urls=seen_urls,
                                    )
                                yield openai_chat_chunk_message_template(
                                    model_id, content=text
                                )
                        elif delta_type in (
                            "thinking_delta",
                            "reasoning_delta",
                        ) or block_type in (
                            "thinking",
                            "reasoning",
                        ):
                            thought = (
                                getattr(delta, "thinking", None)
                                or getattr(delta, "text", None)
                                or (
                                    delta.get("thinking")
                                    if isinstance(delta, dict)
                                    else None
                                )
                                or (
                                    delta.get("text")
                                    if isinstance(delta, dict)
                                    else None
                                )
                            )
                            if isinstance(thought, str) and thought:
                                if full_reasoning_parts is not None:
                                    full_reasoning_parts.append(thought)
                                reasoning_tail = (reasoning_tail + thought)[-20000:]
                                yield openai_chat_chunk_message_template(
                                    model_id, reasoning_content=thought
                                )
                        return

                    if event_type == "message_delta":
                        delta = getattr(event, "delta", None) or (
                            event.get("delta") if isinstance(event, dict) else None
                        )
                        candidate = getattr(delta, "stop_reason", None) or (
                            delta.get("stop_reason")
                            if isinstance(delta, dict)
                            else None
                        )
                        if isinstance(candidate, str) and candidate.strip():
                            stop_reason = candidate.strip()
                        return

                while True:
                    try:
                        if use_http_fallback:
                            async for event in _anthropic_stream_via_http(
                                api_key=api_key,
                                beta_headers=http_beta_headers,
                                request={**stream_request, "stream": True},
                            ):
                                async for chunk in handle_event(event):
                                    yield chunk
                        else:
                            try:
                                async for event in _iter_events_via_async_sdk(
                                    stream_request
                                ):
                                    async for chunk in handle_event(event):
                                        yield chunk
                            except TypeError as exc:
                                if not _looks_like_unexpected_kwarg_error(exc):
                                    raise
                                async for event in _anthropic_stream_via_http(
                                    api_key=api_key,
                                    beta_headers=http_beta_headers,
                                    request={**stream_request, "stream": True},
                                ):
                                    async for chunk in handle_event(event):
                                        yield chunk
                        break
                    except _RetryWebSearchUnavailable:
                        attempt_index += 1
                        if attempt_index >= max_attempts:
                            break
                        await asyncio.sleep(_retry_delay_seconds())
                        content_block_types.clear()
                        url_tail = ""
                        text_tail = ""
                        reasoning_tail = ""
                        if full_text_parts is not None:
                            full_text_parts.clear()
                        if full_reasoning_parts is not None:
                            full_reasoning_parts.clear()
                        provider_items_by_url.clear()
                        seen_queries.clear()
                        web_search_error_codes.clear()
                        web_search_started = bool(provider_web_search_required)
                        stop_reason = None
                        continue
                    except Exception as exc:
                        error_text = str(exc).lower()
                        if (
                            not retried_without_documents
                            and "document" in error_text
                            and (
                                "not supported" in error_text
                                or "unknown" in error_text
                                or "unrecognized" in error_text
                                or "invalid" in error_text
                                or "unsupported" in error_text
                            )
                        ):
                            retried_without_documents = True
                            stream_request = _strip_anthropic_content_block_types(
                                dict(stream_request), remove_types={"document"}
                            )
                            if not is_background_task:
                                await _emit_status(
                                    __event_emitter__,
                                    {
                                        "action": "warning",
                                        "description": (
                                            "document input が非対応のため, 添付PDFを除外して再試行しました."
                                        ),
                                        "done": True,
                                    },
                                )
                            continue
                        if (
                            not retried_without_images
                            and "image" in error_text
                            and (
                                "not supported" in error_text
                                or "unknown" in error_text
                                or "unrecognized" in error_text
                                or "invalid" in error_text
                                or "unsupported" in error_text
                            )
                        ):
                            retried_without_images = True
                            stream_request = _strip_anthropic_content_block_types(
                                dict(stream_request), remove_types={"image"}
                            )
                            if not is_background_task:
                                await _emit_status(
                                    __event_emitter__,
                                    {
                                        "action": "warning",
                                        "description": (
                                            "image input が非対応のため, 画像入力を除外して再試行しました."
                                        ),
                                        "done": True,
                                    },
                                )
                            continue
                        if not is_background_task:
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "error",
                                    "description": (
                                        "Streaming error: "
                                        + type(exc).__name__
                                        + ": "
                                        + str(exc)[:200]
                                    ),
                                    "done": True,
                                    "error": True,
                                },
                            )
                        if provider_web_search and not is_background_task:
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "web_search",
                                    "description": "Web search may be incomplete due to a streaming error",
                                    "queries": [web_search_query],
                                    "done": True,
                                    "error": True,
                                },
                            )
                        raise exc

                if provider_web_search:
                    provider_items = list(provider_items_by_url.values())
                    web_search_used = (
                        bool(seen_queries)
                        or bool(provider_items)
                        or bool(web_search_error_codes)
                    )
                    urls = [
                        i.get("url", "")
                        for i in provider_items
                        if isinstance(i.get("url"), str) and i.get("url")
                    ]
                    if not web_search_used:
                        if provider_web_search_required:
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "web_search",
                                    "description": "Web search was required but not executed",
                                    "done": True,
                                    "error": True,
                                },
                            )
                    elif web_search_error_codes:
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "web_search",
                                "description": f"Web search tool error: {', '.join(web_search_error_codes)}",
                                "urls": urls,
                                "items": (
                                    [
                                        {
                                            "link": i.get("url", ""),
                                            "title": i.get("title", ""),
                                            "snippet": i.get("snippet", ""),
                                        }
                                        for i in provider_items
                                        if isinstance(i.get("url"), str)
                                        and i.get("url")
                                    ]
                                    if provider_items
                                    else [{"link": url} for url in urls]
                                ),
                                "done": True,
                                "error": True,
                            },
                        )
                        if not is_background_task and urls:
                            if provider_items:
                                await _emit_citations_from_items(
                                    __event_emitter__,
                                    items=provider_items,
                                    seen_urls=seen_urls,
                                )
                            else:
                                await _emit_citations_from_urls(
                                    __event_emitter__, urls=urls, seen_urls=seen_urls
                                )
                    elif urls:
                        status_items = (
                            [
                                {
                                    "link": i.get("url", ""),
                                    "title": i.get("title", ""),
                                    "snippet": i.get("snippet", ""),
                                }
                                for i in provider_items
                                if isinstance(i.get("url"), str) and i.get("url")
                            ]
                            if provider_items
                            else [{"link": url} for url in urls]
                        )
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "web_search",
                                "description": "Searched {{count}} sites",
                                "urls": urls,
                                "items": status_items,
                                "done": True,
                            },
                        )
                        if not is_background_task:
                            if provider_items:
                                await _emit_citations_from_items(
                                    __event_emitter__,
                                    items=provider_items,
                                    seen_urls=seen_urls,
                                )
                            else:
                                await _emit_citations_from_urls(
                                    __event_emitter__, urls=urls, seen_urls=seen_urls
                                )
                    else:
                        if provider_web_search_required:
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "web_search",
                                    "description": "No search results found",
                                    "done": True,
                                    "error": True,
                                },
                            )

                if not is_background_task and not web_search_used:
                    await _emit_unverified_citations_from_text(
                        __event_emitter__,
                        text=(
                            "".join(full_text_parts)
                            if full_text_parts is not None
                            else text_tail
                        ),
                        seen_urls=seen_urls,
                    )

                if stop_reason in ("pause_turn", "refusal") and not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "Anthropic " + _stop_reason_note(stop_reason)
                            ),
                            "done": True,
                        },
                    )

                if valves.debug_enabled and valves.debug_include_response:
                    await _emit_debug_parameters(
                        __event_emitter__,
                        provider="anthropic",
                        title="response",
                        parameters={
                            "text": (
                                "".join(full_text_parts)
                                if full_text_parts is not None
                                else text_tail
                            ),
                            "reasoning": (
                                "".join(full_reasoning_parts)
                                if full_reasoning_parts is not None
                                else reasoning_tail
                            ),
                            "web_search_items": list(provider_items_by_url.values()),
                        },
                        max_string_length=valves.debug_max_string_length,
                        max_depth=valves.debug_max_depth,
                    )

                stream_result = {
                    "content": (
                        "".join(full_text_parts)
                        if full_text_parts is not None
                        else text_tail
                    ),
                    "reasoning": (
                        "".join(full_reasoning_parts)
                        if full_reasoning_parts is not None
                        else reasoning_tail
                    ),
                }

                yield openai_chat_chunk_message_template(model_id)
                yield "data: [DONE]"

            if ui_stream:
                return stream()

            async for _ in stream():
                pass

            if not isinstance(stream_result, dict):
                raise RuntimeError("Streaming finished without a result.")

            content = stream_result.get("content")
            reasoning = stream_result.get("reasoning")
            if not isinstance(content, str):
                content = ""
            if not isinstance(reasoning, str):
                reasoning = ""

            return openai_chat_completion_message_template(
                model_id,
                message=content,
                reasoning_content=reasoning or None,
            )

        create_max_attempts = 1
        if provider_web_search:
            create_max_attempts = 1 + max(0, int(valves.web_search_retry_max_attempts))
        create_attempt_index = 0
        create_request: dict[str, Any] = request
        retried_without_documents = False
        retried_without_images = False

        while True:
            try:
                if use_http_fallback:
                    message = await _anthropic_create_via_http(
                        api_key=api_key,
                        beta_headers=http_beta_headers,
                        request=create_request,
                    )
                else:
                    try:
                        if async_client is None:
                            raise RuntimeError(
                                "AsyncAnthropic client is not available."
                            )
                        message = await async_client.messages.create(**create_request)
                    except TypeError as exc:
                        if not _looks_like_unexpected_kwarg_error(exc):
                            raise
                        message = await _anthropic_create_via_http(
                            api_key=api_key,
                            beta_headers=http_beta_headers,
                            request=create_request,
                        )
            except Exception as exc:
                error_text = str(exc).lower()
                if (
                    not retried_without_documents
                    and "document" in error_text
                    and (
                        "not supported" in error_text
                        or "unknown" in error_text
                        or "unrecognized" in error_text
                        or "invalid" in error_text
                        or "unsupported" in error_text
                    )
                ):
                    retried_without_documents = True
                    create_request = _strip_anthropic_content_block_types(
                        dict(create_request), remove_types={"document"}
                    )
                    if not is_background_task:
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "warning",
                                "description": (
                                    "document input が非対応のため, 添付PDFを除外して再試行しました."
                                ),
                                "done": True,
                            },
                        )
                    continue
                if (
                    not retried_without_images
                    and "image" in error_text
                    and (
                        "not supported" in error_text
                        or "unknown" in error_text
                        or "unrecognized" in error_text
                        or "invalid" in error_text
                        or "unsupported" in error_text
                    )
                ):
                    retried_without_images = True
                    create_request = _strip_anthropic_content_block_types(
                        dict(create_request), remove_types={"image"}
                    )
                    if not is_background_task:
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "warning",
                                "description": (
                                    "image input が非対応のため, 画像入力を除外して再試行しました."
                                ),
                                "done": True,
                            },
                        )
                    continue
                raise

            if provider_web_search and create_attempt_index < (create_max_attempts - 1):
                codes = _deep_find_web_search_tool_errors(message)
                if "unavailable" in codes and not is_background_task:
                    create_attempt_index += 1
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "Web search tool error: unavailable. "
                                f"Retrying ({create_attempt_index + 1}/{create_max_attempts})."
                            ),
                            "done": True,
                        },
                    )
                    base = float(valves.web_search_retry_base_delay_seconds)
                    max_delay = float(valves.web_search_retry_max_delay_seconds)
                    if base <= 0:
                        base = 0.1
                    if max_delay <= 0:
                        max_delay = base
                    delay = min(max_delay, base * (2 ** (create_attempt_index - 1)))
                    delay = float(delay + (random.random() * base))
                    await asyncio.sleep(delay)
                    continue
            break

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        for block in _get_message_content_blocks(message):
            block_type = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if block_type == "text":
                text = getattr(block, "text", None) or (
                    block.get("text") if isinstance(block, dict) else ""
                )
                if text:
                    text_parts.append(text)
            elif block_type in ("thinking", "reasoning"):
                thought = (
                    getattr(block, "thinking", None)
                    or getattr(block, "text", None)
                    or (block.get("thinking") if isinstance(block, dict) else None)
                    or (block.get("text") if isinstance(block, dict) else None)
                )
                if isinstance(thought, str) and thought:
                    thinking_parts.append(thought)
            elif block_type == "redacted_thinking":
                thinking_parts.append("（redacted thinking）")

        content = "".join(text_parts)
        reasoning = "".join(thinking_parts) if thinking_parts else None
        stop_reason = _get_message_stop_reason(message)
        if stop_reason in ("pause_turn", "refusal") and not is_background_task:
            await _emit_status(
                __event_emitter__,
                {
                    "action": "warning",
                    "description": ("Anthropic " + _stop_reason_note(stop_reason)),
                    "done": True,
                },
            )
        input_tokens, output_tokens = _get_message_usage_tokens(message)
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

        if not is_background_task and not provider_web_search:
            await _emit_unverified_citations_from_text(
                __event_emitter__, text=content, seen_urls=seen_urls
            )

        if provider_web_search:
            provider_items = _deep_find_citation_items(message)
            web_search_error_codes = _deep_find_web_search_tool_errors(message)
            if provider_items and not _deep_has_web_search_tool_activity(message):
                provider_items = []
            web_search_used = (
                bool(provider_items)
                or bool(web_search_error_codes)
                or _deep_has_web_search_tool_activity(message)
                or bool(_deep_find_queries(message))
            )

            urls = [
                i.get("url", "")
                for i in provider_items
                if isinstance(i.get("url"), str) and i.get("url")
            ]

            if not web_search_used:
                if provider_web_search_required:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "web_search",
                            "description": "Web search was required but not executed",
                            "done": True,
                            "error": True,
                        },
                    )
            elif web_search_error_codes:
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "web_search",
                        "description": f"Web search tool error: {', '.join(web_search_error_codes)}",
                        "urls": urls,
                        "items": [{"link": url} for url in urls],
                        "done": True,
                        "error": True,
                    },
                )
                if not is_background_task and urls:
                    await _emit_citations_from_urls(
                        __event_emitter__, urls=urls, seen_urls=seen_urls
                    )
            elif urls:
                status_items = [
                    {
                        "link": i.get("url", ""),
                        "title": i.get("title", ""),
                        "snippet": i.get("snippet", ""),
                    }
                    for i in provider_items
                    if isinstance(i.get("url"), str) and i.get("url")
                ]
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "web_search",
                        "description": "Searched {{count}} sites",
                        "urls": urls,
                        "items": (
                            status_items
                            if status_items
                            else [{"link": url} for url in urls]
                        ),
                        "done": True,
                    },
                )
                if not is_background_task:
                    await _emit_citations_from_items(
                        __event_emitter__, items=provider_items, seen_urls=seen_urls
                    )
            elif provider_web_search_required:
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "web_search",
                        "description": "No search results found",
                        "done": True,
                        "error": True,
                    },
                )
            if not is_background_task and not web_search_used:
                await _emit_unverified_citations_from_text(
                    __event_emitter__, text=content, seen_urls=seen_urls
                )

        if valves.debug_enabled and valves.debug_include_response:
            await _emit_debug_parameters(
                __event_emitter__,
                provider="anthropic",
                title="response",
                parameters={"response": _to_plain_for_debug(message)},
                max_string_length=valves.debug_max_string_length,
                max_depth=valves.debug_max_depth,
            )

        return openai_chat_completion_message_template(
            model_id,
            message=content,
            reasoning_content=reasoning,
            usage=usage,
        )
