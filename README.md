# zen-free-proxy

[![PyPI](https://img.shields.io/pypi/v/zen-free-proxy.svg)](https://pypi.org/project/zen-free-proxy/)
[![Python](https://img.shields.io/pypi/pyversions/zen-free-proxy.svg)](https://pypi.org/project/zen-free-proxy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/nennneko5787/opencode-zen-server/actions/workflows/ci.yml/badge.svg)](https://github.com/nennneko5787/opencode-zen-server/actions/workflows/ci.yml)

[English](README.md) | [日本語](README-ja.md)

An OpenAI-compatible reverse proxy in front of the free models on
[OpenCode Zen](https://opencode.ai/docs/zen).

- **The client's API key can be anything** — `sk-Whatever`, or none at all. The real
  upstream key is held only by the proxy.
- **No model is hardcoded.** Whether a model is free, which upstream route serves it,
  and how large its window is are all read at startup, so a free model Zen launches
  tomorrow appears without a code change.
- `/v1/models` lists only free models. Ask for a paid one and you get a 404 that says why.
- `/v1/models` also reports **real context windows and output limits** (Zen itself
  returns ids and nothing else). This prevents the expensive kind of guess: clients that
  default to 200K — pi-web-ui's "Enrich params", for one — compact a 1M model while it is
  12% full.
- Absorbs all three upstream routes (Chat Completions, Responses, System One), so the
  plain OpenAI SDK, LiteLLM, Ollama, Continue and friends can point straight at it.
- Streaming (SSE) is relayed byte for byte. Nothing is buffered.

---

## Install

From PyPI:

```bash
# install, then run
uv tool install zen-free-proxy
zen-free-proxy

# or with pip, if you don't use uv
pip install zen-free-proxy
zen-free-proxy
```

Or run it without installing:

```bash
uvx zen-free-proxy
```

Working on a checkout instead? `uv sync`, then `uv run zen-free-proxy`.

### Configuration file (optional)

Everything is an environment variable, or a `.env` file (the prefix is `ZEN_PROXY_`,
except `ZEN_API_KEY`). `.env` is read from the working directory the server starts in:

```bash
# .env
ZEN_API_KEY=sk-...
ZEN_PROXY_PORT=8787
ZEN_PROXY_ALLOWED_CLIENT_KEYS=team-a-secret,team-b-secret
```

---

## Quick start

```bash
zen-free-proxy
```

It listens on `http://0.0.0.0:8787` by default.

```bash
curl http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer literally-anything" \
  -d '{"model":"space-bunny-free","messages":[{"role":"user","content":"hello"}]}'
```

From the OpenAI SDK:

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

## IMPORTANT: what the free tier actually allows

**Measured in 2026-10: every free model needs a key.** Anonymous traffic
(`Bearer public`) gets `403 FreeTierError` from all of them, `space-bunny-free`
included.

| Model | Anonymous (no key) | With a key |
| --- | --- | --- |
| `space-bunny-free` | ❌ `403 FreeTierError` | ✅ works |
| `jev-1.13-free` (on `/v1/systemone`) | ❌ `403 FreeTierError` | ✅ works |
| `big-pickle`, `mimo-*`, `ling-*`, `nemotron-*`, `longcat-*`, `fledge-*`, `muse-spark-*-contributor-free` | ❌ `403 FreeTierError` | ✅ works |

`GET /v1/models` is the exception — it answers anonymously, so the catalog keeps itself
up to date either way.

```bash
# in .env (copy .env.example)
ZEN_API_KEY=sk-...
```

Get a key at <https://opencode.ai/auth>. Once set, upstream requests carry
`Authorization: Bearer <your key>` and the client's own key is never forwarded.

Without a key, a request for a free model comes back as a `free_tier_restricted` error
that names the fix (`set ZEN_API_KEY`) instead of a bare 403.

### The "only from within OpenCode" error, and the headers

Zen answers some free-tier rejections with
`Error from provider (Console): OpenCode's free tier can only be used from within OpenCode`.
Disassembling `opencode.exe` (a bun build) shows the app sends exactly four Zen-specific
headers — that is the complete set of `x-opencode-*`:

```
x-opencode-client: cli
x-opencode-session: ses_…
x-opencode-request: req_…
User-Agent: opencode/<version>
```

The proxy replays them verbatim by default (disable with `ZEN_PROXY_ZEN_CLIENT_HEADERS=0`).
Within the range actually tested, **adding them does not remove the 403**: user agent only,
`+client`, `+session +request`, `+project`, and all of `cli` / `desktop` / `web` were tried
and every combination returned the same 403. On the other hand, a malformed key gives
`401 Invalid API key` and no key gives `401 Missing API key` on paid models. So **Zen is
checking the API key, not the headers**, and setting `ZEN_API_KEY` is the fix. The headers
are kept as insurance in case Zen tightens the check further.

---

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Service info and the list of served models |
| `GET` | `/healthz` | Liveness + upstream reachability (`503` means degraded) |
| `GET` | `/v1/models` | Free models only, in OpenAI's shape plus window sizes (below) |
| `GET` | `/v1/models/{id}` | One model. Paid or unknown → 404 with the reason |
| `POST` | `/v1/chat/completions` | The main one. Streaming and tool calling both work |
| `POST` | `/v1/responses` | For free models that are native to the Responses API |
| `POST` | `/v1/systemone` | For `jev-*` (not an OpenAI-shaped API) |

Provider prefixes are accepted and stripped, so `opencode/space-bunny-free` works.

### Where the catalog comes from (nothing hardcoded)

Not one model id appears in this repository. At startup, and every
`ZEN_PROXY_CATALOG_TTL` seconds after that, the proxy reads:

1. **`GET https://opencode.ai/zen/v1/models`** — what Zen is currently serving. A model
   Zen drops disappears from `/v1/models` on its own, and a model Zen adds appears on its
   own. This list is the only authority on whether a model may be used at all.
2. **The `opencode` entry on models.dev** (`https://models.dev/api.json`) — the catalog
   the official OpenCode client reads, which answers all three remaining questions:

   | Question | Where to look | Why that is the right signal |
   | --- | --- | --- |
   | Is it free? | `cost.input == 0` | `custom.opencode` in `packages/core/src/provider/provider.ts` uses the same value to hide paid models from unauthenticated callers |
   | Which route? | `provider.npm`: `@ai-sdk/openai` is the Responses API; absent (the provider default `@ai-sdk/openai-compatible`) is chat | The same file picks its SDK from `api.npm` |
   | How big? | `limit.context / input / output`, `modalities`, `reasoning` | The same file's models.dev reader |

   Free is therefore a price, not a name. `big-pickle` is free despite looking paid, and a
   model with `-free` in its name that costs hundreds of dollars a million tokens is
   correctly refused — no name table involved.
3. **The snapshot opencode commits to its own repository** (mirror) —
   `packages/core/src/models-dev/snapshot.txt`, refreshed daily by
   `.github/workflows/models-snapshot.yml`. Used when models.dev itself is unreachable
   (`ZEN_PROXY_MODELS_DEV_MIRROR_URL`). It is the same file: measured on its own, it yields
   all 14 free models with their window sizes.

If both sources are unreachable the previous catalog keeps being served, and the proxy
retries on its own schedule (every 15 minutes) rather than on your inference requests. Only
models no source has heard of fall back to name-based detection (`-free` and friends).

### Route dispatch, and how it fixes itself

Zen serves different free models on different upstream endpoints, and the catalog says
which is which:

- **Chat Completions native** → relayed as-is.
- **Responses native** → a request to `/v1/chat/completions` is translated into a Responses
  request, and the answer (JSON or SSE) is translated back: `system` → `instructions`,
  `tool_calls` → `function_call`, and SSE `response.output_text.delta` → a
  `chat.completion.chunk` delta.
- **System One native** (`jev-*`) → served from `POST /v1/systemone`. models.dev does not
  carry it at all, so the name is the only signal there is.

A wrong route is not a dead end. When Zen answers
`Model X is not supported for format openai`, the proxy tries the other OpenAI-compatible
route once and records the route that worked. It also does that when Zen fails with a 5xx,
but only when the route was a name-based guess — a 5xx on a route a source named is a real
failure and is reported as one.

So when Zen moves a model, a source lags behind, or a name guess turns out wrong, the cost
is that one request goes out twice. No patch required.

### `/v1/models` metadata

Context windows come from the same models.dev fetch as above. There is no table to go
stale, so a newly launched free model arrives with its window already filled in.

Clients spell this differently, so every size is published under all the names in use:

```jsonc
{
  "id": "space-bunny-free",
  "object": "model",
  "owned_by": "opencode",
  "name": "Space Bunny Free",
  "context_window": 1048576,        // pi-web-ui, vLLM-style clients
  "context_length": 1048576,        // OpenRouter-style clients
  "max_context_length": 1048576,
  "max_input_tokens": 524288,
  "max_tokens": 524288,             // OpenAI-style clients
  "max_output_tokens": 524288,
  "modalities": ["text", "image", "video"],
  "reasoning": true,
  "limit": { "context": 1048576, "input": 524288, "output": 524288 }  // models.dev-style
}
```

If the fetch fails, or a model is missing from models.dev (`jev-*`, for instance), it is
listed without a window. Set both `ZEN_PROXY_MODELS_DEV_URL` and
`ZEN_PROXY_MODELS_DEV_MIRROR_URL` to empty to turn that fetch off entirely; the catalog is
then built from Zen's list plus name detection alone.

`pi-web-ui`'s "Enrich params" pulls from external catalogs (OpenRouter, then models.dev),
and **Zen's free models are in neither**. What actually works is `/v1/models`, because
pi-web-ui's `server/model-admin.ts` (`parseOpenAiModel`) reads those fields from the
response. Note that pi-web-ui **prefers manual values and only enriches blanks**, so delete
a row you filled in with 200K before fetching it again.

---

## Settings

Everything is an environment variable, or a `.env` file (prefix `ZEN_PROXY_`, except
`ZEN_API_KEY`).

| Variable | Default | Description |
| --- | --- | --- |
| `ZEN_API_KEY` | *(empty)* | Real key sent upstream. Empty means anonymous (`Bearer public`) |
| `ZEN_PROXY_HOST` | `0.0.0.0` | Bind address |
| `ZEN_PROXY_PORT` | `8787` | Port |
| `ZEN_PROXY_ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | Upstream |
| `ZEN_PROXY_REQUEST_TIMEOUT` | `600` | Upstream timeout (seconds) |
| `ZEN_PROXY_CONNECT_TIMEOUT` | `15` | Connect timeout (seconds) |
| `ZEN_PROXY_MAX_RETRIES` | `2` | Retries for 429/5xx (honouring `Retry-After`) |
| `ZEN_PROXY_CATALOG_TTL` | `900` | How often to re-read the catalog (model list, routes, windows). `0` fetches once at startup |
| `ZEN_PROXY_MODELS_DEV_URL` | `https://models.dev/api.json` | Catalog source (free-ness, routes, windows). Empty disables it |
| `ZEN_PROXY_MODELS_DEV_MIRROR_URL` | opencode's `models-dev/snapshot.txt` | Mirror used when the source above is unavailable. Empty disables it |
| `ZEN_PROXY_ZEN_CLIENT_HEADERS` | `1` | Replay the official client's `x-opencode-*` headers and User-Agent upstream |
| `ZEN_PROXY_ALLOWED_CLIENT_KEYS` | *(empty)* | **Empty = anyone gets in.** A comma separated list means only those keys are accepted |
| `ZEN_PROXY_LOG_LEVEL` | `INFO` | Log level |

### Authentication

The default is **completely open**: the client's `Authorization` header is neither read
nor validated, and is never forwarded upstream. You do not have to hand anyone a key.

To require authentication later:

```bash
export ZEN_PROXY_ALLOWED_CLIENT_KEYS="team-a-secret,team-b-secret"
```

Only requests carrying one of those keys are then accepted.

> ⚠️ The default bind is `0.0.0.0` with no authentication. If this is reachable from a LAN
> or the internet, set `ZEN_PROXY_HOST=127.0.0.1` or configure `ZEN_PROXY_ALLOWED_CLIENT_KEYS`.

---

## Development

```bash
uv run pytest -q          # 119 tests
uv run ruff check .
uv run ruff format .
```

The tests mock the upstream with `httpx.MockTransport` and never touch the network.

Layout:

```
src/zen_free_proxy/
  config.py     environment variables -> Settings
  catalog.py    the free-model catalog and the window types (ModelMeta / CatalogSource)
  models_dev.py reads free-ness, routes and window sizes out of models.dev
  upstream.py   HTTP client for Zen (retries, catalog refresh, client headers)
  sse.py        SSE framing
  bridge.py     Chat Completions <-> Responses API conversion
  app.py        the FastAPI routes
```

### Releasing

Publishing to PyPI goes through Trusted Publishing, so there is no API token stored
anywhere. Push a `v*` tag and CI uploads the release:

```bash
uv version patch          # 0.1.0 -> 0.1.1 in pyproject.toml
git commit -am "release 0.1.1" && git push
git tag v0.1.1 && git push --tags
```

Only the very first release needed a one-time publisher registration on PyPI, at
<https://pypi.org/manage/project/zen-free-proxy/settings/publishing>.

---

## Disclaimer

- Zen's free models are licensed on a "for a limited time" basis and may be swapped or
  withdrawn. There is no list to maintain: the catalog is read at startup (see
  [Where the catalog comes from](#where-the-catalog-comes-from-nothing-hardcoded)), so new
  models and retired ones are followed automatically.
- Free-tier limits (rate limits and the like) live on Zen's side and this proxy does not
  try to work around them. A 429 is passed through with its `Retry-After` header. Which
  models are free, and whether anonymous traffic is allowed, is Zen's decision too — see
  above.
- `muse-spark-*-contributor-free` and some other free models keep your prompts for
  privacy reasons such as training consent. For anything sensitive, prefer
  `space-bunny-free` or `longcat-2.5-preview-free` (zero retention).