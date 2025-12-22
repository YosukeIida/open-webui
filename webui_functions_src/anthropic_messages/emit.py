# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import re
from typing import Any


async def _emit_status(event_emitter: Any, data: dict[str, Any]) -> None:
    if event_emitter is None:
        return
    await event_emitter({"type": "status", "data": data})


async def _emit_citation(
    event_emitter: Any,
    *,
    url: str,
    title: str | None = None,
    snippet: str | None = None,
    verified: bool = True,
) -> None:
    if event_emitter is None:
        return

    normalized_url = (url or "").strip()
    if not normalized_url:
        return

    base_name = (title or "").strip() or normalized_url
    name = base_name if verified else f"（未検証）{base_name}"
    document = (snippet or "").strip() or base_name
    if not verified:
        document = f"{document}\n\n（未検証リンク）モデルの出力に含まれていたURLです. 出典として検証していません."

    await event_emitter(
        {
            "type": "citation",
            "data": {
                "source": {"id": normalized_url, "name": name, "url": normalized_url},
                "document": [document],
                "metadata": [
                    {"source": normalized_url, "name": name, "verified": verified}
                ],
            },
        }
    )


def _should_emit_citation(
    seen_urls: dict[str, bool], url: str, *, verified: bool
) -> bool:
    """
    同一 URL の citation を重複表示しない.
    ただし, 未検証 -> 検証済み の "昇格" は許可する.
    """

    current = seen_urls.get(url)
    if current is None:
        return True
    return current is False and verified is True


def _mark_citation_seen(
    seen_urls: dict[str, bool], url: str, *, verified: bool
) -> None:
    current = seen_urls.get(url)
    if current is True:
        return
    seen_urls[url] = bool(verified)


async def _emit_citations_from_urls(
    event_emitter: Any,
    *,
    urls: list[str],
    seen_urls: dict[str, bool],
) -> None:
    for url in urls:
        normalized = (url or "").strip()
        if not normalized:
            continue
        if not _should_emit_citation(seen_urls, normalized, verified=True):
            continue
        _mark_citation_seen(seen_urls, normalized, verified=True)
        await _emit_citation(event_emitter, url=normalized, verified=True)


async def _emit_citations_from_items(
    event_emitter: Any,
    *,
    items: list[dict[str, str]],
    seen_urls: dict[str, bool],
) -> None:
    for item in items:
        url = (item.get("url") or item.get("link") or "").strip()
        if not url:
            continue
        if not _should_emit_citation(seen_urls, url, verified=True):
            continue
        _mark_citation_seen(seen_urls, url, verified=True)
        await _emit_citation(
            event_emitter,
            url=url,
            title=(item.get("title") or item.get("name") or None),
            snippet=(item.get("snippet") or item.get("text") or None),
            verified=True,
        )


_URL_RE = re.compile(r"https?://[^\s\]\)\"'>]+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for url in _URL_RE.findall(text):
        if url.endswith((".", ",", ")", "]")):
            url = url.rstrip(".,)]")
        urls.append(url)
    return list(dict.fromkeys(urls))


def _extract_citation_candidates(text: str) -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    for title, url in _MD_LINK_RE.findall(text):
        normalized = (url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((normalized, (title or "").strip() or None))

    for url in _extract_urls_from_text(text):
        normalized = (url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((normalized, None))

    return candidates


async def _emit_unverified_citations_from_text(
    event_emitter: Any,
    *,
    text: str,
    seen_urls: dict[str, bool],
) -> None:
    for url, title in _extract_citation_candidates(text):
        if not _should_emit_citation(seen_urls, url, verified=False):
            continue
        _mark_citation_seen(seen_urls, url, verified=False)
        await _emit_citation(event_emitter, url=url, title=title, verified=False)


def _looks_like_citation_item(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    url = value.get("url") or value.get("link") or value.get("uri")
    if not isinstance(url, str) or not url.strip().startswith("http"):
        return False
    title = value.get("title") or value.get("name")
    snippet = (
        value.get("snippet")
        or value.get("text")
        or value.get("cited_text")
        or value.get("excerpt")
        or value.get("description")
    )
    return isinstance(title, str) or isinstance(snippet, str)


def _is_admin_user(__user__: dict | None) -> bool:
    if not isinstance(__user__, dict):
        return False
    return __user__.get("role") == "admin"


def _to_plain_for_debug(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            pass
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return value_dict
    return str(value)


def _truncate_value(
    value: Any, *, max_string_length: int, max_depth: int, _depth: int = 0
) -> Any:
    if _depth > max_depth:
        return "<truncated:depth>"

    value = _to_plain_for_debug(value)

    if value is None or isinstance(value, (int, float, bool)):
        return value

    if isinstance(value, str):
        if len(value) <= max_string_length:
            return value
        return value[: max(0, max_string_length - 1)] + "…"

    if isinstance(value, list):
        return [
            _truncate_value(
                item,
                max_string_length=max_string_length,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        ]

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = _truncate_value(
                item,
                max_string_length=max_string_length,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return out

    return str(value)


async def _emit_debug_parameters(
    event_emitter: Any,
    *,
    provider: str,
    title: str,
    parameters: dict[str, Any],
    max_string_length: int,
    max_depth: int,
) -> None:
    if event_emitter is None:
        return

    truncated = _truncate_value(
        parameters, max_string_length=max_string_length, max_depth=max_depth
    )
    await event_emitter(
        {
            "type": "citation",
            "data": {
                "source": {
                    "id": f"debug:{provider}:{title}",
                    "name": f"Debug ({provider}) {title}",
                },
                "document": ["Debug payload. See Parameters."],
                "metadata": [{"parameters": truncated}],
            },
        }
    )
