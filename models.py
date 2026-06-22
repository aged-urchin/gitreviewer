"""
数据模型 — 纯 dataclass + JSON 序列化，零 ORM 依赖。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import uuid
import json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Finding — 单个 review 发现的问题
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str       # "high" | "medium" | "low"
    category: str       # "bug" | "security" | "style" | "performance"
    file: str           # 文件路径
    line: int           # 行号
    title: str          # 问题标题
    description: str    # 问题描述
    suggestion: str     # 修复建议

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(**{k: d.get(k) for k in ["severity", "category", "file", "line", "title", "description", "suggestion"]})


# ---------------------------------------------------------------------------
# Review — 一次 review 任务
# ---------------------------------------------------------------------------

@dataclass
class Review:
    review_id: str
    description: str
    status: str         # "queued" | "processing" | "completed" | "failed" | "cancelled"
    created_at: str
    completed_at: Optional[str] = None
    scope: str = ""     # 本次 review 覆盖的范围，如 "all commits (root..HEAD)"
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        d = {}
        d["review_id"] = self.review_id
        d["description"] = self.description
        d["status"] = self.status
        d["created_at"] = self.created_at
        d["completed_at"] = self.completed_at
        d["scope"] = self.scope
        d["findings"] = [f.to_dict() for f in self.findings]
        d["summary"] = self.summary
        d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Review":
        findings = [Finding.from_dict(f) for f in d.get("findings", [])]
        return cls(
            review_id=d.get("review_id", ""),
            description=d.get("description", ""),
            status=d.get("status", "queued"),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at"),
            scope=d.get("scope", ""),
            findings=findings,
            summary=d.get("summary", ""),
            error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# Session — 一个会话
# ---------------------------------------------------------------------------

@dataclass
class Session:
    session_id: str
    git_url: str
    branch: str
    status: str         # "cloning" | "ready" | "closed"
    created_at: str
    reviews: list[Review] = field(default_factory=list)
    closed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "git_url": self.git_url,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at,
            "reviews": [r.to_dict() for r in self.reviews],
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        reviews = [Review.from_dict(r) for r in d.get("reviews", [])]
        return cls(
            session_id=d.get("session_id", ""),
            git_url=d.get("git_url", ""),
            branch=d.get("branch", "main"),
            status=d.get("status", "ready"),
            created_at=d.get("created_at", ""),
            reviews=reviews,
            closed_at=d.get("closed_at"),
        )

    def get_review(self, review_id: str) -> Optional[Review]:
        for r in self.reviews:
            if r.review_id == review_id:
                return r
        return None
