"""
gemini-openai-proxy: Lightweight Vertex AI Express to OpenAI-compatible proxy

Exposes Gemini models via the Vertex AI Express global endpoint as an OpenAI-compatible API.

Features:
- OpenAI <-> Gemini format conversion
- True SSE streaming via streamGenerateContent
- Automatic model fallback on rate limit / server errors
- Handles both string and array content formats
- Optional proxy-level Bearer Token authentication
- Request body size limit to prevent OOM attacks

Usage:
    VERTEX_AI_API_KEY="your-key" python proxy.py

Environment variables:
    VERTEX_AI_API_KEY  - Vertex AI Express API Key (required)
    PROXY_PORT         - Listen port, default 4000
    PROXY_HOST         - Listen address, default 127.0.0.1 (use 0.0.0.0 for container deployments)
    PROXY_AUTH_TOKEN   - Optional Bearer token required from clients
    MAX_BODY_SIZE      - Max request body size in bytes, default 1048576 (1MB)

Disclaimer:
    Usage is subject to the Google Cloud Terms of Service and Vertex AI usage policies.
    This proxy forwards conversation content to Google's servers. Do not transmit sensitive data.
    See: https://cloud.google.com/terms/
"""
import json
import logging
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

# -- Config ------------------------------------------------
API_KEY = os.environ.get("VERTEX_AI_API_KEY", "")
BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"
PORT = int(os.environ.get("PROXY_PORT", "4000"))
# Listen address: use 127.0.0.1 for local dev, 0.0.0.0 for container/cloud deployments
HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
# Optional: set to require clients to send a Bearer token, protecting your Vertex AI quota
PROXY_AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
# Max request body size in bytes to prevent oversized payloads from causing OOM
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", str(1 * 1024 * 1024)))  # default 1MB

# Fallback chains: when the primary model fails, try alternatives in order
# Model IDs reference: https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models
FALLBACK_CHAINS: dict[str, list[str]] = {
    "gemini-3.1-pro-preview": ["gemini-3.1-flash-lite-preview"],
}

# -- Startup validation ------------------------------------
# Validate at module load time so `uvicorn proxy:app` can't bypass the __main__ check
if not API_KEY:
    raise RuntimeError(
        "VERTEX_AI_API_KEY environment variable is not set.\n"
        "Please set it first: export VERTEX_AI_API_KEY='your-key'"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gemini-proxy")
app = FastAPI(title="gemini-openai-proxy")


# -- Auth Middleware ----------------------------------------
@app.middleware("http")
async def check_proxy_auth(request: Request, call_next):
    """Optional proxy-level authentication. Enabled by setting PROXY_AUTH_TOKEN."""
    if PROXY_AUTH_TOKEN:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {PROXY_AUTH_TOKEN}"
        if auth != expected:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "unauthorized", "type": "auth_error"}},
            )
    return await call_next(request)


# -- Format Conversion -------------------------------------
def extract_text(content) -> str:
    """Extract plain text from an OpenAI content field.
    Handles both formats:
      - "hello"
      - [{"type": "text", "text": "hello"}]
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return str(content) if content else ""


def openai_to_gemini(messages: list) -> dict:
    """Convert OpenAI messages to Gemini contents format.
    Note: Gemini REST API uses snake_case (system_instruction), not camelCase.
    """
    contents, sys_text = [], None
    for msg in messages:
        text = extract_text(msg.get("content", ""))
        if msg["role"] == "system":
            sys_text = text
            continue
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": text}]})
    body: dict = {"contents": contents}
    if sys_text:
        body["system_instruction"] = {"parts": [{"text": sys_text}]}
    return body


def gemini_to_openai(resp: dict, model: str) -> dict:
    """Convert a Gemini generateContent response to OpenAI chat completion format."""
    choices = []
    for i, c in enumerate(resp.get("candidates", [])):
        text = "".join(p.get("text", "") for p in c.get("content", {}).get("parts", []))
        choices.append({
            "index": i,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        })
    u = resp.get("usageMetadata", {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": choices,
        "usage": {
            "prompt_tokens": u.get("promptTokenCount", 0),
            "completion_tokens": u.get("candidatesTokenCount", 0),
            "total_tokens": u.get("totalTokenCount", 0),
        },
    }


def _sanitize_error(raw_error: dict) -> dict:
    """Sanitize Gemini API error responses before returning to clients.
    Only passes through message and status to avoid leaking project IDs or quota details.
    """
    err = raw_error.get("error", raw_error)
    return {
        "error": {
            "message": err.get("message", "upstream error"),
            "type": err.get("status", "api_error"),
        }
    }


# -- Gemini API Headers ------------------------------------
def _gemini_headers() -> dict:
    """Build headers for Gemini API requests.
    Uses x-goog-api-key header instead of query string to avoid leaking the key in logs.
    """
    return {"Content-Type": "application/json", "x-goog-api-key": API_KEY}


# -- Non-streaming Call ------------------------------------
async def call_gemini(gemini_body: dict, models_to_try: list):
    """Call Gemini generateContent with fallback support.
    Returns a (data, model_used, error_response) tuple.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        for i, m in enumerate(models_to_try):
            url = f"{BASE_URL}/{m}:generateContent"
            try:
                r = await client.post(url, json=gemini_body, headers=_gemini_headers())
                if r.status_code == 200:
                    if i > 0:
                        logger.info(f"fallback succeeded -> {m}")
                    return r.json(), m, None
                if r.status_code in (429, 500, 502, 503) and i < len(models_to_try) - 1:
                    logger.warning(f"{m} returned {r.status_code}, switching to next model")
                    continue
                # Sanitize error before returning to client
                try:
                    err_body = _sanitize_error(r.json())
                except Exception:
                    err_body = {"error": {"message": f"upstream returned {r.status_code}"}}
                return None, m, JSONResponse(status_code=r.status_code, content=err_body)
            except httpx.TimeoutException:
                if i < len(models_to_try) - 1:
                    logger.warning(f"{m} timed out, switching to next model")
                    continue
                return None, m, JSONResponse(
                    status_code=504,
                    content={"error": {"message": "all models timed out"}},
                )
            except httpx.HTTPError as e:
                # Catch other network errors: ConnectError, ReadError, etc.
                logger.error(f"{m} network error: {type(e).__name__}")
                if i < len(models_to_try) - 1:
                    continue
                return None, m, JSONResponse(
                    status_code=502,
                    content={"error": {"message": "upstream connection error"}},
                )
    return None, models_to_try[-1], JSONResponse(
        status_code=500,
        content={"error": {"message": "no model available"}},
    )


