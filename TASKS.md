# TASKS

本ファイルは, Open WebUI の Functions（Pipe/Manifold/Filter）をコード管理し, OpenAI/Anthropic/Gemini を直 SDK で叩ける状態にするためのタスクリストです.

## ゴール
- Open WebUI 本体は upstream 追従を優先し, 追加機能は Functions（Pipe/Manifold/Filter）として追加する.
- OpenAI は `/v1/responses`（Responses API）を第一級に扱い, Responses 専用モデルを確実に利用できるようにする.
- Anthropic は Messages API, Gemini は generateContent をそれぞれ直 SDK で呼び出す.
- モデル一覧/パラメータ/コスト等の管理をコード（設定ファイル）で行い, 環境差分を減らす.

## 非ゴール（当面やらない）
- Open WebUI 本体の大規模改造（必要最小限のフック追加は可）.
- LiteLLM の導入（運用要件が固まったら後付けで検討）.
- compose 上での厳密なゼロダウン（k8s 移行時に実現しやすい）.

## 優先度付きタスク

### P1: 3 プロバイダの骨格を揃える（運用を安定化）
- [ ] 3 プロバイダで共通の「入力正規化→SDK 呼び出し→出力正規化」パイプラインを揃える（ストリーミング, 思考/推論表示, ツール呼び出し, 画像入出力, system/developer/user の扱い等）.
- [ ] リトライ/タイムアウト/エラーマッピング方針を統一する（指数バックオフ, 冪等性に注意）.
- [ ] （Claude）Web Search の tool error（unavailable/too_many_requests 等）を user-friendly に可視化し, 自動で「検索できない」フォールバックに入る方針を決める.
- [ ] （Provider Params）OpenAI / Gemini の UserValves（パラメータ群）を拡充し, capabilities に基づく fail-fast を導入する（未対応は無視しない）.

### P2: モデル管理をコード化（追加・変更を速くする）
- [ ] `webui_functions/config/capabilities.(json|yaml)` を導入し, モデルごとの対応機能（ツール, 画像, 最大トークン等）を定義する.
- [ ] `webui_functions/config/pricing.(json|yaml)` を導入し, モデルごとの概算コスト（入力/出力）や上限を定義する.
- [ ] Manifold の `pipes()`（モデル一覧）を API で自動取得し, 設定ファイルでフィルタできるようにする.
- [ ] Valves（UI に出すパラメータ）を capabilities に基づいて制約し, 安全な範囲で編集可能にする.

### P3: 配布・保守（Git 管理とデプロイ反映）
- [ ] compose では `seed-functions` が `webui_functions/*` を read-only で mount し, API 経由で Functions を create/update できる運用にする（Open WebUI 本体は改造しない）.
- [ ] ログの方針（PII/secret マスク, request id, provider 別）を決めて実装する.
- [ ] 最低限のユニットテスト（変換ロジック, 設定読み込み, エラーマッピング）を追加する（既存のテスト方針に合わせる）.

### P4: 追加機能（必要になってから）
- [ ] Blue/Green 方式（2 系統 pipelines + ルーティング）を検討する（compose なら前段 proxy が必要, k8s のほうが適する）.
- [ ] 利用量制限/監査ログ/ルーティング/フォールバックが必要になったら LiteLLM 等の導入を再検討する.
- [ ] （Files）画像/ドキュメント（PDF）を各プロバイダのネイティブ入力にマッピングする（OpenAI: image input, Claude: image/document blocks, Gemini: parts / files）.
- [ ] （Claude Skills）Claude向けに「skills（定型プロンプト/設定プリセット）」を導入する（UIから選択→Pipeで system/developer 指示や Valves を適用）.

## 実装メモ（設計の要点）
- Open WebUI 本体: 認証/権限/履歴/モデル公開の最終決定のみ担当し, 追加のプロバイダ統合は Functions に寄せる.
- Functions: プロバイダ差異を吸収し, 使えるモデル一覧とパラメータを「コード + 設定」で一元管理する.
- モデル露出: 標準 OpenAI 接続と Functions 経由モデルを必要に応じて分離する（表示事故の予防）.

## 導入手順（最小）
- Open WebUI 起動（推奨, prebuilt image）: `docker compose -f docker-compose.webui-only.yaml up -d`
- Functions seed: `docker compose -f docker-compose.webui-only.yaml -f docker-compose.seed-functions.yaml run --rm seed-functions`
