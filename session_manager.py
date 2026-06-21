"""
会话管理 — 每个会话一个目录，state.json 承载全部状态。
"""
import json
from pathlib import Path
from typing import Optional
import asyncio

from config import WORKSPACE_ROOT
from models import Session, Review, _now, _uid
from git_manager import clone as git_clone, GitError


# 每个会话一个 asyncio.Lock，防止 state.json 并发写入冲突
_session_locks: dict[str, asyncio.Lock] = {}


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def _session_dir(session_id: str) -> Path:
    return WORKSPACE_ROOT / session_id


def _repo_dir(session_id: str) -> Path:
    return _session_dir(session_id) / "repo"


def _patches_dir(session_id: str) -> Path:
    return _session_dir(session_id) / "patches"


def _state_path(session_id: str) -> Path:
    return _session_dir(session_id) / "state.json"


# ---------------------------------------------------------------------------
# State file I/O（原子写入）
# ---------------------------------------------------------------------------

def _read_state(session_id: str) -> Optional[Session]:
    path = _state_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return None


def _write_state(session: Session) -> None:
    """原子写入：先写 tmp，再 rename"""
    path = _state_path(session.session_id)
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)  # Windows 上也是原子操作


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

async def create_session(git_url: str, branch: str = "main") -> Session:
    """创建新会话：克隆仓库，返回 session 对象"""
    session_id = _uid()
    session = Session(
        session_id=session_id,
        git_url=git_url,
        branch=branch,
        status="cloning",
        created_at=_now(),
    )

    # 确保目录存在
    _session_dir(session_id).mkdir(parents=True, exist_ok=True)
    _patches_dir(session_id).mkdir(parents=True, exist_ok=True)
    _write_state(session)

    # 异步克隆（实际是同步操作，但在后台线程中执行）
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, git_clone, git_url, branch, _repo_dir(session_id)
        )
        session.status = "ready"
    except GitError as e:
        session.status = "failed"
        # 记录错误到 session（复用 error 字段的场景不多，加一个 note）
        _write_state(session)
        raise e

    _write_state(session)
    return session


async def get_session(session_id: str) -> Optional[Session]:
    """读取会话"""
    async with _get_lock(session_id):
        return _read_state(session_id)


async def add_review(session_id: str, review: Review) -> None:
    """向会话添加一个新的 review"""
    async with _get_lock(session_id):
        session = _read_state(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        session.reviews.append(review)
        _write_state(session)


async def update_review(session_id: str, review: Review) -> None:
    """更新 review 状态"""
    async with _get_lock(session_id):
        session = _read_state(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        for i, r in enumerate(session.reviews):
            if r.review_id == review.review_id:
                session.reviews[i] = review
                break
        _write_state(session)


async def end_session(session_id: str) -> bool:
    """结束会话：标记关闭、清理工作区"""
    async with _get_lock(session_id):
        session = _read_state(session_id)
        if session is None:
            return False
        session.status = "closed"
        session.closed_at = _now()
        _write_state(session)

    # 异步清理文件
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _cleanup_dir(session_id))
    return True


def _cleanup_dir(session_id: str) -> None:
    """清理会话目录"""
    from git_manager import remove_dir
    remove_dir(_session_dir(session_id))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

async def list_sessions(status_filter: Optional[str] = None) -> list[Session]:
    """列出所有会话（遍历 workspaces 目录）"""
    sessions = []
    if not WORKSPACE_ROOT.exists():
        return sessions

    for d in WORKSPACE_ROOT.iterdir():
        if not d.is_dir():
            continue
        state_file = d / "state.json"
        if not state_file.exists():
            continue
        try:
            session = Session.from_dict(
                json.loads(state_file.read_text(encoding="utf-8"))
            )
            if status_filter and session.status != status_filter:
                continue
            sessions.append(session)
        except (json.JSONDecodeError, KeyError):
            continue

    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return sessions
