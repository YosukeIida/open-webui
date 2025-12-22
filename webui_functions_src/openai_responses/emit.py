# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import re
from typing import Any

_URL_RE = re.compile(r"https?://[^\s\]\)\"'>]+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for url in _URL_RE.findall(text or ""):
        if url.endswith((".", ",", ")", "]")):
            url = url.rstrip(".,)]")
        urls.append(url)
    return list(dict.fromkeys(urls))


def _extract_citation_candidates(text: str) -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    for title, url in _MD_LINK_RE.findall(text or ""):
        normalized = (url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((normalized, (title or "").strip() or None))

    for url in _extract_urls_from_text(text or ""):
        normalized = (url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((normalized, None))

    return candidates


def _to_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool, dict, list)):
        return obj
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            pass
    obj_dict = getattr(obj, "__dict__", None)
    if isinstance(obj_dict, dict):
        return obj_dict
    return obj


async def _emit_status(event_emitter: Any, data: dict[str, Any]) -> None:
    if event_emitter is None:
        return
    await event_emitter({"type": "status", "data": data})


async def _emit_citation(
    event_emitter: Any,
    *,
    url: str,
    kind: str,
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
    if kind == "search_result":
        name = f"検索結果: {base_name}"
    elif kind == "evidence":
        name = f"根拠: {base_name}"
    else:
        name = base_name
    if not verified:
        name = f"（未検証）{name}"
    document = (snippet or "").strip() or base_name
    if not verified:
        document = f"{document}\n\n（未検証リンク）モデルの出力に含まれていたURLです. 出典として検証していません."

    await event_emitter(
        {
            "type": "citation",
            "data": {
                "source": {
                    "id": f"{kind}:{normalized_url}",
                    "name": name,
                    "url": normalized_url,
                },
                "document": [document],
                "metadata": [
                    {
                        "source": normalized_url,
                        "name": name,
                        "verified": verified,
                        "kind": kind,
                    }
                ],
            },
        }
    )


async def _emit_info_citation(
    event_emitter: Any,
    *,
    kind: str,
    title: str,
    document: str,
    verified: bool = True,
) -> None:
    """
    URL を持たない情報表示用 citation.

    例: web_search_call.action.sources が {"type":"api","name":"oai-weather"} のように
    URL を返さないケース（内部 API や provider 側の非URLソース）.
    """
    if event_emitter is None:
        return

    safe_title = (title or "").strip()
    if not safe_title:
        return

    await event_emitter(
        {
            "type": "citation",
            "data": {
                "source": {
                    "id": f"{kind}:{safe_title}",
                    "name": safe_title,
                },
                "document": [document.strip() or safe_title],
                "metadata": [{"name": safe_title, "verified": verified, "kind": kind}],
            },
        }
    )


async def _emit_unverified_citations_from_text(
    event_emitter: Any,
    *,
    text: str,
    seen_urls: set[str],
) -> None:
    for url, title in _extract_citation_candidates(text):
        if url in seen_urls:
            continue
        seen_urls.add(url)
        await _emit_citation(
            event_emitter,
            url=url,
            title=title,
            kind="unverified",
            verified=False,
        )


async def _emit_verified_citations_from_url_citations(
    event_emitter: Any,
    *,
    url_citations: list[dict[str, Any]],
    output_text: str | None,
    seen_urls: set[str],
) -> None:
    for citation in url_citations:
        if not isinstance(citation, dict):
            continue
        if (citation.get("type") or "") != "url_citation":
            continue

        url = (citation.get("url") or "").strip()
        if not url:
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = (
            citation.get("title") if isinstance(citation.get("title"), str) else None
        )
        snippet: str | None = None
        if isinstance(output_text, str):
            start = citation.get("start_index")
            end = citation.get("end_index")
            if (
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start < end <= len(output_text)
            ):
                excerpt = output_text[start:end].strip()
                if excerpt:
                    snippet = excerpt

        await _emit_citation(
            event_emitter,
            url=url,
            title=title,
            snippet=snippet,
            kind="evidence",
            verified=True,
        )


async def _emit_search_result_citations_from_source_items(
    event_emitter: Any,
    *,
    items: list[dict[str, str]],
    seen_urls: set[str],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        url = (item.get("link") or "").strip()
        if not url:
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = (item.get("title") or "").strip() or None
        await _emit_citation(
            event_emitter,
            url=url,
            title=title,
            kind="search_result",
            verified=True,
        )


async def _emit_search_result_info_sources(
    event_emitter: Any,
    *,
    sources: list[str],
    seen_sources: set[str],
) -> None:
    for src in sources:
        normalized = (src or "").strip()
        if not normalized:
            continue
        if normalized in seen_sources:
            continue
        seen_sources.add(normalized)
        await _emit_info_citation(
            event_emitter,
            kind="search_source",
            title=f"検索結果: {normalized}",
            document=(
                "Web検索の source は URL を返しませんでした. "
                f"内部 API / provider の非URLソースの可能性があります: {normalized}"
            ),
            verified=True,
        )


def _is_admin_user(__user__: dict | None) -> bool:
    return isinstance(__user__, dict) and __user__.get("role") == "admin"


def _truncate_value(
    value: Any, *, max_string_length: int, max_depth: int, _depth: int = 0
) -> Any:
    if _depth > max_depth:
        return "<truncated:depth>"

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

    return _truncate_value(
        _to_plain(value),
        max_string_length=max_string_length,
        max_depth=max_depth,
        _depth=_depth + 1,
    )


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
