"""Task Checkpoint 阶段一业务核心。只使用 Python 标准库。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = "1"
MANIFEST_CHAIN_MAX = 20
MANIFEST_CACHE_MAX = 256
PAYLOAD_LARGE = 100 * 1024 * 1024
STORE_MARKER = ".task-checkpoint-store.json"

DEFAULT_EXCLUDE = {
    ".git", ".checkpoints", STORE_MARKER, ".branches", ".history", "backup*", "*.bak",
    ".cursor", ".idea", ".vscode", ".workbuddy", "node_modules",
    "__pycache__", ".venv", "venv", "target", "dist", "build",
}

# 配套文件规则：{主文件后缀: 配套对象后缀}。默认只覆盖 PLAXIS 的 .p3d/.p3dat。
# 可通过任务 state 的 companion_rules 覆盖；配套缺失只上报，不中断保存。
DEFAULT_COMPANION_RULES = {".p3d": ".p3dat"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excluded(name: str, extra: set[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_EXCLUDE | extra)


def _clean_exclude(patterns) -> list[str]:
    """校验调用方传入的 exclude 模式。

    匹配方式和 DEFAULT_EXCLUDE 完全一致：按**单个名字段**做 fnmatch ——
    目录名（任意层级），或路径的第一段。所以带路径分隔符的模式永远匹配不上，
    那种情况直接报错，而不是让人以为加了却没生效。
    """
    if patterns is None:
        return []
    if not isinstance(patterns, (list, tuple)):
        raise ValueError("exclude must be a list of glob patterns")
    cleaned: list[str] = []
    for item in patterns:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("exclude entries must be non-empty strings")
        value = item.strip()
        if "/" in value or "\\" in value:
            raise ValueError(f"exclude pattern must be a single path segment: {value!r}")
        if value in {".", ".."} or value.startswith("~"):
            raise ValueError(f"unsafe exclude pattern: {value!r}")
        cleaned.append(value)
    return list(dict.fromkeys(cleaned))


# 目录名黑名单：命中则整个目录内容都算敏感（与后缀无关）。
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube",
                  ".env", ".envdir", ".secrets", ".tokens"}
# 目录名再按这几个子串判定：secrets/ 、credentials/ 这类。
# 目录名比文件名更“刻意”，所以这里允许粗一些。
SENSITIVE_DIR_PATTERNS = ("*secret*", "*credential*", "credentials*", "*.env", ".env*", "*token*")

# 无歧义的凭据文件名：形状具体，正常源码文件名不会撞上。
SENSITIVE_NAMES = (
    # dotenv 家族
    ".env", ".env.*", "*.env", "*.env.*", ".envrc",
    # 私钥与密钥库（*.pub 是公钥，不在此列）
    "*.pem", "*.key", "*.p12", "*.pfx", "*.p8", "*.jks", "*.keystore", "*.kdbx", "*.ppk", "*.ovpn",
    "*_rsa", "*_dsa", "*_ed25519", "*_ecdsa", "*_key",
    "id_rsa", "id_dsa", "id_ed25519", "id_ecdsa",
    # 凭据文件
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass", ".htpasswd",
    ".my.cnf", "my.cnf", "passwd", "shadow",
    "kubeconfig", ".kubeconfig", "*.kubeconfig",
    "terraform.tfstate", "*.tfstate", "terraform.tfvars", "*.tfvars",
    "settings.py", "local_settings.py", "settings.xml", "wp-config.php", "web.config",
    "wrangler.toml", ".dev.vars", "*.jwt",
    "service-account*", "service_account*", "*.serviceaccount.json", "firebase*", "appsettings*.json",
    "credentials", "credentials.*", "creds", "creds.*", "secret", "secret.*", "secrets.json",
    "token", "token.json", "token.txt", "*.token", "*_token", "auth.json", "*auth.json",
)

# 高歧义子串：正常源码文件名（secrets.py / password_policy.py / credentials_manager.py）
# 也会命中，所以只对「配置类后缀」生效，放行 .py/.js/.ts/.go/.rs/.java/.md 等源码。
SENSITIVE_SUBSTRINGS = ("*secret*", "*password*", "*passwd*", "*credential*", "*creds*",
                        "*api_key*", "*apikey*", "*token*", "*auth_token*")
SENSITIVE_CONFIG_SUFFIXES = (
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config", ".properties",
    ".xml", ".txt", ".env", ".envrc", ".cnf", ".sh", ".ps1", ".bat", ".cmd", ".sql", ".php",
)


def _sensitive(rel: str) -> bool:
    """判断一个工作区相对路径是否算敏感（不看内容，只看路径）。

    这是**黑名单**，盖不全：名字无辜的凭据（`config/database.yaml`、
    `docker-compose.yml`、`config.json`）按名字根本抓不到，只能用 exclude。
    两类模式：无歧义的名字直接拦；高歧义子串只在配置类后缀上拦，
    以免把 `secrets.py` / `password_policy.py` 这种源码挡在存档外
    （被挡的文件回退时会静默跳过）。
    """
    parts = [part.casefold() for part in Path(rel.replace("\\", "/")).as_posix().split("/")
             if part not in ("", ".")]
    if not parts:
        return False
    for part in parts[:-1]:
        if part in SENSITIVE_DIRS or any(fnmatch.fnmatch(part, pattern) for pattern in SENSITIVE_DIR_PATTERNS):
            return True
    name = parts[-1]
    if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAMES):
        return True
    # 高歧义子串只在两种情况下生效：文件名以配置类后缀结尾，或者**根本没有扩展名**
    # （`mytoken`、`creds`、`kubeconfig` 这类几乎不会是源码）。
    # `tokenizer.py` / `password_policy.py` / `secrets.md` 因此都被放行。
    if "." not in name or name.endswith(SENSITIVE_CONFIG_SUFFIXES):
        return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_SUBSTRINGS)
    return False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_reuse_enabled() -> bool:
    """依据 mtime+size 复用旧 sha 的性能选项，默认关。

    开着时，只要 mtime+size 没变就不读文件内容，于是「mtime 被还原 / 文件系统
    mtime 粒度粗 / 有工具保留 mtime（cp -p、rsync -t、tar -x）的情况下改了内容」
    会被完全漏掉：不读盘、不存新 payload、把旧的 sha 记进步骤，restore 会静默
    给出旧内容。默认按内容算，需要动这个开关请自己确认工作区满足条件。
    """
    return os.environ.get("TC_SHA_REUSE", "off").strip().lower() in {"on", "1", "true", "yes"}


def _log(store: Path, message: str) -> None:
    path = store / "server.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"[{_now()}] {message}\n")


class _Logger:
    def __init__(self, store: Path):
        self.store = store

    def info(self, message: str) -> None:
        _log(self.store, message)

    def error(self, message: str) -> None:
        _log(self.store, message)

    def flush(self) -> None:
        return None


class _NullLogger:
    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def flush(self) -> None:
        pass


def init_logger(store) -> None:
    global _LOGGER
    _LOGGER = _Logger(Path(store))
    _log(Path(store), "logger ready")


def get_logger():
    return _LOGGER


_LOGGER = _NullLogger()
_WATCH: dict[str, dict] = {}
_WATCH_LOCK = threading.Lock()
_MANIFEST_CACHE: OrderedDict[tuple[str, str], dict] = OrderedDict()
_MANIFEST_CACHE_LOCK = threading.RLock()


@contextmanager
def _lock(store: Path):
    path = store / "state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + 30
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"lock timeout: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _atomic(path: Path, binary: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8", "newline": "\n"}
    try:
        with open(tmp, mode, **kwargs) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows 上 os.replace 不能覆盖只读文件（POSIX 靠目录权限，不受影响）。
            # 去掉只读位再试一次，否则工作区里一个只读文件就能让 restore 失败。
            os.chmod(path, stat.S_IWRITE)
            os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value) -> None:
    with _atomic(path) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


class TaskCheckpoint:
    def __init__(self, root, store: Optional[str] = None):
        self.root = Path(root).resolve()
        # 记住显式传入的 store：init/import_handoff 会重新解析 root，
        # 不能把调用方明确指定的落点丢掉。
        self._store_override = store
        with _WATCH_LOCK:
            self._watch = _WATCH.setdefault(str(self.root), {"dirty_since": None, "content_reads": 0})
        marker = self.root / STORE_MARKER
        persisted = _read_json(marker, {}) if marker.exists() else {}
        configured = store or os.environ.get("TC_STORE")
        selected = configured or self._watch.get("store") or persisted.get("store")
        self.store = Path(selected).resolve() if selected else self.root / ".checkpoints"
        if self.store == self.root:
            raise ValueError("store must not equal workspace root")
        self.logger = None
        # 扫描过程中记录的例外（跳过的软链接、读失败等）。在 __init__ 里就建好，
        # 不能靠 _scan 隐式创建，否则单独调 _store_payloads 之类的路径会 AttributeError。
        self._health: list[str] = []

    def init(self, root: str, name: str, store: Optional[str] = None, goal: str = "",
             constraints: str = "", exclude=None) -> dict:
        patterns = _clean_exclude(exclude)
        root_p = Path(root).resolve()
        self.root = root_p
        marker = root_p / STORE_MARKER
        persisted = _read_json(marker, {}) if marker.exists() else {}
        configured = store or os.environ.get("TC_STORE") or persisted.get("store")
        self.store = Path(configured).resolve() if configured else root_p / ".checkpoints"
        if self.store == root_p:
            raise ValueError("store must not equal workspace root")
        self.store.mkdir(parents=True, exist_ok=True)
        with _WATCH_LOCK:
            self._watch.clear()
            self._watch.update({"dirty_since": None, "content_reads": 0, "store": str(self.store), "root": str(self.root)})
        marker = root_p / STORE_MARKER
        # 显式 store 和 TC_STORE 都要留下工作区指针，否则换个进程（环境变量不在）
        # 就会回落到 ./.checkpoints，存档看似消失。
        if store or os.environ.get("TC_STORE"):
            _write_json(marker, {"store": str(self.store)})
        task_id = f"t{time.time_ns()}"
        with _lock(self.store):
            previous = self._active_id()
            suspended = None
            if previous:
                old = self._state(previous)
                if old["status"] == "open":
                    old["status"] = "suspended"
                    self._write_state(old)
                    suspended = previous
            state = {
                "schema_version": SCHEMA_VERSION, "task_id": task_id, "name": name,
                "goal": goal, "constraints": constraints, "created_at": _now(),
                "updated_at": _now(), "status": "open", "head": 0, "step_count": 0,
                "drift_count": 0, "store": str(self.store), "groups": {},
                "exclude": list(patterns), "companion_rules": dict(DEFAULT_COMPANION_RULES),
            }
            self._write_state(state)
            self._write_active(task_id)
            # 新任务的基线索引必须从空开始，不能继承别的任务留下的快照。
            self._write_index(task_id, {})
        _log(self.store, f"init {task_id}")
        inside = self.store == root_p / ".checkpoints"
        return {
            "task_id": task_id, "name": name, "store": str(self.store),
            "store_is_external": not inside, "prev_suspended": suspended,
            "exclude": list(patterns),
            "note": "store 在工作区内，工作区被删会一起丢；要放到外面请传 store" if inside else "store 在工作区外",
        }

    def switch(self, root: str, task_id: str) -> dict:
        with _lock(self.store):
            return self._switch_locked(task_id)

    def _switch_locked(self, task_id: str) -> dict:
        target = self._state(task_id)
        if target["status"] == "closed":
            raise PermissionError(f"task {task_id} is closed and cannot be reopened")
        active = self._active_id()
        if active and active != task_id:
            old = self._state(active)
            if old["status"] == "open":
                old["status"] = "suspended"
                self._write_state(old)
        target["status"] = "open"
        self._write_state(target)
        self._write_active(task_id)
        return {"task_id": task_id, "status": "open"}

    def _git_after_step(self, state: dict) -> dict:
        """一步落档后，顺手给这一步打个 git 指针。

        git 是增强而不是基础路径，所以这里**任何失败都不能影响存档本身**：
        仓库不存在、git 没装、ref 重名、命令报错，一律降级成一个描述性的返回值。
        没有这个调用方的话，`git_ref` 在 MCP 界面上根本不可达，等于没做。
        """
        if os.environ.get("TC_GIT", "on").strip().lower() in {"off", "0", "false", "no"}:
            return {"ok": True, "ref": None, "reason": "TC_GIT=off"}
        try:
            result = self.git_ref(str(self.root), _state=state)
        except Exception as e:
            return {"ok": False, "ref": None, "error": f"{type(e).__name__}: {e}"}
        if not result.get("git"):
            return {"ok": True, "ref": None, "reason": "not a git repo"}
        return {"ok": True, "ref": result.get("ref"),
                "reason": result.get("reason"), "unchanged": result.get("unchanged")}

    def save(self, root: str, title: str, task_id: Optional[str] = None, description: str = "",
             conclusion: str = "", verified=None, open_questions=None, next_step: str = "",
             close: bool = False, paths=None) -> dict:
        if paths:
            raise ValueError("paths is reserved and must be empty until scoped capture is implemented")
        with _lock(self.store):
            current_id = self._active_id()
            switched = bool(task_id and task_id != current_id and not close)
            if switched:
                self._switch_locked(task_id)
                state = self._active()
            else:
                state = self._active(task_id if close else None)
            if state["status"] == "closed":
                raise PermissionError(f"task {state['task_id']} is closed")
            if close:
                state["status"] = "closed"
                self._write_state(state)
                return {"task_id": state["task_id"], "index": state["head"], "title": title,
                        "groups": [], "skipped_paths": self._skipped_paths(),
                        "idempotent": False, "closed": True, "status": "closed",
                        "active_task_changed": switched,
                        "git": {"ok": True, "ref": None, "reason": "no new step"}}
            previous_index = self._read_index(state["task_id"])
            files = self._scan(state, previous=previous_index)
            groups = self._groups(files, state.get("companion_rules"))
            digest = self._digest(files)
            pending_drift = bool(previous_index and self._changed(previous_index, files))
            same = self._same_step(
                state["task_id"], title, digest,
                description=description, conclusion=conclusion,
                verified=verified, open_questions=open_questions,
                next_step=next_step,
            )
            filled = {"skipped_paths": self._skipped_paths(), "idempotent": False, "closed": False,
                      "status": state["status"], "active_task_changed": switched}
            if same is not None:
                return {"task_id": state["task_id"], "index": same, "title": title, "groups": groups,
                        **{**filled, "idempotent": True},
                        "git": {"ok": True, "ref": None, "reason": "no new step"}}
            self._store_payloads(files)
            filled["skipped_paths"] = self._skipped_paths()
            ref = self._write_manifest(state, self._public_map(files))
            index = state["step_count"] + 1
            step = {
                "index": index, "id": f"s{index:04d}", "title": title, "timestamp": _now(),
                "description": description, "kind": "save", "conclusion": conclusion,
                "verified": verified or [], "open_questions": open_questions or [],
                "next": next_step, "file_digest": digest, "manifest_ref": ref, "groups": groups,
            }
            _write_json(self._task(state["task_id"]) / "steps" / f"s{index:04d}.json", step)
            state.update(head=index, step_count=index, updated_at=_now())
            self._write_state(state)
            if pending_drift:
                self._record_drift(state, "drift", self._public_map(files), previous=previous_index, manifest_ref=ref)
            self._write_index(state["task_id"], files)
            return {"task_id": state["task_id"], "index": index, "title": title, "groups": groups,
                    **filled, "git": self._git_after_step(state)}

    def show(self, root: str, task_id: Optional[str] = None, index: Optional[int] = None,
             include_drift: bool = False, limit: int = 50, cursor: Optional[int] = None) -> dict:
        with _lock(self.store):
            state = self._active(task_id, allow_closed=True)
            task_dir = self._task(state["task_id"])
            selected = []
            if index is not None:
                if index < 1:
                    raise ValueError("index must be at least 1")
                selected = [task_dir / "steps" / f"s{index:04d}.json"]
            else:
                selected = sorted((task_dir / "steps").glob("*.json"))
            drift_paths = sorted((task_dir / "drifts").glob("*.json")) if include_drift else []
            head = state["head"]
        steps = []
        for path in selected:
            if not path.exists():
                raise FileNotFoundError(path.name)
            item = json.loads(path.read_text(encoding="utf-8"))
            if index is None and cursor is not None and item["index"] <= cursor:
                continue
            steps.append(item)
        if index is None:
            # 只有后面确实还有步时才给游标，否则客户端会多取一页空的。
            more = len(steps) > limit
            steps = steps[:limit]
        else:
            more = False
        drifts = [json.loads(path.read_text(encoding="utf-8")) for path in drift_paths if path.exists()]
        return {"steps": steps, "drifts": drifts, "head": head,
                "next_cursor": steps[-1]["index"] if (index is None and more) else None}

    def _safe_workspace_path(self, rel: str) -> Path:
        candidate = Path(rel)
        if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
            raise PermissionError(f"unsafe workspace path: {rel}")
        target = self.root.joinpath(*candidate.parts)
        current = self.root
        for part in candidate.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise PermissionError(f"workspace path traverses symlink: {rel}")
        if target.is_symlink():
            raise PermissionError(f"workspace path is a symlink: {rel}")
        resolved = target.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise PermissionError(f"workspace path escapes root: {rel}")
        return target

    def _checkpoint_map(self, state: dict) -> dict:
        if not state.get("head"):
            return {}
        step = self._task(state["task_id"]) / "steps" / f"s{state['head']:04d}.json"
        if not step.exists():
            return {}
        meta = json.loads(step.read_text(encoding="utf-8"))
        return self._load_manifest(state["task_id"], meta.get("manifest_ref", ""))

    def _copy_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _atomic(destination, binary=True) as handle, open(source, "rb") as source_handle:
            shutil.copyfileobj(source_handle, handle, length=1024 * 1024)

    def restore(self, root: str, index: Optional[int] = None, drift_id: Optional[str] = None, apply: bool = False) -> dict:
        if (index is None) == (drift_id is None):
            raise ValueError("index and drift_id are mutually exclusive")
        with _lock(self.store):
            state = self._active(allow_closed=True)
            if apply and state["status"] == "closed":
                raise PermissionError(f"task {state['task_id']} is closed and cannot be restored")
            target = self._target_map(state["task_id"], index, drift_id)
            previous_index = self._read_index(state["task_id"])
            current = self._scan(state, previous=previous_index)
            current_map = self._public_map(current)
            delete_candidates = sorted(set(current_map) - set(target))
            # 敏感文件从不写入 payload；恢复旧步骤时也不能删除它们。
            protected_deletes = [rel for rel in delete_candidates if current_map[rel].get("sensitive")]
            delete = [rel for rel in delete_candidates if rel not in protected_deletes]
            write = sorted(set(target) - set(current_map))
            replace = sorted(p for p in set(target) & set(current_map) if self._different(current_map[p], target[p]))
            # 不能真的还原的项（敏感文件、>100MB 未存内容、payload 丢失）只跳过并上报。
            # 以前是任一不可回退就整次拒绝，而真实工作区几乎必有 .env，
            # 一个 .env 动过就让整个回退失效。
            unavailable = set(protected_deletes)
            unavailable.update(rel for rel in write + replace if not self._payload_available(target[rel]))
            unavailable = sorted(unavailable)
            skipped_targets = set(unavailable)
            write = [rel for rel in write if rel not in skipped_targets]
            replace = [rel for rel in replace if rel not in skipped_targets]
            plan = {"to_delete": delete, "to_write": write, "to_replace": replace, "unrestorable": unavailable}
            if not apply:
                return {"applied": False, "recovered": None, "skipped_paths": self._skipped_paths(), "plan": plan}

            affected = sorted(set(delete + replace))
            journal = self.store / f".restore-{time.time_ns()}"
            backup_dir = journal / "backup"
            staged_dir = journal / "staged"
            original_state = json.dumps(state, ensure_ascii=False, indent=2)
            original_index = self._index_path(state["task_id"]).read_bytes() if self._index_path(state["task_id"]).exists() else None
            old_manifests = set(self._manifest_refs(state["task_id"]))
            old_drifts = set((self._task(state["task_id"]) / "drifts").glob("*.json"))
            originals: dict[str, bool] = {}
            recovered = None
            # 备份与暂存只动 journal，工作区一个字节都没改。
            mutated = False
            try:
                for rel in affected:
                    destination = self._safe_workspace_path(rel)
                    if destination.exists():
                        if not destination.is_file():
                            raise PermissionError(f"restore target is not a regular file: {rel}")
                        backup = backup_dir / rel
                        self._copy_file(destination, backup)
                        originals[rel] = True
                    else:
                        originals[rel] = False
                for rel in write + replace:
                    destination = self._safe_workspace_path(rel)
                    info = target[rel]
                    staged = staged_dir / rel
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    data = self._read_payload(info["sha256"])
                    with _atomic(staged, binary=True) as handle:
                        handle.write(data)
                # 到这里为止工作区仍然是原样。从下一条语句开始才真的改它，
                # 所以“需要回滚”只可能发生在这之后。
                mutated = True
                for rel in delete:
                    self._remove(self._safe_workspace_path(rel))
                for rel in write + replace:
                    destination = self._safe_workspace_path(rel)
                    self._copy_file(staged_dir / rel, destination)
                    # 还原权限位（POSIX 上的可执行位）。mtime 故意不还原：
                    # 把旧时间戳写回去会让 make 类工具误以为文件没变。
                    mode = target[rel].get("mode")
                    if mode is not None:
                        try:
                            os.chmod(destination, mode)
                        except OSError:
                            pass
                recovered = self._record_drift(state, "recovered", current_map, previous=current_map)
                if index is not None:
                    state["head"] = index
                    self._write_state(state)
                scanned = self._scan(state, previous=previous_index)
                self._write_index(state["task_id"], scanned)
            except Exception:
                # 只有在真的改过工作区之后才需要回滚。备份阶段就失败时工作区是完好的，
                # 而 originals 只覆盖已备份成功的项——“先删光再回填”会把未备份的文件删掉
                # 且永不回填，那就是静默丢数据。
                if mutated:
                    for rel in sorted(set(delete + write + replace), reverse=True):
                        destination = self._safe_workspace_path(rel)
                        if destination.exists() or os.path.lexists(str(destination)):
                            self._remove(destination)
                    for rel, existed in originals.items():
                        if existed:
                            self._copy_file(backup_dir / rel, self._safe_workspace_path(rel))
                _write_json(self._state_path(state["task_id"]), json.loads(original_state))
                if original_index is None:
                    index_path = self._index_path(state["task_id"])
                    if index_path.exists():
                        index_path.unlink()
                else:
                    with _atomic(self._index_path(state["task_id"]), binary=True) as handle:
                        handle.write(original_index)
                for path in set(self._manifest_refs(state["task_id"])) - old_manifests:
                    if path.exists():
                        path.unlink()
                for path in set((self._task(state["task_id"]) / "drifts").glob("*.json")) - old_drifts:
                    if path.exists():
                        path.unlink()
                raise
            finally:
                if journal.exists():
                    shutil.rmtree(journal, ignore_errors=True)
        return {"applied": True, "recovered": recovered, "skipped_paths": self._skipped_paths(), "plan": plan}

    def resume(self, root: str, task_id: Optional[str] = None) -> dict:
        with _lock(self.store):
            state = self._active(task_id, allow_closed=True)
            previous = self._read_index(state["task_id"])
        return self._resume_snapshot(state, previous)

    def _resume_locked(self, task_id: Optional[str] = None) -> dict:
        state = self._active(task_id, allow_closed=True)
        previous = self._read_index(state["task_id"])
        return self._resume_snapshot(state, previous)

    def _resume_snapshot(self, state: dict, previous: dict) -> dict:
        scanned = self._scan(state, content=False)
        drift = sorted(
            (set(scanned) - set(previous))
            | (set(previous) - set(scanned))
            | {p for p in set(scanned) & set(previous)
               if scanned[p]["mtime"] != previous[p]["mtime"] or scanned[p]["size"] != previous[p]["size"]}
        )
        last = {}
        skipped = []
        step = self._task(state["task_id"]) / "steps" / f"s{state['head']:04d}.json"
        if step.exists():
            last = json.loads(step.read_text(encoding="utf-8"))
            manifest = self._load_manifest(state["task_id"], last.get("manifest_ref", ""))
            skipped = sorted(p for p, info in manifest.items() if info.get("sensitive") or info.get("skipped"))
        errors = [p.name for p in self._task(state["task_id"]).rglob("*.tmp")]
        errors.extend(dict.fromkeys(getattr(self, "_health", [])))
        # 这两个兜底字面量必须先取出来：f-string 的表达式部分在 Python 3.10 /
        # 3.11 里不允许出现反斜杠（含 \uXXXX 转义），写进去会让整个模块 import
        # 失败 —— 而 pyproject 承诺 requires-python >= 3.10。
        next_text = last.get("next") or "\u672a\u8bb0\u5f55"
        drift_text = ", ".join(drift) or "\u65e0"
        handoff = (
            f"\u4efb\u52a1\uff1a{state['name']}\n\u76ee\u6807\uff1a{state['goal']}\n\u5f53\u524d\u6b65\u9aa4\uff1a{state['head']}\n"
            f"\u4e0b\u4e00\u6b65\uff1a{next_text}\n\u672a\u843d\u6863\uff1a{drift_text}"
        )
        return {
            "goal": state["goal"], "constraints": state["constraints"], "head": state["head"],
            "last": {"title": last.get("title"), "conclusion": last.get("conclusion"), "description": last.get("description"), "next": last.get("next", "")},
            "open_questions": last.get("open_questions", []), "drift_paths": drift, "next": last.get("next", ""),
            "handoff": handoff, "health": {"ok": not errors, "errors": errors}, "store": state["store"],
            "active_task": state["task_id"], "skipped": skipped,
        }

    def _skipped_paths(self) -> list[str]:
        return sorted(dict.fromkeys(getattr(self, "_health", [])))

    def _store_rel_prefix(self) -> str:
        """存档在工作区内时，返回其相对前缀（带尾斜杠）；在工作区外则为空串。"""
        try:
            rel = self.store.resolve().relative_to(self.root)
        except ValueError:
            return ""
        return rel.as_posix() + "/"

    def _capture_result(self, captured: bool = False, drift_id: Optional[str] = None, partial: bool = False, reads: int = 0, waiting: bool = False) -> dict:
        result = {"captured": captured, "drift_id": drift_id, "partial": partial, "reads": reads, "waiting": waiting, "skipped_paths": self._skipped_paths()}
        return result

    def _capture_locked(self, state: dict, now: Optional[float] = None, partial: bool = False, force: bool = False) -> dict:
        previous = self._read_index(state["task_id"])
        scanned = self._scan(state, content=False)
        changed = self._changed(previous, scanned)
        if not changed:
            self._watch["dirty_since"] = None
            return self._capture_result()
        moment = time.time() if now is None else now
        since = self._watch.get("dirty_since")
        if since is None:
            self._watch["dirty_since"] = moment
            if not force:
                return self._capture_result(waiting=True)
            since = moment
        forced = moment - since >= float(os.environ.get("TC_DRIFT_MAX_WAIT", "60"))
        if not force and not forced:
            return self._capture_result(waiting=True)
        full = self._scan(state, content=True, previous=previous)
        self._store_payloads(full)
        mapping = self._public_map(full)
        drift_id = self._record_drift(state, "drift", mapping, partial=partial, previous=previous)
        self._write_index(state["task_id"], full)
        self._watch["dirty_since"] = None
        return self._capture_result(True, drift_id, partial, len(changed))

    def capture(self, root: str, now: Optional[float] = None, partial: bool = False, force: bool = False) -> dict:
        """记录当前相对最近索引的变化。force 立即落档，partial 只决定标记。"""
        with _lock(self.store):
            state = self._active()
            return self._capture_locked(state, now=now, partial=partial, force=force)

    def tick(self, now: Optional[float] = None, content_changed: Optional[bool] = None) -> dict:
        """稳定后立即落完整快照。持续变化超过上限才落 partial。"""
        moment = time.time() if now is None else now
        if content_changed is None:
            try:
                state = self._active(allow_closed=True)
                current = {rel: {"mtime": info["mtime"], "size": info["size"]} for rel, info in self._scan(state, content=False).items()}
            except Exception:
                current = {}
            previous = self._watch.get("last_stat")
            self._watch["last_stat"] = current
            content_changed = previous is None or current != previous
        since = self._watch.get("dirty_since")
        if content_changed and since is None:
            self._watch["dirty_since"] = moment
            return self._capture_result(waiting=True)
        if content_changed and moment - since >= float(os.environ.get("TC_DRIFT_MAX_WAIT", "60")):
            return self.capture(str(self.root), now=moment, force=True, partial=True)
        if not content_changed:
            return self.capture(str(self.root), now=moment, force=True, partial=False)
        return self._capture_result(waiting=True)

    def compress(self, keep_seconds: int = 7 * 24 * 3600, minimum: int = 20, maximum: int = 50) -> dict:
        """回收变更层 payload。

        只删「超过保留期、不再是保留窗口内的一份、且没被步骤基线或 recovered
        层引用」的 payload；被回收的变更层会被标成 restorable=false，之后
        restore 它会被明确拒绝，而不是静默恢复出错的内容。步骤基线不会被碰。
        """
        with _lock(self.store):
            return self._compress(keep_seconds, minimum, maximum)

    def _compress(self, keep_seconds: int, minimum: int, maximum: int) -> dict:
        state = self._active(allow_closed=True)
        protected = self._protected_hashes(state["task_id"])
        drifts = self._drift_records(state["task_id"])
        by_path: dict[str, list[dict]] = {}
        for record in drifts:
            if record["kind"] == "recovered":
                continue
            for rel, info in record["entries"].items():
                if info.get("sha256"):
                    by_path.setdefault(rel, []).append({"drift": record, "sha": info["sha256"]})
        converted = 0
        eligible = 0
        already_gone = 0
        bytes_freed = 0
        cutoff = time.time() - keep_seconds
        for rel, copies in by_path.items():
            recent = []
            stale = []
            for copy in copies:
                try:
                    timestamp = datetime.fromisoformat(copy["drift"]["meta"]["timestamp"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    timestamp = time.time()
                if timestamp >= cutoff:
                    recent.append(copy)
                else:
                    stale.append(copy)
            if len(stale) > minimum:
                # minimum=0 时 stale[:-0] 是空列表，会让时间路径静默失效。
                stale = stale[:-minimum] if minimum > 0 else list(stale)
            else:
                stale = []
            if len(copies) > maximum:
                stale.extend(copy for copy in copies[:-maximum] if copy not in stale and copy not in recent)
            counts: dict[str, int] = {}
            for copy in copies:
                counts[copy["sha"]] = counts.get(copy["sha"], 0) + 1
            for copy in stale:
                sha = copy["sha"]
                if sha in protected or counts[sha] != 1:
                    continue
                eligible += 1
                path = self.store / "payload" / sha[:2] / sha
                if not path.exists():
                    copy["drift"]["meta"]["restorable"] = False
                    already_gone += 1
                    continue
                newer = copies[-1]["sha"]
                if newer == sha:
                    continue
                for record in drifts:
                    if any(info.get("sha256") == sha for info in record["entries"].values()):
                        record["meta"]["restorable"] = False
                if sha not in protected and path.exists():
                    bytes_freed += path.stat().st_size
                    path.unlink()
                converted += 1
        for record in drifts:
            if record["meta"].get("restorable") is False:
                _write_json(record["path"], record["meta"])
        # converted=0 最常见的原因是保留期还没到（默认 7 天），把参数和候选数一并返回，
        # 使用者才看得出“为什么什么都没回收”，而不用去猜。
        return {"converted": converted, "eligible": eligible, "already_gone": already_gone,
                "bytes_freed": bytes_freed, "keep_seconds": keep_seconds,
                "minimum": minimum, "maximum": maximum}

    def export(self, root: str, to: str) -> dict:
        destination = Path(to)
        if not destination.exists() or not destination.is_dir():
            raise FileNotFoundError("export destination must be an existing directory")
        with _lock(self.store):
            state = self._active(allow_closed=True)
            resume_data = self._resume_locked()
            filtered = []
            files = {}
            step = self._task(state["task_id"]) / "steps" / f"s{state['head']:04d}.json"
            entries = {}
            if step.exists():
                meta = json.loads(step.read_text(encoding="utf-8"))
                entries = self._load_manifest(state["task_id"], meta.get("manifest_ref", ""))
            for rel, info in entries.items():
                if info.get("sensitive") or _sensitive(rel):
                    filtered.append(rel)
                    continue
                if info.get("sha256"):
                    # 和 restore 用同一套判断：payload 文件真的在，才导出。
                    # 否则一个丢了的 payload 会让整个导出失败，而不是只跳过它。
                    if self._payload_available(info):
                        files[rel] = self._read_payload(info["sha256"])
                    else:
                        filtered.append(rel)
            package = destination / f"{state['task_id']}-handoff"
            package.mkdir()
            for rel, data in files.items():
                target = package / "files" / rel
                with _atomic(target, binary=True) as handle:
                    handle.write(data)
            portable = {
                "schema_version": SCHEMA_VERSION,
                "task": {key: state[key] for key in ("task_id", "name", "goal", "constraints", "head", "status")},
                "resume": resume_data,
                "filtered": filtered,
            }
            _write_json(package / "handoff.json", portable)
            prompt = (
                f"任务：{state['name']}\n目标：{state['goal']}\n当前步骤：{state['head']}\n"
                f"下一步：{resume_data.get('next') or '未记录'}\n不要读取这些被过滤的敏感文件：{', '.join(filtered) or '无'}"
            )
            with _atomic(package / "HANDOFF.md") as handle:
                handle.write(prompt)
            return {"path": str(package), "filtered": filtered, "files": sorted(files)}

    def import_handoff(self, package: str, root: str) -> dict:
        self.root = Path(root).resolve()
        # 以前这里硬写 root/.checkpoints，调用方配的 store 会被默默丢掉，
        # 导入落点不可控。改成按同样的优先级解析：显式 store > TC_STORE > 工作区指针 > 默认。
        marker = self.root / STORE_MARKER
        persisted = _read_json(marker, {}) if marker.exists() else {}
        configured = self._store_override or os.environ.get("TC_STORE") or persisted.get("store")
        self.store = Path(configured).resolve() if configured else self.root / ".checkpoints"
        if self.store == self.root:
            raise ValueError("store must not equal workspace root")
        self.store.mkdir(parents=True, exist_ok=True)
        if self._store_override or os.environ.get("TC_STORE"):
            _write_json(self.root / STORE_MARKER, {"store": str(self.store)})
        with _lock(self.store):
            return self._import_handoff(package)

    def _import_handoff(self, package: str) -> dict:
        source = Path(package).resolve()
        handoff_path = source / "handoff.json"
        files_root = source / "files"
        if not handoff_path.is_file() or not files_root.is_dir():
            raise ValueError("invalid handoff package")
        data = json.loads(handoff_path.read_text(encoding="utf-8"))
        task = data.get("task")
        if not isinstance(task, dict):
            raise ValueError("handoff task must be an object")
        task_id = task.get("task_id")
        task_dir = self._task(task_id)
        if task_dir.exists():
            raise FileExistsError(f"task already exists: {task_id}")
        resume = data.get("resume") or {}
        if not isinstance(resume, dict):
            raise ValueError("handoff resume must be an object")
        planned = []
        for path in files_root.rglob("*"):
            if path.is_symlink():
                raise PermissionError(f"handoff contains symlink: {path.relative_to(files_root).as_posix()}")
            if not path.is_file():
                continue
            rel = path.relative_to(files_root).as_posix()
            target = self._safe_workspace_path(rel)
            planned.append((path, rel, target))
        task = {
            "task_id": task_id,
            "name": task.get("name", "imported handoff"),
            "goal": task.get("goal", ""),
            "constraints": task.get("constraints", ""),
        }
        state = {
            "schema_version": SCHEMA_VERSION, "task_id": task_id, "name": task["name"],
            "goal": task["goal"], "constraints": task["constraints"], "created_at": _now(),
            "updated_at": _now(), "status": "open", "head": 1, "step_count": 1,
            "drift_count": 0, "store": str(self.store), "groups": {}, "exclude": [],
        }
        import_journal = self.store / f".import-{time.time_ns()}"
        backup_dir = import_journal / "backup"
        staged_dir = import_journal / "staged"
        created_targets: list[Path] = []
        replaced_targets: list[tuple[Path, Path]] = []
        created_payloads: list[Path] = []
        task_dir = self._task(task_id)
        original_current = self._current_path().read_bytes() if self._current_path().exists() else None
        # index.json 现在在任务目录内，回滚时跟着 task_dir 一起被删，不再需要单独备份。
        try:
            files = {}
            for path, rel, target in planned:
                data_bytes = path.read_bytes()
                digest = _sha(data_bytes)
                staged_file = staged_dir / "files" / rel
                with _atomic(staged_file, binary=True) as handle:
                    handle.write(data_bytes)
                payload = self.store / "payload" / digest[:2] / digest
                if not payload.exists():
                    staged_payload = staged_dir / "payload" / digest[:2] / digest
                    with _atomic(staged_payload, binary=True) as handle:
                        handle.write(data_bytes)
                    created_payloads.append(payload)
                files[rel] = {"sha256": digest, "size": len(data_bytes), "staged": str(staged_file)}
            for rel, info in files.items():
                target = self._safe_workspace_path(rel)
                if target.exists():
                    if not target.is_file():
                        raise PermissionError(f"import target is not a regular file: {rel}")
                    backup = backup_dir / rel
                    self._copy_file(target, backup)
                    replaced_targets.append((target, backup))
                else:
                    created_targets.append(target)
                self._copy_file(Path(info["staged"]), target)
                data = self._read_payload(info["sha256"]) if (self.store / "payload" / info["sha256"][:2] / info["sha256"]).exists() else Path(info["staged"]).read_bytes()
                payload = self.store / "payload" / info["sha256"][:2] / info["sha256"]
                if not payload.exists():
                    payload.parent.mkdir(parents=True, exist_ok=True)
                    with _atomic(payload, binary=True) as handle:
                        handle.write(data)
                stat = target.stat()
                info.pop("staged", None)
                info["mtime"] = stat.st_mtime_ns
            self._write_state(state)
            ref = self._write_manifest(state, files, force_full=True)
            _write_json(self._task(task_id) / "steps" / "s0001.json", {
                "index": 1, "title": "imported handoff", "manifest_ref": ref, "conclusion": "", "next": resume.get("next", ""),
            })
            self._write_active(task_id)
            self._write_index(task_id, files)
            return {"task_id": task_id}
        except Exception:
            for target in created_targets:
                if target.exists() or os.path.lexists(str(target)):
                    self._remove(target)
            for target, backup in replaced_targets:
                if target.exists() or os.path.lexists(str(target)):
                    self._remove(target)
                self._copy_file(backup, target)
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)
            if original_current is None:
                if self._current_path().exists():
                    self._current_path().unlink()
            else:
                with _atomic(self._current_path(), binary=True) as handle:
                    handle.write(original_current)
            for payload in created_payloads:
                if payload.exists():
                    payload.unlink()
            raise
        finally:
            if import_journal.exists():
                shutil.rmtree(import_journal, ignore_errors=True)

    def _task(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise ValueError(f"invalid task id: {task_id!r}")
        return self.store / "tasks" / task_id

    def _state_path(self, task_id: str) -> Path:
        return self._task(task_id) / "state.json"

    # ---- 活动指针与基线索引 -------------------------------------------------
    # 两者都必须按范围隔离，否则同一个 store 被多个工作区共用时会互相串味：
    # 活动指针按工作区 root 分键，基线索引挂在任务目录下。

    def _current_path(self) -> Path:
        return self.store / "current.json"

    def _root_key(self) -> str:
        return self.root.as_posix().casefold() if os.name == "nt" else self.root.as_posix()

    def _read_current(self) -> dict:
        """读活动指针表。兼容 0.1.1 的单键格式（active_task_id）。"""
        raw = _read_json(self._current_path(), {})
        if not isinstance(raw, dict):
            return {}
        active = raw.get("active")
        if isinstance(active, dict):
            return active
        legacy = raw.get("active_task_id")
        return {self._root_key(): legacy} if legacy else {}

    def _active_id(self) -> Optional[str]:
        return self._read_current().get(self._root_key())

    def _write_active(self, task_id: Optional[str]) -> None:
        table = self._read_current()
        if task_id is None:
            table.pop(self._root_key(), None)
        else:
            table[self._root_key()] = task_id
        _write_json(self._current_path(), {"schema_version": SCHEMA_VERSION, "active": table})

    def _index_path(self, task_id: str) -> Path:
        return self._task(task_id) / "index.json"

    def _read_index(self, task_id: str) -> dict:
        return _read_json(self._index_path(task_id), {})

    def _write_index(self, task_id: str, files: dict) -> None:
        """写基线索引。条目里存 sha256，下一次 _scan 才能不读盘直接复用。

        content=False 的扫描（restore 后、tick 等）没有 sha，此时若 mtime/size
        未变就从旧索引里把 sha 带过来，不要把已有基线抹掉。
        """
        old = self._read_index(task_id)
        out = {}
        for rel, info in files.items():
            entry = {"mtime": info["mtime"], "size": info["size"]}
            if info.get("sensitive"):
                entry["sensitive"] = True
            elif info.get("skipped"):
                entry["skipped"] = info["skipped"]
            if info.get("sha256"):
                entry["sha256"] = info["sha256"]
            prior = old.get(rel) or {}
            if ("sha256" not in entry and prior.get("sha256")
                    and prior.get("mtime") == info["mtime"] and prior.get("size") == info["size"]):
                entry["sha256"] = prior["sha256"]
            out[rel] = entry
        _write_json(self._index_path(task_id), out)

    def _state(self, task_id: str) -> dict:
        path = self._state_path(task_id)
        if not path.exists():
            raise FileNotFoundError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        state["updated_at"] = _now()
        _write_json(self._state_path(state["task_id"]), state)

    def _active(self, task_id: Optional[str] = None, allow_closed: bool = False) -> dict:
        if task_id:
            return self._state(task_id)
        active = self._active_id()
        if not active:
            raise RuntimeError("no active task")
        state = self._state(active)
        if state["status"] == "closed" and not allow_closed:
            raise PermissionError(f"task {active} is closed")
        return state

    def _scan(self, state: dict, content: bool = True, previous: Optional[dict] = None) -> dict:
        """扫工作区。

        content=True 时给非敏感文件算 sha256。默认每次都读内容（正确优先）；
        设了 TC_SHA_REUSE 后，传入上一份索引 previous（rel -> {mtime,size,
        sha256}）时 mtime+size 没变的文件可以复用旧 sha 不读盘。
        无论哪种模式，内容都不在内存里保留（_store_payloads 需要时再从盘上读）。
        """
        previous = previous or {}
        result = {}
        exclude = set(state.get("exclude") or [])
        store_prefix = self._store_rel_prefix().rstrip("/")
        self._health = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            linked_dirs = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
            for name in linked_dirs:
                self._health.append(f"skipped symlink: {(Path(dirpath) / name).relative_to(self.root).as_posix()}")
            kept_dirs = []
            for d in dirnames:
                candidate = (Path(dirpath) / d).relative_to(self.root).as_posix()
                in_store = bool(store_prefix) and (candidate == store_prefix or candidate.startswith(store_prefix + "/"))
                if not in_store and not _excluded(d, exclude) and not (Path(dirpath) / d).is_symlink():
                    kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink():
                    self._health.append(f"skipped symlink: {path.relative_to(self.root).as_posix()}")
                    continue
                rel = path.relative_to(self.root).as_posix()
                if (store_prefix and (rel == store_prefix or rel.startswith(store_prefix + "/"))) or _excluded(rel.split("/")[0], exclude):
                    continue
                try:
                    stat = path.stat()
                except OSError as e:
                    self._health.append(f"stat failed: {rel} ({type(e).__name__})")
                    continue
                info = {"mtime": stat.st_mtime_ns, "size": stat.st_size, "mode": stat.st_mode & 0o777,
                        "sensitive": _sensitive(rel)}
                if content and not info["sensitive"]:
                    prior = previous.get(rel) or {}
                    reuse = (_sha_reuse_enabled() and prior.get("sha256")
                             and prior.get("mtime") == info["mtime"] and prior.get("size") == info["size"])
                    if reuse:
                        info["sha256"] = prior["sha256"]
                        if prior.get("skipped"):
                            info["skipped"] = prior["skipped"]
                    elif stat.st_size <= PAYLOAD_LARGE:
                        try:
                            data = path.read_bytes()
                        except OSError as e:
                            self._health.append(f"read failed: {rel} ({type(e).__name__})")
                            continue
                        info["sha256"] = _sha(data)
                        # 不在内存里留下内容：_store_payloads 需要时再从盘上读一次。
                        # 这样内存占用与工作区大小无关。
                        self._watch["content_reads"] = self._watch.get("content_reads", 0) + 1
                    else:
                        info["skipped"] = "size"
                        # Streaming hash to avoid memory exhaustion
                        try:
                            h = hashlib.sha256()
                            with open(path, "rb") as f:
                                while True:
                                    chunk = f.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    h.update(chunk)
                            info["sha256"] = h.hexdigest()
                        except OSError as e:
                            self._health.append(f"hash failed: {rel} ({type(e).__name__})")
                result[rel] = info
        return result

    def _changed(self, previous: dict, scanned: dict) -> list[str]:
        return sorted(
            (set(scanned) - set(previous))
            | (set(previous) - set(scanned))
            | {p for p in set(scanned) & set(previous)
               if scanned[p]["mtime"] != previous[p]["mtime"] or scanned[p]["size"] != previous[p]["size"]}
        )

    def _protected_hashes(self, task_id: str) -> set[str]:
        """Return payloads that compression must preserve across the whole store.

        Payloads are content-addressed and may be referenced by several tasks.
        The active task's ordinary drift history is eligible for compression,
        but every other task's history must remain restorable.
        """
        found = set()
        tasks_dir = self.store / "tasks"
        for task_dir in tasks_dir.iterdir() if tasks_dir.exists() else ():
            if not task_dir.is_dir():
                continue
            other_task = task_dir.name != task_id
            for path in (task_dir / "steps").glob("*.json"):
                meta = json.loads(path.read_text(encoding="utf-8"))
                for info in self._load_manifest(task_dir.name, meta.get("manifest_ref", "")).values():
                    if info.get("sha256"):
                        found.add(info["sha256"])
            for path in (task_dir / "drifts").glob("*.json"):
                meta = json.loads(path.read_text(encoding="utf-8"))
                if other_task or meta.get("kind") == "recovered":
                    for info in self._load_manifest(task_dir.name, meta.get("manifest_ref", "")).values():
                        if info.get("sha256"):
                            found.add(info["sha256"])
        return found

    def _drift_records(self, task_id: str) -> list[dict]:
        records = []
        for path in sorted((self._task(task_id) / "drifts").glob("*.json")):
            meta = json.loads(path.read_text(encoding="utf-8"))
            records.append({"path": path, "meta": meta, "kind": meta.get("kind"), "entries": self._load_manifest(task_id, meta.get("manifest_ref", ""))})
        return records

    def git_ref(self, root: str, _state: Optional[dict] = None) -> dict:
        """只新增 refs/checkpoints。失败时不改工作区。

        `_state` 仅供内部调用方传入它已经读到的 state，省一次重复读取；
        外部调用仍只需 `root`。
        """
        if not (self.root / ".git").exists():
            return {"git": False}
        base_env = os.environ.copy()
        base_env.update(GIT_AUTHOR_NAME="task-checkpoint", GIT_AUTHOR_EMAIL="tc@localhost", GIT_COMMITTER_NAME="task-checkpoint", GIT_COMMITTER_EMAIL="tc@localhost")
        index_file = self.store / "git-index.tmp"

        def run(args, use_temp_index: bool = False):
            """自检必须用不含 GIT_INDEX_FILE 的干净环境，否则比较的是两个不同的索引。"""
            env = dict(base_env)
            if use_temp_index:
                env["GIT_INDEX_FILE"] = str(index_file)
            return subprocess.run(args, cwd=self.root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)

        def run_input(args, text: str, use_temp_index: bool = False):
            """必须用字节传输入：文本模式会把 \\n 转成 \\r\\n，git 会把 \\r 当成路径的一部分。"""
            env = dict(base_env)
            if use_temp_index:
                env["GIT_INDEX_FILE"] = str(index_file)
            return subprocess.run(args, cwd=self.root, env=env, input=text.encode("utf-8"), capture_output=True, check=False)

        def fingerprint() -> tuple:
            """外部可复核的两项指标：工作区状态、用户索引，其中状态含 HEAD。

            用 `--porcelain=v2 --branch`：v1 的 `-b` 只输出 `## master` 这种分支行，
            **不含 HEAD 的 oid**；v2 会输出 `# branch.oid <sha>`。这样两次 git 调用
            就能同时盖住“工作区、用户索引、HEAD”，而不是把 HEAD 默默丢掉。
            两项都是不透明字符串，只用于前后比对。
            """
            return (
                run(["git", "status", "--porcelain=v2", "--branch", "-z"]).stdout,
                run(["git", "ls-files", "-s", "-z"]).stdout,
            )

        # 自证（git 前后指纹对比）是产品对“不动用户 git 历史”的证明，默认开；
        # 大仓库上若在意这几百毫秒，用 TC_GIT_VERIFY=off 跳过。
        verify = os.environ.get("TC_GIT_VERIFY", "on").strip().lower() not in {"off", "0", "false", "no"}
        before = fingerprint() if verify else None
        state = _state if _state is not None else self._active(allow_closed=True)
        if state["head"] == 0:
            return {"git": True, "ref": None, "reason": "no step yet", "unchanged": True}
        ref = f"refs/checkpoints/{state['task_id']}/s{state['head']:04d}"
        exists = run(["git", "show-ref", "--verify", "--quiet", ref])
        if exists.returncode == 0:
            raise FileExistsError(ref)
        if index_file.exists():
            index_file.unlink()
        try:
            mapping = self._target_map(state["task_id"], state["head"], None)
            inside = self._store_rel_prefix()
            tracked = sorted(
                rel for rel, info in mapping.items()
                if info.get("sha256") and not info.get("sensitive") and not info.get("skipped")
                and not (inside and rel.startswith(inside))
            )
            blob = run_input(["git", "hash-object", "-w", "--stdin-paths"], "".join(f"{rel}\n" for rel in tracked))
            # 注意：hash-object 读的是工作区当前内容，不是 payload。save 已在同一把
            # 锁里刚扫过这些文件，时间窗很小；ref 只做增强，不影响存档本身。
            if blob.returncode != 0:
                raise RuntimeError(blob.stderr.decode("utf-8", "replace").strip())
            shas = blob.stdout.decode("utf-8", "replace").strip().splitlines()
            if len(shas) != len(tracked):
                raise RuntimeError("hash-object returned an unexpected number of blobs")
            lines = []
            for rel, sha in zip(tracked, shas):
                mode = "100755" if os.name != "nt" and os.access(self.root / rel, os.X_OK) else "100644"
                lines.append(f"{mode} {sha}\t{rel}\n")
            staged = run_input(["git", "update-index", "--add", "--index-info"], "".join(lines), use_temp_index=True)
            if staged.returncode != 0:
                raise RuntimeError(staged.stderr.decode("utf-8", "replace").strip())
            tree = run(["git", "write-tree"], use_temp_index=True)
        finally:
            if index_file.exists():
                index_file.unlink()
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.strip())
        commit = run(["git", "commit-tree", tree.stdout.strip(), "-m", f"checkpoint {state['head']}"])
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip())
        updated = run(["git", "update-ref", ref, commit.stdout.strip()])
        if updated.returncode != 0:
            raise RuntimeError(updated.stderr.strip())
        return {"git": True, "ref": ref, "unchanged": (fingerprint() == before) if verify else None}

    def _groups(self, files: dict, rules: Optional[dict] = None) -> list[dict]:
        """识别「主文件 + 配套目录」组合，如 PLAXIS 的 .p3d/.p3dat。

        配套缺失只作为 status="missing" 上报，不再中断 save：通用工作区里
        偶尔出现一个孤立的 .p3d 不应该让整个存档失败。
        """
        rules = DEFAULT_COMPANION_RULES if rules is None else rules
        report = []
        for rel in sorted(files):
            if "/" in rel:
                continue
            for main_suffix, companion_suffix in sorted(rules.items()):
                if not rel.endswith(main_suffix):
                    continue
                companion = f"{Path(rel).stem}{companion_suffix}"
                included = any(p == companion or p.startswith(companion + "/") for p in files)
                report.append({"main": rel, "companion": companion,
                               "status": "included" if included else "missing"})
        return report

    def _digest(self, files: dict) -> str:
        # mode 不算进摘要：单改权限不应破坏文件层面的幂等。
        visible = {k: {key: value for key, value in info.items() if key not in ("mtime", "_data", "mode")} for k, info in sorted(files.items())}
        return _sha(json.dumps(visible, sort_keys=True).encode("utf-8"))

    def _same_step(self, task_id: str, title: str, digest: str, *, description: str = "",
                   conclusion: str = "", verified=None, open_questions=None,
                   next_step: str = "") -> Optional[int]:
        expected = {
            "title": title, "file_digest": digest, "description": description,
            "conclusion": conclusion, "verified": verified or [],
            "open_questions": open_questions or [], "next": next_step,
        }
        for path in sorted((self._task(task_id) / "steps").glob("*.json")):
            meta = json.loads(path.read_text(encoding="utf-8"))
            if all(meta.get(key) == value for key, value in expected.items()):
                return meta["index"]
        return None

    def _store_payloads(self, files: dict) -> None:
        for rel, info in files.items():
            sha = info.get("sha256")
            if not sha or info.get("sensitive") or info.get("skipped"):
                continue
            destination = self.store / "payload" / sha[:2] / sha
            if destination.exists():
                continue
            data = info.get("_data")
            if data is None:
                try:
                    data = (self.root / rel).read_bytes()
                except OSError as e:
                    self._health.append(f"payload read failed: {rel} ({type(e).__name__})")
                    continue
            if _sha(data) != sha:
                # 扫描与落盘之间文件被改了。宁可不存，也不要存一份与 sha 对不上的内容。
                self._health.append(f"payload changed mid-save: {rel}")
                continue
            with _atomic(destination, binary=True) as handle:
                handle.write(data)

    def _public_map(self, files: dict) -> dict:
        result = {}
        for rel, info in files.items():
            item = {"mtime": info["mtime"], "size": info["size"]}
            if "mode" in info:
                item["mode"] = info["mode"]
            if info.get("sensitive"):
                item["sensitive"] = True
            elif info.get("skipped"):
                item["skipped"] = info["skipped"]
            elif info.get("sha256"):
                item["sha256"] = info["sha256"]
            result[rel] = item
        return result

    def _manifest_refs(self, task_id: str) -> list[Path]:
        return sorted((self._task(task_id) / "manifest").glob("m*.json"))

    def _last_manifest_ref(self, task_id: str) -> Optional[str]:
        refs = self._manifest_refs(task_id)
        return refs[-1].name if refs else None

    def _write_manifest(self, state: dict, mapping: dict, force_full: bool = False) -> str:
        """增量写清单：默认只存相对上一份的差异，每 MANIFEST_CHAIN_MAX 份强制全量。"""
        task_id = state["task_id"]
        existing = self._manifest_refs(task_id)
        parent = None if force_full else self._last_manifest_ref(task_id)
        if parent and len(existing) % MANIFEST_CHAIN_MAX == 0:
            parent = None
        if not parent:
            return self._save_manifest(task_id, {"version": SCHEMA_VERSION, "mode": "full", "entries": mapping})
        base = self._load_manifest(task_id, parent)
        entries = {rel: info for rel, info in mapping.items() if base.get(rel) != info}
        removed = sorted(set(base) - set(mapping))
        return self._save_manifest(task_id, {
            "version": SCHEMA_VERSION, "mode": "delta", "parent": parent,
            "entries": entries, "removed": removed,
        })

    def _save_manifest(self, task_id: str, manifest: dict) -> str:
        directory = self._task(task_id) / "manifest"
        name = f"m{len(list(directory.glob('*.json'))) + 1:04d}.json"
        _write_json(directory / name, manifest)
        return name

    def _load_manifest(self, task_id: str, ref: str) -> dict:
        """解析清单链。旧格式（entries 即全量）仍可读。"""
        if not ref:
            return {}
        key = (str(self.store), task_id, ref)
        with _MANIFEST_CACHE_LOCK:
            cached = _MANIFEST_CACHE.get(key)
            if cached is not None:
                _MANIFEST_CACHE.move_to_end(key)
                return dict(cached)
        raw = json.loads((self._task(task_id) / "manifest" / ref).read_text(encoding="utf-8"))
        if raw.get("mode") == "delta":
            resolved = self._load_manifest(task_id, raw.get("parent") or "")
            resolved.update(raw.get("entries") or {})
            for rel in raw.get("removed") or []:
                resolved.pop(rel, None)
        elif raw.get("full_baseline"):
            resolved = raw["full_baseline"]
        else:
            resolved = raw.get("entries") or {}
        with _MANIFEST_CACHE_LOCK:
            if key in _MANIFEST_CACHE:
                _MANIFEST_CACHE.move_to_end(key)
            else:
                if len(_MANIFEST_CACHE) >= MANIFEST_CACHE_MAX:
                    _MANIFEST_CACHE.popitem(last=False)
                _MANIFEST_CACHE[key] = dict(resolved)
        return dict(resolved)

    def _target_map(self, task_id: str, index: Optional[int], drift_id: Optional[str]) -> dict:
        directory = self._task(task_id)
        if index is not None:
            meta_path = directory / "steps" / f"s{index:04d}.json"
        else:
            meta_path = directory / "drifts" / f"{drift_id}.json"
        if not meta_path.exists():
            raise FileNotFoundError(meta_path.name)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("restorable") is False:
            raise PermissionError(f"{drift_id} is not restorable")
        return self._load_manifest(task_id, meta["manifest_ref"])

    def _different(self, current: dict, target: dict) -> bool:
        if "sha256" in current and "sha256" in target:
            return current["sha256"] != target["sha256"]
        return current.get("mtime") != target.get("mtime") or current.get("size") != target.get("size")

    def _record_drift(self, state: dict, kind: str, mapping: dict, partial: bool = False, previous: Optional[dict] = None, manifest_ref: Optional[str] = None) -> str:
        state["drift_count"] += 1
        drift_id = f"d{state['drift_count']:04d}"
        ref = manifest_ref or self._write_manifest(state, mapping)
        previous = previous or {}
        changed_paths = sorted(set(previous) | set(mapping))
        changes = []
        for rel in changed_paths:
            before = previous.get(rel)
            after = mapping.get(rel)
            if before == after:
                continue
            if before is None:
                change_type = "added"
            elif after is None:
                change_type = "deleted"
            else:
                change_type = "modified"
            changes.append({
                "path": rel,
                "change_type": change_type,
                "bytes": (after or before).get("size", 0),
                "binary": False,
                "payload": (after or {}).get("sha256"),
            })
        _write_json(self._task(state["task_id"]) / "drifts" / f"{drift_id}.json", {
            "drift_index": drift_id, "kind": kind, "partial": partial, "timestamp": _now(),
            "manifest_ref": ref, "changes": changes, "restorable": True,
        })
        self._write_state(state)
        return drift_id

    def _read_payload(self, sha: str) -> bytes:
        path = self.store / "payload" / sha[:2] / sha
        if not path.exists():
            raise FileNotFoundError(sha)
        return path.read_bytes()

    def _payload_available(self, info: dict) -> bool:
        """这份 target 条目能不能真的从 payload 还原出来。

        只看有没有 sha256 是不够的：payload 文件可能已经丢了（回收过、被手工清掉、
        或写入时校验未通过而跳过）。“能给出 sha”和“能拿出内容”必须分开看。
        """
        sha = info.get("sha256")
        if not sha:
            return False
        return (self.store / "payload" / sha[:2] / sha).exists()

    def _remove(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
