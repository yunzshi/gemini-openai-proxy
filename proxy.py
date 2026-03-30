"""
gemini-openai-proxy: Lightweight Vertex AI Express to OpenAI-compatible proxy

Features:
- OpenAI <-> Gemini format conversion (messages, tools, function calls)
- True SSE streaming via streamGenerateContent
- thoughtSignature passthrough for Gemini thinking models
- Automatic model fallback on rate limit / server errors
- Optional proxy-level Bearer Token authentication

Usage:
    VERTEX_AI_API_KEY="your-key" python proxy.py

Environment variables:
    VERTEX_AI_API_KEY  - Vertex AI Express API Key (required)
    PROXY_PORT         - Listen port, default 4000
    PROXY_HOST         - Listen address, default 127.0.0.1
    PROXY_AUTH_TOKEN   - Optional Bearer token required from clients
    MAX_BODY_SIZE      - Max request body in bytes, default 1048576 (1MB)
    DEBUG_DUMP         - Set to "1" to dump requests to /tmp/proxy_debug.jsonl

Note: Run with a single worker (gunicorn -w 1 or uvicorn) because
thoughtSignature caching uses in-process memory.
"""
import json
import logging
import os
import re
import time
import uuid

import httpx
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

# -- Config ------------------------------------------------
API_KEY = os.environ.get("VERTEX_AI_API_KEY", "")
BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"
PORT = int(os.environ.get("PROXY_PORT", "4000"))
HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", str(1 * 1024 * 1024)))
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "") == "1"

FALLBACK_CHAINS: dict[str, list[str]] = {
    "gemini-3.1-pro-preview": ["gemini-3.1-flash-lite-preview"],
}

_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# Retry config for 429 rate limits
RETRY_429_DELAY = 5
RETRY_429_MAX = 2

if not API_KEY:
    raise RuntimeError("VERTEX_AI_API_KEY environment variable is not set.")

