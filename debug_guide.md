# OpenClaw + Gemini Proxy 排障指南

当 OpenClaw 突然不回复消息时，按以下步骤从外到内逐层排查。

## 架构回顾

```
OpenClaw → proxy.py (:4000) → aiplatform.googleapis.com (Vertex AI Express)
```

问题可能出在三个环节中的任何一个。

---

## Step 1: 确认代理进程是否还活着

```bash
# 检查 proxy.py 是否在运行
ps aux | grep proxy.py | grep -v grep

# 检查 4000 端口是否在监听
lsof -i :4000
```

如果进程不在了，重启：
```bash
VERTEX_AI_API_KEY="你的KEY" python3 ~/.openclaw/vertex_ai/opensource/gemini-openai-proxy/proxy.py
```

---

## Step 2: 确认网络能否到达 Vertex AI

```bash
# 测试 DNS 解析
nslookup aiplatform.googleapis.com

# 测试 HTTPS 连通性（不发请求，只测 TLS 握手）
curl -sv --max-time 10 https://aiplatform.googleapis.com 2>&1 | head -20

# 如果上面超时，测试基本网络
ping -c 3 google.com
curl -sv --max-time 10 https://www.google.com 2>&1 | head -10
```

如果 DNS 通但 HTTPS 不通，可能是软路由代理规则没覆盖 `aiplatform.googleapis.com`，检查软路由的代理规则。

---

## Step 3: 直接调 Vertex AI API（绕过代理）

```bash
curl -s -w "\nHTTP_STATUS:%{http_code} TIME:%{time_total}s" \
  "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-pro-preview:generateContent" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: ${VERTEX_AI_API_KEY}" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}' \
  --max-time 30
```

预期结果：
- `HTTP_STATUS:200` + JSON 响应 → Vertex AI 正常，问题在代理或 OpenClaw
- `HTTP_STATUS:429` → 配额用完或限流
- `HTTP_STATUS:403` → API Key 失效或被禁用
- `HTTP_STATUS:000` + 超时 → 网络不通，回 Step 2
- `HTTP_STATUS:000` + 立即返回 → 连接被拒绝

---

## Step 4: 通过代理调用（测试 proxy.py）

```bash
# 非流式
curl -s -w "\nHTTP_STATUS:%{http_code} TIME:%{time_total}s" \
  http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"hi"}]}' \
  --max-time 30

# 流式
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-pro-preview","stream":true,"messages":[{"role":"user","content":"hi"}]}' \
  --max-time 30
```

预期结果：
- 非流式返回 JSON + `HTTP_STATUS:200` → 代理正常
- 流式返回 `data: {...}` + `data: [DONE]` → 流式正常
- 超时 → 代理卡住了，看 Step 5

---

## Step 5: 查看代理日志

代理的日志直接输出到终端（stdout/stderr）。关注以下信息：

```bash
# 如果代理在后台运行，查看日志
# 如果用 nohup 启动的：
tail -100 nohup.out

# 如果用 systemd：
journalctl -u gemini-proxy -n 100
```

关注的关键词：
- `timed out` → Vertex AI 响应超时
- `returned 429` → 限流，配额可能用完
- `returned 403` → API Key 问题
- `returned 500/502/503` → Vertex AI 服务端问题
- `fallback ->` → 主模型失败，已切换到备选模型
- `network error` → 网络连接问题

---

## Step 6: 检查 GCP 配额和计费

```bash
# 如果装了 gcloud
gcloud auth activate-service-account --key-file=vertex_ai/credits.json --project=你的项目ID
gcloud services list --enabled --project=你的项目ID 2>&1 | grep aiplatform
```

或者直接在浏览器里检查：
1. 配额：https://console.cloud.google.com/iam-admin/quotas?project=你的项目ID
2. 计费：https://console.cloud.google.com/billing （确认 $300 额度是否用完）
3. API Key 状态：https://console.cloud.google.com/apis/credentials （确认 Key 没被禁用）

---

## Step 7: 检查 OpenClaw 侧

```bash
# 查看 OpenClaw 状态
openclaw status

# 查看最近的会话
openclaw sessions list

# 查看 OpenClaw 日志
tail -100 ~/.openclaw/logs/gateway.log
tail -100 ~/.openclaw/logs/gateway.err.log
```

---

## 常见问题速查

| 现象 | 可能原因 | 解决 |
|---|---|---|
| 代理进程不在了 | OOM / 崩溃 | 重启代理，考虑用 systemd 管理 |
| Step 3 超时 | 网络不通 | 检查软路由代理规则 |
| Step 3 返回 429 | 配额限流 | 等待或检查 GCP 配额 |
| Step 3 返回 403 | API Key 失效 | 去 GCP Console 检查 Key 状态 |
| Step 4 通但 OpenClaw 不回复 | OpenClaw 侧问题 | 查 gateway.log |
| Step 4 流式卡住 | 代理流式解析问题 | 查代理日志，尝试非流式 |
| 一切正常但 OpenClaw 不回复 | 会话上下文太长 | 新建会话试试 |

---

## 快速一键诊断脚本

把以下内容保存为 `diagnose.sh`，出问题时直接运行：

```bash
#!/bin/bash
echo "=== 1. 代理进程 ==="
ps aux | grep proxy.py | grep -v grep || echo "❌ proxy.py 未运行"

echo ""
echo "=== 2. 端口监听 ==="
lsof -i :4000 2>/dev/null | head -3 || echo "❌ 4000 端口无监听"

echo ""
echo "=== 3. DNS 解析 ==="
nslookup aiplatform.googleapis.com 2>&1 | tail -3

echo ""
echo "=== 4. 直连 Vertex AI ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
  "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.1-pro-preview:generateContent" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: ${VERTEX_AI_API_KEY}" \
  -d '{"contents":[{"role":"user","parts":[{"text":"ping"}]}]}')
echo "HTTP Status: $HTTP_CODE"
[ "$HTTP_CODE" = "200" ] && echo "✅ Vertex AI 正常" || echo "❌ Vertex AI 异常 ($HTTP_CODE)"

echo ""
echo "=== 5. 通过代理调用 ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
  http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"ping"}],"max_tokens":10}')
echo "HTTP Status: $HTTP_CODE"
[ "$HTTP_CODE" = "200" ] && echo "✅ 代理正常" || echo "❌ 代理异常 ($HTTP_CODE)"

echo ""
echo "=== 6. OpenClaw 日志（最后 5 行）==="
tail -5 ~/.openclaw/logs/gateway.err.log 2>/dev/null || echo "无日志文件"
```

运行：`chmod +x diagnose.sh && ./diagnose.sh`