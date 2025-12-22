# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
import re
from typing import Any


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
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
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


async def _emit_citations_from_urls(
    event_emitter: Any,
    *,
    urls: list[str],
    seen_urls: set[str],
) -> None:
    for url in urls:
        normalized = (url or "").strip()
        if not normalized or normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        await _emit_citation(event_emitter, url=normalized)


async def _emit_citations_from_items(
    event_emitter: Any,
    *,
    items: list[dict[str, str]],
    seen_urls: set[str],
) -> None:
    for item in items:
        url = (item.get("url") or item.get("link") or item.get("uri") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        await _emit_citation(
            event_emitter,
            url=url,
            title=(item.get("title") or item.get("name") or None),
            snippet=(item.get("snippet") or item.get("text") or None),
        )


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


async def _emit_citations_from_text(
    event_emitter: Any,
    *,
    text: str,
    seen_urls: set[str],
) -> None:
    for url, title in _extract_citation_candidates(text):
        if url in seen_urls:
            continue
        seen_urls.add(url)
        await _emit_citation(event_emitter, url=url, title=title)


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
        await _emit_citation(event_emitter, url=url, title=title, verified=False)