logging.basicConfig(
    level=logging.DEBUG if DEBUG_DUMP else logging.INFO, 
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logger = logging.getLogger("gemini-proxy")
app = FastAPI(title="gemini-openai-proxy")

_thought_sig_cache: dict[str, tuple[str, float]] = {}
_THOUGHT_SIG_TTL = 600


def _cache_thought_sig(call_id: str, sig: str):
    _thought_sig_cache[call_id] = (sig, time.time())
    now = time.time()
    expired = [k for k, (_, ts) in _thought_sig_cache.items() if now - ts > _THOUGHT_SIG_TTL]
    for k in expired:
        del _thought_sig_cache[k]


def _pop_thought_sig(call_id: str) -> str | None:
    entry = _thought_sig_cache.pop(call_id, None)
    if entry is None:
        return None
    sig, ts = entry
    if time.time() - ts > _THOUGHT_SIG_TTL:
        return None
    return sig


# -- Auth Middleware ----------------------------------------
@app.middleware("http")
async def check_proxy_auth(request: Request, call_next):
    if PROXY_AUTH_TOKEN:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {PROXY_AUTH_TOKEN}":
            return JSONResponse(status_code=401, content={"error": {"message": "unauthorized"}})
    return await call_next(request)


# -- Format Conversion: Text -------------------------------
def extract_text(content) -> str:
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


# -- Format Conversion: Tools (OpenAI -> Gemini) -----------
def _clean_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    for k, v in schema.items():
        if k in ("strict", "additionalProperties"):
            continue
        if k == "required" and isinstance(v, list) and len(v) == 0:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        else:
            cleaned[k] = v
    return cleaned


def openai_tools_to_gemini(tools: list) -> list:
    declarations = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        decl = {"name": func["name"]}
        if "description" in func:
            decl["description"] = func["description"]
        if "parameters" in func:
            decl["parameters"] = _clean_schema(func["parameters"])
        declarations.append(decl)
    if not declarations:
        return []
    return [{"function_declarations": declarations}]


# -- Format Conversion: Messages (OpenAI -> Gemini) --------
def openai_to_gemini(messages: list) -> dict:
    contents: list[dict] = []
    sys_text = None
    skip_tc_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            sys_text = extract_text(msg.get("content", ""))
            continue

        if role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in skip_tc_ids:
                skip_tc_ids.discard(tc_id)
                continue
            fn_name = msg.get("name", "unknown")
            fn_content = msg.get("content", "")
            try:
                response_data = json.loads(fn_content) if isinstance(fn_content, str) else fn_content
            except (json.JSONDecodeError, TypeError):
                response_data = {"result": fn_content}
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": fn_name, "response": response_data}}],
            })
            continue

        if role == "assistant" and msg.get("tool_calls"):
            parts = []
            text = extract_text(msg.get("content", ""))
            if text:
                parts.append({"text": text})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                try:
                    fn_args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}
                tc_id = tc.get("id", "")
                sig = _pop_thought_sig(tc_id)
                if sig:
                    fc_part: dict = {
                        "functionCall": {"name": fn.get("name", ""), "args": fn_args}
                    }
                    fc_part["thoughtSignature"] = sig
                    logger.info(f"injected thoughtSignature for {tc_id}")
                    parts.append(fc_part)
                else:
                    skip_tc_ids.add(tc_id)
                    logger.warning(f"no thoughtSignature for {tc_id}, skipping fc+result pair")
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        gemini_role = "user" if role == "user" else "model"
        text = extract_text(msg.get("content", ""))
        contents.append({"role": gemini_role, "parts": [{"text": text}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})

    body: dict = {"contents": contents}
    if sys_text:
        body["system_instruction"] = {"parts": [{"text": sys_text}]}
    return body


# -- Format Conversion: Response (Gemini -> OpenAI) --------
def _map_finish_reason(gemini_reason: str, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    return {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
            "MALFORMED_FUNCTION_CALL": "stop"}.get(gemini_reason, "stop")


def gemini_to_openai(resp: dict, model: str) -> dict:
    choices = []
    for i, c in enumerate(resp.get("candidates", [])):
        parts = c.get("content", {}).get("parts", [])
        finish = c.get("finishReason", "STOP")
        tool_calls, text_parts = [], []

        for part in parts:
            fc_data = part.get("functionCall") or part.get("function_call")
            if fc_data:
                call_id = f"call{uuid.uuid4().hex[:8]}"
                tool_calls.append({
                    "id": call_id, "type": "function",
                    "function": {"name": fc_data.get("name", ""), "arguments": json.dumps(fc_data.get("args", {}))},
                })
                sig = part.get("thoughtSignature") or part.get("thought_signature")
                if sig:
                    _cache_thought_sig(call_id, sig)
                    logger.info(f"cached thoughtSignature for {call_id}")
            elif "text" in part:
                text_parts.append(part["text"])
                sig = part.get("thoughtSignature") or part.get("thought_signature")
                if sig:
                    _cache_thought_sig("_last_text_sig", sig)

        message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish_reason = _map_finish_reason(finish, bool(tool_calls))
        if finish == "MALFORMED_FUNCTION_CALL":
            logger.warning(f"MALFORMED_FUNCTION_CALL, parts={parts}")
        choices.append({"index": i, "message": message, "finish_reason": finish_reason})

    u = resp.get("usageMetadata", {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": model, "choices": choices,
        "usage": {"prompt_tokens": u.get("promptTokenCount", 0),
                  "completion_tokens": u.get("candidatesTokenCount", 0),
                  "total_tokens": u.get("totalTokenCount", 0)},
    }


def _sanitize_error(raw_error: dict) -> dict:
    err = raw_error.get("error", raw_error)
    return {"error": {"message": err.get("message", "upstream error"), "type": err.get("status", "api_error")}}


def _gemini_headers() -> dict:
    return {"Content-Type": "application/json", "x-goog-api-key": API_KEY}


# -- Non-streaming Call ------------------------------------
async def call_gemini(gemini_body: dict, models_to_try: list):
    async with httpx.AsyncClient(timeout=120) as client:
        for i, m in enumerate(models_to_try):
            for retry in range(RETRY_429_MAX + 1):
                try:
                    r = await client.post(f"{BASE_URL}/{m}:generateContent", json=gemini_body, headers=_gemini_headers())
                    if r.status_code == 200:
                        if i > 0: 
                            logger.info(f"fallback -> {m}")
                        return r.json(), m, None
                    
                    if r.status_code == 429 and retry < RETRY_429_MAX:
                        logger.warning(f"{m} returned 429, retrying in {RETRY_429_DELAY}s ({retry+1}/{RETRY_429_MAX})")
                        await asyncio.sleep(RETRY_429_DELAY)
                        continue
                        
                    if r.status_code in (429, 500, 502, 503) and i < len(models_to_try) - 1:
                        logger.warning(f"{m} returned {r.status_code}, switching")
                        break
                        
                    try: 
                        err_body = _sanitize_error(r.json())
                    except Exception: 
                        err_body = {"error": {"message": f"upstream {r.status_code}"}}
                    return None, m, JSONResponse(status_code=r.status_code, content=err_body)
                    
                except httpx.TimeoutException:
                    if i < len(models_to_try) - 1:
                        logger.warning(f"{m} timed out, switching")
                        break
                    return None, m, JSONResponse(status_code=504, content={"error": {"message": "all models timed out"}})
                except httpx.HTTPError as e:
                    logger.error(f"{m} network error: {type(e).__name__}")
                    if i < len(models_to_try) - 1: 
                        break
                    return None, m, JSONResponse(status_code=502, content={"error": {"message": "upstream connection error"}})
    return None, models_to_try[-1], JSONResponse(status_code=500, content={"error": {"message": "no model available"}})


# -- True SSE Streaming ------------------------------------
async def stream_gemini(gemini_body: dict, models_to_try: list):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    ts = int(time.time())
    tool_call_ids: dict[int, str] = {}
    tool_call_index = 0
    has_tool_calls = False

    for i, m in enumerate(models_to_try):
        url = f"{BASE_URL}/{m}:streamGenerateContent?alt=sse"
        for retry in range(RETRY_429_MAX + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", url, json=gemini_body, headers=_gemini_headers()) as resp:
                        if resp.status_code == 429 and retry < RETRY_429_MAX:
                            logger.warning(f"{m} returned 429, retrying in {RETRY_429_DELAY}s ({retry+1}/{RETRY_429_MAX})")
                            await asyncio.sleep(RETRY_429_DELAY)
                            continue
                            
                        if resp.status_code != 200:
                            if resp.status_code in (429, 500, 502, 503) and i < len(models_to_try) - 1:
                                logger.warning(f"{m} returned {resp.status_code}, switching")
                                break
                            await resp.aread()
                            logger.error(f"upstream error: {resp.text[:1000]}")
                            yield f"data: {json.dumps({'error': {'message': f'upstream {resp.status_code}'}})}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                            
                        if i > 0: 
                            logger.info(f"fallback -> {m}")

                        # 修复点：确保 aiter_lines() 在 async with resp 的上下文中执行
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

                            for cand in data.get("candidates", []):
                                fr = cand.get("finishReason", "")
                                if fr == "MALFORMED_FUNCTION_CALL":
                                    logger.warning(f"MALFORMED_FUNCTION_CALL on {m}")

                                for part in cand.get("content", {}).get("parts", []):
                                    if "text" in part and part["text"]:
                                        if DEBUG_DUMP:
                                            logger.debug(f"SSE text: {part['text'][:100]}")
                                        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': ts, 'model': m, 'choices': [{'index': 0, 'delta': {'content': part['text']}, 'finish_reason': None}]})}\n\n"

                                    fc_data = part.get("functionCall") or part.get("function_call")
                                    if fc_data:
                                        has_tool_calls = True
                                        fn_name = fc_data.get("name", "")
                                        if tool_call_index not in tool_call_ids:
                                            tool_call_ids[tool_call_index] = f"call{uuid.uuid4().hex[:8]}"
                                        call_id = tool_call_ids[tool_call_index]
                                        
                                        sig = part.get("thoughtSignature") or part.get("thought_signature")
                                        if sig:
                                            _cache_thought_sig(call_id, sig)
                                            logger.info(f"cached thoughtSignature for {call_id}")
                                            
                                        tc = {"id": call_id, "type": "function",
                                              "function": {"name": fn_name, "arguments": json.dumps(fc_data.get("args", {}))}}
                                        tool_call_index += 1
                                        if DEBUG_DUMP:
                                            logger.debug(f"SSE tool_call: {fn_name}")
                                        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': ts, 'model': m, 'choices': [{'index': 0, 'delta': {'tool_calls': [tc]}, 'finish_reason': None}]})}\n\n"

                        final_reason = "tool_calls" if has_tool_calls else "stop"
                        if DEBUG_DUMP:
                            logger.debug(f"SSE stop: finish_reason={final_reason}")
                        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': ts, 'model': m, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': final_reason}]})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

            except httpx.TimeoutException:
                if i < len(models_to_try) - 1:
                    logger.warning(f"{m} timed out, switching")
                    break
                yield f"data: {json.dumps({'error': {'message': 'all models timed out'}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            except httpx.HTTPError as e:
                logger.error(f"{m} network error: {type(e).__name__}")
                if i < len(models_to_try) - 1: 
                    break
                yield f"data: {json.dumps({'error': {'message': 'upstream connection error'}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            break 

    yield f"data: {json.dumps({'error': {'message': 'no model available'}})}\n\n"
    yield "data: [DONE]\n\n"


