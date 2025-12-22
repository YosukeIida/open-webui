#!/usr/bin/env python3
"""
Open WebUI Functions bundler.

`webui_functions_src/<function_id>/` の複数ファイルを, seed 時に 1 つの .py に結合するためのスクリプトです.

設計意図:
- 開発時は各ファイルを「本物のモジュール」として成立させ, editor/lint/type-check を通しやすくする.
- DB へ投入する最終コードは 1 ファイルのため, 内部モジュール import は結合時に取り除く.
"""

import argparse
import re
from pathlib import Path


def _trim_and_unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and (
        (value[0] == value[-1] == "'") or (value[0] == value[-1] == '"')
    ):
        return value[1:-1]
    return value


def _read_manifest(manifest_path: Path) -> list[str]:
    parts: list[str] = []
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\r")
        line = _trim_and_unquote(line)
        if not line or line.lstrip().startswith("#"):
            continue
        parts.append(line)
    return parts


def _is_first_file_frontmatter(path: Path) -> bool:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except Exception:
        return False
    return first_line.startswith('"""')


def _strip_internal_imports(
    *,
    content: str,
    function_id: str,
) -> str:
    """
    Open WebUI の DB 実行環境では「分割ファイルの import」が成立しないため, bundling 時に内部 import を取り除く.

    NOTE:
    - ここで取り除くのは「トップレベルの import 文」のみ.
    - `if TYPE_CHECKING:` 配下など, インデントされた import は実行されない前提で残す.
    """

    internal_from_patterns = [
        re.compile(r"^from\s+\.+"),  # from .foo import ...
        re.compile(rf"^from\s+webui_functions_src\.{re.escape(function_id)}(\.|\s)"),
    ]
    internal_import_patterns = [
        re.compile(
            rf"^import\s+webui_functions_src\.{re.escape(function_id)}(\.|\s|$)"
        ),
    ]

    out_lines: list[str] = []
    skipping = False
    paren_balance = 0
    backslash_continuation = False

    for line in content.splitlines(keepends=True):
        if skipping:
            paren_balance += line.count("(") - line.count(")")
            backslash_continuation = line.rstrip().endswith("\\")
            if paren_balance <= 0 and not backslash_continuation:
                skipping = False
            continue

        # トップレベルのみを対象にする（先頭が空白なら残す）.
        if line[:1].isspace():
            out_lines.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue

        is_internal = False
        if stripped.startswith("from "):
            is_internal = any(
                p.search(stripped) is not None for p in internal_from_patterns
            )
        elif stripped.startswith("import "):
            is_internal = any(
                p.search(stripped) is not None for p in internal_import_patterns
            )

        if not is_internal:
            out_lines.append(line)
            continue

        # internal import の開始. 続き行（括弧/バックスラッシュ）もまとめて skip.
        skipping = True
        paren_balance = line.count("(") - line.count(")")
        backslash_continuation = line.rstrip().endswith("\\")
        if paren_balance <= 0 and not backslash_continuation:
            skipping = False

    return "".join(out_lines)


def bundle_function(*, src_dir: Path, function_id: str, out_file: Path) -> None:
    manifest = src_dir / "bundle.txt"
    if manifest.exists():
        part_names = _read_manifest(manifest)
        parts = [src_dir / name for name in part_names]
    else:
        parts = sorted([p for p in src_dir.glob("*.py") if p.name != "__init__.py"])

    if not parts:
        raise SystemExit(f"No .py parts found under {src_dir}")

    if not _is_first_file_frontmatter(parts[0]):
        raise SystemExit(
            f"First bundled file must start with a triple-quote frontmatter docstring: {parts[0]}"
        )

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("", encoding="utf-8")

    for part in parts:
        if not part.exists():
            raise SystemExit(
                f"Bundled part not found: {part} (function_id={function_id})"
            )

        raw = part.read_text(encoding="utf-8")
        if 'if __name__ == "__main__":' in raw or "if __name__ == '__main__':" in raw:
            raise SystemExit(
                f"__main__ block is not allowed in bundled functions: {part}"
            )

        rel = part.relative_to(src_dir.parent)
        with out_file.open("a", encoding="utf-8") as f:
            f.write(f"\n# --- BEGIN {rel.as_posix()} ---\n")
            f.write(_strip_internal_imports(content=raw, function_id=function_id))
            f.write(f"\n# --- END {rel.as_posix()} ---\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--src-dir", required=True, help="webui_functions_src/<function_id> directory"
    )
    parser.add_argument(
        "--function-id", required=True, help="function id (directory name)"
    )
    parser.add_argument("--out-file", required=True, help="output .py path")
    args = parser.parse_args()

    bundle_function(
        src_dir=Path(args.src_dir).resolve(),
        function_id=str(args.function_id),
        out_file=Path(args.out_file).resolve(),
    )


if __name__ == "__main__":
    main()
