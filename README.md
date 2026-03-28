# gemini-openai-proxy

A lightweight proxy that exposes Google Vertex AI Express Gemini models through an OpenAI-compatible HTTP API.

```
Your app (OpenAI format)  →  proxy.py (:4000)  →  Vertex AI Express
                                               aiplatform.googleapis.com
```

## Why this proxy?

Vertex AI's Gemini models use Google's own request/response format while many AI tools (OpenClaw, Continue, Cursor, etc.) expect an OpenAI-compatible API. This proxy translates between the two so you can call Gemini using the OpenAI-style endpoints.

Vertex AI Express uses a global endpoint with API Key authentication that some lightweight local libraries (e.g., LiteLLM) do not support; this proxy bridges that gap.

## Features

- OpenAI `/v1/chat/completions` compatible
- SSE streaming support (`stream: true`) using Gemini's streaming endpoint
- Model fallback (automatic downgrade on rate limits or server errors)
- Accepts both string and array `content` formats
- Automatic conversion of the system prompt

## Quick start

### 1. Obtain a Vertex AI Express API key

1. Sign up for Google Cloud Free Trial (https://cloud.google.com/free)
2. Create a project and enable the Vertex AI API
3. Create a Service Account and grant it the `Vertex AI User` role
4. Create an API Key in the Cloud Console and restrict it to the Vertex AI API (when creating, choose the option to authenticate API calls via the service account)

### 2. Run the proxy

```bash
pip install -r requirements.txt

VERTEX_AI_API_KEY="YOUR_KEY" python proxy.py
```

By default the proxy listens on `http://127.0.0.1:4000`. Configure with environment variables as described below.

### 3. Example request

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-lite-preview",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

## Configuration

| Environment variable | Description | Default |
|---|---:|---|
| `VERTEX_AI_API_KEY` | Vertex AI Express API key (required) | - |
| `PROXY_PORT` | Proxy listen port | `4000` |
| `PROXY_HOST` | Proxy listen address | `127.0.0.1` |
| `PROXY_AUTH_TOKEN` | Optional: require clients to present this Bearer token | (disabled) |
| `MAX_BODY_SIZE` | Maximum request body size in bytes | `1048576` (1MB) |

For container or cloud deployments set `PROXY_HOST=0.0.0.0`.

## Security notes

- The API key is sent in the `x-goog-api-key` header (not in the URL) to avoid leaking it in logs.
- The proxy defaults to listening on `127.0.0.1`; when exposed publicly, set `PROXY_AUTH_TOKEN` to protect your quota.
- Request body size is limited (default 1MB) to reduce risk of OOM from large payloads.
- Upstream error responses are sanitized before returning to clients to avoid leaking project or quota details.

## Model fallback

The proxy includes fallback chains: if a primary model returns 429 or 5xx, it will try the configured alternatives.

Default fallback mapping (configured in `proxy.py`):

| Primary model | Fallback |
|---|---|
| `gemini-3.1-pro-preview` | `gemini-3.1-flash-lite-preview` |

Update the `FALLBACK_CHAINS` dict in `proxy.py` to change this. See Vertex AI model list for current model IDs.

## Integration examples

### OpenClaw

Add a provider to your `openclaw.json` `models.providers`:

```json
"vertex-gemini": {
  "baseUrl": "http://localhost:4000/v1",
  "apiKey": "any",
  "api": "openai-completions",
  "models": [
    {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro"},
    {"id": "gemini-3.1-flash-lite-preview", "name": "Gemini 3.1 Flash Lite"}
  ]
}
```

### Other OpenAI-compatible clients

Point the client's base URL to `http://localhost:4000/v1` and provide any API key value.

## Known limitations

- Vertex AI Express supports only Google-owned models (Gemini); third-party models (e.g., Claude) are not supported.
- Some GCP free-trial service accounts cannot call models via standard endpoints (a known platform limitation).
- Streaming responses: once an SSE stream begins the HTTP status is 200; upstream errors are delivered as chunks in the stream rather than non-200 HTTP codes. Some client libraries handle this differently.
- Runtime fallback during a streaming session is only possible before the first chunk is emitted. If the upstream aborts mid-stream, the proxy cannot transparently replay or switch models for already-sent content.
- The script runs a single uvicorn worker by default. For production use, run under Gunicorn with multiple workers, e.g.: `gunicorn proxy:app -w 4 -k uvicorn.workers.UvicornWorker`.

## Disclaimer

This project is not affiliated with Google. Use is subject to the Google Cloud Terms of Service and Vertex AI policies. The proxy forwards conversation content to Google's servers; do not send personal sensitive data.

"OpenAI-compatible" refers only to the API request/response format; this project is not affiliated with OpenAI.

## License

MIT
