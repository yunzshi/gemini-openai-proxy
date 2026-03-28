# gemini-openai-proxy

轻量级代理，将 Google Vertex AI Express 的 Gemini 模型转换为 OpenAI 兼容 API。

```
你的应用 (OpenAI 格式)  →  proxy.py (:4000)  →  Vertex AI Express
                                                  aiplatform.googleapis.com
```

## 为什么需要这个？

Vertex AI 的 Gemini 模型使用 Google 自己的 API 格式，而大多数 AI 工具（如 OpenClaw、Continue、Cursor 等）只支持 OpenAI 兼容 API。这个代理做格式转换，让你用 OpenAI 的方式调用 Gemini。

为什么不用 LiteLLM？因为 Vertex AI Express 模式使用全局端点 + API Key 认证，LiteLLM 目前不支持这种模式。详见 [踩坑博客](blog.md)。

## 功能

- OpenAI `/v1/chat/completions` 兼容
- SSE 流式响应（`stream: true`）
- 模型 fallback（主模型失败自动降级）
- 兼容字符串和数组两种 content 格式
- system prompt 自动转换

## 快速开始

### 1. 获取 Vertex AI Express API Key

1. 注册 [Google Cloud 免费试用](https://cloud.google.com/free)（$300 额度 / 90 天）
2. 创建项目，启用 [Vertex AI API](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com)
3. 创建 [Service Account](https://console.cloud.google.com/iam-admin/serviceaccounts)，赋予 `Vertex AI User` 角色
4. 创建 [API Key](https://console.cloud.google.com/apis/credentials)：
   - 先勾选"通过服务账号对 API 调用进行身份验证"并选择 Service Account
   - 再将 API 限制设为 `Vertex AI API`

### 2. 启动代理

```bash
pip install -r requirements.txt

VERTEX_AI_API_KEY="你的KEY" python proxy.py
```

代理默认监听 `http://127.0.0.1:4000`。可通过环境变量修改配置。

### 3. 使用

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-lite-preview",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

## 配置

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `VERTEX_AI_API_KEY` | Vertex AI Express API Key（必填） | - |
| `PROXY_PORT` | 代理监听端口 | `4000` |
| `PROXY_HOST` | 代理监听地址 | `127.0.0.1` |
| `PROXY_AUTH_TOKEN` | 可选，设置后要求客户端携带 Bearer Token | （禁用） |
| `MAX_BODY_SIZE` | 请求体大小上限（字节） | `1048576`（1MB） |

容器/云部署时设置 `PROXY_HOST=0.0.0.0`。

## 安全说明

- API Key 通过 `x-goog-api-key` header 传递，不出现在 URL 中，避免泄露到日志
- 代理默认只监听 `127.0.0.1`（本机），容器部署时设置 `PROXY_HOST=0.0.0.0`
- 在共享网络部署时设置 `PROXY_AUTH_TOKEN`，防止他人消耗你的 Vertex AI 配额
- 请求体大小默认限制为 1MB，防止超大 payload 导致 OOM
- 上游错误信息经过脱敏处理后再返回给客户端

## 模型 Fallback

代理内置了 fallback 链，主模型遇到限流（429）或服务端错误（5xx）时自动切换：

| 主模型 | Fallback |
|---|---|
| `gemini-3.1-pro-preview` | `gemini-3.1-flash-lite-preview` |

可在 `proxy.py` 的 `FALLBACK_CHAINS` 中自定义。最新模型 ID 参考 [Vertex AI 模型列表](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models)。

## 接入示例

### OpenClaw

在 `openclaw.json` 的 `models.providers` 中添加：

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

### 其他 OpenAI 兼容客户端

将 `base_url` 设为 `http://localhost:4000/v1`，`api_key` 填任意值即可。

## 已知限制

- Vertex AI Express 模式仅支持 Google 自家模型（Gemini），不支持第三方模型（Claude 等）
- GCP 免费试用项目的 Service Account 无法通过标准 Vertex AI 端点调用模型（已知 bug），这也是本项目存在的原因
- **流式错误状态码**：`StreamingResponse` 一旦开始，HTTP 状态码固定为 200。上游出错时，错误信息以 chunk payload 形式返回，而非 HTTP 错误码。部分客户端库（如 `openai-python`）对此行为处理不一致。这是 HTTP 流式传输的固有限制，无法在不增加预检请求（会引入额外延迟）的前提下解决。
- **流式传输中途 fallback**：模型 fallback 仅在第一个 chunk 发出前有效。若上游在流式传输开始后断开（如 `ReadError`），流会以错误 chunk 结束，而不会透明地切换到备用模型，因为已发出的部分内容无法重放。
- **单 worker**：默认 `python proxy.py` 启动使用单个 uvicorn worker。生产环境有并发需求时，建议使用 gunicorn：`gunicorn proxy:app -w 4 -k uvicorn.workers.UvicornWorker`

## 免责声明

本项目与 Google 无任何官方关联。使用须遵守 [Google Cloud 服务条款](https://cloud.google.com/terms/) 及 [Vertex AI 使用政策](https://cloud.google.com/vertex-ai/docs/generative-ai/use-model-responsibly)。本代理会将对话内容转发至 Google 服务器，请勿传输个人敏感数据。

"OpenAI 兼容"仅指 API 格式兼容，本项目与 OpenAI 无任何关联。

## License

MIT