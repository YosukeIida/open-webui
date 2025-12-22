# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from httpx import Timeout


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _httpx_timeout(*, stream: bool) -> Timeout:
    # Valveには出さない. 必要なら env で上書きする.
    connect = _float_env("ANTHROPIC_HTTP_CONNECT_TIMEOUT", 10.0)
    read_default = 600.0 if stream else 120.0
    read = _float_env("ANTHROPIC_HTTP_READ_TIMEOUT", read_default)
    write = _float_env("ANTHROPIC_HTTP_WRITE_TIMEOUT", 30.0)
    pool = _float_env("ANTHROPIC_HTTP_POOL_TIMEOUT", 10.0)
    return Timeout(connect=connect, read=read, write=write, pool=pool)


def _anthropic_api_key_from_env(valves: "Valves") -> str:
    api_key = os.environ.get(valves.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"Missing API key env var: {valves.api_key_env}")
    return api_key


def _anthropic_headers(
    *,
    api_key: str,
    beta_headers: list[str],
    stream: bool,
) -> dict[str, str]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    uniq = list(
        dict.fromkeys(
            [h.strip() for h in beta_headers if isinstance(h, str) and h.strip()]
        )
    )
    if uniq:
        headers["anthropic-beta"] = ",".join(uniq)
    if stream:
        headers["accept"] = "text/event-stream"
    return headers


def _anthropic_files_headers(*, api_key: str, beta_headers: list[str]) -> dict[str, str]:
    """
    Files API 用の headers（multipart/form-data なので content-type は付けない）.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    uniq = list(
        dict.fromkeys(
            [h.strip() for h in beta_headers if isinstance(h, str) and h.strip()]
        )
    )
    if uniq:
        headers["anthropic-beta"] = ",".join(uniq)
    return headers


async def _anthropic_files_create_via_http(
    *,
    api_key: str,
    beta_headers: list[str],
    filename: str,
    content_type: str,
    data: bytes,
) -> str:
    """
    Anthropic Files API へアップロードして file_id を返す.
    """
    url = "https://api.anthropic.com/v1/files"
    headers = _anthropic_files_headers(api_key=api_key, beta_headers=beta_headers)

    safe_filename = filename.strip() or "document.pdf"
    safe_content_type = content_type.strip() or "application/octet-stream"

    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=False)) as client:
        resp = await client.post(
            url,
            headers=headers,
            files={"file": (safe_filename, data, safe_content_type)},
        )
        resp.raise_for_status()
        payload = resp.json()

    file_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(file_id, str) or not file_id.strip():
        raise RuntimeError("Invalid Anthropic file upload response (missing id).")
    return file_id.strip()


def _anthropic_models_list_via_http(
    *, api_key: str, beta_headers: list[str]
) -> list[str]:
    url = "https://api.anthropic.com/v1/models"
    headers = _anthropic_headers(
        api_key=api_key, beta_headers=beta_headers, stream=False
    )

    with httpx.Client(timeout=_httpx_timeout(stream=False)) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(model_id, str) and model_id.strip():
            out.append(model_id.strip())
    return out


async def _anthropic_stream_via_http(
    *,
    api_key: str,
    beta_headers: list[str],
    request: dict[str, Any],
) -> AsyncGenerator[dict[str, Any], None]:
    """
    anthropic==0.43.0 は `messages.stream()` が新しい引数（例: output_config）を受け付けない.
    その場合でも API 機能を使えるよう, HTTP(SSE)を自前で処理する.
    """

    url = "https://api.anthropic.com/v1/messages"
    headers = _anthropic_headers(
        api_key=api_key, beta_headers=beta_headers, stream=True
    )

    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=True)) as client:
        async with client.stream(
            "POST", url, headers=headers, json=request
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []

            async def flush() -> AsyncGenerator[dict[str, Any], None]:
                nonlocal data_lines
                if not data_lines:
                    return
                raw = "\n".join(data_lines).strip()
                data_lines = []
                if not raw:
                    return
                if raw == "[DONE]":
                    return
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    return
                if isinstance(payload, dict):
                    yield payload

            async for line in response.aiter_lines():
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
                # event: は data の中に type が含まれるため, ここでは無視する.

            async for payload in flush():
                yield payload


async def _anthropic_create_via_http(
    *,
    api_key: str,
    beta_headers: list[str],
    request: dict[str, Any],
) -> dict[str, Any]:
    url = "https://api.anthropic.com/v1/messages"
    headers = _anthropic_headers(
        api_key=api_key, beta_headers=beta_headers, stream=False
    )
    async with httpx.AsyncClient(timeout=_httpx_timeout(stream=False)) as client:
        response = await client.post(url, headers=headers, json=request)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid Anthropic response payload.")
        return payload
