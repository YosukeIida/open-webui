# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import base64
import json
import os
from typing import Any

from open_webui.utils.misc import (
    openai_chat_chunk_message_template,
    openai_chat_completion_message_template,
)
from pydantic import BaseModel
from starlette.requests import Request

from webui_functions_src.openai_responses.config import UserValves, Valves
from webui_functions_src.openai_responses.emit import (
    _emit_debug_parameters,
    _emit_search_result_citations_from_source_items,
    _emit_search_result_info_sources,
    _emit_status,
    _emit_unverified_citations_from_text,
    _emit_verified_citations_from_url_citations,
    _is_admin_user,
)
from webui_functions_src.openai_responses.models import _compile_filter, _passes_filter
from webui_functions_src.openai_responses.normalize import (
    _extract_openai_messages,
    _format_query_for_status,
    _last_user_text,
    _looks_like_background_task_prompt,
    _to_responses_input,
)
from webui_functions_src.openai_responses.upstream_http import (
    _files_create_via_http,
    _models_list_via_http,
    _openai_base_url,
    _responses_create_via_http,
    _responses_stream_via_http,
)
from webui_functions_src.openai_responses.upstream_parse import (
    _collect_output_text,
    _collect_reasoning_summary,
    _collect_url_citations,
)
from webui_functions_src.openai_responses.web_search import (
    _collect_web_search_activity,
    _extract_web_search_call_items_from_event,
    _web_search_sources_from_call_item,
)

try:
    from openai import AsyncOpenAI, OpenAI
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment]
    OpenAI = None  # type: ignore[assignment]


def _strip_responses_input_content_types(
    request: dict[str, Any], *, remove_types: set[str]
) -> dict[str, Any]:
    if not remove_types:
        return request
    items = request.get("input")
    if not isinstance(items, list):
        return request
    new_items: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            new_items.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list):
            new_items.append(item)
            continue
        filtered_content: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in remove_types:
                continue
            filtered_content.append(part)
        if filtered_content is content:
            new_items.append(item)
        else:
            copied = dict(item)
            copied["content"] = filtered_content
            new_items.append(copied)
    copied_request = dict(request)
    copied_request["input"] = new_items
    return copied_request


