# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import base64
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

from open_webui.utils.misc import (
    openai_chat_chunk_message_template,
    openai_chat_completion_message_template,
)
from pydantic import BaseModel
from starlette.requests import Request

from webui_functions_src.gemini_generatecontent.config import UserValves, Valves
from webui_functions_src.gemini_generatecontent.emit import (
    _emit_citations_from_items,
    _emit_citations_from_urls,
    _emit_debug_parameters,
    _emit_status,
    _emit_unverified_citations_from_text,
    _is_admin_user,
    _to_plain_for_debug,
)
from webui_functions_src.gemini_generatecontent.models import (
    _compile_filter,
    _passes_filter,
)
from webui_functions_src.gemini_generatecontent.normalize import (
    _extract_openai_messages,
    _format_query_for_status,
    _last_user_text,
    _looks_like_background_task_prompt,
)
from webui_functions_src.gemini_generatecontent.upstream_http import (
    _build_generation_config,
)
from webui_functions_src.gemini_generatecontent.upstream_parse import (
    _iter_genai_thought_and_text,
)
from webui_functions_src.gemini_generatecontent.web_search import (
    _coalesce_web_search_backend,
    _deep_find_citation_items,
    _deep_find_urls,
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


def _load_open_webui_file_bytes_for_gemini(
    file_id: str, *, __user__: dict | None, max_bytes: int
) -> tuple[bytes, str, str]:
    """
    Open WebUI の Files storage から raw bytes を取得する（Gemini inline_data 用）.
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
            f"File too large for upstream inline_data. file_id={file_id} size={size} max_bytes={max_bytes}"
        )

    if not getattr(file, "path", None):
        raise RuntimeError(f"File path is missing. file_id={file_id}")
    disk_path = Storage.get_file(file.path)
    with open(disk_path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(
            f"File too large for upstream inline_data. file_id={file_id} read_bytes>{max_bytes}"
        )
    return data, content_type, filename


class Pipe:
    valves: Valves
    UserValves = UserValves

    def __init__(self) -> None:
        self.valves = Valves()

    @property
    def name(self) -> str:
        return "Gemini generateContent: "

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

        # セキュリティ: dict をそのまま Valves に merge しない（UserValves でホワイトリスト化）
        allowed_keys = set(getattr(UserValves, "model_fields", {}).keys())
        filtered_values = {k: v for k, v in (raw or {}).items() if k in allowed_keys}

        # UserValves のデフォルト値を "ユーザー指定" と誤認しないように exclude_defaults する.
        user_values = UserValves(**filtered_values).model_dump(
            exclude_none=True, exclude_defaults=True
        )
        merged = {**self.valves.model_dump(), **(user_values or {})}
        return Valves(**merged)

    def _configure(self, valves: Valves) -> None:
        api_key = os.environ.get(valves.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")
        self._api_key = api_key

    def pipes(self) -> list[dict[str, str]]:
        self._configure(self.valves)
        model_filter = _compile_filter(
            self.valves.model_allow_regex, self.valves.model_deny_regex
        )

        models: list[dict[str, str]] = []
        try:
            from google import genai

            client = genai.Client(api_key=self._api_key)
            for model in client.models.list():
                model_id = getattr(model, "name", None) or (
                    model.get("name") if isinstance(model, dict) else None
                )
                if not isinstance(model_id, str) or not model_id:
                    continue
                if not _passes_filter(model_id, model_filter):
                    continue
                models.append({"id": model_id, "name": model_id})
            return models
        except Exception:
            configured = os.environ.get("GEMINI_MODEL_IDS", "").strip()
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
        self._configure(valves)

        model_id = body.get("model", "")
        if not isinstance(model_id, str) or "." not in model_id:
            raise ValueError("Invalid model id.")
        _pipe_id, upstream_model = model_id.split(".", 1)

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

        policy_raw = metadata.get("pipe_web_search_policy")
        if isinstance(policy_raw, str):
            policy_raw = policy_raw.strip().lower()
        if not web_search_enabled:
            web_search_policy: str = "off"
        elif policy_raw in ("auto", "required", "off"):
            web_search_policy = policy_raw
        else:
            # 後方互換: 古い Router は policy を付与しないため, 従来どおり required とする.
            web_search_policy = "required"

        seen_urls: set[str] = set()

        web_search_backend = _coalesce_web_search_backend(
            metadata, default_backend="provider"
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
                            "Web検索が有効です. google_search tool を使って最新情報を取得してから回答してください.",
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
                            "必要に応じて google_search tool を使って最新情報を確認してから回答してください.",
                            "特に『今日/最新/ニュース/天気』など時事性が強い場合は検索を優先してください.",
                            "可能なら情報源(URL)も含めてください.",
                        ]
                    ),
                )

        def _message_text_for_prompt(message: dict[str, Any]) -> str:
            raw = message.get("content", "")
            if isinstance(raw, list):
                return "\n".join(
                    part.get("text", "")
                    for part in raw
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            if isinstance(raw, str):
                return raw
            return str(raw)

        prompt_parts: list[str] = []
        if system:
            prompt_parts.append(f"System: {system}")
        for message in filtered:
            role = message.get("role")
            label = "User" if role == "user" else "Assistant"
            prompt_parts.append(f"{label}: {_message_text_for_prompt(message)}")
        prompt = "\n\n".join(prompt_parts)

        # 添付ファイル / 画像入力（multimodal）は best-effort で渡す.
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

        pdf_file_uris: list[str] = []
        pdf_inline_parts: list[tuple[str, str]] = []
        # NOTE: RAGのテンプレ自体も "### Task:" から始まるため, promptパターンではなく metadata.task のみで判定する.
        if pdf_file_ids and not is_metadata_task:
            # まずは Files API でアップロードして file_uri 参照を試す（失敗時は inline_data fallback）.
            try:
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=self._api_key)
                for file_id in pdf_file_ids:
                    try:
                        data, content_type, filename = (
                            _load_open_webui_file_bytes_for_gemini(
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
                                    "添付ファイルを Gemini へ渡せませんでした: "
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

                    # SDK の upload は path を想定するため一時ファイルに落とす.
                    import tempfile

                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False
                    ) as temp:
                        temp.write(data)
                        temp_path = temp.name

                    try:
                        uploaded = client.files.upload(
                            file=temp_path,
                            config=types.UploadFileConfig(mime_type="application/pdf"),
                        )
                    finally:
                        try:
                            os.unlink(temp_path)
                        except Exception:
                            pass
                    uri = getattr(uploaded, "uri", None) or (
                        uploaded.get("uri") if isinstance(uploaded, dict) else None
                    )
                    if isinstance(uri, str) and uri.strip():
                        pdf_file_uris.append(uri.strip())
                        continue

                    # 互換: uri が取れない場合は inline_data fallback を作る.
                    encoded = base64.b64encode(data).decode("ascii")
                    pdf_inline_parts.append(("application/pdf", encoded))
            except Exception as exc:
                # Files API 自体が使えない場合は inline_data にフォールバック.
                if not is_background_task:
                    await _emit_status(
                        __event_emitter__,
                        {
                            "action": "warning",
                            "description": (
                                "Gemini Files API が使えないため inline_data にフォールバックします. "
                                + str(exc)[:200]
                            ),
                            "done": True,
                        },
                    )
                for file_id in pdf_file_ids:
                    try:
                        data, content_type, filename = (
                            _load_open_webui_file_bytes_for_gemini(
                                file_id,
                                __user__=__user__,
                                max_bytes=int(valves.file_inputs_max_bytes),
                            )
                        )
                    except Exception:
                        continue
                    is_pdf = (
                        content_type.strip().lower() == "application/pdf"
                        or filename.strip().lower().endswith(".pdf")
                    )
                    if not is_pdf:
                        continue
                    encoded = base64.b64encode(data).decode("ascii")
                    pdf_inline_parts.append(("application/pdf", encoded))

        if (pdf_file_uris or pdf_inline_parts) and not is_background_task:
            await _emit_status(
                __event_emitter__,
                {
                    "action": "info",
                    "description": (
                        "添付PDFを視覚入力として使用します（"
                        + str(len(pdf_file_uris) + len(pdf_inline_parts))
                        + "件）."
                    ),
                    "done": True,
                },
            )

        def _collect_openai_like_parts_for_gemini(
            message: dict[str, Any],
        ) -> list[tuple[str, str] | tuple[str, str, str]]:
            """
            ('text', text) または ('inline', mime, base64) の列にする.
            """
            raw = message.get("content", "")
            parts: list[tuple[str, str] | tuple[str, str, str]] = []

            if isinstance(raw, list):
                for part in raw:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text" and isinstance(part.get("text"), str):
                        parts.append(("text", part["text"]))
                        continue
                    if part_type == "image_url":
                        image_url = part.get("image_url")
                        url: str | None = None
                        if isinstance(image_url, dict):
                            url = image_url.get("url")
                        elif isinstance(image_url, str):
                            url = image_url
                        if not isinstance(url, str) or not url.strip():
                            continue
                        if not valves.image_inputs_enabled:
                            continue
                        parsed = _parse_base64_data_url(url.strip())
                        if parsed is None:
                            parts.append(("text", f"[image omitted: {url.strip()}]"))
                            continue
                        mime, encoded = parsed
                        if not mime.lower().startswith("image/"):
                            parts.append(("text", f"[non-image omitted: {mime}]"))
                            continue
                        parts.append(("inline", mime, encoded))
                        continue
            else:
                parts.append(("text", str(raw)))

            if not parts:
                parts = [("text", "")]
            return parts

        # Gemini SDK はバージョン差分があるため, snake_case / camelCase を両方試す.
        def _build_gemini_contents(
            *, use_camel: bool, pdf_transport: str
        ) -> list[dict[str, Any]]:
            contents: list[dict[str, Any]] = []
            for message in filtered:
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                gemini_role = "user" if role == "user" else "model"
                message_parts = _collect_openai_like_parts_for_gemini(message)

                parts_payload: list[dict[str, Any]] = []
                for part in message_parts:
                    if part[0] == "text":
                        parts_payload.append({"text": part[1]})
                    else:
                        _kind, mime, encoded = part  # type: ignore[misc]
                        if use_camel:
                            parts_payload.append(
                                {"inlineData": {"mimeType": mime, "data": encoded}}
                            )
                        else:
                            parts_payload.append(
                                {
                                    "inline_data": {
                                        "mime_type": mime,
                                        "data": encoded,
                                    }
                                }
                            )

                contents.append({"role": gemini_role, "parts": parts_payload})

            # system は role を分けず, 先頭 user の text part に入れる（後方互換を優先）.
            if system:
                system_part = {"text": f"System: {system}"}
                if contents and contents[0].get("role") == "user":
                    first_parts = contents[0].get("parts")
                    if not isinstance(first_parts, list):
                        first_parts = []
                    contents[0]["parts"] = [system_part, *first_parts]
                else:
                    contents = [{"role": "user", "parts": [system_part]}] + contents

            if pdf_file_uris or pdf_inline_parts:
                target: dict[str, Any] | None = None
                for content in reversed(contents):
                    if isinstance(content, dict) and content.get("role") == "user":
                        target = content
                        break
                if target is None:
                    target = {"role": "user", "parts": []}
                    contents.append(target)
                current_parts = target.get("parts")
                if not isinstance(current_parts, list):
                    current_parts = []
                pdf_parts_payload: list[dict[str, Any]] = []
                if pdf_transport == "file_uri":
                    for uri in pdf_file_uris:
                        if use_camel:
                            pdf_parts_payload.append(
                                {
                                    "fileData": {
                                        "fileUri": uri,
                                        "mimeType": "application/pdf",
                                    }
                                }
                            )
                        else:
                            pdf_parts_payload.append(
                                {
                                    "file_data": {
                                        "file_uri": uri,
                                        "mime_type": "application/pdf",
                                    }
                                }
                            )
                else:
                    for mime, encoded in pdf_inline_parts:
                        if use_camel:
                            pdf_parts_payload.append(
                                {"inlineData": {"mimeType": mime, "data": encoded}}
                            )
                        else:
                            pdf_parts_payload.append(
                                {"inline_data": {"mime_type": mime, "data": encoded}}
                            )
                target["parts"] = [*pdf_parts_payload, *current_parts]

            return contents

        # image/dataURL や PDF があるときのみ, contents=... を使う（テキストのみは既存実装を維持）.
        use_multimodal = bool(pdf_file_uris) or bool(pdf_inline_parts)
        if not use_multimodal and valves.image_inputs_enabled:
            for message in filtered:
                raw = message.get("content", "")
                if not isinstance(raw, list):
                    continue
                for part in raw:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        image_url = part.get("image_url")
                        url = (
                            (image_url.get("url") if isinstance(image_url, dict) else image_url)
                            if isinstance(image_url, (dict, str))
                            else None
                        )
                        if isinstance(url, str) and _parse_base64_data_url(url.strip()) is not None:
                            use_multimodal = True
                            break
                if use_multimodal:
                    break

        contents_snake_file: list[dict[str, Any]] | None = None
        contents_camel_file: list[dict[str, Any]] | None = None
        contents_snake_inline: list[dict[str, Any]] | None = None
        contents_camel_inline: list[dict[str, Any]] | None = None
        if use_multimodal:
            if pdf_file_uris:
                contents_snake_file = _build_gemini_contents(
                    use_camel=False, pdf_transport="file_uri"
                )
                contents_camel_file = _build_gemini_contents(
                    use_camel=True, pdf_transport="file_uri"
                )
            if pdf_inline_parts or not pdf_file_uris:
                contents_snake_inline = _build_gemini_contents(
                    use_camel=False, pdf_transport="inline"
                )
                contents_camel_inline = _build_gemini_contents(
                    use_camel=True, pdf_transport="inline"
                )

        generation_config: dict[str, Any] = {}
        if valves.max_output_tokens is not None:
            generation_config["max_output_tokens"] = valves.max_output_tokens
        if valves.temperature is not None:
            generation_config["temperature"] = valves.temperature
        if valves.top_p is not None:
            generation_config["top_p"] = valves.top_p

        if body.get("max_tokens") is not None:
            generation_config["max_output_tokens"] = body.get("max_tokens")
        if body.get("max_completion_tokens") is not None:
            generation_config["max_output_tokens"] = body.get("max_completion_tokens")
        if body.get("temperature") is not None:
            generation_config["temperature"] = body.get("temperature")
        if body.get("top_p") is not None:
            generation_config["top_p"] = body.get("top_p")

        if valves.extra_json:
            override = json.loads(valves.extra_json)
            if not isinstance(override, dict):
                raise ValueError("extra_json must be a JSON object.")
            generation_config = {**generation_config, **override}

        if provider_web_search:
            generation_config["tools"] = [{"google_search": {}}]

        if valves.debug_enabled and not is_background_task:
            if not _is_admin_user(__user__):
                raise RuntimeError("debug_enabled requires an admin user.")
            if valves.debug_include_request:
                await _emit_debug_parameters(
                    __event_emitter__,
                    provider="gemini",
                    title="request",
                    parameters={
                        "model_id": model_id,
                        "upstream_model": upstream_model,
                        "prompt": prompt,
                        "contents": (
                            (contents_snake_file or contents_snake_inline)
                            if use_multimodal
                            else None
                        ),
                        "generation_config": generation_config,
                    },
                    max_string_length=valves.debug_max_string_length,
                    max_depth=valves.debug_max_depth,
                )

        if body.get("stream", False):

            async def stream() -> AsyncGenerator[Any, None]:
                from google import genai

                client = genai.Client(api_key=self._api_key)
                config = _build_generation_config(valves, generation_config)
                full_text_parts: list[str] = []
                provider_urls: set[str] = set()
                provider_items_by_url: dict[str, dict[str, str]] = {}
                url_tail = ""

                def _stream_chunks(contents_value: Any):
                    return client.models.generate_content_stream(
                        model=upstream_model,
                        contents=contents_value,
                        config=config,
                    )

                stream_sources: list[tuple[str, Any]] = []
                if use_multimodal and contents_snake_file is not None:
                    stream_sources.append(("contents_snake_file", contents_snake_file))
                if use_multimodal and contents_camel_file is not None:
                    stream_sources.append(("contents_camel_file", contents_camel_file))
                if use_multimodal and contents_snake_inline is not None:
                    stream_sources.append(("contents_snake_inline", contents_snake_inline))
                if use_multimodal and contents_camel_inline is not None:
                    stream_sources.append(("contents_camel_inline", contents_camel_inline))
                stream_sources.append(("prompt", prompt))

                last_err: Exception | None = None
                for label, candidate in stream_sources:
                    emitted_any = False
                    try:
                        for chunk in _stream_chunks(candidate):
                            emitted_any = True
                            if provider_web_search:
                                items = _deep_find_citation_items(chunk)
                                urls = [
                                    i.get("url", "")
                                    for i in items
                                    if isinstance(i.get("url"), str)
                                ]
                                if not urls:
                                    urls = _deep_find_urls(chunk)

                                provider_urls.update([u for u in urls if u])
                                for item in items:
                                    url = (item.get("url") or "").strip()
                                    if not url:
                                        continue
                                    record: dict[str, str] = {"link": url}
                                    title = (item.get("title") or "").strip()
                                    snippet = (item.get("snippet") or "").strip()
                                    if title:
                                        record["title"] = title
                                    if snippet:
                                        record["snippet"] = snippet
                                    provider_items_by_url.setdefault(url, record)
                                for url in urls:
                                    normalized = (url or "").strip()
                                    if not normalized:
                                        continue
                                    provider_items_by_url.setdefault(
                                        normalized, {"link": normalized}
                                    )

                                if not is_background_task:
                                    if items:
                                        await _emit_citations_from_items(
                                            __event_emitter__,
                                            items=items,
                                            seen_urls=seen_urls,
                                        )
                                    elif urls:
                                        await _emit_citations_from_urls(
                                            __event_emitter__,
                                            urls=urls,
                                            seen_urls=seen_urls,
                                        )

                            for kind, text in _iter_genai_thought_and_text(chunk):
                                if kind == "thought":
                                    yield openai_chat_chunk_message_template(
                                        model_id, reasoning_content=text
                                    )
                                else:
                                    full_text_parts.append(text)
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
                        last_err = None
                        break
                    except Exception as exc:
                        last_err = exc
                        if emitted_any:
                            raise
                        if not is_background_task and label != "prompt":
                            await _emit_status(
                                __event_emitter__,
                                {
                                    "action": "warning",
                                    "description": (
                                        "Gemini multimodal 入力に失敗したため, "
                                        f"別形式で再試行します: {label}. {exc}"
                                    ),
                                    "done": True,
                                },
                            )
                        continue

                if last_err is not None:
                    raise last_err

                if provider_web_search:
                    urls = list(dict.fromkeys([*provider_urls]))
                    if urls:
                        status_items = [
                            provider_items_by_url.get(url, {"link": url})
                            for url in urls
                        ]
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
                            await _emit_citations_from_urls(
                                __event_emitter__, urls=urls, seen_urls=seen_urls
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
                    elif not is_background_task:
                        await _emit_unverified_citations_from_text(
                            __event_emitter__,
                            text="".join(full_text_parts),
                            seen_urls=seen_urls,
                        )
                elif not is_background_task:
                    # provider web search が無効な場合は, 出力内URLを未検証リンクとして扱う.
                    await _emit_unverified_citations_from_text(
                        __event_emitter__,
                        text="".join(full_text_parts),
                        seen_urls=seen_urls,
                    )

                if (
                    valves.debug_enabled
                    and valves.debug_include_response
                    and not is_background_task
                ):
                    await _emit_debug_parameters(
                        __event_emitter__,
                        provider="gemini",
                        title="response",
                        parameters={
                            "text": "".join(full_text_parts),
                            "urls": sorted(provider_urls),
                        },
                        max_string_length=valves.debug_max_string_length,
                        max_depth=valves.debug_max_depth,
                    )

                yield openai_chat_chunk_message_template(model_id)
                yield "data: [DONE]"

            return stream()

        from google import genai

        client = genai.Client(api_key=self._api_key)
        config = _build_generation_config(valves, generation_config)
        response: Any | None = None
        last_error: Exception | None = None
        if use_multimodal:
            candidates: list[tuple[str, Any]] = []
            if contents_snake_file is not None:
                candidates.append(("contents_snake_file", contents_snake_file))
            if contents_camel_file is not None:
                candidates.append(("contents_camel_file", contents_camel_file))
            if contents_snake_inline is not None:
                candidates.append(("contents_snake_inline", contents_snake_inline))
            if contents_camel_inline is not None:
                candidates.append(("contents_camel_inline", contents_camel_inline))
            candidates.append(("prompt", prompt))

            for label, candidate in candidates:
                try:
                    response = client.models.generate_content(
                        model=upstream_model, contents=candidate, config=config
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if not is_background_task and label != "prompt":
                        await _emit_status(
                            __event_emitter__,
                            {
                                "action": "warning",
                                "description": (
                                    "Gemini multimodal 入力に失敗したため, "
                                    f"別形式で再試行します: {label}. {exc}"
                                ),
                                "done": True,
                            },
                        )
                    continue
        else:
            response = client.models.generate_content(
                model=upstream_model, contents=prompt, config=config
            )
        if response is None:
            raise last_error or RuntimeError("Gemini generate_content failed.")

        content_parts: list[str] = []
        thought_parts: list[str] = []
        for kind, text in _iter_genai_thought_and_text(response):
            if kind == "thought":
                thought_parts.append(text)
            else:
                content_parts.append(text)

        content = "".join(content_parts)
        reasoning = "".join(thought_parts) if thought_parts else None

        web_search_used = False

        if provider_web_search:
            items = _deep_find_citation_items(response) if response is not None else []
            urls = [
                i.get("url", "")
                for i in items
                if isinstance(i.get("url"), str) and i.get("url")
            ]
            if not urls:
                urls = _deep_find_urls(response) if response is not None else []
            if urls:
                web_search_used = True
                status_items = (
                    [
                        {
                            "link": i.get("url", ""),
                            "title": i.get("title", ""),
                            "snippet": i.get("snippet", ""),
                        }
                        for i in items
                    ]
                    if items
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
                    if items:
                        await _emit_citations_from_items(
                            __event_emitter__, items=items, seen_urls=seen_urls
                        )
                    else:
                        await _emit_citations_from_urls(
                            __event_emitter__, urls=urls, seen_urls=seen_urls
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

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if (
            valves.debug_enabled
            and valves.debug_include_response
            and not is_background_task
        ):
            await _emit_debug_parameters(
                __event_emitter__,
                provider="gemini",
                title="response",
                parameters={"response": _to_plain_for_debug(response)},
                max_string_length=valves.debug_max_string_length,
                max_depth=valves.debug_max_depth,
            )
        return openai_chat_completion_message_template(
            model_id,
            message=content,
            reasoning_content=reasoning,
            usage=usage,
        )
