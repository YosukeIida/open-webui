# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from httpx import Timeout

def _httpx_timeout(*, stream: bool) -> Timeout:
    """
    タイムアウトは Valve に出さず固定する.
    必要なら env で上書きできる逃げ道だけ用意する.
    """
    connect = float(os.environ.get("OPENAI_HTTP_CONNECT_TIMEOUT", "10"))
    read_default = "600" if stream else "120"
    read = float(os.environ.get("OPENAI_HTTP_READ_TIMEOUT", read_default))
    write = float(os.environ.get("OPENAI_HTTP_WRITE_TIMEOUT", "30"))
    pool = float(os.environ.get("OPENAI_HTTP_POOL_TIMEOUT", "10"))
    return Timeout(connect=connect, read=read, write=write, pool=pool)


def _openai_base_url(valves: "Valves") -> str:
    base = (valves.base_url or "").strip()
    if base:
        return base.rstrip("/")
    return "https://api.openai.com"


def _openai_headers(api_key: str, *, stream: bool) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    if stream:
        headers["accept"] = "text/event-stream"
    return headers


async def _responses_stream_via_http(
    *,
    base_url: str,
    api_key: str,
    request: dict[str, Any],
) -> AsyncGenerator[dict[str, Any], None]:
    url = f"{base_url}/v1/responses"
    headers = _openai_headers(api_key, stream=True)

    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=True)) as client:
        async with client.stream(
            "POST", url, headers=headers, json={**request, "stream": True}
        ) as r:
            if r.status_code >= 400:
                raw = await r.aread()
                text = raw.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"OpenAI /v1/responses stream failed: status={r.status_code}. body={text[:2000]}"
                )
            data_lines: list[str] = []

            async def flush() -> AsyncGenerator[dict[str, Any], None]:
                nonlocal data_lines
                if not data_lines:
                    return
                raw = "\n".join(data_lines).strip()
                data_lines = []
                if not raw or raw == "[DONE]":
                    return
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    return
                if isinstance(payload, dict):
                    yield payload

            async for line in r.aiter_lines():
                current = (line or "").rstrip("\r")
                if current == "":
                    async for payload in flush():
                        yield payload
                    continue

                stripped = current.strip()
                if not stripped or stripped.startswith(":"):
                    continue

                if stripped.startswith("data:"):
                    data_lines.append(stripped[len("data:") :].lstrip())
                    continue

            async for payload in flush():
                yield payload


async def _responses_create_via_http(
    *,
    base_url: str,
    api_key: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    url = f"{base_url}/v1/responses"
    headers = _openai_headers(api_key, stream=False)

    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=False)) as client:
        r = await client.post(url, headers=headers, json=request)
        if r.status_code >= 400:
            text = (r.text or "").strip()
            raise RuntimeError(
                f"OpenAI /v1/responses failed: status={r.status_code}. body={text[:2000]}"
            )
        payload = r.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid OpenAI response payload.")
        return payload


async def _files_create_via_http(
    *,
    base_url: str,
    api_key: str,
    filename: str,
    content_type: str,
    data: bytes,
    purpose: str = "user_data",
) -> str:
    """
    OpenAI Files API へアップロードして file_id を返す.
    """
    url = f"{base_url}/v1/files"
    headers = {"authorization": f"Bearer {api_key}"}

    safe_filename = filename.strip() or "document.pdf"
    safe_content_type = content_type.strip() or "application/octet-stream"

    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=False)) as client:
        resp = await client.post(
            url,
            headers=headers,
            data={"purpose": purpose},
            files={"file": (safe_filename, data, safe_content_type)},
        )
        if resp.status_code >= 400:
            text = (resp.text or "").strip()
            raise RuntimeError(
                f"OpenAI /v1/files failed: status={resp.status_code}. body={text[:2000]}"
            )
        payload = resp.json()

    file_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(file_id, str) or not file_id.strip():
        raise RuntimeError("OpenAI /v1/files returned no file id.")
    return file_id.strip()


def _models_list_via_http(*, base_url: str, api_key: str) -> list[str]:
    """
    OpenAI Models API から model ids を取得する.

    openai SDK が利用できない, もしくは SDK 側で models.list() が失敗する場合のフォールバック用.
    """
    url = f"{base_url}/v1/models"
    headers = _openai_headers(api_key, stream=False)

    with httpx.Client(timeout=_httpx_timeout(stream=False)) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        payload = r.json()

    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())
    return list(dict.fromkeys(model_ids))
