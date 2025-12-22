# Functions seed（本体無改造運用）

このリポジトリの Functions を Open WebUI の Functions（Pipe/Manifold/Filter）として登録し, DBへ反映するための手順です.

- `webui_functions/*.py`: 単一ファイル版（そのまま seed 可能）
- `webui_functions_src/*/`: 分割版（seed 時に結合して投入）

## 前提
- Open WebUI の admin ユーザが存在する
- admin ユーザの API key（`sk-...`）を発行済み

API key は Open WebUI の `POST /api/v1/auths/api_key` で発行できます（ログイン済みの admin で実行）.
発行できない（`POST /api/v1/auths/api_key` が 403）場合は, Admin Panel の `Settings -> General -> Enable API Key` を ON にして保存してください（設定は保存するまで反映されません）.

## 使い方（docker compose）
1) `api_key.env` に `WEBUI_SEED_API_KEY` と各プロバイダの API key を設定（クォート不要）
2) Open WebUI を起動
3) seed を実行

```
docker compose -f docker-compose.yaml up -d
docker compose -f docker-compose.yaml -f docker-compose.seed-functions.yaml run --rm seed-functions
```

Ollama を使わない場合は, `docker-compose.webui-only.yaml` を利用できます.

```
docker compose -f docker-compose.webui-only.yaml up -d
docker compose -f docker-compose.webui-only.yaml -f docker-compose.seed-functions.yaml run --rm seed-functions
```

## 注意
- `webui_functions/*.py` の frontmatter に `requirements:` は入れない（seedスクリプトが失敗します）.
- `webui_functions_src/*/` を利用する場合も, 結合後の先頭 docstring に `requirements:` は入れないでください.
- `POST /api/v1/functions/sync` はDB内のFunctionsを削除し得るため, seed用途には使いません.
- Open WebUI の `GET /api/v1/functions/id/{id}` は, Functions が存在しない場合でも 401 を返すことがあります（`detail` が `We could not find what you're looking for :/`）. seed はこれを「未作成」として扱います.
- `User Valves` が UI に出ない場合は, seed 実行後に Open WebUI を再起動し, `GET /api/v1/functions/id/{id}/valves/user/spec` が `null` 以外になることを確認してください.

## 分割版（`webui_functions_src`）の設計
- Open WebUI は DB に保存した Function code を「単一の .py」としてロードする前提になりがちです.
- そのため, `webui_functions_src/<function_id>/` 配下のファイルを結合し, 単一ファイルとして API 経由で投入します.
- 結合順は `bundle.txt`（推奨）で明示できます. `bundle.txt` が無い場合は `*.py` をファイル名の昇順で結合します.
- 先頭ファイルには, 必ず `"""name: ...` / `description: ...` の frontmatter docstring を置いてください.
- 開発時は分割ファイルを「本物のモジュール」として扱えるよう, 各ファイルに必要な import を置き, 相互参照も import で解決して構いません.
- seed 時は `scripts/bundle-function.py` が内部モジュール import を取り除いて 1 ファイル化します（DB 実行環境で import が成立しないため）.
- `if __name__ == "__main__":` は DB 実行環境で意味が変わるため禁止です（seed がエラーにします）.
- seed は結合したファイルに `# --- BEGIN ... ---` / `# --- END ... ---` を挿入します（incident 時にどの部品由来か追いやすくするため）.

### 推奨ファイル構成（全 provider 共通）
provider 間の揃いを優先し, 機能（責務）で分割します.

- `header.py`: frontmatter docstring（+ module-level constants が必要ならここ）
- `models.py`: capabilities, フィルタ, 小さな dataclass
- `normalize.py`: 入力正規化（messages 変換など）
- `web_search.py`: Router metadata 解釈, 検索ポリシー/結果の抽出
- `emit.py`: status/citation/debug の emit, URL 抽出
- `upstream_parse.py`: provider response の解析（output_text/usage/citations 等）
- `upstream_http.py`: provider HTTP fallback（stream/non-stream）
- `config.py`: Valves/UserValves
- `preflight.py`: capabilities 変換/安全化（必要な provider のみ）
- `pipe.py`: `class Pipe`（orchestrator）

