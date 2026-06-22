# GitReviewer

基于 Git + Claude Code 的代码 Review 服务。AI 后端取决于 Claude Code 的配置，不绑定任何特定模型。通过 HTTP API 接收 review 任务，跨平台运行。

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
       └─ Review Engine ──── Claude Code CLI（后端取决于 settings.json 配置）
```

## 快速开始

### 1. 环境要求

- Python 3.10+（Windows / macOS / Linux）
- Node.js LTS + Claude Code CLI（`npm install -g @anthropic-ai/claude-code`）
- Git（已配置好账号和 PATH）
- Claude Code CLI 已配置好 API 后端（settings.json 中设置，如 Anthropic / DeepSeek / 自建代理等）

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

### 3. 配置 Claude Code 后端

GitReviewer 本身不持有任何 API 凭据。Claude Code 的后端由用户在其 `~/.claude/settings.json` 中配置，可以是 Anthropic 官方 API，也可以是任何 Anthropic 兼容端点（如 DeepSeek `/anthropic`、自建代理等）。

```powershell
# 编辑 Claude Code 全局配置
notepad $env:USERPROFILE\.claude\settings.json
```

示例——使用 DeepSeek 作为后端：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-your-api-key",
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-chat",
    ...
  }
}
```

> 使用其他后端只需修改 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_MODEL`。参考 `docs/claude_settings.example.json`。

验证配置：

```powershell
echo 'reply OK' | claude --print --max-turns 1
```

### 4. 启动服务

```powershell
# 直接启动
python server.py

# 或使用 uvicorn
uvicorn server:app --host 0.0.0.0 --port 8000
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger API 文档。

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

### 完整示例

以下展示一个典型的 review 闭环：提交 → 发现缺陷 → 修改 → 复查 → 结束。

**1. 首次 review——提交最近一次 commit：**

```powershell
PS D:\work\myproject> .\send-to-review.ps1 -des "review 最近一次 commit，重点检查安全性"

Creating session for https://github.com/user/myproject.git (main)...
Session: a1b2c3d4e5f6 (ready)
Submitting review...
Review: r001 (queued)
Waiting...
.
========================================
  Review Complete
========================================
Summary: 发现 1 个高危 SQL 注入漏洞和 2 个代码风格问题
Findings: 3
========================================

[HIGH] src/auth.py:42 - SQL 注入风险
  Category: security
  Problem: user_id 直接拼接到 SQL 查询中
  Fix: 使用参数化查询 cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

[MEDIUM] src/auth.py:78 - 密码明文存储
  Category: security
  Problem: 密码未哈希直接存入数据库
  Fix: 使用 bcrypt 哈希后再存储

[LOW] src/utils.py:15 - 变量命名不清晰
  Category: style
  Problem: 变量名 'x' 和 'y' 含义不明
  Fix: 改为 'user_count' 和 'active_count'

Summary: 1 high, 1 medium, 1 low
```

**2. 根据 findings 修改代码**，修复 SQL 注入和密码哈希问题后 commit：

```powershell
git add src/auth.py
git commit -m "fix: 修复 SQL 注入和密码明文存储"
```

**3. 复查修改结果：**

```powershell
PS D:\work\myproject> .\send-to-review.ps1 -des "review 修复 SQL 注入和密码哈希的改动，确认安全问题已解决"

Session: a1b2c3d4e5f6 @ http://gitreviewer-pc:8000   # 复用已有 session
Submitting review...
Review: r002 (queued)
Waiting...
.
========================================
  Review Complete
========================================
Summary: SQL 注入已修复，密码哈希实现正确，建议补上 salt
Findings: 1
========================================

[LOW] src/auth.py:82 - 建议为 bcrypt 添加 salt
  Category: style  Problem: 使用默认 salt 强度  Fix: 显式设置 rounds=12

Summary: 0 high, 0 medium, 1 low
```

**4. 确认高危和中危问题已修复，结束 session：**

```powershell
PS D:\work\myproject> .\send-to-review.ps1 -EndSession

Session ended
```

