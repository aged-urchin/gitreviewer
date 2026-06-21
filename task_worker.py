"""
任务调度器 — 管理 review 任务的异步队列，通过信号量控制并发。
"""
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from config import MAX_CONCURRENT_REVIEWS
from models import Review, _now, _uid
from review_engine import run_review
from git_manager import apply_patch, GitError
from session_manager import (
    add_review, update_review, get_session,
    _repo_dir,
)

logger = logging.getLogger("gitreviewer.task_worker")


@dataclass
class ReviewTask:
    """待处理的 review 任务"""
    session_id: str
    review: Review = field(default_factory=lambda: Review(
        review_id=_uid(),
        description="",
        status="queued",
        created_at=_now(),
    ))
    description: str = ""
    patch: str = ""
    webhook_url: str = ""


class TaskWorker:
    """管理 review 任务的队列和执行"""

    def __init__(self):
        self._queue: asyncio.Queue[ReviewTask] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REVIEWS)
        self._session_locks: dict[str, asyncio.Lock] = {}  # 同一 session 内串行处理
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """获取 per-session 锁，确保同一 session 的 review 串行执行"""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    async def start(self):
        """启动 worker 主循环"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info(f"Task worker started (max concurrent: {MAX_CONCURRENT_REVIEWS})")

    async def stop(self):
        """停止 worker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Task worker stopped")

    async def submit(self, task: ReviewTask) -> str:
        """提交任务到队列，返回 review_id"""
        await add_review(task.session_id, task.review)
        await self._queue.put(task)
        logger.info(
            f"Review {task.review.review_id} queued for session {task.session_id}"
        )
        return task.review.review_id

    async def _main_loop(self):
        """主循环：持续从队列取任务并处理"""
        while self._running:
            try:
                # 等待新任务（1s 超时以便检查 _running）
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # 异步启动处理（不阻塞队列消费）
            asyncio.create_task(self._process_task(task))

    async def _process_task(self, task: ReviewTask):
        """处理单个 review 任务"""
        session_id = task.session_id
        review = task.review

        # 1. 获取全局并发槽位（控制跨 session 并发）
        async with self._semaphore:
            # 2. 获取 per-session 锁（同一 session 内串行，git 操作不冲突）
            session_lock = self._get_session_lock(session_id)
            async with session_lock:

                # 更新状态为 processing
                review.status = "processing"
                await update_review(session_id, review)

                repo_dir = _repo_dir(session_id)

                try:
                    # 如果有本地未提交改动，先 apply 到工作区
                    if task.patch:
                        patch_file = repo_dir / "_gitreviewer_uncommitted.patch"
                        await asyncio.get_running_loop().run_in_executor(
                            None, apply_patch, repo_dir, task.patch, patch_file
                        )
                        try:
                            patch_file.unlink()
                        except Exception:
                            pass

                    # Claude Code agent 在 repo 目录中自行探索，按描述 review
                    findings, summary = await run_review(
                        description=task.description,
                        repo_dir=repo_dir,
                    )

                    review.findings = findings
                    review.summary = summary
                    review.status = "completed"
                    review.completed_at = _now()

                except GitError as e:
                    review.status = "failed"
                    review.error = f"Git error: {e}"
                    review.completed_at = _now()

                except Exception as e:
                    review.status = "failed"
                    review.error = str(e)
                    review.completed_at = _now()
                    logger.exception(f"Review {review.review_id} failed with exception")

                # 保存结果
                await update_review(session_id, review)

                # 如果有 webhook，发送回调
                if task.webhook_url:
                    await _send_webhook(task.webhook_url, review)

                logger.info(
                    f"Review {review.review_id} {review.status}: "
                    f"{len(review.findings)} findings"
            )


# ---------------------------------------------------------------------------
# Webhook 回调
# ---------------------------------------------------------------------------

async def _send_webhook(url: str, review: Review):
    """向调用方推送 review 完成通知"""
    try:
        import httpx
        from config import WEBHOOK_TIMEOUT

        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            await client.post(url, json=review.to_dict())
            logger.info(f"Webhook sent to {url}")
    except Exception as e:
        logger.warning(f"Webhook failed for {url}: {e}")


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_worker: Optional[TaskWorker] = None


def get_worker() -> TaskWorker:
    global _worker
    if _worker is None:
        _worker = TaskWorker()
    return _worker