## Pipe/Manifold の Web Search について
- Open WebUI の標準 Web Search（クエリ生成 + 検索エンジン呼び出し）は, Open WebUI 本体側の機能です.
- 本リポジトリは「Open WebUI 本体の検索エンジン設定を使わず, provider 側の tool で検索する」方式を優先します.
  - OpenAI: Responses API `web_search` tool
  - Anthropic: server tool `web_search_20250305`
  - Gemini: `tools: [{google_search: {}}]`（Google Search grounding）
- 検索の実行は, `Pipe Web Search Router` が `pipe_web_search_enabled` を付与することで制御します（後述）.
  - そのため, Pipe/Manifold では Open WebUI の Web Search トグル（`features.web_search`）は使用しません.

### Web Search トグルを OFF にしたまま使う
- `webui_functions_src/provider_web_search_router/`（UI表示名: Pipe Web Search Router）は, デフォルト設定では「Pipe 側の Web Search」を強制的に有効化して検索を実行します.
  - これにより, Open WebUI の Web Search トグルや検索エンジン設定（API key 等）を使わずに, provider 側の tool だけで検索できます.
  - `force_web_search_when_filter_enabled=false` にすると, 従来どおり `features.web_search=true`（UI側のWeb Search）に依存する挙動へ戻せます.
  - また, Filter が有効な場合は Open WebUI 側の Web Search（クエリ生成/検索エンジン呼び出し）は実行しません（Pipe 側の検索toolに寄せます）.
  - follow-ups/title/tags など Open WebUI の背景タスクは検索を強制しません（意図せず検索コストが増えないようにするため）.

### Open WebUI 側の検索APIキーを使いたくない場合
- OpenAI: `web_search_backend=provider` を使うと, OpenAI 側の `web_search` tool を使用します（Open WebUI の検索エンジン設定は不要）.
- Anthropic: `web_search_20250305` の server tool を使用します（Anthropic Console 側で Web Search 有効化が必要）.
- Gemini: `tools: [{google_search: {}}]` を有効化して Google Search grounding を使用します（Google 側の設定/契約が必要）.
- いずれも Open WebUI 側の検索エンジンAPIキーは不要です.

## Pipe Web Search Router について
- `Pipe Web Search Router` は toggleable filter です（UIでON/OFFできます）.
  - 本リポジトリの compose（`docker-compose.webui-only.yaml`）では, Pipe/Manifold モデルの `defaultFilterIds` によりデフォルトONになります.
  - OFFにしたい場合は, チャットの Integrations メニューから当該FilterをOFFにしてください.
  - Filter 有効時は, Open WebUI 側の検索エンジン設定を使わず provider 側の検索toolを利用します.
  - 検索は `pipe_web_search_policy`（`auto|required|off`）で制御します.
    - 既定は `auto`（必要時のみ検索）.
    - ユーザープロンプトが「検索して/調べて/出典/引用」等を含む場合は `required` に昇格します（regexで判定）.
    - 「検索しない/調べない」等を含む場合は `off` にします.

## 思考（Reasoning/Thinking）と引用（Sources）のリアルタイム表示について
- Open WebUI は, ストリーミング応答（SSE相当の差分）で `delta.reasoning_content` を受け取ると, 画面上で `<think>...</think>` として表示できます.
  - OpenAI: `webui_functions_src/openai_responses/` は Responses の reasoning/summary 系 delta を `reasoning_content` にマッピングします.
  - Anthropic: `webui_functions_src/anthropic_messages/` は `thinking_delta` / `reasoning_delta` を `reasoning_content` にマッピングします（`thinking_enabled=true` の場合）.
  - Gemini: `webui_functions_src/gemini_generatecontent/` は thought を `reasoning_content` にマッピングします（`include_thoughts=true` の場合）.
