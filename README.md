# zen-free-proxy

[![PyPI](https://img.shields.io/pypi/v/zen-free-proxy.svg)](https://pypi.org/project/zen-free-proxy/)
[![Python](https://img.shields.io/pypi/pyversions/zen-free-proxy.svg)](https://pypi.org/project/zen-free-proxy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/nennneko5787/opencode-zen-server/actions/workflows/ci.yml/badge.svg)](https://github.com/nennneko5787/opencode-zen-server/actions/workflows/ci.yml)

OpenCode Zen の無料モデルだけを中継する、OpenAI 互換のリバースプロキシ。

- **クライアントの API キーは何でもいい**（`sk-Whatever` でも空でも通る）。上流用の実キーはプロキシ側だけが保持します。
- **モデルの一覧はハードコードしていません。** 無料かどうか・どの上流ルートで配信されるか・窓サイズ是多少かを、起動時に取得します。Zen が出した新しい無料モデルは、コードを書き換えずに現れます。
- `/v1/models` に出るのは無料モデルのみ。有料モデルを叩くと 404 + 理由付き。
- `/v1/models` には **実際の context window / 出力上限** も入ります（Zen 自身は id しか返さないため）。pi-web-ui の「補参数 / Enrich params」のように 200K を仮定するクライアントが、1M モデルで 12% しか使っていないのに圧縮してしまう事故を防げます。
- Chat Completions / Responses / System One の 3 ルートを吸収し、素の OpenAI SDK・LiteLLM・Ollama・Continue などにそのまま差し込めます。
- ストリーミング（SSE）はバイト単位で中継。バッファしません。

---

## インストール

PyPI から:

```powershell
# インストールして起動
uv tool install zen-free-proxy
zen-free-proxy

# uv がなければ pip でも同じ
py -m pip install zen-free-proxy
zen-free-proxy
```

インストールせずに動かす:

```powershell
uvx zen-free-proxy
```

リポジトリから開発するときは `uv sync` → `uv run zen-free-proxy` です。

### 設定ファイル（任意）

環境変数でも `.env` でも指定します（prefix は `ZEN_PROXY_`、`ZEN_API_KEY` のみ例外）。
`.env` は起動時のカレントディレクトリから読み込まれます:

```bash
# .env
ZEN_API_KEY=sk-...
ZEN_PROXY_PORT=8787
ZEN_PROXY_ALLOWED_CLIENT_KEYS=team-a-secret,team-b-secret
```

---

## クイックスタート

```powershell
zen-free-proxy
```

既定で `http://0.0.0.0:8787` で待ち受けます。

```powershell
curl http://localhost:8787/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer なんでもいい" `
  -d '{"model":"space-bunny-free","messages":[{"role":"user","content":"hello"}]}'
```

OpenAI SDK から:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8787/v1", api_key="whatever")
print([m.id for m in client.models.list().data])

reply = client.chat.completions.create(
    model="space-bunny-free",
    messages=[{"role": "user", "content": "hello"}],
)
print(reply.choices[0].message.content)
```

---

##  IMPORTANT: 無料モデルの実際利用可能範囲

**2026-10 時点の実測: 無料モデルは全部、キー必須です。** 匿名（`Bearer public`）では
`space-bunny-free` を含めすべて `403 FreeTierError` になります。

| モデル | 実キー無し（匿名） | 実キーあり |
| --- | --- | --- |
| `space-bunny-free` | ❌ `403 FreeTierError` | ✅ 動作 |
| `jev-1.13-free`（`/v1/systemone`） | ❌ `403 FreeTierError` | ✅ 動作 |
| `big-pickle`, `mimo-*`, `ling-*`, `nemotron-*`, `longcat-*`, `fledge-*`, `muse-spark-*-contributor-free` | ❌ `403 FreeTierError` | ✅ 動作 |

`GET /v1/models` だけは匿名でも通るので、モデル一覧の自動更新は匿名でも動きます。

```powershell
# .env に書く（.env.example をコピー）
ZEN_API_KEY=sk-...
```

キーは <https://opencode.ai/auth> で取得できます。設定すると上流には
`Authorization: Bearer <あなたのキー>` が送られ、クライアントのキーは一切
上流へ漏れません。

実キー無しで対象モデルを叩くと、プロキシは素の 403 ではなく
`free_tier_restricted` というタイプで「`ZEN_API_KEY` を設定せよ」と明示した
エラーを返します。

### 「OpenCode 外で使うな」エラーとヘッダー

`Error from provider (Console): OpenCode's free tier can only be used from within OpenCode`
という 403 が返る件について。`opencode.exe`（bun コンパイル）を解析したところ、
Zen 宛てに送られるアプリ固有ヘッダーは次の 4 つだけです（`x-opencode-*` はこれが全部）:

```
x-opencode-client: cli
x-opencode-session: ses_…
x-opencode-request: req_…
User-Agent: opencode/<バージョン>
```

プロキシは既定でこれらをそのまま再生します（`ZEN_PROXY_ZEN_CLIENT_HEADERS=0` で無効化）。
ただし実測した範囲では、**足しても 403 は消えません**。UA のみ / +client /
+session +request / +project、`cli` `desktop` `web` の 6 通りを試しましたが全て同じ 403。
一方、偽キーを付けると `401 Invalid API key`、キー無しだと有料モデルで
`401 Missing API key` になります。つまり**判定しているのはヘッダーではなく API キー**で、
解決は `ZEN_API_KEY` の設定です。ヘッダーは Zen がさらに検査を厳しくした時のための保険です。

---

## エンドポイント

| メソッド | パス | 説明 |
| --- | --- | --- |
| `GET` | `/` | サービス情報と公開モデル一覧 |
| `GET` | `/healthz` | 起動確認 + 上流到達性（`503` を返せば劣化） |
| `GET` | `/v1/models` | 無料モデルのみ（OpenAI の形式 + 窓サイズ。下節） |
| `GET` | `/v1/models/{id}` | 個別取得。有料/未知名は 404 + 理由 |
| `POST` | `/v1/chat/completions` | メイン。ストリーミング可、tool calling 可 |
| `POST` | `/v1/responses` | Responses API ネイティブの無料モデル用 |
| `POST` | `/v1/systemone` | `jev-*` 用（OpenAI 形式ではない） |

モデル名には `opencode/` などのプロバイダ接頭辞が付けてもそのまま動きます
（`opencode/space-bunny-free` → `space-bunny-free`）。

### カタログの入手元（ハードコードなし）

このリポジトリにはモデル id が 1 つも書かれていません。Zen が新しい無料モデルを
公開したあとにコードを書き換えなくて済むよう、起動時と `ZEN_PROXY_CATALOG_TTL`
秒ごとに次のソースを読みます。

1. **`GET https://opencode.ai/zen/v1/models`** — Zen が現在扱っているモデルの一覧。
   Zen が外したモデルはそのまま `/v1/models` から消え、Zen が出したモデルは自動で
   追加されます。「このモデルを使ってよいか」の根拠はこの一覧です。
2. **models.dev の `opencode` エントリ**（`https://models.dev/api.json`） — 本家
   OpenCode クライアントが使っているカタログで、「無料かどうか」「どのルートで
   配信されるか」「窓はいく大きいか」をこの 1 ファイルで答えられます。

   | 知りたいこと | 見る場所 | opencode のソースでの根拠 |
   | --- | --- | --- |
   | 無料かどうか | `cost.input == 0` | `packages/core/src/provider/provider.ts` の `custom.opencode` が「未認証には有料モデルを隠す」判定に使う値 |
   | どのルート | `provider.npm` が `@ai-sdk/openai` なら Responses API、省がなければ（プロバイダ既定の `@ai-sdk/openai-compatible`）chat | 同じファイルが `api.npm` から SDK を選ぶ部分 |
   | 窓サイズ | `limit.context / input / output`、`modalities`、`reasoning` | 同じファイルが models.dev を読む部分 |

   無料かどうかは名前ではなく値段で判定します。無料なのに有料モデルらしい名前の
   `big-pickle` も、`-free` が付いていて数百ドルかかるモデルも、名前の表に頼らず
   正しい答えになります。
3. **opencode のリポジトリにコミットされているスナップショット**（ミラー） —
   `.github/workflows/models-snapshot.yml` が毎日更新して push する
   `packages/core/src/models-dev/snapshot.txt`。上の models.dev が取れなかった
   ときのフォールバックです（`ZEN_PROXY_MODELS_DEV_MIRROR_URL`）。中身はまったく
   同じファイルで、実測ではこれ 1 つだけで 14 件の無料モデルと窓サイズまで揃います。

どちらのソースも取れなかったときは、直前のカタログをそのまま使い続けます
（15 分あけて再試行するので、推論リクエストのたびに取得に走ることはありません）。
どのソースも把握していないモデルだけは、名前ベースの判定（`-free` など）に
フォールバックします。

### ルート自動振り分け

Zen は無料モデルごとに上流エンドポイントが違います。上のカタログが教える
「どのルートで配信されるか」に従って自動で振り分けます。

- Chat Completions ネイティブ → そのまま中継
- Responses ネイティブ → `chat/completions` に叩かれても要求を Responses 形式へ
  変換して送り、応答（JSON / SSE とも）を Chat Completions 形式へ戻します。
  `system` → `instructions`、`tool_calls` → `function_call`、
  SSE の `response.output_text.delta` → `chat.completion.chunk` の delta に対応。
- System One ネイティブ（`jev-*`）→ `POST /v1/systemone` 向け。models.dev に載って
  いないので、名前でしか判別できません。

ルートの当てが外れても止まりません。Zen が
`Model X is not supported for format openai` と返した場合、もう一方の OpenAI 互換
ルートで 1 回だけ試して、成功したルートをカタログに記録します。Zen が 500 で
落ちる場合にそれを試すのは、名前からの推測で得たルートだけです。ソースが
教えてくれたルートの 500 は本物の失敗なので、そのまま返します。

つまり「モデルがルートを移った」「ソースの更新が遅れた」「名前からの推定が外れた」
場合でも、最初の 1 リクエストが 2 本になるだけで、コードを直す必要はありません。

### `/v1/models` のメタデータ

context window も上の models.dev と同じ取得から来ます。ハードコード表は持たないので、
Zen が無料モデルを新しく出した場合も窓サイズが自動で入ります。

クライアントごとに綴りが違うので、まとめて出します:

```jsonc
{
  "id": "space-bunny-free",
  "object": "model",
  "owned_by": "opencode",
  "name": "Space Bunny Free",
  "context_window": 1048576,        // pi-web-ui / vLLM 系
  "context_length": 1048576,        // OpenRouter 系
  "max_context_length": 1048576,
  "max_input_tokens": 524288,
  "max_tokens": 524288,             // OpenAI 系
  "max_output_tokens": 524288,
  "modalities": ["text", "image", "video"],
  "reasoning": true,
  "limit": { "context": 1048576, "input": 524288, "output": 524288 }  // models.dev 系
}
```

取得に失敗した場合や models.dev に無いモデル（`jev-*` など）は、窓サイズ無しで
そのまま配信されます。`ZEN_PROXY_MODELS_DEV_URL` と
`ZEN_PROXY_MODELS_DEV_MIRROR_URL` の両方を空にすると、その取得自体を無効化できます
（その場合、Zen の一覧と名前の判定だけでカタログを作ります）。

`pi-web-ui` の「补参数（Enrich params）」は外部カタログ（OpenRouter → models.dev）を
引く仕組みで、Zen の無料モデルは **どちらのカタログにも一致しません**。実際に効くのは
`/v1/models` で、pi-web-ui 側の `server/model-admin.ts` の `parseOpenAiModel` が
そのレスポンスのフィールドを読みます。ただし pi-web-ui は **手動で入れた値を優先し、
Enrich は空欄しか埋めない**ため、200K を入れてある行は一度消してから取り直してください。

---

## 設定

すべて環境変数、または `.env` で指定します（_prefix は `ZEN_PROXY_`、ただし
`ZEN_API_KEY` のみ例外）。

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `ZEN_API_KEY` | *(空)* | 上流に送る実キー。空なら匿名（`Bearer public`） |
| `ZEN_PROXY_HOST` | `0.0.0.0` | バインド先 |
| `ZEN_PROXY_PORT` | `8787` | ポート |
| `ZEN_PROXY_ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | 上流 |
| `ZEN_PROXY_REQUEST_TIMEOUT` | `600` | 上流タイムアウト(秒) |
| `ZEN_PROXY_CONNECT_TIMEOUT` | `15` | 接続タイムアウト(秒) |
| `ZEN_PROXY_MAX_RETRIES` | `2` | 429/5xx の再試行回数（`Retry-After` 考慮） |
| `ZEN_PROXY_CATALOG_TTL` | `900` | カタログ（モデル一覧・ルート・窓サイズ）の再取得間隔。`0` で「起動時に 1 回だけ取得」 |
| `ZEN_PROXY_MODELS_DEV_URL` | `https://models.dev/api.json` | カタログ（判定・ルート・窓サイズ）の取得元。空で無効化 |
| `ZEN_PROXY_MODELS_DEV_MIRROR_URL` | opencode の `models-dev/snapshot.txt` | 上の取得元が使えないときのミラー。空で無効化 |
| `ZEN_PROXY_ZEN_CLIENT_HEADERS` | `1` | 本番クライアントの `x-opencode-*` と User-Agent を上流に再生する |
| `ZEN_PROXY_ALLOWED_CLIENT_KEYS` | *(空)* | **空 = 誰でも通過。** カンマ区切りで指定すると、そのキーだけ受理 |
| `ZEN_PROXY_LOG_LEVEL` | `INFO` | ログレベル |

### 認証について

既定は**完全なオープン**です。クライアントの `Authorization` ヘッダは
読みも検証もせず、上流へ転送しません（「API キー何でも通る」要件）。
共有先将誰かのキーを渡す必要もありません。

後から認証を強めたいときは:

```powershell
$env:ZEN_PROXY_ALLOWED_CLIENT_KEYS = "team-a-secret,team-b-secret"
```

とすると、指定したキーのどれかを持つリクエストだけが通ります。

> ⚠️ バインドは既定で `0.0.0.0` かつ無認証です。LAN や公開先将想定する場合は
> `ZEN_PROXY_HOST=127.0.0.1` にするか、`ZEN_PROXY_ALLOWED_CLIENT_KEYS` を
> 設定してください。

---

## 開発

```powershell
uv run pytest -q          # 115 tests
uv run ruff check .
uv run ruff format .
```

テストは全て `httpx.MockTransport` で上游をモックしています。実ネットワークに
アクセスしません。

### リリース

PyPI へは Trusted Publishing で公開しています（API トークンを持っていません）。
`v*` タグを push すると、CI を通って自動でアップロードされます:

```powershell
uv version patch          # 0.1.0 -> 0.1.1（pyproject を更新）
git commit -am "release 0.1.1" && git push
git tag v0.1.1 && git push --tags
```

初回の公開時だけ、PyPI 側に一度だけ発行者の登録が必要です:
<https://pypi.org/manage/project/zen-free-proxy/settings/publishing> で
「GitHub のリポジトリ `nennneko5787/opencode-zen-server`、ワークフロー
`publish.yml`、 Environments は空欄」を登録します。

構成:

```
src/zen_free_proxy/
  config.py     環境変数 → Settings
  catalog.py    無料モデルのカタログと窓の型（ModelMeta / CatalogSource）。id は持たない
  models_dev.py models.dev から無料判定・ルート・窓サイズを取得・解析
  upstream.py   Zen への HTTP クライアント（再試行・カタログ更新・クライアントヘッダー）
  sse.py        SSE のフレーミング
  bridge.py     Chat Completions ⇄ Responses API の相互変換
  app.py        FastAPI のルート定義
```

---

## 免責

- Zen の無料モデルは「期間限定」的ライセンスで、勝手に入れ替わったり消えたりします。
  あれこれリストを直す必要はありません。カタログは起動時に取得します
  （下記「カタログの入手元」）。新しいモデルが出ても、古いモデルが消えても、
  そのまま追従します。
- 無料ティールの利用制限（レートリミット等）は Zen 側にあり、このプロキシでは
  回避しません。429 は `Retry-After` を付けてそのままクライアントに返します。
  無料モデルの公開範囲も Zen 側の判断で、匿名利用は 403 になります（上記のとおり）。
- `muse-spark-*-contributor-free` と一部無料モデルは、プライバシー上の理由
  （学習利用への同意など）でプロンプトが保存されます。機密情報を扱う場合は
  `space-bunny-free` / `longcat-2.5-preview-free`（ゼロレテンション）を選んでください。
