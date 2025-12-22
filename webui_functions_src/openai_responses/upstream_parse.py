# NOTE: このファイルは `bundle.txt` に従って 1 つの Function code に結合されます.
from typing import Any

from webui_functions_src.openai_responses.emit import _to_plain

def _response_output_items(resp: Any) -> list[dict[str, Any]]:
    plain = _to_plain(resp)
    if isinstance(plain, dict):
        output = plain.get("output")
        return output if isinstance(output, list) else []
    return []


def _collect_output_text(resp: Any) -> str:
    plain = _to_plain(resp)
    if isinstance(plain, dict) and isinstance(plain.get("output_text"), str):
        return plain["output_text"]

    texts: list[str] = []
    for item in _response_output_items(resp):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "".join(texts)


def _collect_reasoning_summary(resp: Any) -> str:
    plain = _to_plain(resp)
    if isinstance(plain, dict):
        reasoning = plain.get("reasoning")
        if isinstance(reasoning, dict):
            summary = reasoning.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return ""


def _collect_url_citations(resp: Any) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for item in _response_output_items(resp):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            annotations = block.get("annotations")
            if not isinstance(annotations, list):
                continue
            for ann in annotations:
                if isinstance(ann, dict) and ann.get("type") == "url_citation":
                    citations.append(ann)
    return citations
