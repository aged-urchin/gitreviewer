# GitReviewer

基于 Git + Claude Code + DeepSeek 的代码 Review 服务。运行在 Windows 台式机上，通过 HTTP API 接收 review 任务。

## 架构

```
调用方 (Codex / curl / 脚本)
       │
       ▼
  HTTP REST API (FastAPI)
       │
       ├─ Session Manager ── workspaces/{session_id}/state.json
       ├─ Git Manager ────── git clone / apply / diff / reset
       ├─ Task Worker ────── asyncio.Queue + Semaphore(3)
       └─ Review Engine ──── Claude Code CLI → DeepSeek API (/anthropic)
```

## 快速开始

### 1. 环境要求

- Windows 10/11
- Python 3.10+
- Node.js LTS + Claude Code CLI（`npm install -g @anthropic-ai/claude-code`）
- Git（已配置好账号和 PATH）
- DeepSeek API Key（从 https://platform.deepseek.com 获取）

### 2. 安装

```powershell
cd d:\work\experiment\gitreviewer

# 创建虚拟环境（推荐）
python -m venv venv
.\venv\Scripts\Activate.ps1

# 安装 Python 依赖
pip install -r requirements.txt

# 创建 GitReviewer 配置
copy .env.example .env
```

### 3. 配置 Claude Code（DeepSeek 后端）

Claude Code 的 API 配置在 `~/.claude/settings.json` 中管理，与 GitReviewer 无关。
参考 `docs/claude_settings.example.json`，将 DeepSeek API Key 填入 `env` 字段：

```powershell
# 编辑 Claude Code 全局配置
notepad $env:USERPROFILE\.claude\settings.json
```

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-your-deepseek-api-key",
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-chat",
    ...
  }
}
```

验证配置生效：

```powershell
claude -p "reply OK" --print --max-turns 1
```

### 4. 启动服务

```powershell
# 直接启动
python server.py

# 或使用 uvicorn
uvicorn server:app --host 0.0.0.0 --port 8000
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger API 文档。

### 4. 配置 DeepSeek API

DeepSeek 提供了 Anthropic 兼容端点（`/anthropic`），Claude Code CLI 可以直接使用。

编辑 `.env` 文件：

```ini
DEEPSEEK_API_KEY=sk-your-actual-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MODEL=deepseek-chat
```

## API 使用

### 创建会话

```powershell
$session = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/sessions" `
    -Method Post `
    -Body '{"git_url":"https://github.com/user/repo.git","branch":"main"}' `
    -ContentType "application/json"

$sessionId = $session.session_id
```

### 提交 Review

```powershell
$patch = git diff HEAD~1

$review = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/sessions/$sessionId/reviews" `
    -Method Post `
    -Body (@{patch=$patch; description="检查登录模块的安全性"} | ConvertTo-Json) `
    -ContentType "application/json"

$reviewId = $review.review_id
```

### 轮询结果

```powershell
# 每 5 秒轮询一次
do {
    Start-Sleep 5
    $result = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/sessions/$sessionId/reviews/$reviewId"
} while ($result.status -eq "queued" -or $result.status -eq "processing")

# 输出 findings
$result.findings | Format-Table severity, category, file, line, title
```

### 结束会话

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/sessions/$sessionId" -Method Delete
```

## 客户端脚本

项目包含一个一键式客户端脚本 `client/send-to-review.ps1`：

```powershell
# 在目标 git repo 中运行
.\send-to-review.ps1 -Description "实现了用户登录，请检查安全性"

# 提交到远程服务器
.\send-to-review.ps1 -Description "重构数据库层" -Server "http://gitreviewer-pc:8000"

# 只提交不等待结果
.\send-to-review.ps1 -Description "修复 bug" -NoPoll

# Review 后清理 session
.\send-to-review.ps1 -Description "最终版本" -EndSession
```

### Codex 集成

在 Codex 工作的 repo 中告诉它：

> "每次完成一个功能变更后，运行 `.\send-to-review.ps1 -Description '<change summary>'` 提交 review，等待结果并根据 findings 修复问题。"

或者在 Codex 的项目配置文件中加入指令，使其自动在 commit 后触发 review。

## 配置项

### GitReviewer 配置（`.env`）

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `SERVER_PORT` | 8000 | HTTP 服务端口 |
| `WORKSPACE_ROOT` | ./workspaces | Session + Git 工作区根目录 |
| `MAX_CONCURRENT_REVIEWS` | 3 | 最大并行 review 数 |
| `API_KEY` | (空) | API 认证密钥（不填则跳过认证） |
| `WEBHOOK_TIMEOUT` | 30 | Webhook 回调超时（秒） |

### Claude Code 配置（`~/.claude/settings.json`）

| 配置 | 说明 |
|------|------|
| `env.ANTHROPIC_AUTH_TOKEN` | DeepSeek API Key |
| `env.ANTHROPIC_BASE_URL` | `https://api.deepseek.com/anthropic` |
| `env.ANTHROPIC_MODEL` | 模型名，如 `deepseek-chat` |

> 参考 `docs/claude_settings.example.json` 查看完整模板。

## 目录结构

```
gitreviewer/
├── server.py              # FastAPI 入口
├── config.py              # 配置管理
├── models.py              # 数据模型（dataclass）
├── git_manager.py         # Git 操作封装
├── session_manager.py     # 会话生命周期管理
├── review_engine.py       # DeepSeek API 调用
├── task_worker.py         # 异步任务调度
├── requirements.txt       # Python 依赖
├── .env.example           # 配置模板
├── client/
│   └── send-to-review.ps1 # 客户端脚本
├── workspaces/            # 运行时创建的 session 目录
└── README.md
```

## Session 目录结构

```
workspaces/{session_id}/
├── state.json             # 全部状态（session + reviews + findings）
├── repo/                  # Git clone 的工作目录
└── patches/               # 各 review 的 patch 备份
    ├── {review_id_1}.patch
    └── {review_id_2}.patch
```
