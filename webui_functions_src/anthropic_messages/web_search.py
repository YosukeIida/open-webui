# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any


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
                    value.get("url") or value.get("link") or value.get("uri") or ""
                ).strip()
                title = value.get("title") or value.get("name") or ""
                snippet = (
                    value.get("snippet")
                    or value.get("text")
                    or value.get("cited_text")
                    or value.get("excerpt")
                    or value.get("description")
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


def _deep_find_queries(obj: Any) -> list[str]:
    def _walk(value: Any, seen: set[int], depth: int) -> list[str]:
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
            queries: list[str] = []
            for k, v in value.items():
                if isinstance(k, str) and k in (
                    "query",
                    "search_query",
                    "q",
                    "queries",
                    "search_terms",
                ):
                    if isinstance(v, str) and v.strip():
                        queries.append(v.strip())
                    elif isinstance(v, list):
                        queries.extend(
                            [s.strip() for s in v if isinstance(s, str) and s.strip()]
                        )
                queries.extend(_walk(v, seen, depth + 1))
            return queries

        if isinstance(value, list):
            queries: list[str] = []
            for child in value:
                queries.extend(_walk(child, seen, depth + 1))
            return queries

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return _walk(model_dump(), seen, depth + 1)
            except Exception:
                pass

        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            return _walk(value_dict, seen, depth + 1)

        return []

    found = _walk(obj, set(), 0)
    return list(dict.fromkeys([q for q in found if q]))


def _deep_has_web_search_tool_activity(obj: Any) -> bool:
    """
    provider Web Search(tool) が実行された痕跡を検出する.

    注意:
    - URL が本文に含まれるだけでは「検索した」とは断定しない（幻の検索ログを避ける）.
    - stream/non-stream どちらでも使えるよう, tool ブロックや usage の server_tool_use を広めに拾う.
    """

    def _walk(value: Any, seen: set[int], depth: int) -> bool:
        if depth > 12:
            return False
        if value is None or isinstance(value, (int, float, bool)):
            return False
        if isinstance(value, str):
            return False

        value_id = id(value)
        if value_id in seen:
            return False
        seen.add(value_id)

        if isinstance(value, dict):
            value_type = value.get("type")
            if isinstance(value_type, str) and value_type in (
                "web_search_tool_result",
                "web_search_result",
                "web_search_tool_result_error",
            ):
                return True
            if isinstance(value_type, str) and value_type == "server_tool_use":
                name = value.get("name")
                if isinstance(name, str) and name.strip() == "web_search":
                    return True

            # usage.server_tool_use.web_search_requests など
            server_tool_use = value.get("server_tool_use")
            if isinstance(server_tool_use, dict):
                requests = server_tool_use.get("web_search_requests")
                try:
                    if int(requests or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
            for child in value.values():
                if _walk(child, seen, depth + 1):
                    return True
            return False

        if isinstance(value, list):
            for child in value:
                if _walk(child, seen, depth + 1):
                    return True
            return False

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                if _walk(model_dump(), seen, depth + 1):
                    return True
            except Exception:
                pass

        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            return _walk(value_dict, seen, depth + 1)

        return False

    return _walk(obj, set(), 0)


def _deep_find_web_search_tool_errors(obj: Any) -> list[str]:
    def _walk(value: Any, seen: set[int], depth: int) -> list[str]:
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
            codes: list[str] = []
            value_type = value.get("type")
            if (
                isinstance(value_type, str)
                and value_type == "web_search_tool_result_error"
            ):
                error_code = value.get("error_code")
                if isinstance(error_code, str) and error_code.strip():
                    codes.append(error_code.strip())
            for child in value.values():
                codes.extend(_walk(child, seen, depth + 1))
            return codes

        if isinstance(value, list):
            codes: list[str] = []
            for child in value:
                codes.extend(_walk(child, seen, depth + 1))
            return codes

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return _walk(model_dump(), seen, depth + 1)
            except Exception:
                pass

        value_dict = getattr(value, "__dict__", None)
        if isinstance(value_dict, dict):
            return _walk(value_dict, seen, depth + 1)

        return []

    found = _walk(obj, set(), 0)
    return list(dict.fromkeys([c for c in found if c]))


def _coalesce_web_search_backend(metadata: dict[str, Any], default_backend: str) -> str:
    backend = metadata.get("pipe_web_search_backend")
    if isinstance(backend, str) and backend.strip():
        return backend.strip().lower()
    return (default_backend or "").strip().lower()


def _with_system_note(system: str, note: str) -> str:
    if system.strip():
        return f"{system.strip()}\n\n{note.strip()}"
    return note.strip()
