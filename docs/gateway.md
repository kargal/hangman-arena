# Arena gateway

Every Hangman Arena leaderboard entry is produced with the **same harness: Claude Code**, run headless by the
arena runner. Comparing models inside one agent scaffold isolates the model from differences in tooling,
prompting and context management.

Claude Code speaks the Anthropic Messages API. Non-Anthropic models reach it in one of two ways:

| Model family | Endpoint | `ANTHROPIC_BASE_URL` |
| --- | --- | --- |
| Claude | Anthropic API | *(unset)* |
| Kimi | Moonshot's Anthropic-compatible API | `https://api.moonshot.ai/anthropic` |
| GLM | Z.ai's Anthropic-compatible API | `https://api.z.ai/api/anthropic` |
| Qwen | DashScope's Anthropic-compatible API | `https://dashscope-intl.aliyuncs.com/apps/anthropic` |
| DeepSeek | DeepSeek's Anthropic-compatible API | `https://api.deepseek.com/anthropic` |
| GPT (OpenAI Responses API) | **arena gateway** | `http://gateway:8090` |

The **arena gateway** (`arena_gateway/`) is a small FastAPI service that serves the Anthropic Messages API on
top of the OpenAI Responses API. It is ours, deliberately small, and has no proxy-library dependencies, so that
every translation decision is visible and reviewable in one place.

## Running it

```bash
export OPENAI_API_KEY=sk-...                 # OPENAI_BASE_URL is honoured if set
export GATEWAY_API_KEY=$(openssl rand -hex 16)
python -m arena_gateway --port 8090          # or: docker compose --profile gateway up -d gateway

# Point Claude Code (or the runner) at it:
export ANTHROPIC_BASE_URL=http://localhost:8090
export ANTHROPIC_AUTH_TOKEN=$GATEWAY_API_KEY
python -m arena_runner run --agent claude_code --model gpt-5.6-sol
```

With Docker Compose, the agent container reaches the gateway as `http://gateway:8090` — see the header of
`docker/docker-compose.yml`.

## Model routing — `configs/gateway/models.yaml`

```yaml
default_model: gpt-5.6-sol
max_output_tokens_floor: 64000
providers:
  openai:
    base_url: ${OPENAI_BASE_URL:-https://api.openai.com/v1}
    api_key: ${OPENAI_API_KEY}
models:
  gpt-5.6-sol:
    provider: openai
    upstream_model: gpt-5.6-sol
    reasoning_effort: medium      # the model's default
aliases:
  "claude-*haiku*": gpt-5.6-sol   # Claude Code's background model
```

Every model name Claude Code sends is resolved as: exact match under `models` → first matching glob under
`aliases` → `default_model`. A context-size suffix such as `[1m]` is ignored. This covers the name passed with
`--model` and the built-in background ("small/fast") model Claude Code uses for housekeeping requests.