# -- Debug Dump --------------------------------------------
def _debug_dump(body: dict):
    try:
        import datetime
        dump_path = "/tmp/proxy_debug.jsonl"
        if os.path.exists(dump_path) and os.path.getsize(dump_path) > 100 * 1024 * 1024:
            return
        with open(dump_path, "a") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(), "body": body}, ensure_ascii=False) + "\n")
    except Exception:
        pass


# -- API Endpoints -----------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_SIZE:
        return JSONResponse(status_code=413, content={"error": {"message": f"body too large (max {MAX_BODY_SIZE})"}})
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": {"message": "invalid JSON"}})

    model = body.get("model", "gemini-3.1-pro-preview").replace("google/", "")
    is_stream = body.get("stream", False)
    if not _MODEL_NAME_RE.match(model):
        return JSONResponse(status_code=400, content={"error": {"message": f"invalid model name: {model}"}})

    logger.info(f"req model={model} stream={is_stream} msgs={len(body.get('messages', []))} tools={len(body.get('tools', []))}")
    if DEBUG_DUMP:
        _debug_dump(body)

    gb = openai_to_gemini(body.get("messages", []))
    if "tools" in body and body["tools"]:
        gemini_tools = openai_tools_to_gemini(body["tools"])
        if gemini_tools:
            gb["tools"] = gemini_tools
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
    seen: set[str] = set()
    models = []
    for chain in [[k] + v for k, v in FALLBACK_CHAINS.items()]:
        for m in chain:
            if m not in seen:
                seen.add(m)
                models.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": "google"})
    return {"object": "list", "data": models}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting proxy at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)