- 引用（Sources）は `citation` イベントとして増分送信し, `Sources` パネルに表示します.
  - provider の検索toolが実行された場合は, tool 由来の結果（URL/title/snippet 等）のみを「出典（verified）」として扱います.
  - tool が実行されなかった場合は, 出力内URLを「未検証リンク（未検証）」として表示します（モデルが生成したURLを出典扱いしないため）.
  - follow-ups/title/tags など Open WebUI の背景タスクでは, 意図せず増えるのを避けるため citation を送信しません.

## User Valves（ユーザーが調整する設定）について
- 本リポジトリの Pipe/Manifold は, Open WebUI の「User Valves（ユーザー設定）」を提供します.
  - OpenAI: `openai_responses`（`max_output_tokens`, `reasoning_*`, `temperature/top_p` 等）
  - Anthropic: `anthropic_messages`（`thinking_*`, `effort_*`, `service_tier`, `metadata_user_id` 等）
  - Gemini: `gemini_generatecontent`（`include_thoughts`, `max_output_tokens`, `temperature/top_p` 等）
- 設定場所:
  - チャット画面でモデルを選択 → 入力欄右のつまみアイコン（Valves） → 対象 Function の `User Valves` を更新（通常ユーザーでも自分の設定を変更可能）.
- `max_tokens` は既定で大きめ（例: 64k）にしています. Anthropic は `max_tokens > 21333` の場合にストリーミングが必須になるため, 非ストリームで呼ばれた場合は fail-fast でエラーになります.
- Open WebUI の User Valves は Function 単位で永続化されるため, モデルを切り替えると「前のモデルで有効だったトグル」が残ることがあります.
  - 例: `output_128k_enabled=true` のまま Sonnet 4.5 に切り替える等.
  - 本リポジトリの Pipe は, モデル非対応の User Valves を自動で無視し, `StatusHistory` に警告を表示します（呼び出しは継続）.
- セキュリティのため, User Valves はホワイトリスト（UserValves）で検証し, 想定外のキーは無視します（`api_key_env` や `extra_json` 等をユーザー側から上書きできません）.

## デバッグ（incident用途, 管理者限定）
- `debug_enabled=true` にすると, provider に送った request / 受け取った response の要約を `Sources` に `Debug (...) request/response` として表示します（管理者のみ）.
  - 事故防止のため, 管理者以外が `debug_enabled=true` を設定した場合は fail-fast でエラーになります.
  - follow-ups/title/tags 等の背景タスクでもデバッグ出力を行います（`debug_enabled=true`, 管理者のみ）.

## Anthropic（Claude）の追加パラメータについて
- 共通パラメータ（`max_tokens`, `temperature`, `top_p`）は Pipe の valves または Open WebUI のパラメータから設定できます.
- Claude 固有:
  - `thinking_enabled` / `thinking_budget_tokens`（raw thinking のストリーム表示）
  - `interleaved_thinking_enabled`（Claude 4 の interleaved thinking, beta header を自動付与）
  - `top_k`, `stop_sequences`
  - `effort_enabled` / `effort_level`（Opus 4.5 の `output_config.effort` を想定, beta header が必要な場合は `anthropic_beta_header` にカンマ区切りで設定）
  - Web Search / effort の beta header は `auto_append_beta_headers=true` の場合に自動付与します（必要に応じて `anthropic_beta_header` で追加できます）.
  - Web Search tool の暴走（検索回数の増加）を避けるため, `web_search_max_uses` を既定 `3` として付与します（User Valves からは変更できません）.
- 設定場所:
  - チャット画面で Anthropic のモデルを選択 → 入力欄右のつまみアイコン（Valves） → `anthropic_messages` の `User Valves` を更新（通常ユーザーでも自分の設定を変更可能）.

## OpenAI Responses の注意
- OpenAI Responses API の `input` は role によって content type が異なります（例: `user` は `input_text`, `assistant` は `output_text`）. 本リポジトリの Pipe はこれを変換して送信します.
