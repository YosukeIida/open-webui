# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any

from webui_functions_src.gemini_generatecontent.emit import _extract_urls_from_text


def _coalesce_web_search_backend(metadata: dict[str, Any], default_backend: str) -> str:
    backend = metadata.get("pipe_web_search_backend")
    if isinstance(backend, str) and backend.strip():
        return backend.strip().lower()
    return (default_backend or "").strip().lower()


def _with_system_note(system: str, note: str) -> str:
    if system.strip():
        return f"{system.strip()}\n\n{note.strip()}"
    return note.strip()


def _deep_find_urls(obj: Any) -> list[str]:
    def _walk(value: Any, seen: set[int], depth: int) -> list[str]:
        if depth > 10:
            return []

        if value is None:
            return []

        if isinstance(value, str):
            return _extract_urls_from_text(value)

        if isinstance(value, (int, float, bool)):
            return []

        value_id = id(value)
        if value_id in seen:
            return []
        seen.add(value_id)

        if isinstance(value, dict):
            urls: list[str] = []
            for item in value.values():
                urls.extend(_walk(item, seen, depth + 1))
            return urls

        if isinstance(value, list):
            urls: list[str] = []
            for item in value:
                urls.extend(_walk(item, seen, depth + 1))
            return urls

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return _walk(model_dump(), seen, depth + 1)
            except Exception:
                pass

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return _walk(to_dict(), seen, depth + 1)
            except Exception:
                pass

        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            return _walk(value_dict, seen, depth + 1)

        return []

    return list(dict.fromkeys(_walk(obj, set(), 0)))


def _looks_like_citation_item(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    url = value.get("uri") or value.get("url") or value.get("link")
    if not isinstance(url, str) or not url.strip().startswith("http"):
        return False
    title = value.get("title") or value.get("name")
    snippet = value.get("snippet") or value.get("text") or value.get("excerpt")
    return isinstance(title, str) or isinstance(snippet, str)


def _deep_find_citation_items(obj: Any) -> list[dict[str, str]]:
    def _walk(value: Any, seen: set[int], depth: int) -> list[dict[str, str]]:
        if depth > 10:
            return []

        if value is None or isinstance(value, (int, float, bool)):
            return []

        if isinstance(value, str):
            return []

        value_id = id(value)
        if value_id in seen:
            return []
        seen.add(value_id)

        if isinstance(value, dict):
            items: list[dict[str, str]] = []
            if _looks_like_citation_item(value):
                url = (
                    value.get("uri") or value.get("url") or value.get("link") or ""
                ).strip()
                title = value.get("title") or value.get("name") or ""
                snippet = (
                    value.get("snippet")
                    or value.get("text")
                    or value.get("excerpt")
                    or ""
                )
                record: dict[str, str] = {"url": url}
                if isinstance(title, str) and title.strip():
                    record["title"] = title.strip()
                if isinstance(snippet, str) and snippet.strip():
                    record["snippet"] = snippet.strip()
                items.append(record)

            for child in value.values():
                items.extend(_walk(child, seen, depth + 1))
            return items

        if isinstance(value, list):
            items: list[dict[str, str]] = []
            for child in value:
                items.extend(_walk(child, seen, depth + 1))
            return items

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return _walk(model_dump(), seen, depth + 1)
            except Exception:
                pass

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return _walk(to_dict(), seen, depth + 1)
            except Exception:
                pass

        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            return _walk(value_dict, seen, depth + 1)

        return []

    raw_items = _walk(obj, set(), 0)
    dedup: dict[str, dict[str, str]] = {}
    for item in raw_items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if url not in dedup:
            dedup[url] = item
    return list(dedup.values())