## 客户端脚本

提供 PowerShell（`client/send-to-review.ps1`）和 Bash（`client/send-to-review.sh`）两个客户端脚本，功能完全相同。放入目标 git 工程目录后使用。脚本会缓存 session，同一工程只 clone 一次。

### 场景一：本地有未提交的改动

**只 review 本地改动**（必须跟上 `-des` 描述改了什么）：

```powershell
.\send-to-review.ps1 -local -des "修复了用户登录时的空指针异常"
.\send-to-review.ps1 -local -des "重构数据库连接池，改为单例模式"
```

脚本自动将 `git diff HEAD` 发给服务端 apply 后再 review。不加 `-local` 则忽略本地改动，走场景二。

### 场景二：工作区干净（或忽略本地改动）

**有 `-des`**：服务端根据描述指令操作。

```powershell
.\send-to-review.ps1 -des "review 最近 3 次提交，重点检查安全问题"
.\send-to-review.ps1 -des "检查 src/auth 目录的权限校验逻辑"
.\send-to-review.ps1 -des "review 整个工程，找出设计缺陷并给出改进建议"
.\send-to-review.ps1 -des "review main..feature 分支的差异"
```

**没有 `-des`**：默认 review 最近一次 commit。

```powershell
.\send-to-review.ps1
```

效果等同于 `-des "Review the last commit (git diff HEAD~1)"`。

### 常用参数

| 参数 | 说明 |
|------|------|
| `-des` | review 描述/指令，别名 `-Description` |
| `-local` | 附带本地未提交改动，必须同时给 `-des` |
| `-Server` | 服务端地址，默认 `http://localhost:8000` |
| `-NoPoll` | 只提交不等待结果 |
| `-EndSession` | review 完后清理服务端 session |
| `-PollInterval` | 轮询间隔秒数，默认 2 |

### `.gitreviewer_session`

脚本首次运行时会在当前目录自动创建 `.gitreviewer_session`，缓存 session_id 和服务器地址，之后无需重复指定 `-Server`。文件内容为 JSON：

```json
{"session_id": "517f130696e6", "server": "http://gitreviewer-pc:8000"}
```

- **复用 session**：同一工程后续调用跳过 clone，直接复用已有仓库
- **切换服务器**：再次指定 `-Server` 会覆盖缓存
- **结束 session**：`-EndSession` 会删除此文件并清理服务端
- **手动切换项目**：删除此文件即可从零开始
- 建议加入 `.gitignore`（服务端信息不应提交）

### 轮询与任务取消

客户端默认每 2 秒轮询一次结果。服务端在 review 执行期间持续检查心跳——若客户端 **超过 10 秒未轮询**（如关闭了终端），服务端会终止当前 review，任务状态变为 `cancelled`。

使用 `-NoPoll` 则跳过轮询，服务端不受影响，review 会跑完为止。

### Codex 集成

将 `send-to-review.ps1` 放入 Codex 工作的 repo，在 Codex 的项目配置中加入：

> 完成功能变更后运行 `.\send-to-review.ps1 -local -des "<change summary>"`，等待结果并根据 findings 修复问题后再提交。

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
| `env.ANTHROPIC_AUTH_TOKEN` | API Key（后端认证） |
| `env.ANTHROPIC_BASE_URL` | API 端点（任何 Anthropic 兼容地址） |
| `env.ANTHROPIC_MODEL` | 模型名称 |

> 参考 `docs/claude_settings.example.json` 查看完整模板。

## 目录结构

```
gitreviewer/
├── server.py              # FastAPI 入口
├── config.py              # 配置管理
├── models.py              # 数据模型（dataclass）
├── git_manager.py         # Git 操作封装
├── session_manager.py     # 会话生命周期管理
├── review_engine.py       # Claude Code CLI 调用
├── task_worker.py         # 异步任务调度
├── requirements.txt       # Python 依赖
├── .env.example           # 配置模板
├── client/
│   ├── send-to-review.ps1  # PowerShell 客户端
│   └── send-to-review.sh   # Bash 客户端
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
