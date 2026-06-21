"""
Review 引擎 — 通过 Claude Code CLI 执行代码 review。

Claude Code 在目标仓库目录中运行，可以自由使用 git、读文件等工具，
根据用户的描述自行决定 review 范围和重点。
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from models import Finding

logger = logging.getLogger("gitreviewer.review_engine")

CLAUDE_TIMEOUT = 600  # agent 模式可能多轮，放宽超时


def _find_claude() -> str:
    for candidate in [
        shutil.which("claude"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.ps1"),
    ]:
        if candidate and Path(candidate).exists():
            return candidate

    raise FileNotFoundError(
        "Claude Code CLI not found.\n"
        "Install: npm install -g @anthropic-ai/claude-code"
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """You are a code reviewer. You are in a git repository. Use git commands (log, diff, show, etc.) and read files to understand the code and complete the review task described below. When you are done, output your findings as the JSON object specified. Answer in Simplified Chinese (简体中文)."""


def _build_prompt(description: str) -> str:
    parts = [SYSTEM_INSTRUCTION, "", "## Task"]
    if description:
        parts.append(description)
    else:
        parts.append("Review this codebase for bugs, security issues, and design problems.")
    parts.append("")
    parts.append(
        "## Output Format (MUST follow exactly, use Simplified Chinese 简体中文)\n"
        "When done, output ONLY a JSON object, no markdown fences:\n"
        '{"summary":"<summary in Chinese>",'
        '"findings":['
        '{"severity":"high|medium|low",'
        '"category":"bug|security|style|performance",'
        '"file":"path/to/file",'
        '"line":42,'
        '"title":"short title",'
        '"description":"what is wrong",'
        '"suggestion":"how to fix"}]}\n'
        "If no issues, use empty findings array."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude Code 调用
# ---------------------------------------------------------------------------

async def _call_claude(prompt: str, repo_dir: Path) -> str:
    claude_path = _find_claude()
    cmd = [claude_path, "--print"]

    logger.info(f"Calling Claude Code agent in: {repo_dir}")

    loop = asyncio.get_running_loop()

    try:
        process = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                cwd=str(repo_dir),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            ),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude Code timed out after {CLAUDE_TIMEOUT}s")
    except FileNotFoundError:
        raise RuntimeError("Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code")

    if process.returncode != 0:
        stderr = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"Claude Code exited with {process.returncode}: {stderr[:500]}")

    return process.stdout.strip()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse_output(raw: str) -> tuple[list[Finding], str]:
    candidates: list[str] = []
    text = raw.strip()

    candidates.append(text)

    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        candidates.append(m.group(1))

    for m in re.finditer(r"\{", text):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[m.start() : i + 1])
                    break

    for c in candidates:
        c = c.strip()
        if not c:
            continue
        try:
            data = json.loads(c)
            if isinstance(data, dict) and "findings" in data:
                findings = [
                    Finding(
                        severity=f.get("severity", "low"),
                        category=f.get("category", "style"),
                        file=f.get("file", ""),
                        line=int(f.get("line", 0)),
                        title=f.get("title", "Untitled"),
                        description=f.get("description", ""),
                        suggestion=f.get("suggestion", ""),
                    )
                    for f in data["findings"]
                    if isinstance(f, dict)
                ]
                return findings, data.get("summary", "")
        except (json.JSONDecodeError, ValueError, KeyError):
            continue

    logger.warning(f"Could not parse output. Preview: {text[:300]}")
    return [], text[:500]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_review(description: str, repo_dir: Path) -> tuple[list[Finding], str]:
    """在 repo_dir 中启动 Claude Code agent，按 description 执行 review。"""
    prompt = _build_prompt(description)

    logger.info(f"Starting review in {repo_dir}: {description[:80]}")

    raw = await _call_claude(prompt, repo_dir)

    findings, summary = _parse_output(raw)

    logger.info(f"Review done: {len(findings)} findings, summary={summary[:80] if summary else 'N/A'}")

    return findings, summary
