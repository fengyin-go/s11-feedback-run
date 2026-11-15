#!/usr/bin/env python3
"""Git-free run tree generation and host-side integration.

New runs mount ``run-tree`` instead of the Git ``workspace``. The run tree is
created from the frozen commit and may receive ignored dependency caches. It
starts without ``.git``; any Git metadata created by the model is excluded from
delivery. After the run, this module applies its non-ignored files back to the
host Git workspace so the existing snapshot/push pipeline can create the
artifact commit.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Iterable

import fbrlib as lib

META = "run/run-tree.json"
TREE_NAME = "run-tree"
VERSION = 1
FIXED_EXCLUDES = {".git", ".claude"}
CODE_NOISE_RE = re.compile(
    r"(^|/)(node_modules|__pycache__|\.cache|\.pytest_cache|dist|coverage|\.venv|venv)(/|$)"
    r"|\.(log|pyc|mp4|webm)$",
    re.I,
)
DOC_SUFFIXES = {".md", ".rst", ".txt"}


def tree_dir(task_dir: Path) -> Path:
    return Path(task_dir) / TREE_NAME


def metadata(task_dir: Path) -> dict:
    return lib.load_json(Path(task_dir) / META, default={})


def enabled(task_dir: Path) -> bool:
    return (Path(task_dir) / META).is_file()


def _safe_rel(relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise lib.FbrError(f"运行树路径越界：{relative}")
    return path.as_posix()


def _dependency_paths(task_dir: Path, initial: str) -> list[str]:
    receipt = lib.load_json(Path(task_dir) / "run/dependencies.json", default={})
    if receipt.get("state") != "copied" or receipt.get("initial_commit") != initial:
        return []
    result = []
    for raw in receipt.get("paths", []):
        value = str(raw or "").strip().rstrip("/")
        if value:
            result.append(_safe_rel(value))
    return sorted(set(result))


def _excluded(relative: str, dependencies: Iterable[str]) -> bool:
    path = PurePosixPath(relative)
    if any(part in FIXED_EXCLUDES for part in path.parts):
        return True
    return any(path == PurePosixPath(dep) or path.is_relative_to(PurePosixPath(dep))
               for dep in dependencies)


def _hash_algorithm(repo: Path) -> str:
    value = lib.git_out(repo, "rev-parse", "--show-object-format")
    if value not in {"sha1", "sha256"}:
        raise lib.FbrError(f"不支持的 Git 对象格式：{value}")
    return value


def _blob_oid(path: Path, algorithm: str) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        payload = os.readlink(path).encode("utf-8")
        size = len(payload)
    elif stat.S_ISREG(info.st_mode):
        size = info.st_size
        payload = None
    else:
        raise lib.FbrError(f"运行树只允许普通文件和符号链接：{path}")
    digest = hashlib.new(algorithm)
    digest.update(f"blob {size}\0".encode("ascii"))
    if payload is not None:
        digest.update(payload)
    else:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _mode(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    if not stat.S_ISREG(info.st_mode):
        raise lib.FbrError(f"运行树只允许普通文件和符号链接：{path}")
    return "100755" if info.st_mode & 0o111 else "100644"


def _baseline(repo: Path, initial: str) -> dict[str, dict[str, str]]:
    raw = lib.git_out(repo, "ls-tree", "-rz", "--full-tree", initial)
    rows: dict[str, dict[str, str]] = {}
    for item in raw.split("\0"):
        if not item:
            continue
        head, sep, relative = item.partition("\t")
        if not sep:
            raise lib.FbrError("Git 基线清单格式异常")
        parts = head.split()
        if len(parts) != 3:
            raise lib.FbrError("Git 基线清单格式异常")
        mode, kind, oid = parts
        if kind != "blob":
            continue
        rows[_safe_rel(relative)] = {"mode": mode, "oid": oid}
    return rows


def _ignored(repo: Path, relative: str) -> bool:
    checked = lib.git(repo, "check-ignore", "-q", "--no-index", "--", relative)
    if checked.returncode not in (0, 1):
        raise lib.FbrError(f"无法检查忽略规则：{relative}")
    return checked.returncode == 0


def _walk_files(root: Path, dependencies: Iterable[str]) -> tuple[list[str], list[str]]:
    included: list[str] = []
    excluded: set[str] = set(dependencies)
    root = Path(root)
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept = []
        for name in sorted(dirnames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, dependencies):
                excluded.add(relative)
            elif path.is_symlink():
                included.append(relative)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, dependencies):
                excluded.add(relative)
            else:
                included.append(relative)
    return sorted(included), sorted(excluded)


def _model_git_created(root: Path) -> bool:
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        if ".git" in dirnames or ".git" in filenames:
            return True
        dirnames[:] = [name for name in dirnames if name not in {".git", ".claude"}]
    return False


def _deliverable(task_dir: Path, repo: Path, initial: str) -> tuple[dict[str, dict[str, str]],
                                                                  list[str], list[str]]:
    baseline = _baseline(repo, initial)
    dependencies = _dependency_paths(task_dir, initial)
    files, excluded = _walk_files(tree_dir(task_dir), dependencies)
    deliverable = []
    for relative in files:
        if relative not in baseline and _ignored(repo, relative):
            excluded.append(relative)
        else:
            deliverable.append(relative)
    return baseline, sorted(deliverable), sorted(set(excluded))


def summary(task_dir: Path, record: dict) -> dict:
    task_dir = Path(task_dir)
    if not enabled(task_dir):
        raise lib.FbrError(f"{task_dir.name}：没有 Git-free 运行树")
    repo, initial = task_dir / "workspace", record["initial_commit"]
    baseline, deliverable, excluded = _deliverable(task_dir, repo, initial)
    algorithm = _hash_algorithm(repo)
    current = set(deliverable)
    files = []
    for relative in sorted(set(baseline) | current):
        if relative not in current:
            files.append({"path": relative, "status": "D"})
            continue
        path = tree_dir(task_dir) / relative
        oid = _blob_oid(path, algorithm)
        mode = _mode(path)
        if relative not in baseline:
            files.append({"path": relative, "status": "A"})
        elif oid != baseline[relative]["oid"] or mode != baseline[relative]["mode"]:
            files.append({"path": relative, "status": "M"})
    return {"files": files, "excluded": excluded, "deliverable": deliverable,
            "tree": str(tree_dir(task_dir)), "source_commit": initial,
            "model_git_created": _model_git_created(tree_dir(task_dir))}


def code_changes(task_dir: Path, record: dict) -> list[str]:
    found = summary(task_dir, record)
    return [
        item["path"] for item in found["files"]
        if not CODE_NOISE_RE.search(item["path"])
        and PurePosixPath(item["path"]).suffix.lower() not in DOC_SUFFIXES
    ]


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _ensure_parent(root: Path, path: Path) -> None:
    current = path.parent
    while current != root:
        if current.is_symlink():
            raise lib.FbrError(f"目标父目录不能是符号链接：{current.relative_to(root)}")
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True)


def _copy(source: Path, target: Path, root: Path) -> None:
    _ensure_parent(root, target)
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
        return
    shutil.copy2(source, target, follow_symlinks=False)


def _materialize(task_dir: Path, record: dict, target: Path) -> dict:
    found = summary(task_dir, record)
    baseline = _baseline(task_dir / "workspace", record["initial_commit"])
    for relative in baseline:
        _remove(target / relative)
    for relative in found["deliverable"]:
        _copy(tree_dir(task_dir) / relative, target / relative, target)
    return found


def prepare(task_dir: Path, record: dict, cfg: dict) -> dict:
    """Create ``run-tree`` from the frozen commit and optional dependency cache."""
    task_dir = Path(task_dir)
    workspace, initial = task_dir / "workspace", record["initial_commit"]
    lib.validate_git_layout(task_dir)
    destination = tree_dir(task_dir)
    if destination.exists() or destination.is_symlink():
        _remove(destination)
    (task_dir / "run/dependencies.json").unlink(missing_ok=True)
    (task_dir / "run/integration.json").unlink(missing_ok=True)
    clone = lib.clean_clone(workspace, initial, f"s11-run-tree-{task_dir.name}")
    try:
        copied_dependencies = False
        if cfg.get("dependencies", {}).get("enabled"):
            import fbr_dependencies
            copied_dependencies = bool(fbr_dependencies.seed(clone, cfg, task=task_dir))
        shutil.copytree(clone, destination, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".claude"))
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)
    dependencies = _dependency_paths(task_dir, initial)
    lib.open_permissions(destination)
    result = {
        "version": VERSION,
        "task_id": record["task_id"],
        "task_repo_full": record["task_repo_full"],
        "prompt_sha256": hashlib.sha256(record["prompt"].encode("utf-8")).hexdigest(),
        "source_commit": initial,
        "no_git": True,
        "dependency_paths": dependencies,
        "dependencies_seeded": copied_dependencies,
        "created_at": lib.now_iso(),
    }
    lib.save_json(task_dir / META, result)
    validate(task_dir, record)
    return result


def validate(task_dir: Path, record: dict) -> dict:
    task_dir = Path(task_dir)
    meta = metadata(task_dir)
    root = tree_dir(task_dir)
    problems = []
    if meta.get("version") != VERSION or meta.get("no_git") is not True:
        problems.append("运行树元数据缺失或版本不符")
    if meta.get("source_commit") != record["initial_commit"]:
        problems.append("运行树不是从冻结初始提交生成")
    expected_prompt = hashlib.sha256(record["prompt"].encode("utf-8")).hexdigest()
    if meta.get("task_id") != record["task_id"] or meta.get("task_repo_full") != record["task_repo_full"]:
        problems.append("运行树归属与当前任务不一致")
    if meta.get("prompt_sha256") != expected_prompt:
        problems.append("运行树对应题面与当前任务不一致")
    if root.is_symlink() or not root.is_dir():
        problems.append("运行树目录缺失或不是独立目录")
    if problems:
        raise lib.FbrError(f"{task_dir.name}：" + "；".join(problems))
    return meta


def integrate(task_dir: Path, record: dict, cfg: dict) -> dict:
    """Apply the run tree to the host Git workspace without exposing ``.git``."""
    task_dir = Path(task_dir)
    validate(task_dir, record)
    workspace, initial = task_dir / "workspace", record["initial_commit"]
    hygiene = lib.restore_git_config(task_dir)
    lib.git_out(workspace, "reset", "--hard", initial)
    lib.git_out(workspace, "clean", "-fdx")
    found = _materialize(task_dir, record, workspace)
    lib.git_out(workspace, "add", "-A")
    result = {
        "state": "integrated",
        "source": "git-free-run-tree",
        "source_commit": initial,
        "files": found["files"],
        "excluded": found["excluded"],
        "model_git_created": found["model_git_created"],
        "git_hygiene": hygiene,
        "integrated_at": lib.now_iso(),
    }
    lib.save_json(task_dir / "run/integration.json", result)
    return result


def archive_patch(task_dir: Path, record: dict, target: Path) -> dict:
    """Write a binary-safe patch for an invalid attempt, then remove run-tree."""
    task_dir = Path(task_dir)
    validate(task_dir, record)
    clone = lib.clean_clone(task_dir / "workspace", record["initial_commit"],
                            f"s11-attempt-tree-{task_dir.name}")
    try:
        _materialize(task_dir, record, clone)
        lib.git_out(clone, "add", "-A")
        patch = lib.git(clone, "diff", "--cached", "--binary", "--no-color",
                        record["initial_commit"]).stdout
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch, encoding="utf-8")
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)
    meta = metadata(task_dir)
    discard(task_dir, record)
    meta.update(discarded_at=lib.now_iso())
    return meta


def discard(task_dir: Path, record: dict) -> None:
    task_dir = Path(task_dir)
    root = tree_dir(task_dir)
    if root.exists() or root.is_symlink():
        _remove(root)
    (task_dir / META).unlink(missing_ok=True)
    workspace = task_dir / "workspace"
    if (workspace / ".git").is_dir():
        lib.restore_git_config(task_dir)
        lib.git_out(workspace, "reset", "--hard", record["initial_commit"])
        lib.git_out(workspace, "clean", "-fdx")