Routes shipped in `models.yaml` (reasoning effort is each model's default; summaries `auto`):

| Arena model | Upstream model | Reasoning effort | Output cap | Context window |
| --- | --- | --- | --- | --- |
| `gpt-5.6-sol` (default) | `gpt-5.6-sol` | `medium` | 128k | 1.05M |
| `gpt-6-sol` | `gpt-6-sol` | `medium` | 128k | 1.05M |
| `gpt-6.1-sol` | `gpt-6.1-sol` | `medium` | 128k | 1.05M |

| Environment variable | Meaning |
| --- | --- |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | upstream credentials / endpoint (via `models.yaml`) |
| `GATEWAY_API_KEY` | if set, clients must send it (`x-api-key` or `Authorization: Bearer`) |
| `GATEWAY_CONFIG` | path to `models.yaml` |
| `GATEWAY_LOG_FILE` | request log (default `logs/gateway.jsonl`) |
| `GATEWAY_LOG_BODIES=1` | also log full request/response bodies |
| `GATEWAY_MAX_OUTPUT_TOKENS_FLOOR` | override `max_output_tokens_floor` |

## What is translated

**Endpoints.** `POST /v1/messages` (streaming and non-streaming), `POST /v1/messages/count_tokens` (character-based
estimate), `GET /v1/models`, `GET /v1/models/{id}`, and `HEAD /api/hello` / `HEAD /` (Claude Code's connectivity
check). Anything else returns an Anthropic-style 404 and is logged.

**Requests** (Anthropic → Responses):

| Anthropic | Responses |
| --- | --- |
| `system` (string or text blocks) | `instructions` (Claude Code's billing-header block is dropped) |
| `role: "system"` message mid-conversation | `developer` message |
| user `text` / `image` / `document` | `input_text` / `input_image` / `input_file` |
| assistant `text` | assistant message with `output_text` |
| assistant `tool_use` | `function_call` (`call_id` = tool-use id, `arguments` = JSON) |
| user `tool_result` (string or blocks, `is_error`) | `function_call_output`; errors are prefixed `Error:`; images follow as a user message |
| assistant `thinking` with a gateway signature | `reasoning` item with `encrypted_content` (see below) |
| `tools[].input_schema` | `function` tool, `parameters` = schema without `$schema`, `strict: false` |
| `tools` with `defer_loading` | not sent (not visible to the model until loaded via ToolSearch) |
| `web_search_*` server tool | hosted `web_search` tool — see [Web search](#web-search) |
| other server tools (code execution, web fetch, …) | not sent (logged as untranslated) |
| `tool_choice` auto / any / tool / none | `auto` / `required` / `{"type":"function","name":…}` / `none` |
| `disable_parallel_tool_use` | `parallel_tool_calls: false` |
| `max_tokens` | `max_output_tokens = max(max_tokens, min(floor, model cap))` — never below the client's value |
| `stop_sequences` | applied by the gateway to the text stream (Responses has no stop parameter) |
| `temperature`, `top_p` | dropped for reasoning models, passed through otherwise; `top_k` is dropped |
| `thinking`, `output_config.effort` | ignored — reasoning effort comes from `models.yaml` |
| `metadata.user_id` / session header | `prompt_cache_key` (hashed Claude Code session id) |

Every request is sent with `store: false`, `include: ["reasoning.encrypted_content"]` and
`reasoning: {effort: <models.yaml>, summary: "auto"}`.

**Responses** (Responses → Anthropic):

| Responses output | Anthropic content block |
| --- | --- |
| `reasoning` | `thinking` — text = reasoning summary, `signature` = encrypted reasoning state + item id |
| `message` / `output_text`, `refusal` | `text` |
| `function_call` | `tool_use` (arguments streamed as `input_json_delta`) |
| `web_search_call` (search) | `server_tool_use` + `web_search_tool_result` — see [Web search](#web-search) |

| Responses status | `stop_reason` |
| --- | --- |
| completed with a function call | `tool_use` |
| completed | `end_turn` |
| incomplete: `max_output_tokens` | `max_tokens` |
| incomplete: `content_filter` | `refusal` |
| stop sequence matched | `stop_sequence` |

Streaming produces exactly the Anthropic event sequence: `message_start`, `ping`, then per block
`content_block_start` → `content_block_delta`* → `content_block_stop` (`thinking_delta` + `signature_delta`,
`text_delta`, or `input_json_delta`), then `message_delta` (stop reason and usage) and `message_stop`. While the
model reasons silently, `ping` events are sent every 10 s. Usage is reported Anthropic-style:
`input_tokens` excludes cached tokens, which are reported as `cache_read_input_tokens`; GPT-5.6 cache writes
(`input_tokens_details.cache_write_tokens`, billed at 1.25x input) are reported as `cache_creation_input_tokens`.

## Reasoning across turns

Requests use `store: false`, so OpenAI keeps no state between turns. The gateway asks for
`reasoning.encrypted_content` and emits each reasoning item as a `thinking` block whose `signature` is
`arenagw1.<base64 {"id", "enc"}>`. Claude Code returns thinking blocks verbatim with the conversation, and the
gateway turns them back into `reasoning` input items, in their original position, on the next request. The model
therefore keeps its reasoning across tool calls, as it would with the native Responses API. Thinking blocks with
signatures the gateway did not produce are dropped.

## Web search

Claude Code's `WebSearch` tool does not search by itself. It sends a short side request to the model ("Perform a
web search for the query: …") whose only tool is Anthropic's web search **server tool** (`{"type":
"web_search_20250305", "name": "web_search", "max_uses": 8, "allowed_domains": …, "blocked_domains": …}`) and
reads the hits (title + URL) and the model's summary out of the reply. The gateway serves these requests with the
provider-hosted search of the Responses API:

| Anthropic | Responses |
| --- | --- |
| `web_search_*` tool | `{"type": "web_search"}`; `allowed_domains` / `blocked_domains` → `filters` (bare domains), `user_location` → `user_location` |
| `max_uses` | `max_tool_calls` |
| `tool_choice: {"type": "tool", "name": "web_search"}`, or web search as the only tool with `tool_choice` auto / unset | `tool_choice: {"type": "web_search"}` (search required) |
| — | `include: ["web_search_call.action.sources"]` |

| Responses output | Anthropic content |
| --- | --- |
| `web_search_call`, action `search` | `server_tool_use` (`name: "web_search"`, `input: {"query"}`) + `web_search_tool_result` with one `web_search_result` (`url`, `title`, `page_age: null`) per source URL |
| `web_search_call`, action `open_page` / `find_in_page` | no block (counted in the request log) |
| `web_search_call` with status `failed` | `web_search_tool_result` with `web_search_tool_result_error` (`unavailable`) |
| `output_text` `url_citation` annotations | text blocks with `web_search_result_location` citations |

Result titles come from the model's citations; the upstream returns no title for a source it did not cite, and
the gateway then uses the URL as the title. The `utm_source=openai` parameter is removed from URLs, and
`encrypted_content` / `encrypted_index` are opaque gateway markers (`arenagw-ws1.…`), not Anthropic's.

**No search, no answer.** If a search was required (forced, or web search was the only tool, as in Claude Code's
requests) or attempted and no search succeeded, the model's text is dropped and the client receives a
`web_search_tool_result_error` instead, so Claude Code reports `Web search error: unavailable` rather than an
answer written without search results. Upstream HTTP errors are returned as errors, as for any other request.

Web search requests are served from one complete (non-streaming) upstream response, because the result titles
are only known once the model's final text is complete; for a streaming client the gateway replays the finished
message as the usual SSE event sequence (with `citations_delta` events for citations). Earlier
`server_tool_use` / `web_search_tool_result` blocks in a conversation are passed to the model as assistant text
(query and result URLs). Search calls are billed by the provider per call, on top of the search content tokens
(which are included in `input_tokens`).

## Errors and retries

Upstream `408/409/429/5xx` responses and connection errors are retried with exponential backoff (honouring
`Retry-After`) up to `max_retries` times **before** any bytes are sent to the client. Other errors are returned in
Anthropic's error format with the upstream status. A context-length error is phrased `prompt is too long: …`,
which is the message Claude Code reacts to by compacting the conversation. A failure in the middle of a stream is
sent as an SSE `error` event.

## Request log

`logs/gateway.jsonl` gets one line per `/v1/messages` call:

```json
{"ts": "2026-10-14T09:13:02.114+00:00", "request_id": "5c1e0f6a9b2d4e71", "session": "d0c9a52d-…",
 "client_model": "gpt-5.6-sol", "arena_model": "gpt-5.6-sol", "upstream_model": "gpt-5.6-sol",
 "stream": true, "messages": 7, "input_items": 9, "tools": 21, "client_max_tokens": 32000,
 "max_output_tokens": 64000, "reasoning_effort": "medium", "retries": 0, "status": 200,
 "stop_reason": "tool_use", "usage": {"input_tokens": 18234, "cached_input_tokens": 17920, "cache_write_tokens": 0,
 "output_tokens": 412},
 "reasoning_tokens": 256, "content_blocks": ["thinking", "tool_use"], "first_token_ms": 2210, "latency_ms": 4630}
```

Web search requests also record `web_search_calls` (upstream `web_search_call` items, the billed unit) and
`web_search` (`searches`, `failed`, `sources`, `queries`, other actions, and whether model text was withheld).

## Claude Code settings used by the runner

`configs/agents/claude_code.yaml` sets, for every model:

* `ENABLE_TOOL_SEARCH=false` — all tool schemas are sent up front, so every model sees the same tool list
  regardless of endpoint;
* `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1` — no auto-updates or telemetry during runs.

## Tests

`tests/test_gateway_translate.py`, `tests/test_gateway_stream.py`, `tests/test_gateway_app.py` and
`tests/test_gateway_websearch.py` exercise the translation against a mocked upstream: request mapping, the exact
SSE event sequence for reasoning + streamed tool calls, reasoning round-trips across turns, stop sequences,
retries and error mapping, and web search results, citations and failures.
