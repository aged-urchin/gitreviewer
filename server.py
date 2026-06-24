"""
GitReviewer HTTP API Server — FastAPI 入口。

启动方式:
    python server.py
    或
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import API_KEY, SERVER_PORT
from models import Session, Review, Finding, _now, _uid
from session_manager import (
    create_session,
    get_session,
    end_session,
    list_sessions,
    _repo_dir,
)
from task_worker import ReviewTask, get_worker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gitreviewer")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时管理 worker"""
    worker = get_worker()
    await worker.start()
    logger.info(f"GitReviewer started on port {SERVER_PORT}")
    yield
    await worker.stop()
    logger.info("GitReviewer stopped")


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="GitReviewer",
    description="Based on Git + Claude Code + DeepSeek",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

async def verify_api_key(request: Request):
    """如果配置了 API_KEY，则验证请求头"""
    if API_KEY is None:
        return  # 未配置 API_KEY，跳过认证
    auth = request.headers.get("Authorization", "")
    if not auth:
        raise HTTPException(status_code=401, detail="Authorization header required")
    # 支持 "Bearer <key>" 和 "<key>" 两种格式
    if auth.startswith("Bearer "):
        auth = auth[7:]
    if auth != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    git_url: str = Field(..., description="Git 仓库地址")
    branch: str = Field("main", description="分支名")


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str
    created_at: str


class SubmitReviewRequest(BaseModel):
    description: str = Field("", description="Review 描述，告诉模型关注什么；留空则默认 review 整个仓库")
    patch: str = Field("", description="本地未提交的 diff（客户端自动检测并附带）")
    no_poll: bool = Field(False, description="客户端不轮询，服务端跑完为止")
    fast_mode: bool = Field(False, description="快速模式，跳过 JSON 严格验证格式，直接输出摘要")
    webhook_url: str = Field("", description="可选的回调 URL，review 完成后 POST 通知")


class SubmitReviewResponse(BaseModel):
    review_id: str
    session_id: str
    status: str
    created_at: str


class ReviewResponse(BaseModel):
    review_id: str
    description: str
    status: str
    created_at: str
    completed_at: Optional[str]
    findings: list[dict]
    summary: str
    error: str


class SessionResponse(BaseModel):
    session_id: str
    git_url: str
    branch: str
    status: str
    created_at: str
    reviews: list[ReviewResponse]
    closed_at: Optional[str]


# ---------------------------------------------------------------------------
# Routes: Session
# ---------------------------------------------------------------------------

@app.post("/api/v1/sessions", response_model=CreateSessionResponse)
async def api_create_session(
    body: CreateSessionRequest,
    _: None = Depends(verify_api_key),
):
    """开启新会话：克隆仓库并返回 session_id"""
    logger.info(f"Creating session for {body.git_url} (branch: {body.branch})")

    try:
        session = await create_session(body.git_url, body.branch)
    except Exception as e:
        logger.error(f"Session creation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")

    return CreateSessionResponse(
        session_id=session.session_id,
        status=session.status,
        created_at=session.created_at,
    )


@app.get("/api/v1/sessions")
async def api_list_sessions(
    status: Optional[str] = None,
    _: None = Depends(verify_api_key),
):
    """列出所有会话"""
    sessions = await list_sessions(status)
    return {
        "count": len(sessions),
        "sessions": [
            {
                "session_id": s.session_id,
                "git_url": s.git_url,
                "branch": s.branch,
                "status": s.status,
                "created_at": s.created_at,
                "review_count": len(s.reviews),
                "closed_at": s.closed_at,
            }
            for s in sessions
        ],
    }


@app.get("/api/v1/sessions/{session_id}")
async def api_get_session(
    session_id: str,
    _: None = Depends(verify_api_key),
):
    """获取会话详情（包含 review 列表）"""
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.session_id,
        "git_url": session.git_url,
        "branch": session.branch,
        "status": session.status,
        "created_at": session.created_at,
        "reviews": [r.to_dict() for r in session.reviews],
        "closed_at": session.closed_at,
    }


@app.delete("/api/v1/sessions/{session_id}")
async def api_end_session(
    session_id: str,
    _: None = Depends(verify_api_key),
):
    """结束会话：清理工作区并标记关闭"""
    ok = await end_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    logger.info(f"Session {session_id} ended")
    return {"status": "cleaned", "message": "Session ended, workspace cleaned"}


# ---------------------------------------------------------------------------
# Routes: Review
# ---------------------------------------------------------------------------

@app.post("/api/v1/sessions/{session_id}/reviews", response_model=SubmitReviewResponse)
async def api_submit_review(
    session_id: str,
    body: SubmitReviewRequest,
    _: None = Depends(verify_api_key),
):
    """提交 review 任务（异步处理）。

    服务端默认 review 整个仓库（第一个 commit 到 HEAD）。通过 description 指导模型关注重点。
    """
    # 验证 session 存在
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == "closed":
        raise HTTPException(status_code=400, detail="Session is closed")
    if session.status != "ready":
        raise HTTPException(status_code=400, detail=f"Session is not ready (status: {session.status})")

    # 创建 review 任务
    review = Review(
        review_id=_uid(),
        description=body.description,
        status="queued",
        created_at=_now(),
    )

    task = ReviewTask(
        session_id=session_id,
        review=review,
        description=body.description,
        patch=body.patch,
        keepalive=not body.no_poll,
        fast_mode=body.fast_mode,
        webhook_url=body.webhook_url,
    )

    worker = get_worker()
    await worker.submit(task)

    logger.info(f"Review {review.review_id} submitted to session {session_id}")

    return SubmitReviewResponse(
        review_id=review.review_id,
        session_id=session_id,
        status="queued",
        created_at=review.created_at,
    )


@app.get("/api/v1/sessions/{session_id}/reviews")
async def api_list_reviews(
    session_id: str,
    _: None = Depends(verify_api_key),
):
    """列出会话中的所有 review"""
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "count": len(session.reviews),
        "reviews": [
            {
                "review_id": r.review_id,
                "description": r.description,
                "status": r.status,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "finding_count": len(r.findings),
                "summary": r.summary,
            }
            for r in session.reviews
        ],
    }


@app.get("/api/v1/sessions/{session_id}/reviews/{review_id}")
async def api_get_review(
    session_id: str,
    review_id: str,
    _: None = Depends(verify_api_key),
):
    """获取 review 结果（调用方轮询此接口）"""
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    review = session.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    # 通知 worker：客户端还在轮询
    worker = get_worker()
    worker.touch(review_id)

    return review.to_dict()


# ---------------------------------------------------------------------------
# Routes: Reset (可选)
# ---------------------------------------------------------------------------

@app.post("/api/v1/sessions/{session_id}/reset")
async def api_reset_session(
    session_id: str,
    _: None = Depends(verify_api_key),
):
    """重置会话 repo 到初始状态（撤销所有已应用的 patch）"""
    import asyncio
    from git_manager import reset_hard

    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    repo_dir = _repo_dir(session_id)
    if not repo_dir.exists():
        raise HTTPException(status_code=500, detail="Repo directory not found")

    try:
        await asyncio.get_running_loop().run_in_executor(None, reset_hard, repo_dir)
        logger.info(f"Session {session_id} repo reset to HEAD")
        return {"status": "reset", "message": "Repo reset to initial state"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )
