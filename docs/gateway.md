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
| server tools (`web_search_*`, …) | not sent (logged as untranslated) |
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

## Errors and retries

Upstream `408/409/429/5xx` responses and connection errors are retried with exponential backoff (honouring
`Retry-After`) up to `max_retries` times **before** any bytes are sent to the client. Other errors are returned in
Anthropic's error format with the upstream status. A context-length error is phrased `prompt is too long: …`,
which is the message Claude Code reacts to by compacting the conversation. A failure in the middle of a stream is
sent as an SSE `error` event.

## Request log

`logs/gateway.jsonl` gets one line per `/v1/messages` call:

```json
{"ts": "2026-09-14T09:13:02.114+00:00", "request_id": "5c1e0f6a9b2d4e71", "session": "d0c9a52d-…",
 "client_model": "gpt-5.6-sol", "arena_model": "gpt-5.6-sol", "upstream_model": "gpt-5.6-sol",
 "stream": true, "messages": 7, "input_items": 9, "tools": 21, "client_max_tokens": 32000,
 "max_output_tokens": 64000, "reasoning_effort": "medium", "retries": 0, "status": 200,
 "stop_reason": "tool_use", "usage": {"input_tokens": 18234, "cached_input_tokens": 17920, "cache_write_tokens": 0,
 "output_tokens": 412},
 "reasoning_tokens": 256, "content_blocks": ["thinking", "tool_use"], "first_token_ms": 2210, "latency_ms": 4630}
```

## Tests

`tests/test_gateway_translate.py`, `tests/test_gateway_stream.py` and `tests/test_gateway_app.py` exercise the
translation against a mocked upstream: request mapping, the exact SSE event sequence for reasoning + streamed tool
calls, reasoning round-trips across turns, stop sequences, retries and error mapping.
