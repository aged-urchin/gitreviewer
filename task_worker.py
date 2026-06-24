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
from review_engine import launch_review, read_review_output
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
    keepalive: bool = True   # False → 客户端不轮询，服务端跑完为止
    fast_mode: bool = False
    webhook_url: str = ""


class TaskWorker:
    """管理 review 任务的队列和执行"""

    def __init__(self):
        self._queue: asyncio.Queue[ReviewTask] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REVIEWS)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._last_poll: dict[str, float] = {}  # review_id → 最近一次轮询时间
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._processing_count = 0
        self._completed_count = 0
        self._update_title()

    def _update_title(self):
        import sys
        queued = self._queue.qsize()
        processing = self._processing_count
        completed = self._completed_count
        
        title = f"GitReviewer - Processing: {processing} | Completed: {completed} | Queued: {queued}"
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        else:
            sys.stdout.write(f"\033]0;{title}\007")
            
        if self._running:
            sys.stdout.write(f"\033[s\033[1;1H\033[2K\033[44;37m GitReviewer Tasks | Processing: {processing} | Queued: {queued} | Completed: {completed} \033[0m\033[u")
            
        sys.stdout.flush()

    def touch(self, review_id: str):
        """客户端轮询了一次，更新心跳时间"""
        import time
        self._last_poll[review_id] = time.time()

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
        
        import os
        import sys
        if os.name == "nt":
            os.system("")
        sys.stdout.write("\033[2J\033[2;r\033[2;1H")
        sys.stdout.flush()
        self._update_title()

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
                
        import sys
        sys.stdout.write("\033[r")
        sys.stdout.flush()
        logger.info("Task worker stopped")

    async def submit(self, task: ReviewTask) -> str:
        """提交任务到队列，返回 review_id"""
        await add_review(task.session_id, task.review)
        await self._queue.put(task)
        self._update_title()
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

            self._processing_count += 1
            self._update_title()

            async def wrap_process(t: ReviewTask):
                try:
                    await self._process_task(t)
                finally:
                    self._processing_count -= 1
                    self._completed_count += 1
                    self._update_title()

            # 异步启动处理（不阻塞队列消费）
            asyncio.create_task(wrap_process(task))

    async def _process_task(self, task: ReviewTask):
        session_id = task.session_id
        review = task.review

        # 1. 获取全局并发槽位（控制跨 session 并发）
        async with self._semaphore:
            # 2. 获取 per-session 锁（同一 session 内串行，git 操作不冲突）
            session_lock = self._get_session_lock(session_id)
            async with session_lock:

                # 更新状态为 processing
                review.status = "processing"
                review.started_at = _now()
                await update_review(session_id, review)

                repo_dir = _repo_dir(session_id)

                # 确定 review 范围
                if task.patch:
                    review.scope = "local uncommitted changes"
                elif task.description and task.description != "Review the last commit (git diff HEAD~1)":
                    review.scope = f"agent-driven: {task.description[:80]}"
                else:
                    review.scope = "last commit (HEAD~1)"

                try:
                    # 如果有本地未提交改动，先 apply 到工作区
                    if task.patch:
                        patch_file = repo_dir / "_gitreviewer_uncommitted.patch"
                        await asyncio.get_running_loop().run_in_executor(
                            None, apply_patch, repo_dir, task.patch, patch_file
                        )
                        try: patch_file.unlink()
                        except Exception: pass

                    import time
                    KEEPALIVE_TIMEOUT = 10  # 客户端超过 30s 未轮询则停止

                    process = await launch_review(task.description, repo_dir, fast_mode=task.fast_mode)

                    if task.keepalive:
                        # 轮询模式：边等 Claude Code 边检查客户端是否还在
                        cancelled = False
                        
                        # 把 communicate 放入后台任务中，避免 stdout 管道塞满导致子进程死锁
                        communicate_task = asyncio.create_task(read_review_output(process, fast_mode=task.fast_mode))
                        
                        while True:
                            done, pending = await asyncio.wait([communicate_task], timeout=3.0)
                            if communicate_task in done:
                                break
                            
                            # 客户端还在吗？
                            last = self._last_poll.get(review.review_id, 0)
                            if time.time() - last > KEEPALIVE_TIMEOUT:
                                process.kill()
                                await process.wait()
                                cancelled = True
                                break

                        if cancelled:
                            communicate_task.cancel()
                            review.status = "cancelled"
                            review.summary = "Client disconnected, review cancelled."
                            review.completed_at = _now()
                        else:
                            findings, summary = communicate_task.result()
                            review.findings = findings
                            review.summary = summary
                            review.status = "completed"
                            review.completed_at = _now()
                    else:
                        # NoPoll 模式：跑完为止，不检查客户端
                        findings, summary = await read_review_output(process, fast_mode=task.fast_mode)
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
