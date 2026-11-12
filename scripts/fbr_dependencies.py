"""Prepare Linux dependencies once; copy isolated ignored trees, never host native packages."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import time
import fbrlib as lib

NAMES = {'node_modules', '.venv', 'venv', 'vendor'}


def roots(repo, cfg):
    configured = cfg.get('dependencies', {}).get('paths') or []
    if configured:
        result = list(configured)
    else:
        files = lib.git_out(repo, 'ls-files').splitlines()
        result = []
        for name in files:
            p = PurePosixPath(name)
            if p.name == 'package.json': result.append(str(p.parent/'node_modules'))
            if p.name in ('requirements.txt', 'pyproject.toml', 'Pipfile'):
                result.append(str(p.parent/'.venv'))
    result = sorted(set(result), key=lambda x: (len(PurePosixPath(x).parts), x))
    clean = []
    for name in result:
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or p.name not in NAMES or '\\' in name:
            raise lib.FbrError('依赖复用只允许明确的 node_modules/.venv/venv/vendor 目录')
        if any(p.is_relative_to(PurePosixPath(old)) for old in clean): continue
        if lib.git(repo, 'check-ignore', '-q', '--no-index', '--', name+'/__s11_ignore_check__').returncode:
            raise lib.FbrError('冻结基线缺少依赖忽略规则，先由出题侧修复基线：'+name)
        if lib.git_out(repo, 'ls-files', '--', name):
            raise lib.FbrError('依赖目录已被 Git 跟踪，拒绝复用：'+name)
        clean.append(name)
    return clean


def wait_lock(path, deadline):
    # lib.file_lock deliberately fails fast; wait here only for another cache builder.
    from contextlib import contextmanager
    @contextmanager
    def held():
        while True:
            lock = lib.file_lock(path)
            try:
                lock.__enter__()
                break
            except lib.FbrError:
                if time.time() >= deadline: raise
                time.sleep(0.5)
        try: yield
        finally: lock.__exit__(None, None, None)
    return held()


def ensure(repo, cfg, setup_cmd=None):
    repo = Path(repo)
    if not cfg.get('dependencies', {}).get('enabled', False): return None
    paths = roots(repo, cfg)
    if not paths: return None
    root = lib.containing_root(repo)
    commit = lib.git_out(repo, 'rev-parse', 'HEAD')
    image = lib.run(['docker', 'image', 'inspect', '--format', '{{.Id}}|{{.Os}}|{{.Architecture}}', cfg['image']], timeout=60)
    parts = image.stdout.strip().split('|')
    if image.returncode or len(parts) != 3 or parts[0] != cfg['image_id'] or parts[1] != 'linux':
        raise lib.FbrError('依赖缓存镜像/平台与真实预检不符')
    setup = setup_cmd if setup_cmd is not None else cfg['facts']['setup_cmd']
    smoke = cfg.get('dependencies', {}).get('smoke_cmd') or cfg['facts']['test_cmd']
    dependency_files = {}
    for name in lib.git_out(repo, 'ls-files').splitlines():
        p = Path(name)
        if p.name in ('package.json','package-lock.json','pnpm-lock.yaml','yarn.lock','requirements.txt','pyproject.toml','poetry.lock','Pipfile','Pipfile.lock','uv.lock') or p.name.startswith('requirements'):
            dependency_files[name] = hashlib.sha256((repo/name).read_bytes()).hexdigest()
    # Full commit also isolates install hooks and local file dependencies; never reuse across changed product code implicitly.
    restore = cfg.get('dependencies', {}).get('setup_after_restore_cmd') or 'true'
    identity = {'version': 2, 'commit': commit, 'image': parts, 'dependency_files': dependency_files,
                'paths': paths, 'setup': setup, 'smoke': smoke, 'restore':restore}
    key = lib.content_hash(identity)
    directory = root/'_registry/dependencies'/key
    directory.parent.mkdir(parents=True, exist_ok=True)
    with wait_lock(directory.with_suffix('.lock'), min(cfg.get('_deadline',float('inf')),time.time()+2*cfg['facts']['setup_timeout']+2*cfg['facts']['test_timeout']+240)):
        saved = lib.load_json(directory/'manifest.json', default={})
        if saved.get('identity') == identity and saved.get('state') == 'unavailable':
            return None
        if saved.get('identity') == identity and saved.get('state') == 'verified' and all((directory/'trees'/p).is_dir() for p in saved.get('paths', [])):
            return directory, saved
        clone = lib.clean_clone(repo, commit, 's11-dependency-seed')
        results = lib.run_steps_in_image(cfg['image'], clone, [
            {'name':'setup','cmd':setup,'timeout':cfg['facts']['setup_timeout']},
            {'name':'dependency-smoke','cmd':smoke,'timeout':cfg['facts']['test_timeout']}], _seed=False)
        present = [p for p in paths if (clone/p).is_dir() and not (clone/p).is_symlink()]
        if len(results) != 2 or any(r.get('rc') != 0 for r in results) or (cfg.get('dependencies', {}).get('paths') and present != paths):
            lib.save_json(directory/'failure.json', {'identity':identity,'results':results,'clone':str(clone)})
            raise lib.FbrError('Linux 依赖基线准备/兼容性验证失败，保留日志，不复制宿主依赖')
        dirty = lib.git_out(clone,'status','--porcelain')
        if dirty:
            # Cache reuse is optional: preserve proof and leave the task's frozen tree untouched.
            lib.save_json(directory/'manifest.json', {'identity':identity,'state':'unavailable','paths':[],
                'reason':'缓存安装改变了克隆源码或锁文件，拒绝复用；任务仍从原冻结源码自行准备依赖',
                'dirty':dirty,'results':results,'clone':str(clone)})
            return None
        if not present:
            lib.save_json(directory/'manifest.json', {'identity':identity,'state':'unavailable','paths':[],
                'reason':'安装没有生成可复制的依赖目录，沿用原 setup；不复制全局 site-packages', 'results':results})
            return None
        # A fresh container sees only copied trees. This catches /tmp or global installs
        # that made the first smoke pass but cannot travel with the dependency cache.
        restored = lib.clean_clone(repo, commit, 's11-dependency-restore-check')
        for name in present:
            (restored/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(clone/name, restored/name, symlinks=True)
            lib.open_permissions(restored/name)
        restored_results = lib.run_steps_in_image(cfg['image'], restored, [
            {'name':'restore-prepare','cmd':restore,'timeout':cfg['facts']['setup_timeout']},
            {'name':'dependency-smoke','cmd':smoke,'timeout':cfg['facts']['test_timeout']}], _seed=False)
        if len(restored_results) != 2 or any(r.get('rc') != 0 for r in restored_results):
            lib.save_json(directory/'manifest.json', {'identity':identity,'state':'unavailable','paths':[],
                'reason':'复制后在新容器中不能无安装运行，沿用原 setup', 'results':results,
                'restored_results':restored_results,'clone':str(restored)})
            return None
        if directory.exists(): shutil.rmtree(directory)
        for name in present:
            (directory/'trees'/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(clone/name,directory/'trees'/name,symlinks=True)
        saved = {'identity':identity,'state':'verified','paths':present,'results':results,
                 'restored_results':restored_results,'created_at':lib.now_iso()}
        lib.save_json(directory/'manifest.json', saved)
        return directory, saved


def seed(repo, cfg, *, task=None, setup_cmd=None):
    prepared = ensure(repo, cfg, setup_cmd)
    if not prepared: return False
    directory, saved = prepared
    repo = Path(repo)
    destinations = []
    for name in saved['paths']:
        dst = repo/name
        if dst.exists() or dst.is_symlink():
            # Never silently replace an existing task's dependencies.
            raise lib.FbrError('依赖目标已有内容，拒绝覆盖：'+name)
        current = dst.parent
        while current != repo:
            if current.is_symlink(): raise lib.FbrError('依赖目标父目录不能是符号链接')
            current = current.parent
        destinations.append((name, dst))
    # Validate every destination before the first copy; a failure must not leave half a seed.
    try:
        for name, dst in destinations:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(directory/'trees'/name,dst,symlinks=True)
            lib.open_permissions(dst)
    except (OSError, lib.FbrError):
        for _, dst in destinations:
            if dst.exists() and not dst.is_symlink(): shutil.rmtree(dst)
        raise
    if task:
        lib.save_json(Path(task)/'run/dependencies.json', {'state':'copied','initial_commit':lib.git_out(repo,'rev-parse','HEAD'),
            'cache':str(directory),'identity':saved['identity'],'paths':saved['paths'],'at':lib.now_iso()})
    return True


def unexpected_ignored(task, status):
    receipt = lib.load_json(task/'run/dependencies.json', default={})
    record = lib.load_task(task)
    paths = receipt.get('paths',[]) if receipt.get('initial_commit') == record['initial_commit'] and receipt.get('state') == 'copied' else []
    # Only a verified Linux cache copied by the runner may explain ignored files.
    cached = lib.load_json(Path(receipt.get('cache','/nonexistent'))/'manifest.json', default={})
    if cached.get('identity') != receipt.get('identity') or cached.get('state') != 'verified': paths=[]
    for line in status.splitlines():
        if not line.startswith('!! '): return True
        name = line[3:].strip('"').rstrip('/')
        if not any(name == p or name.startswith(p+'/') for p in paths): return True
    return False


def snapshot_problems(repo, tree, history_tip=None, initial=None):
    revisions = [tree] + ([initial] if initial else [])
    if history_tip and initial:
        revisions += lib.git_out(repo,'rev-list',f'{initial}..{history_tip}').splitlines()
    for revision in revisions:
        for name in lib.git_out(repo,'ls-tree','-r','--name-only',revision).splitlines():
            if any(p in ('node_modules','.venv','venv','__pycache__') for p in PurePosixPath(name).parts):
                return ['Git 快照/保留历史包含依赖或缓存文件：'+name]
    return []
