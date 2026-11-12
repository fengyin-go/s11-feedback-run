#!/usr/bin/env python3
"""R3 产物快照：宿主机提交工作区，再把提交推到任务仓库的 main。

    fbr_snapshot.py commit --batch b1110-01 [--tasks …]                       # 只动本地
    fbr_snapshot.py push   --batch b1110-01 [--tasks …]                       # 演练
    fbr_snapshot.py push   --batch b1110-01 --execute --confirm PUBLISH       # 真推

规则：
- 模型自己的提交原样保留，不 squash、不 rebase、不强推；宿主机只在它后面补一个提交收走没提交的改动。
- 初始提交必须是产物提交的祖先。模型改写了历史时，用它最终的文件树在初始提交上另建一个提交，并记入内部快照证据，不进入对外备注。
- 提交前扫密钥：网关 key 原文、.env 和各类密钥文件、token 形态的字符串。命中就停，等人看。
- 推送的 token 只从 gh 登录态按账号取，走环境变量，不落盘、不打印；推完回读远端 SHA 和可见性。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fbrlib as lib  # noqa: E402

COMMIT_MESSAGE = "snapshot: workspace state after the run"
MAX_FILE_BYTES = 50 * 1024 * 1024
SECRET_NAME_RE = re.compile(
    r"(^|/)(\.env(\.(?!example$|sample$|template$|dist$)[\w.-]+)?|id_(rsa|dsa|ecdsa|ed25519)|\.npmrc|\.netrc|\.pypirc"
    r"|credentials(\.json)?|[\w.-]+\.(pem|p12|pfx|jks|keystore))$")
SECRET_TEXT_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@'\"]+:[^\s@'\"]+@)")


def scan_secrets(workspace: Path, base: str, tree: str, key: str, waived: set[str],
                 history_tip: str | None = None) -> list[str]:
    """检查最终文件树，以及会随快照发布的模型提交中的新增内容。"""
    problems = []
    names = lib.git_out(workspace, "diff", "--no-renames", "--name-only", "--diff-filter=AM", base, tree).splitlines()
    patches = [lib.git_out(workspace, "diff", "--no-renames", "--unified=0", "--no-color", base, tree)]
    if history_tip:
        span = f"{base}..{history_tip}"
        names += lib.git_out(workspace, "log", "-m", "--format=", "--no-renames", "--name-only",
                             "--diff-filter=AM", span).splitlines()
        patches.append(lib.git_out(workspace, "log", "-m", "--format=", "--no-renames", "--unified=0",
                                   "--no-color", "-p", span))
        messages = lib.git_out(workspace, "log", "--format=%B", span)
        if key and len(key) >= 6 and key in messages:
            problems.append("网关 key 原文出现在模型提交消息")
        if SECRET_TEXT_RE.search(messages):
            problems.append("token、私钥或连接串出现在模型提交消息")
    for name in names:
        if name in waived:
            continue
        if SECRET_NAME_RE.search(name):
            problems.append(f"密钥类文件进了改动：{name}")
    current = ""
    for line in "\n".join(patches).splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            if key and len(key) >= 6 and key in line:
                problems.append(f"网关 key 原文出现在 {current}")
            elif current not in waived and SECRET_TEXT_RE.search(line):
                problems.append(f"token 或私钥形态的内容出现在 {current}")
    # 文本diff不会展开二进制blob；逐个检查最终树与保留历史新增的blob原始字节。
    raw = lib.git_out(workspace, "diff", "--raw", "--no-abbrev", "--no-renames", "--diff-filter=AM", base, tree)
    if history_tip:
        raw += "\n" + lib.git_out(workspace, "log", "-m", "--format=", "--raw", "--no-abbrev", "--no-renames",
                                  "--diff-filter=AM", f"{base}..{history_tip}")
    seen = set()
    for line in raw.splitlines():
        meta, sep, path = line.partition("\t")
        parts = meta.split()
        if not sep or not meta.startswith(":") or len(parts) != 5 or parts[1] == "160000":
            continue
        oid = parts[3]
        if (oid, path) in seen:
            continue
        seen.add((oid, path))
        blob = subprocess.run(["git", *lib.GIT_SAFE, "cat-file", "blob", oid], cwd=workspace,
                              capture_output=True, timeout=60)
        if blob.returncode != 0:
            raise lib.FbrError(f"无法扫描快照文件：{path}")
        if key and len(key) >= 6 and key.encode("utf-8") in blob.stdout:
            problems.append(f"网关 key 原文出现在 {path}")
        elif path not in waived and SECRET_TEXT_RE.search(blob.stdout.decode("latin1")):
            problems.append(f"token 或私钥形态的内容出现在 {path}")
    return sorted(set(problems))


@lib.task_locked
def commit_one(task_dir: Path, cfg: dict) -> dict:
    record = lib.load_task(task_dir)
    name, workspace, initial = record["task_id"], task_dir / "workspace", record["initial_commit"]
    if lib.stage_ok(task_dir, "commit"):
        return {"task_id": name, "state": "skip", "detail": "已经提交过"}
    lib.require_stage(task_dir, "collect")
    hygiene = lib.restore_git_config(task_dir)
    head = lib.git_out(workspace, "rev-parse", "HEAD")
    branch = lib.git(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    rewritten = lib.git(workspace, "merge-base", "--is-ancestor", initial, head).returncode != 0
    model_commits = 0 if rewritten else int(lib.git_out(workspace, "rev-list", "--count", f"{initial}..{head}"))

    lib.git_out(workspace, "add", "-A")
    excluded = []
    for line in lib.git_out(workspace, "diff", "--cached", "--raw", "--no-abbrev", "HEAD").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].split()[1] == "160000":      # 工作区里嵌了别的 git 仓库
            lib.git_out(workspace, "rm", "--cached", "-f", "-q", "--", parts[1])     # 只退出暂存区，不删文件
            excluded.append(parts[1])
    tree = lib.git_out(workspace, "write-tree")
    waived = {item["path"] for item in lib.load_json(task_dir / "review" / "secret-waivers.json", default=[])
              if isinstance(item, dict) and item.get("path") and item.get("reason")}
    problems = scan_secrets(workspace, initial, tree, lib.read_key(cfg), waived,
                            history_tip=None if rewritten else head)
    import fbr_dependencies
    problems += fbr_dependencies.snapshot_problems(workspace, tree, history_tip=None if rewritten else head, initial=initial)
    for line in lib.git_out(workspace, "diff", "--name-only", "--diff-filter=AM", initial, tree).splitlines():
        path = workspace / line
        if path.is_file() and not path.is_symlink() and path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"单个文件超过 50MB：{line}")
    if problems:
        lib.git(workspace, "reset", "-q")
        lib.set_stage(task_dir, "commit", "failed", problems=problems)
        return {"task_id": name, "state": "blocked", "detail": "；".join(problems)}

    identity = ["-c", f"user.name={cfg.get('git_user_name') or 'solo11'}",
                "-c", f"user.email={cfg.get('git_user_email') or 'solo11@users.noreply.github.com'}",
                "-c", "commit.gpgsign=false"]
    host_commit = False
    if rewritten:
        commit = lib.git_out(workspace, *identity, "commit-tree", tree, "-p", initial, "-m", COMMIT_MESSAGE)
        lib.git(workspace, "reset", "-q")
        host_commit = True
    elif tree != lib.git_out(workspace, "rev-parse", "HEAD^{tree}"):
        lib.git_out(workspace, *identity, "commit", "-q", "--no-verify", "-m", COMMIT_MESSAGE)
        commit = lib.git_out(workspace, "rev-parse", "HEAD")
        host_commit = True
    else:
        commit = head
    if lib.git(workspace, "merge-base", "--is-ancestor", initial, commit).returncode != 0:
        raise lib.FbrError(f"{name}：初始提交不是产物提交的祖先")

    review = task_dir / "review"
    review.mkdir(exist_ok=True)
    (review / "diff.patch").write_text(
        lib.git(workspace, "diff", "--no-color", initial, commit).stdout, encoding="utf-8")
    changed = lib.git_out(workspace, "diff", "--name-only", initial, commit).splitlines()
    snapshot = {
        "task_id": name, "initial_commit": initial, "commit": commit, "branch": branch,
        "model_commits": model_commits, "host_commit": host_commit, "history_rewritten": rewritten,
        "no_change": commit == initial, "changed_files": len(changed), "excluded": excluded,
        "git_hygiene": hygiene, "committed_at": lib.now_iso(), "pushed": False, "artifact_snapshot": "",
    }
    lib.save_json(review / "snapshot.json", snapshot)
    lib.set_stage(task_dir, "commit", "ok", commit=commit)
    detail = f"{commit[:12]}，改动 {len(changed)} 个文件，模型自己提交 {model_commits} 次"
    return {"task_id": name, "state": "ok", "detail": detail + ("，历史被改写过" if rewritten else "")}


# ---------- 推送 ----------

class Hub:
    def __init__(self, owner: str) -> None:
        if owner != "fengyin-go":
            raise lib.FbrError("数据发布仅允许 GitHub 账号 fengyin-go，拒绝其他账号")
        self.owner = owner
        result = lib.run(["gh", "auth", "token", "--hostname", "github.com", "--user", owner], timeout=60)
        self.token = result.stdout.strip()
        if result.returncode != 0 or not self.token:
            raise lib.FbrError(f"gh 里没有 {owner} 的登录态")

    def url(self, full: str) -> str:
        return f"https://github.com/{full}.git"

    def api_json(self, path: str) -> tuple[int, dict]:
        env = dict(os.environ, GH_TOKEN=self.token, GH_PROMPT_DISABLED="1")
        result = lib.run(["gh", "api", "--include", path], env=env, timeout=90)
        head, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
        found = re.match(r"HTTP/\S+\s+(\d{3})", head)
        if not found:
            raise lib.FbrError("gh api 调用失败：" + lib.redact(result.stderr.strip()[:300], [self.token]))
        try:
            return int(found.group(1)), json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            return int(found.group(1)), {}

    def git(self, repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        env = dict(
            os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="http.https://github.com/.extraHeader",
            GIT_CONFIG_VALUE_1=f"Authorization: Basic {basic}",
        )
        return subprocess.run(["git", *lib.GIT_SAFE, *args], cwd=repo, capture_output=True, text=True,
                              timeout=timeout, env=env)

    def remote_main(self, repo: Path, url: str) -> str | None:
        result = self.git(repo, "ls-remote", url, "refs/heads/main", timeout=90)
        if result.returncode != 0:
            raise lib.FbrError("读取远端失败：" + lib.redact(result.stderr.strip()[:300], [self.token]))
        lines = result.stdout.strip().splitlines()
        return lines[0].split()[0] if lines else None

    def is_public(self, full: str) -> bool:
        status, info = self.api_json(f"/repos/{full}")
        return status == 200 and info.get("visibility") == "public" and not info.get("private")

    def login(self) -> str:
        status, info = self.api_json("/user")
        return info.get("login", "") if status == 200 else ""


@lib.task_locked
def push_one(hub, task_dir: Path, cfg: dict) -> dict:
    record = lib.load_task(task_dir)
    name, workspace = record["task_id"], task_dir / "workspace"
    lib.require_stage(task_dir, "commit")
    snapshot = lib.load_json(task_dir / "review" / "snapshot.json")
    was_pushed = snapshot.get("pushed", False)
    full = record.get("task_repo_full") or f"{cfg['github_owner']}/{record['task_repo']}"
    if full.split("/")[0] != cfg["github_owner"]:
        raise lib.FbrError(f"{name}：任务仓库 {full} 不在 {cfg['github_owner']} 名下")
    lib.restore_git_config(task_dir)
    url, commit, initial = hub.url(full), snapshot["commit"], snapshot["initial_commit"]
    if not hub.is_public(full):
        raise lib.FbrError(f"{name}：{full} 可见性不是 public，拒绝推送")
    if lib.git(workspace, "merge-base", "--is-ancestor", initial, commit).returncode != 0:
        raise lib.FbrError(f"{name}：初始提交不是产物提交的祖先")
    remote = hub.remote_main(workspace, url)
    if remote is None:
        raise lib.FbrError(f"{name}：远端没有 main，任务仓库还没发布")
    if remote != commit:
        known = lib.git(workspace, "cat-file", "-e", f"{remote}^{{commit}}").returncode == 0
        if not known or lib.git(workspace, "merge-base", "--is-ancestor", remote, commit).returncode != 0:
            raise lib.FbrError(f"{name}：远端 main 是 {remote[:12]}，不是产物提交的祖先；不强推，先人工确认")
        pushed = hub.git(workspace, "push", url, f"{commit}:refs/heads/main")
        if pushed.returncode != 0:
            raise lib.FbrError(f"{name}：推送失败：" + lib.redact(pushed.stderr.strip()[-300:], [hub.token]))
    if hub.remote_main(workspace, url) != commit:
        raise lib.FbrError(f"{name}：回读远端 main 和产物提交不一致")
    if not hub.is_public(full):
        raise lib.FbrError(f"{name}：{full} 回读可见性不是 public")
    snapshot.update(pushed=True, pushed_at=lib.now_iso(),
                    artifact_snapshot=f"https://github.com/{full}/commit/{commit}")
    lib.save_json(task_dir / "review" / "snapshot.json", snapshot)
    return {"task_id": name, "state": "skip" if was_pushed else "ok", "detail": snapshot["artifact_snapshot"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="solo11 产物快照")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("commit", "push"):
        item = sub.add_parser(name)
        item.add_argument("--batch", required=True)
        item.add_argument("--tasks")
    sub.choices["push"].add_argument("--execute", action="store_true")
    sub.choices["push"].add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        root = lib.find_root(args.root)
        cfg = lib.settings(root)
        items = lib.task_dirs(root, args.batch, args.tasks)
        failed = 0
        if args.command == "commit":
            for task_dir in [p for p in items if lib.stage_ok(p, "collect")]:
                try:
                    row = commit_one(task_dir, cfg)
                except lib.FbrError as exc:
                    row = {"task_id": task_dir.name, "state": "failed", "detail": str(exc)}
                failed += row["state"] not in ("ok", "skip")
                print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
            return 1 if failed else 0
        todo = []
        for task_dir in items:
            snapshot = lib.load_json(task_dir / "review" / "snapshot.json", default={})
            if lib.stage_ok(task_dir, "commit") and snapshot and not snapshot.get("pushed"):
                todo.append((task_dir, snapshot))
        if not todo:
            print("没有待推送的产物快照")
            return 0
        if not args.execute:
            for task_dir, snapshot in todo:
                record = lib.load_task(task_dir)
                print(f"{record['task_id']}  {record.get('task_repo_full', '')}  "
                      f"{snapshot['initial_commit'][:12]} → {snapshot['commit'][:12]}")
            print(f"演练结束：待推送 {len(todo)} 个。真推加 --execute --confirm PUBLISH")
            return 0
        if args.confirm != "PUBLISH":
            raise lib.FbrError("真推需要 --confirm PUBLISH")
        hub = Hub(cfg["github_owner"])
        if hub.login() != cfg["github_owner"]:
            raise lib.FbrError(f"token 对应的账号不是 {cfg['github_owner']}")
        streak = 0
        for task_dir, _ in todo:
            try:
                row = push_one(hub, task_dir, cfg)
                streak = 0
            except (lib.FbrError, subprocess.TimeoutExpired) as exc:
                row = {"task_id": task_dir.name, "state": "failed", "detail": lib.redact(str(exc), [hub.token])}
                failed += 1
                streak += 1
            print(f"{row['task_id']}  {row['state']}  {row['detail']}", flush=True)
            if streak >= 3:
                print("连续失败 3 次，停下来等人看")
                break
            time.sleep(1)
        return 1 if failed else 0
    except lib.FbrError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
