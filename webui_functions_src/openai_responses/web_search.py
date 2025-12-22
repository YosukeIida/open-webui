# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any

from webui_functions_src.openai_responses.upstream_parse import _response_output_items

def _collect_web_search_activity(
    resp: Any,
) -> tuple[bool, list[str], list[dict[str, str]], list[str]]:
    """
    OpenAI Responses の web_search_call を解析して, “検索が実行されたか” と sources を取り出す.

    注意:
      - sources が Web URL を返さず (例: {"type":"api","name":"oai-weather"}), URL が 0 件の場合がある.
      - その場合でも “web_search_call が存在する” = 検索（= tool）が動いた, として扱う.
    """
    used = False
    urls: list[str] = []
    items: list[dict[str, str]] = []
    non_url_sources: list[str] = []

    for item in _response_output_items(resp):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue

        used = True
        action = item.get("action")
        if not isinstance(action, dict):
            continue

        sources = action.get("sources")
        if not isinstance(sources, list):
            continue

        for src in sources:
            if not isinstance(src, dict):
                continue

            url = src.get("url")
            title = src.get("title") or src.get("name")

            if isinstance(url, str) and url.strip().startswith("http"):
                record: dict[str, str] = {"link": url.strip()}
                if isinstance(title, str) and title.strip():
                    record["title"] = title.strip()
                items.append(record)
                urls.append(url.strip())
                continue

            # URL が無い sources もログ用に拾う（例: oai-weather）
            src_type = src.get("type")
            name = title if isinstance(title, str) else ""
            if isinstance(src_type, str) and src_type.strip() and name.strip():
                non_url_sources.append(f"{src_type.strip()}:{name.strip()}")
            elif name.strip():
                non_url_sources.append(name.strip())
            elif isinstance(src_type, str) and src_type.strip():
                non_url_sources.append(src_type.strip())

    urls = list(dict.fromkeys(urls))
    non_url_sources = list(dict.fromkeys(non_url_sources))

    if items:
        seen: set[str] = set()
        dedup: list[dict[str, str]] = []
        for item in items:
            link = item.get("link", "")
            if link and link not in seen:
                seen.add(link)
                dedup.append(item)
        items = dedup

    # UI へ “URL が無い source” も伝えたいので, items に擬似的に載せる（link は空）.
    # Open WebUI 側の UI は link が無いとクリック表示されないが, 0件のままよりは理由が伝わる.
    if not items and non_url_sources:
        items = [{"link": "", "title": s} for s in non_url_sources]

    return used, urls, items, non_url_sources


def _extract_web_search_call_items_from_event(
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Responses streaming event から web_search_call item を見つける.

    OpenAI の event schema は拡張される可能性があるため, 代表的なキーを複数拾って柔軟に対応する.
    """
    candidates: list[Any] = []

    item = event.get("item")
    if isinstance(item, dict):
        candidates.append(item)

    output_item = event.get("output_item")
    if isinstance(output_item, dict):
        candidates.append(output_item)

    response_obj = event.get("response")
    if isinstance(response_obj, dict):
        candidates.extend(_response_output_items(response_obj))
        output = response_obj.get("output")
        if isinstance(output, list):
            candidates.extend(output)

    output = event.get("output")
    if isinstance(output, list):
        candidates.extend(output)

    found: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("type") == "web_search_call":
            found.append(candidate)
    return found


def _web_search_sources_from_call_item(
    item: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    urls: list[str] = []
    items: list[dict[str, str]] = []
    non_url_sources: list[str] = []

    action = item.get("action")
    if not isinstance(action, dict):
        return [], [], []

    sources = action.get("sources")
    if not isinstance(sources, list):
        return [], [], []

    for src in sources:
        if not isinstance(src, dict):
            continue

        url = src.get("url")
        title = src.get("title") or src.get("name")

        if isinstance(url, str) and url.strip().startswith("http"):
            record: dict[str, str] = {"link": url.strip()}
            if isinstance(title, str) and title.strip():
                record["title"] = title.strip()
            items.append(record)
            urls.append(url.strip())
            continue

        src_type = src.get("type")
        name = title if isinstance(title, str) else ""
        if isinstance(src_type, str) and src_type.strip() and name.strip():
            non_url_sources.append(f"{src_type.strip()}:{name.strip()}")
        elif name.strip():
            non_url_sources.append(name.strip())
        elif isinstance(src_type, str) and src_type.strip():
            non_url_sources.append(src_type.strip())

    urls = list(dict.fromkeys(urls))
    non_url_sources = list(dict.fromkeys(non_url_sources))

    if items:
        seen: set[str] = set()
        dedup: list[dict[str, str]] = []
        for item in items:
            link = item.get("link", "")
            if link and link not in seen:
                seen.add(link)
                dedup.append(item)
        items = dedup

    return urls, items, non_url_sources