def _load_open_webui_file_bytes(
    file_id: str, *, __user__: dict | None, max_bytes: int
) -> tuple[bytes, str, str]:
    """
    Open WebUI の Files storage から raw bytes を取得する.
    - 権限: owner/admin/knowledge access のみ許可する.
    - サイズ: max_bytes を超える場合は例外にする.
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
            f"File too large for upstream file input. file_id={file_id} size={size} max_bytes={max_bytes}"
        )

    if not getattr(file, "path", None):
        raise RuntimeError(f"File path is missing. file_id={file_id}")
    disk_path = Storage.get_file(file.path)
    with open(disk_path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(
            f"File too large for upstream file input. file_id={file_id} read_bytes>{max_bytes}"
        )
    return data, content_type, filename


class Pipe:
    valves: Valves
    UserValves = UserValves

    def __init__(self) -> None:
        self.valves = Valves()

    @property
    def name(self) -> str:
        return "OpenAI Responses: "

    def _effective_valves(self, __user__: dict | None) -> Valves:
        if not isinstance(__user__, dict):
            return self.valves

        user_valves = __user__.get("valves")
        if user_valves is None:
            return self.valves

        raw: dict[str, Any] | None = None
        if isinstance(user_valves, BaseModel):
            raw = user_valves.model_dump(exclude_none=True)
        elif isinstance(user_valves, dict):
            raw = {k: v for k, v in user_valves.items() if v is not None}
        else:
            return self.valves

        allowed = set(getattr(UserValves, "model_fields", {}).keys())
        filtered = {k: v for k, v in (raw or {}).items() if k in allowed}
        validated = UserValves(**filtered).model_dump(
            exclude_none=True, exclude_defaults=True
        )
        merged = {**self.valves.model_dump(), **validated}
        return Valves(**merged)

    def _sync_client(self, valves: Valves) -> Any:
        if OpenAI is None:
            return None
        api_key = os.environ.get(valves.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")

        base_url = _openai_base_url(valves)
        try:
            return OpenAI(api_key=api_key, base_url=base_url)
        except TypeError:
            return OpenAI(api_key=api_key)

    def _async_client(self, valves: Valves) -> Any:
        if AsyncOpenAI is None:
            return None
        api_key = os.environ.get(valves.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")

        base_url = _openai_base_url(valves)
        try:
            return AsyncOpenAI(api_key=api_key, base_url=base_url)
        except TypeError:
            return AsyncOpenAI(api_key=api_key)

    def pipes(self) -> list[dict[str, str]]:
        model_filter = _compile_filter(
            self.valves.model_allow_regex, self.valves.model_deny_regex
        )
        models: list[dict[str, str]] = []

        client = None
        try:
            client = self._sync_client(self.valves)
        except Exception:
            client = None

        list_fn = (
            getattr(getattr(client, "models", None), "list", None) if client else None
        )
        if callable(list_fn):
            try:
                resp = list_fn()
            except Exception:
                resp = None

            data = None
            if resp is not None:
                data = getattr(resp, "data", None)
                if data is None and isinstance(resp, dict):
                    data = resp.get("data")

            # openai SDKは data(list) を返すことが多いが, iterable を直接返す実装もあるため両対応する.
            candidates: list[Any] = []
            if isinstance(data, list):
                candidates = data
            elif resp is not None and not isinstance(resp, dict):
                try:
                    candidates = list(resp)
                except TypeError:
                    candidates = []

            for m in candidates:
                model_id = getattr(m, "id", None) or (
                    m.get("id") if isinstance(m, dict) else None
                )
                if not isinstance(model_id, str) or not model_id:
                    continue
                if not _passes_filter(model_id, model_filter):
                    continue
                models.append({"id": model_id, "name": model_id})
            if models:
                return models

        # SDK 経路が使えない/失敗した場合は Models API を直に叩いて取得する.
        try:
            api_key = os.environ.get(self.valves.api_key_env, "").strip()
            if api_key:
                base_url = _openai_base_url(self.valves)
                for model_id in _models_list_via_http(
                    base_url=base_url, api_key=api_key
                ):
                    if _passes_filter(model_id, model_filter):
                        models.append({"id": model_id, "name": model_id})
                if models:
                    return models
        except Exception:
            # フォールバックなので握りつぶす.
            pass

        configured = os.environ.get("OPENAI_MODEL_IDS", "").strip()
        for model_id in [m.strip() for m in configured.split(";") if m.strip()]:
            if _passes_filter(model_id, model_filter):
                models.append({"id": model_id, "name": model_id})
        return models

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict | None = None,
        __metadata__: dict | None = None,
        __files__: list[dict[str, Any]] | None = None,
        __event_emitter__: Any = None,
        __request__: Request | None = None,
    ) -> Any:
        valves = self._effective_valves(__user__)

        model_id = body.get("model", "")
        if not isinstance(model_id, str) or "." not in model_id:
            raise ValueError("Invalid model id.")
        _pipe_id, upstream_model = model_id.split(".", 1)

        messages = body.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Invalid messages.")

        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        is_metadata_task = bool(metadata.get("task"))

        if metadata.get("task"):
            web_search_enabled = False
        else:
            web_search_enabled = metadata.get("pipe_web_search_enabled") is True

        forced_backend = metadata.get("pipe_web_search_backend")
        web_search_backend = (
            (
                (
                    forced_backend
                    if isinstance(forced_backend, str)
                    else valves.web_search_backend
                )
                or ""
            )
            .strip()
            .lower()
        )
        if web_search_backend and web_search_backend != "provider":
            web_search_backend = "provider"

        web_search_query = _last_user_text(messages)
        is_background_task = is_metadata_task or _looks_like_background_task_prompt(
            web_search_query
        )
        if is_background_task:
            web_search_enabled = False

        policy_raw = metadata.get("pipe_web_search_policy")
        if isinstance(policy_raw, str):
            policy_raw = policy_raw.strip().lower()
        if not web_search_enabled:
            web_search_policy: str = "off"
        elif policy_raw in ("auto", "required", "off"):
            web_search_policy = policy_raw
        else:
            web_search_policy = "required"

        provider_web_search = (
            web_search_enabled
            and web_search_backend == "provider"
            and web_search_policy != "off"
            and web_search_query.strip() != ""
        )
        provider_web_search_required = (
            provider_web_search and web_search_policy == "required"
        )
        provider_web_search_query_for_status = _format_query_for_status(
            web_search_query
        )

        instructions, filtered_messages = _extract_openai_messages(messages)
        request: dict[str, Any] = {
            "model": upstream_model,
            "input": _to_responses_input(filtered_messages),
        }
        if instructions.strip():
            request["instructions"] = instructions.strip()

        if not valves.image_inputs_enabled:
            request = _strip_responses_input_content_types(
                request, remove_types={"input_image"}
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

        # 背景タスク（follow-ups/title/etc）では添付を使わない（コスト/漏洩防止）.
        # NOTE: RAGのテンプレ自体も "### Task:" から始まるため, promptパターンではなく metadata.task のみで判定する.
        if pdf_file_ids and not is_metadata_task:
            file_items: list[dict[str, Any]] = []
            uploaded_ids: dict[str, str] = {}
            for file_id in pdf_file_ids:
                try:
                    data, content_type, filename = _load_open_webui_file_bytes(
                        file_id,
                        __user__=__user__,
                        max_bytes=int(valves.file_inputs_max_bytes),
                    )
                except Exception as exc:
                    if not is_background_task:
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "warning",
                                "description": (
                                    "添付ファイルを upstream file input として渡せませんでした: "
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

                # まずは Files API に upload して file_id 参照で渡す（失敗したら base64 fallback）.
                try:
                    api_key = os.environ.get(valves.api_key_env, "").strip()
                    if not api_key:
                        raise RuntimeError(
                            f"Missing API key env var: {valves.api_key_env}"
                        )
                    base_url = _openai_base_url(valves)
                    upstream_file_id = uploaded_ids.get(file_id)
                    if not upstream_file_id:
                        upstream_file_id = await _files_create_via_http(
                            base_url=base_url,
                            api_key=api_key,
                            filename=filename,
                            content_type=content_type,
                            data=data,
                            purpose="user_data",
                        )
                        uploaded_ids[file_id] = upstream_file_id
                    file_items.append(
                        {"type": "input_file", "file_id": upstream_file_id}
                    )
                except Exception:
                    b64 = base64.b64encode(data).decode("ascii")
                    data_url = (
                        f"data:{content_type.strip() or 'application/pdf'};base64,{b64}"
                    )
                    file_items.append(
                        {
                            "type": "input_file",
                            "filename": filename,
                            "file_data": data_url,
                        }
                    )

            if file_items:
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "info",
                        "description": (
                            "添付PDFを視覚入力として使用します（"
                            + str(len(file_items))
                            + "件）."
                        ),
                        "done": True,
                    },
                )
                # 最後の user message にまとめて挿入する（UIの「この質問に対する添付」を想定）
                input_items = request.get("input")
                if isinstance(input_items, list):
                    target: dict[str, Any] | None = None
                    for msg in reversed(input_items):
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            target = msg
                            break
                    if target is None:
                        target = {"role": "user", "content": []}
                        input_items.append(target)

                    existing = target.get("content")
                    if not isinstance(existing, list):
                        existing = []
                    target["content"] = [*file_items, *existing]

        if valves.max_output_tokens is not None:
            request["max_output_tokens"] = int(valves.max_output_tokens)
        if valves.temperature is not None:
            request["temperature"] = float(valves.temperature)
        if valves.top_p is not None:
            request["top_p"] = float(valves.top_p)

        if valves.reasoning_summary is not None or valves.reasoning_effort is not None:
            request.setdefault("reasoning", {})
            if not isinstance(request["reasoning"], dict):
                raise ValueError("Invalid reasoning parameter (must be an object).")
            # 既定で reasoning summary を取得する（UI の “思考 summary” 表示向け）.
            # 非対応モデルでエラーになる場合は, 後段で reasoning を外して再試行する.
            summary = (
                valves.reasoning_summary.strip()
                if isinstance(valves.reasoning_summary, str)
                else ""
            )
            request["reasoning"]["summary"] = summary or "auto"
            if valves.reasoning_effort is not None:
                request["reasoning"]["effort"] = valves.reasoning_effort
        else:
            request["reasoning"] = {"summary": "auto"}

        if valves.extra_json:
            try:
                override = json.loads(valves.extra_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"extra_json is not valid JSON: {exc.msg}") from None
            if not isinstance(override, dict):
                raise ValueError("extra_json must be a JSON object.")
            allowed_top_keys = {"metadata", "reasoning", "include"}
            safe_override = {k: v for k, v in override.items() if k in allowed_top_keys}
            request = {**request, **safe_override}

        if provider_web_search:
            tool: dict[str, Any] = {"type": "web_search"}
            filters: dict[str, Any] = {}
            if (
                isinstance(valves.web_search_allowed_domains, list)
                and valves.web_search_allowed_domains
            ):
                filters["allowed_domains"] = [
                    d.strip()
                    for d in valves.web_search_allowed_domains
                    if isinstance(d, str) and d.strip()
                ]
            if (
                isinstance(valves.web_search_blocked_domains, list)
                and valves.web_search_blocked_domains
            ):
                filters["blocked_domains"] = [
                    d.strip()
                    for d in valves.web_search_blocked_domains
                    if isinstance(d, str) and d.strip()
                ]
            if filters:
                tool["filters"] = filters
            if (
                isinstance(valves.web_search_context_size, str)
                and valves.web_search_context_size.strip()
            ):
                tool["search_context_size"] = valves.web_search_context_size.strip()

            request["tools"] = [tool]
            request["tool_choice"] = (
                "required" if provider_web_search_required else "auto"
            )
            if valves.include_web_search_sources:
                request.setdefault("include", [])
                if not isinstance(request["include"], list):
                    raise ValueError("Invalid include parameter (must be an array).")
                if "web_search_call.action.sources" not in request["include"]:
                    request["include"].append("web_search_call.action.sources")

            # 他 provider と同様に, required のときだけ “Searching …” を表示する.
            # auto の場合はモデルが検索しない可能性があるため, status を出すと UI 側で未完了に見えやすい.
            if provider_web_search_required:
                await _emit_status(
                    __event_emitter__,
                    {
                        "action": "web_search",
                        "description": "Searching the web",
                        "done": False,
                    },
                )
                if provider_web_search_query_for_status:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "web_search_queries_generated",
                            "queries": [provider_web_search_query_for_status],
                            "done": False,
                        },
                    )

        if valves.debug_enabled and not _is_admin_user(__user__):
            raise RuntimeError("debug_enabled requires an admin user.")

        if valves.debug_enabled and valves.debug_include_request:
            await _emit_debug_parameters(
                __event_emitter__,
                provider="openai",
                title="request",
                parameters={
                    "model_id": model_id,
                    "upstream_model": upstream_model,
                    "request": request,
                },
                max_string_length=valves.debug_max_string_length,
                max_depth=valves.debug_max_depth,
            )

        api_key = os.environ.get(valves.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")
        base_url = _openai_base_url(valves)

        seen_search_result_urls: set[str] = set()
        seen_search_info_sources: set[str] = set()
        seen_evidence_urls: set[str] = set()
        seen_unverified_urls: set[str] = set()

        if body.get("stream", False):

            async def stream() -> AsyncGenerator[Any, None]:
                output_text_parts: list[str] = []
                reasoning_parts: list[str] = []
                final_response: dict[str, Any] | None = None
                web_search_used = False
                web_search_status_done_emitted = False

                def _should_retry_without_reasoning(exc: BaseException) -> bool:
                    text = str(exc).lower()
                    return ("reasoning" in text) and (
                        "not supported" in text
                        or "unknown parameter" in text
                        or "invalid" in text
                        or "unrecognized" in text
                        or "summary" in text
                    )

                def _should_retry_without_input_types(
                    exc: BaseException, *, type_name: str
                ) -> bool:
                    text = str(exc).lower()
                    if type_name not in text:
                        return False
                    return (
                        "not supported" in text
                        or "unknown" in text
                        or "unrecognized" in text
                        or "invalid" in text
                        or "unsupported" in text
                    )

                stream_request = request
                retried_without_reasoning = False
                retried_without_files = False
                retried_without_images = False

                while True:
                    try:
                        async for event in _responses_stream_via_http(
                            base_url=base_url,
                            api_key=api_key,
                            request=stream_request,
                        ):
                            event_type = event.get("type")
                            if not isinstance(event_type, str):
                                continue

                            if provider_web_search:
                                for call_item in _extract_web_search_call_items_from_event(event):
                                    web_search_used = True
                                    urls, items, non_url_sources = (
                                        _web_search_sources_from_call_item(call_item)
                                    )
                                    if (
                                        urls or non_url_sources
                                    ) and not web_search_status_done_emitted:
                                        status_items = items
                                        if not status_items and non_url_sources:
                                            status_items = [
                                                {"link": "", "title": s}
                                                for s in non_url_sources
                                            ]
                                        await _emit_status(
                                            __event_emitter__,
                                            {
                                                "action": "web_search",
                                                "description": (
                                                    "Searched {{count}} sites"
                                                    if urls
                                                    else "Web search used (no web URLs returned)"
                                                ),
                                                "urls": urls,
                                                "items": status_items,
                                                "done": True,
                                            },
                                        )
                                        web_search_status_done_emitted = True

                                    if not is_background_task:
                                        if items:
                                            await _emit_search_result_citations_from_source_items(
                                                __event_emitter__,
                                                items=items,
                                                seen_urls=seen_search_result_urls,
                                            )
                                        if non_url_sources:
                                            await _emit_search_result_info_sources(
                                                __event_emitter__,
                                                sources=non_url_sources,
                                                seen_sources=seen_search_info_sources,
                                            )

                            if event_type == "response.output_text.delta":
                                delta = event.get("delta")
                                if isinstance(delta, str) and delta:
                                    output_text_parts.append(delta)
                                    yield openai_chat_chunk_message_template(
                                        model_id, content=delta
                                    )
                                continue

                            if event_type in (
                                "response.reasoning_text.delta",
                                "response.reasoning_summary_text.delta",
                            ):
                                delta = event.get("delta")
                                if isinstance(delta, str) and delta:
                                    reasoning_parts.append(delta)
                                    yield openai_chat_chunk_message_template(
                                        model_id, reasoning_content=delta
                                    )
                                continue

                            if event_type == "response.completed":
                                response_obj = event.get("response")
                                if isinstance(response_obj, dict):
                                    final_response = response_obj
                                continue

                            if event_type == "response.failed":
                                error = event.get("error")
                                if provider_web_search and not is_background_task:
                                    await _emit_status(
                                        __event_emitter__,
                                        {
                                            "action": "web_search",
                                            "description": f"Web search failed: {error}",
                                            "done": True,
                                            "error": True,
                                        },
                                    )
                                raise RuntimeError(f"OpenAI response failed: {error}")

                        break
                    except Exception as exc:
                        if (
                            not retried_without_reasoning
                            and isinstance(stream_request, dict)
                            and "reasoning" in stream_request
                            and _should_retry_without_reasoning(exc)
                        ):
                            retried_without_reasoning = True
                            stream_request = dict(stream_request)
                            stream_request.pop("reasoning", None)
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "warning",
                                    "description": (
                                        "reasoning summary が非対応のため, reasoning を外して再試行しました."
                                    ),
                                    "done": True,
                                },
                            )
                            continue
                        if (
                            not retried_without_files
                            and isinstance(stream_request, dict)
                            and _should_retry_without_input_types(
                                exc, type_name="input_file"
                            )
                        ):
                            retried_without_files = True
                            stream_request = _strip_responses_input_content_types(
                                dict(stream_request), remove_types={"input_file"}
                            )
                            if not is_background_task:
                                await _emit_status(
                                    __event_emitter__,
                                    {
                                        "action": "warning",
                                        "description": (
                                            "file input が非対応のため, 添付ファイルを除外して再試行しました."
                                        ),
                                        "done": True,
                                    },
                                )
                            continue
                        if (
                            not retried_without_images
                            and isinstance(stream_request, dict)
                            and _should_retry_without_input_types(
                                exc, type_name="input_image"
                            )
                        ):
                            retried_without_images = True
                            stream_request = _strip_responses_input_content_types(
                                dict(stream_request), remove_types={"input_image"}
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

                if retried_without_reasoning:
                    # デバッグ request は “最終的に送った内容” が追えるのが重要なので, 差分を出す.
                    if valves.debug_enabled and valves.debug_include_request:
                        await _emit_debug_parameters(
                            __event_emitter__,
                            provider="openai",
                            title="request(retried)",
                            parameters={
                                "model_id": model_id,
                                "upstream_model": upstream_model,
                                "request": stream_request,
                            },
                            max_string_length=valves.debug_max_string_length,
                            max_depth=valves.debug_max_depth,
                        )
                if (retried_without_files or retried_without_images) and (
                    valves.debug_enabled and valves.debug_include_request
                ):
                    await _emit_debug_parameters(
                        __event_emitter__,
                        provider="openai",
                        title="request(retried_inputs)",
                        parameters={
                            "model_id": model_id,
                            "upstream_model": upstream_model,
                            "request": stream_request,
                            "retried_without_files": retried_without_files,
                            "retried_without_images": retried_without_images,
                        },
                        max_string_length=valves.debug_max_string_length,
                        max_depth=valves.debug_max_depth,
                    )

                # ここ以降は元の処理（post-processing）へ流す.
                # NOTE: 直上の while/async for で event を処理しているため, 以下のループは削除済み.

                full_text = "".join(output_text_parts)
                if final_response is not None:
                    summary = _collect_reasoning_summary(final_response)
                    if summary and not reasoning_parts:
                        yield openai_chat_chunk_message_template(
                            model_id, reasoning_content=summary
                        )

                if provider_web_search:
                    (
                        web_search_used_final,
                        urls,
                        items,
                        non_url_sources,
                    ) = _collect_web_search_activity(final_response or {})
                    web_search_used = web_search_used or web_search_used_final
                    if web_search_used and not web_search_status_done_emitted:
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "web_search",
                                "description": (
                                    "Searched {{count}} sites"
                                    if urls
                                    else "Web search used (no web URLs returned)"
                                ),
                                "urls": urls if urls else [],
                                "items": items,
                                "done": True,
                            },
                        )
                        web_search_status_done_emitted = True

                    if not is_background_task and web_search_used:
                        if items:
                            await _emit_search_result_citations_from_source_items(
                                __event_emitter__,
                                items=items,
                                seen_urls=seen_search_result_urls,
                            )
                        if non_url_sources:
                            await _emit_search_result_info_sources(
                                __event_emitter__,
                                sources=non_url_sources,
                                seen_sources=seen_search_info_sources,
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

                if not is_background_task:
                    if (
                        provider_web_search
                        and web_search_used
                        and final_response is not None
                    ):
                        await _emit_verified_citations_from_url_citations(
                            __event_emitter__,
                            url_citations=_collect_url_citations(final_response),
                            output_text=full_text,
                            seen_urls=seen_evidence_urls,
                        )
                    elif not provider_web_search:
                        await _emit_unverified_citations_from_text(
                            __event_emitter__,
                            text=full_text,
                            seen_urls=seen_unverified_urls,
                        )

                if valves.debug_enabled and valves.debug_include_response:
                    debug_response: dict[str, Any]
                    if final_response is not None:
                        debug_response = final_response
                    else:
                        # 念のため: response.completed が届かない実装差分があっても,
                        # 何が返ってきたか追えるように最小限の情報を出す.
                        debug_response = {
                            "type": "missing_response_completed",
                            "output_text": full_text,
                            "reasoning_text": "".join(reasoning_parts),
                        }
                    await _emit_debug_parameters(
                        __event_emitter__,
                        provider="openai",
                        title="response",
                        parameters={"response": debug_response},
                        max_string_length=valves.debug_max_string_length,
                        max_depth=valves.debug_max_depth,
                    )

                yield openai_chat_chunk_message_template(model_id)
                yield "data: [DONE]"

            return stream()

        async_client = self._async_client(valves)
        response: Any
        try:
            if async_client is not None:
                response = await async_client.responses.create(**request)
            else:
                response = await _responses_create_via_http(
                    base_url=base_url, api_key=api_key, request=request
                )
        except Exception as exc:
            text = str(exc).lower()
            request_retry: dict[str, Any] | None = None
            retry_note: str | None = None

            if "reasoning" in text and ("not supported" in text or "summary" in text):
                request_retry = dict(request)
                request_retry.pop("reasoning", None)
                retry_note = "reasoning summary が非対応のため, reasoning を外して再試行しました."
            elif "input_file" in text and (
                "not supported" in text
                or "unknown" in text
                or "unrecognized" in text
                or "invalid" in text
                or "unsupported" in text
            ):
                request_retry = _strip_responses_input_content_types(
                    dict(request), remove_types={"input_file"}
                )
                retry_note = (
                    "file input が非対応のため, 添付ファイルを除外して再試行しました."
                )
            elif "input_image" in text and (
                "not supported" in text
                or "unknown" in text
                or "unrecognized" in text
                or "invalid" in text
                or "unsupported" in text
            ):
                request_retry = _strip_responses_input_content_types(
                    dict(request), remove_types={"input_image"}
                )
                retry_note = (
                    "image input が非対応のため, 画像入力を除外して再試行しました."
                )

            if request_retry is None:
                raise

            if retry_note:
                await _emit_status(
                    __event_emitter__,
                    {"action": "warning", "description": retry_note, "done": True},
                )
            if valves.debug_enabled and valves.debug_include_request:
                await _emit_debug_parameters(
                    __event_emitter__,
                    provider="openai",
                    title="request(retried)",
                    parameters={
                        "model_id": model_id,
                        "upstream_model": upstream_model,
                        "request": request_retry,
                    },
                    max_string_length=valves.debug_max_string_length,
                    max_depth=valves.debug_max_depth,
                )

            if async_client is not None:
                response = await async_client.responses.create(**request_retry)
            else:
                response = await _responses_create_via_http(
                    base_url=base_url, api_key=api_key, request=request_retry
                )

        content = _collect_output_text(response)
        reasoning_summary = _collect_reasoning_summary(response)

        web_search_used, urls, items, non_url_sources = (
            _collect_web_search_activity(response)
            if provider_web_search
            else (False, [], [], [])
        )
        if provider_web_search:
            if web_search_used:
                if urls:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "web_search",
                            "description": "Searched {{count}} sites",
                            "urls": urls,
                            "items": items,
                            "done": True,
                        },
                    )
                else:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "web_search",
                            "description": "Web search used (no web URLs returned)",
                            "urls": [],
                            "items": items,
                            "done": True,
                        },
                    )
                if not is_background_task:
                    await _emit_search_result_citations_from_source_items(
                        __event_emitter__,
                        items=items,
                        seen_urls=seen_search_result_urls,
                    )
                    await _emit_search_result_info_sources(
                        __event_emitter__,
                        sources=non_url_sources,
                        seen_sources=seen_search_info_sources,
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

        if not is_background_task:
            if provider_web_search and web_search_used:
                await _emit_verified_citations_from_url_citations(
                    __event_emitter__,
                    url_citations=_collect_url_citations(response),
                    output_text=content,
                    seen_urls=seen_evidence_urls,
                )
            elif not provider_web_search:
                await _emit_unverified_citations_from_text(
                    __event_emitter__,
                    text=content,
                    seen_urls=seen_unverified_urls,
                )

        plain = _to_plain(response)
        usage_obj = plain.get("usage") if isinstance(plain, dict) else None
        input_tokens = (
            int((usage_obj or {}).get("input_tokens") or 0)
            if isinstance(usage_obj, dict)
            else 0
        )
        output_tokens = (
            int((usage_obj or {}).get("output_tokens") or 0)
            if isinstance(usage_obj, dict)
            else 0
        )
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

        if valves.debug_enabled and valves.debug_include_response:
            await _emit_debug_parameters(
                __event_emitter__,
                provider="openai",
                title="response",
                parameters={"response": plain},
                max_string_length=valves.debug_max_string_length,
                max_depth=valves.debug_max_depth,
            )

        return openai_chat_completion_message_template(
            model_id,
            message=content,
            reasoning_content=reasoning_summary or None,
            usage=usage,
        )
