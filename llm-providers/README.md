# llm-providers

One provider-agnostic chat-completion interface (`llm_providers.LLMClient`) with one
adapter per vendor SDK: NVIDIA-hosted Nemotron-3 Ultra via the OpenAI-compatible NIM
API (`openai`), Anthropic Claude (`anthropic`), and Google Gemini (`google-genai`).
Callers pick a backend with `get_client(provider)` or `$LLM_PROVIDER` and never touch
SDK-specific code.

This is a sibling of `ticker-advisor/`, `ml-forecaster/`, `wondrous-octopus/`, and
`ticker-market-breadth/`: a self-contained, third-party-dependency subproject with its
own `requirements.txt` and `tests/`, excluded from the core repo's stdlib-only ruff and
coverage gates (see the root `CLAUDE.md`). **Nothing in the core imports it**, and it
is not wired into any live DCA path.

## Install

```bash
cd llm-providers
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/      # 62 tests, all against mocked SDK clients -- no network
```

## Usage

```python
from llm_providers import get_client

client = get_client("anthropic")            # or "nvidia" / "gemini"; or set $LLM_PROVIDER
messages = [
    {"role": "system", "content": "Be terse."},
    {"role": "user", "content": "Ping?"},
]
text = client.complete(messages, temperature=0.2, max_tokens=64)
for chunk in client.stream(messages):
    print(chunk, end="", flush=True)
```

`complete` returns the assistant's text; `stream` yields text deltas such that
`"".join(client.stream(m)) == client.complete(m)` for a deterministic model. Roles are
`system` / `user` / `assistant` only.

## Configuration (all via environment; nothing is hard-coded)

| Variable | Read by | Notes |
| --- | --- | --- |
| `LLM_PROVIDER` | `get_client()` when called without an argument | `nvidia` (default), `anthropic`, `gemini` (aliases: `nim`, `nemotron`, `claude`, `google`) |
| `NVIDIA_API_KEY` | `NvidiaNimClient` | NIM bearer key for `https://integrate.api.nvidia.com/v1` |
| `ANTHROPIC_API_KEY` | `AnthropicClient` | |
| `GEMINI_API_KEY`, then `GOOGLE_API_KEY` | `GeminiClient` | first non-empty wins |

Per-adapter constructor keywords (forwarded through `get_client(name, **kwargs)`):
`model`, `timeout` (seconds); NVIDIA also takes `base_url` and `enable_thinking`
(Nemotron-3's `chat_template_kwargs.enable_thinking`); Anthropic also takes
`default_max_tokens` because its API requires `max_tokens` on every request.

Default models: `nvidia/nemotron-3-ultra-550b-a55b`, `claude-sonnet-5`, `gemini-2.5-flash`.

## Errors

Every adapter maps its SDK's exceptions onto one family, each carrying `.provider`:

| Normalised | NVIDIA / Anthropic (`openai` / `anthropic`) | Gemini (`google.genai.errors`) |
| --- | --- | --- |
| `LLMAuthError` | `AuthenticationError`, `PermissionDeniedError`, missing env var | `APIError` with code 401/403, missing env var |
| `LLMRateLimitError` | `RateLimitError` | code 429 |
| `LLMTimeoutError` | `APITimeoutError` | code 408/504, `TimeoutError`, httpx `*Timeout*` |
| `LLMProviderError` | any other `APIError` (5xx, connection) | any other `APIError` |

Non-SDK exceptions propagate untouched. Error messages name the env var, never its
value; the adapters log nothing.

## Testing

Each adapter takes `client=` in its constructor. Tests pass a `MagicMock` (or a tiny
fake) through that seam and assert on the exact SDK call kwargs, so behaviour is
deterministic and no test opens a socket. `tests/conftest.py` scrubs the provider env
vars so a developer's real keys never leak into a run.
