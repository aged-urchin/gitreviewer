"""
Review 引擎 — 通过 Claude Code CLI 执行代码 review。
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

CLAUDE_TIMEOUT = 600


def _find_claude() -> str:
    # 1) PATH 中查找（跨平台）
    found = shutil.which("claude")
    if found:
        return found

    # 2) npm 全局目录（Windows）
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for name in ["claude.cmd", "claude", "claude.ps1"]:
            p = Path(appdata) / "npm" / name
            if p.exists():
                return str(p)

    # 3) npm 全局目录（Unix）
    for prefix in [os.path.expanduser("~/.npm-global/bin"), "/usr/local/bin", "/usr/bin"]:
        p = Path(prefix) / "claude"
        if p.exists():
            return str(p)

    raise FileNotFoundError("Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code")


SYSTEM_INSTRUCTION = """You are a code reviewer. You are in a git repository. Use git commands (log, diff, show, etc.) and read files to understand the code and complete the review task described below. Answer in Simplified Chinese (简体中文)."""


def _build_prompt(description: str, fast_mode: bool = False) -> str:
    parts = [SYSTEM_INSTRUCTION, "", "## Task"]
    parts.append(description if description else "Review this codebase for bugs, security issues, and design problems.")
    parts.append("")
    if fast_mode:
        parts.append(
            "## Output Format (MUST use Simplified Chinese 简体中文)\n"
            "FAST MODE: Please output a brief, free-form text summary of your findings. "
            "Do NOT output the strict JSON format. Just describe any issues concisely."
        )
    else:
        parts.append(
            "## Output Format (MUST follow exactly, use Simplified Chinese 简体中文)\n"
            "When you are done, output your findings as the JSON object specified.\n"
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


def _parse_output(raw: str, fast_mode: bool = False) -> tuple[list[Finding], str]:
    text = raw.strip()
    if fast_mode:
        # Claude 有时会在混合指令下输出字面量 \n，这里做一下清理
        text = text.replace("\\n\n", "\n").replace("\\n", "\n")
        return [], text

    candidates: list[str] = []
    candidates.append(text)
    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        candidates.append(m.group(1))
    for m in re.finditer(r"\{", text):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[m.start() : i + 1]); break
    last_error = None
    for c in candidates:
        c = c.strip()
        if not c: continue
        try:
            data = json.loads(c)
            if isinstance(data, dict) and "findings" in data:
                findings = [Finding(
                    severity=f.get("severity","low"), category=f.get("category","style"),
                    file=f.get("file",""), line=int(f.get("line",0)), title=f.get("title","Untitled"),
                    description=f.get("description",""), suggestion=f.get("suggestion",""),
                ) for f in data["findings"] if isinstance(f, dict)]
                return findings, data.get("summary", "")
        except json.JSONDecodeError as e:
            last_error = f"JSON parse failed (len={len(c)}): {e} | first 100 chars: {c[:100]}"
            continue
        except (ValueError, KeyError) as e:
            last_error = f"Parse exception: {e}"
            continue
            
    if last_error:
        logger.warning(last_error)
    logger.warning(f"Could not parse output. text_len={len(text)} preview={text[:200]}")
    return [], text[:500]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def launch_review(description: str, repo_dir: Path, fast_mode: bool = False) -> asyncio.subprocess.Process:
    """启动 Claude Code 子进程并立即发送 prompt，返回 Process 句柄。"""
    claude_path = _find_claude()
    prompt = _build_prompt(description, fast_mode)
    logger.info(f"Launching Claude Code agent in: {repo_dir}")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    process = await asyncio.create_subprocess_exec(
        claude_path, "--print",
        cwd=str(repo_dir),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    # 立即发送 prompt，避免 Claude Code 等 stdin 超时
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    return process


async def read_review_output(process: asyncio.subprocess.Process, fast_mode: bool = False) -> tuple[list[Finding], str]:
    """等待 Claude Code 结束，读取并解析输出。"""
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=CLAUDE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Claude Code timed out after {CLAUDE_TIMEOUT}s")

    if process.returncode != 0:
        err = (stderr or stdout).decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Claude Code exited with {process.returncode}: {err}")

    return _parse_output(stdout.decode("utf-8", errors="replace"), fast_mode)
