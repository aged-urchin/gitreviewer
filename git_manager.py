"""
Git 操作封装 — 全部通过 subprocess 调用 git CLI，零第三方 Git 库依赖。
"""
import subprocess
import shutil
from pathlib import Path
from typing import Optional


class GitError(Exception):
    """Git 操作失败"""
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _run(cwd: Path, args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """运行 git 命令，返回 (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Git command timed out"
    except FileNotFoundError:
        return -1, "", "Git not found. Please install git and ensure it's on PATH."
    except Exception as e:
        return -1, "", str(e)


def clone(url: str, branch: str, target: Path, timeout: int = 300) -> None:
    """克隆仓库到目标目录"""
    if target.exists():
        shutil.rmtree(str(target), ignore_errors=True)

    target.parent.mkdir(parents=True, exist_ok=True)

    code, out, err = _run(target.parent, [
        "clone", "--single-branch", "--branch", branch, url, str(target.name)
    ], timeout=timeout)

    if code != 0:
        raise GitError(f"Clone failed: {err or out}")

    # 配置 git 用户（后续操作需要）
    _run(target, ["config", "user.email", "gitreviewer@local"])
    _run(target, ["config", "user.name", "GitReviewer"])

    # 关闭 ignorestat（Windows 下 git 可能有全局 ignorestat=true，
    # 导致 git 不检测文件变更，使 diff 返回空）
    _run(target, ["config", "core.ignorestat", "false"])


def apply_patch(repo_dir: Path, patch_content: str, patch_file: Path) -> None:
    """将 patch 内容写入文件并应用到仓库"""
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch_content, encoding="utf-8")

    # --index 同时更新 index，确保即使 ignorestat=true 也能被 git diff 检测到
    code, out, err = _run(repo_dir, ["apply", "--index", "--ignore-whitespace", str(patch_file)])

    if code != 0:
        raise GitError(f"Patch apply failed: {err or out}")


def diff_unstaged(repo_dir: Path) -> str:
    """获取工作区所有未暂存的 diff"""
    code, out, err = _run(repo_dir, ["diff"])
    # diff 为空时返回空字符串，code=0 是正常的
    return out


def diff_staged(repo_dir: Path) -> str:
    """获取已暂存的 diff"""
    code, out, err = _run(repo_dir, ["diff", "--cached"])
    return out


def first_commit(repo_dir: Path) -> str:
    """获取仓库的第一个 commit hash"""
    code, out, err = _run(repo_dir, ["rev-list", "--max-parents=0", "HEAD"])
    return out if code == 0 else ""


def full_diff(repo_dir: Path) -> str:
    """获取整个仓库的 diff（从第一个 commit 到 HEAD 的所有变更）。

    对于单 commit 仓库，使用 git show 获取初始内容。
    """
    root = first_commit(repo_dir)
    if not root:
        return ""

    code, out, err = _run(repo_dir, ["diff", f"{root}..HEAD"])
    if out:
        return out

    # 可能只有一个 commit，尝试 git show
    code, out, err = _run(repo_dir, ["show", "--format=", "HEAD"])
    return out


def diff_range(repo_dir: Path, range_spec: str) -> str:
    """获取指定 revision 范围的 diff，如 HEAD~3, main..feature"""
    code, out, err = _run(repo_dir, ["diff", range_spec])
    return out


def diff_head(repo_dir: Path) -> str:
    """获取 HEAD 以来的所有变更（暂存 + 未暂存）"""
    code, out, err = _run(repo_dir, ["diff", "HEAD"])
    return out


def reset_hard(repo_dir: Path) -> None:
    """硬重置到 HEAD，丢弃所有本地变更"""
    code, out, err = _run(repo_dir, ["reset", "--hard", "HEAD"])
    if code != 0:
        raise GitError(f"Reset failed: {err or out}")
    # 清理 untracked 文件
    _run(repo_dir, ["clean", "-fd"])


def current_commit(repo_dir: Path) -> str:
    """获取当前 HEAD commit hash"""
    code, out, err = _run(repo_dir, ["rev-parse", "HEAD"])
    return out if code == 0 else ""


def remove_dir(path: Path) -> None:
    """安全删除目录（处理 Windows 文件锁定）"""
    if not path.exists():
        return
    # 先尝试常规删除
    try:
        shutil.rmtree(str(path), ignore_errors=False)
    except Exception:
        # Windows 下可能有文件锁定，强制重试
        import time
        time.sleep(0.5)
        shutil.rmtree(str(path), ignore_errors=True)