# -- True SSE Streaming ------------------------------------
async def stream_gemini(gemini_body: dict, models_to_try: list):
    """Stream responses via the Gemini streamGenerateContent endpoint.
    Yields OpenAI-compatible SSE chunks as they arrive from Gemini.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    ts = int(time.time())

    for i, m in enumerate(models_to_try):
        url = f"{BASE_URL}/{m}:streamGenerateContent?alt=sse"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", url, json=gemini_body, headers=_gemini_headers()) as resp:
                    if resp.status_code != 200:
                        if resp.status_code in (429, 500, 502, 503) and i < len(models_to_try) - 1:
                            logger.warning(f"{m} returned {resp.status_code}, switching to next model")
                            continue
                        await resp.aread()
                        yield f"data: {json.dumps({'error': {'message': f'upstream returned {resp.status_code}', 'code': resp.status_code}})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    if i > 0:
                        logger.info(f"fallback succeeded -> {m}")

                    # Parse Gemini SSE events and convert to OpenAI format
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if not raw.strip():
                            continue
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Extract text from the Gemini chunk
                        text = ""
                        for cand in data.get("candidates", []):
                            for part in cand.get("content", {}).get("parts", []):
                                text += part.get("text", "")

                        if text:
                            chunk = {
                                "id": chunk_id,
                                "object": "chat.completion.chunk",
                                "created": ts,
                                "model": m,
                                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"

                    # Send the final stop chunk
                    stop = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": ts,
                        "model": m,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(stop)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

        except httpx.TimeoutException:
            if i < len(models_to_try) - 1:
                logger.warning(f"{m} timed out, switching to next model")
                continue
            yield f"data: {json.dumps({'error': {'message': 'all models timed out'}})}\n\n"
            yield "data: [DONE]\n\n"
            return
        except httpx.HTTPError as e:
            # Catch ConnectError, ReadError, and other network errors
            logger.error(f"{m} network error: {type(e).__name__}")
            if i < len(models_to_try) - 1:
                continue
            yield f"data: {json.dumps({'error': {'message': 'upstream connection error'}})}\n\n"
            yield "data: [DONE]\n\n"
            return

    yield f"data: {json.dumps({'error': {'message': 'no model available'}})}\n\n"
    yield "data: [DONE]\n\n"


# -- API Endpoints -----------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Read the raw body first to enforce size limit on actual bytes received.
    # Checking Content-Length header alone is insufficient — clients can omit it
    # or set it to an incorrect value. Reading body_bytes is the only reliable check.
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": {"message": f"request body too large (max {MAX_BODY_SIZE} bytes)"}},
        )

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": {"message": "invalid JSON body"}})
    model = body.get("model", "gemini-2.5-pro-preview-03-25").replace("google/", "")
    is_stream = body.get("stream", False)

    gb = openai_to_gemini(body.get("messages", []))
    if "temperature" in body:
        gb.setdefault("generation_config", {})["temperature"] = body["temperature"]
    if "max_tokens" in body:
        gb.setdefault("generation_config", {})["max_output_tokens"] = body["max_tokens"]

    models = [model] + FALLBACK_CHAINS.get(model, [])

    if is_stream:
        return StreamingResponse(stream_gemini(gb, models), media_type="text/event-stream")

    resp_data, used_model, err = await call_gemini(gb, models)
    if err:
        return err
    return gemini_to_openai(resp_data, used_model)


@app.get("/v1/models")
async def list_models():
    # Deduplicate model IDs across all fallback chains
    seen: set[str] = set()
    models = []
    for chain in [[k] + v for k, v in FALLBACK_CHAINS.items()]:
        for m in chain:
            if m not in seen:
                seen.add(m)
                models.append({
                    "id": m,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "google",
                })
    return {"object": "list", "data": models}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting proxy at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)