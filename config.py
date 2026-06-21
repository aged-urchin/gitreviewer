"""
GitReviewer 配置 — 全部从环境变量/.env 读取。

注意：Claude Code 的 API 配置（API Key、Base URL、Model）在 Claude Code 自己的
settings.json 中管理，与 GitReviewer 无关。GitReviewer 只负责调用 claude CLI。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 服务器配置
# ---------------------------------------------------------------------------
SERVER_PORT = _int("SERVER_PORT", 8000)
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "./workspaces")).resolve()
MAX_CONCURRENT_REVIEWS = _int("MAX_CONCURRENT_REVIEWS", 3)
API_KEY = os.getenv("API_KEY", "") or None
WEBHOOK_TIMEOUT = _int("WEBHOOK_TIMEOUT", 30)

# 确保工作区根目录存在
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
