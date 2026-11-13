"""
Task Checkpoint MCP - stdio MCP 服务器 (阶段一)
换行分隔单行 JSON，工具白名单
"""
from __future__ import annotations

import json
import queue
import sys
import os
import threading
import time
import traceback
from pathlib import Path

# 将 scripts 目录加入 sys.path，确保导入 tc
sys.path.insert(0, str(Path(__file__).parent))

import tc
from tc import TaskCheckpoint, get_logger, init_logger

# ============================================================
# 工具白名单
# ============================================================

TOOLS = [
    "tc_init", "tc_switch", "tc_save", "tc_capture",
    "tc_show", "tc_restore", "tc_resume", "tc_export", "tc_import", "tc_compress",
]

MCP_VERSION_LATEST = "2025-06-18"
MCP_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


# ============================================================
# 输出辅助
# ============================================================

def _err(code: int, msg: str, req_id=None):
    return {"jsonrpc": "2.0", "error": {"code": code, "message": msg}, "id": req_id}


def _ok(res, req_id=None):
    return {"jsonrpc": "2.0", "result": res, "id": req_id}


def _tool_result(content: list, is_error: bool = False, structured=None):
    result = {"content": content, "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _text_content(text: str) -> dict:
    return {"type": "text", "text": text}


# ============================================================
# MCP 处理
# ============================================================

def _server_version() -> str:
    """报给客户端的版本号。

    从源码目录直接跑时读同级的 pyproject.toml；装成包之后 pyproject 不在，
    退回包元数据。两边都拿不到就报 unknown。

    这里刻意不放硬编码的版本号：改 pyproject 忘了改这里的话，客户端看到的
    版本就和实际包里的一致不了，而且没人会发现。
    """
    try:
        import tomllib
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        return tomllib.loads(text)["project"]["version"]
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("task-checkpoint-mcp")
    except Exception:
        return "unknown"


def handle_initialize(params: dict, req_id) -> dict:
    if not isinstance(params, dict):
        return _err(-32602, "initialize params must be an object", req_id)
    ver = params.get("protocolVersion", "")
    if ver in MCP_VERSIONS:
        agreed = ver
    else:
        agreed = MCP_VERSION_LATEST
    return _ok({
        "protocolVersion": agreed,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "task-checkpoint-mcp", "version": _server_version()},
    }, req_id)


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}

SKIPPED_PATHS = {"type": "array", "items": {"type": "string"}, "description": "paths skipped (e.g. symlinks pointing outside the workspace)"}
GIT_INFO = {"type": "object", "description": "Git pointer for this step. Git is an enhancement, not a requirement: when `ok` is false, `reason` says why no ref was created (not a repo, TC_GIT=off, ref already taken), and the checkpoint is still valid",
            "properties": {"ok": {"type": "boolean"}, "ref": {"type": ["string", "null"]},
                           "reason": {"type": ["string", "null"]}, "error": {"type": "string"},
                           "unchanged": {"type": ["boolean", "null"]}},
            "required": ["ok"]}


ROOT = _string("Workspace root")
TASK_ID = _string("Task id; omit to use the current active task")
TOOL_SPECS = [
    {
        "name": "tc_init",
        "description": "Create a task. Use this once at the start of work that will span several steps, before touching files, so there is something to checkpoint into. An already-active task is suspended, not closed. Do not call it per step (that is `tc_save`) or to resume existing work (that is `tc_switch`). Fill in `goal` and `constraints` now: a later session reads them and should not have to ask you again.",
        "inputSchema": _schema({"root": ROOT, "name": _string("Task name"), "goal": _string("What the task is meant to achieve"), "constraints": _string("Hard rules to keep, e.g. no new dependencies"), "store": _string("Checkpoint directory; defaults to .checkpoints inside the workspace"), "exclude": {"type": "array", "items": {"type": "string"}, "description": "Extra exclude patterns, matched against a single path segment (a directory name or the first component of a path), e.g. [\"config\", \"*.local\"]"}}, ["root", "name"]),
        "outputSchema": _schema({"task_id": _string("New task id"), "name": _string("Task name"), "store": _string("Checkpoint directory"), "store_is_external": {"type": "boolean"}, "prev_suspended": {"type": ["string", "null"]}, "exclude": {"type": "array", "items": {"type": "string"}}, "note": _string("Hint about where checkpoints are stored")}, ["task_id", "name", "store", "store_is_external", "note"]),
    },
    {
        "name": "tc_switch",
        "description": "Make an existing task the active one. Use it when you are picking up a different, already-recorded task in the same workspace. It rejects closed tasks and does not create tasks (use `tc_init` for that). You rarely need it: most tools accept an explicit `task_id`.",
        "inputSchema": _schema({"root": ROOT, "task_id": _string("Task id to activate")}, ["root", "task_id"]),
        "outputSchema": _schema({"task_id": TASK_ID, "status": _string("open")}, ["task_id", "status"]),
    },
    {
        "name": "tc_save",
        "description": "Record one step of the task. Use it after every verifiable mini-step (one file changed, one test passes), not once at the end: this record is what a later session resumes from. Say what you did, what you concluded and what comes next; do not log a play-by-play. Passing `close=true` closes the task and accepts no further saves. If you only want to snapshot the changed files without a step, use `tc_capture`.",
        "inputSchema": _schema({"root": ROOT, "title": _string("Step title"), "task_id": TASK_ID, "description": _string("Why you did it this way"), "conclusion": _string("What this step established"), "verified": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}, "next": _string("What to do next"), "close": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}, "description": "Reserved for path scoping; must be an empty array for now"}}, ["root", "title"]),
        "outputSchema": _schema({"task_id": TASK_ID, "index": {"type": "integer"}, "title": _string("Step title"), "groups": {"type": "array"}, "skipped_paths": SKIPPED_PATHS, "idempotent": {"type": "boolean"}, "closed": {"type": "boolean"}, "status": _string("open / suspended / closed"), "active_task_changed": {"type": "boolean", "description": "Whether this call moved the active task (happens when you pass a task_id that is not the active one)"}, "git": GIT_INFO}, ["task_id", "index", "title", "groups", "skipped_paths", "idempotent", "closed", "status", "active_task_changed", "git"]),
    },
    {
        "name": "tc_capture",
        "description": "Record the current file changes immediately, without waiting for the quiet period. Use it before a risky edit, or right before handing off when you want the changed files accounted for now. It records no step: if you also have something to conclude, use `tc_save`, which captures the changes as part of the step.",
        "inputSchema": _schema({"root": ROOT}, ["root"]),
        "outputSchema": _schema({"captured": {"type": "boolean"}, "waiting": {"type": "boolean"}, "drift_id": {"type": ["string", "null"]}, "partial": {"type": "boolean"}, "reads": {"type": "integer"}, "skipped_paths": SKIPPED_PATHS}, ["captured", "drift_id", "partial", "reads", "waiting", "skipped_paths"]),
    },
    {
        "name": "tc_show",
        "description": "List the task steps; with `include_drift=true` it also lists the change (drift) layers. Use it to look back at what happened, or to find the `index` or `drift_id` you need for `tc_restore`. For just \"where am I and what is next\", `tc_resume` is cheaper. Pass `index` to read one step in full.",
        "inputSchema": _schema({"root": ROOT, "task_id": TASK_ID, "index": {"type": "integer", "minimum": 1}, "include_drift": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "integer", "minimum": 0}}, ["root"]),
        "outputSchema": _schema({"steps": {"type": "array"}, "drifts": {"type": "array"}, "head": {"type": "integer"}, "next_cursor": {"type": ["integer", "null"]}}, ["steps", "head"]),
    },
    {
        "name": "tc_restore",
        "description": "Roll the workspace back to a task step (`index`) or to a change layer (`drift_id`) — pass exactly one of the two. Use it when an edit went wrong. The rollback is itself recorded as a change layer, so a mistaken restore can be undone. Call it with `apply=false` first to see the plan without writing anything, then repeat with `apply=true`.",
        "inputSchema": _schema({"root": ROOT, "index": {"type": "integer", "description": "Task step index"}, "drift_id": _string("Change layer id, e.g. d0001"), "apply": {"type": "boolean", "description": "true actually writes to the workspace"}}, ["root"]),
        "outputSchema": _schema({"applied": {"type": "boolean"}, "recovered": {"type": ["string", "null"], "description": "Change layer id auto-preserved by this call"}, "plan": {"type": "object"}, "skipped_paths": SKIPPED_PATHS}, ["applied", "recovered", "plan", "skipped_paths"]),
    },
    {
        "name": "tc_resume",
        "description": "Read-only: returns the goal, current step, paths changed but not yet recorded, and what to do next. Call it first in a new session, before assuming anything about prior work — it writes nothing. If you already know what to do and only need the changed files, use `tc_show`; to continue on another machine, use `tc_export`.",
        "inputSchema": _schema({"root": ROOT, "task_id": TASK_ID}, ["root"]),
        "outputSchema": _schema({"goal": _string("What the task is meant to achieve"), "constraints": _string("Hard rules to keep"), "head": {"type": "integer"}, "last": {"type": "object"}, "open_questions": {"type": "array"}, "drift_paths": {"type": "array"}, "next": _string("What to do next"), "handoff": _string("Text you can paste into a new session"), "health": {"type": "object"}, "store": _string("Checkpoint directory"), "active_task": _string("Active task"), "skipped": {"type": "array"}}, ["goal", "head", "drift_paths", "handoff", "health", "active_task"]),
    },
    {
        "name": "tc_export",
        "description": "Write a handoff package into an existing directory so the task can continue on another machine or in another checkout; pair it with `tc_import` on the other side. Sensitive file contents are never exported (they appear in `filtered`). Use it to move work between machines — to resume in the same workspace, `tc_resume` is enough.",
        "inputSchema": _schema({"root": ROOT, "to": _string("Existing parent directory to export into")}, ["root", "to"]),
        "outputSchema": _schema({"path": _string("Package directory"), "filtered": {"type": "array"}, "files": {"type": "array"}}, ["path", "filtered"]),
    },
    {
        "name": "tc_import",
        "description": "Import a package produced by `tc_export` and continue the task in another directory or on another machine. Use it on the receiving side of a handoff; to resume in the same workspace, `tc_resume` is enough and needs no package. It validates the task id and the workspace path, and refuses duplicate tasks, `..` traversal and symlink targets; if anything fails it rolls back the files it already wrote.",
        "inputSchema": _schema({"root": ROOT, "package": _string("Package directory, i.e. the path returned by tc_export")}, ["root", "package"]),
        "outputSchema": _schema({"task_id": _string("Task id after import")}, ["task_id"]),
    },
    {
        "name": "tc_compress",
        "description": "Reclaim checkpoint space by deleting change-layer payloads older than the retention window. Use it when the store has grown large. Reclaimed layers can no longer be restored, but step baselines are untouched, so `tc_restore` by `index` still works.",
        "inputSchema": _schema({
            "root": ROOT,
            "keep_seconds": {"type": "integer", "minimum": 0, "description": "Keep change layers newer than this many seconds; default 7 days"},
            "minimum": {"type": "integer", "minimum": 0, "description": "Minimum number of older layers to keep per path; default 20"},
            "maximum": {"type": "integer", "minimum": 1, "description": "Maximum number of layers to keep per path; default 50"},
        }, ["root"]),
        "outputSchema": _schema({"converted": {"type": "integer", "description": "Payloads actually deleted"}, "eligible": {"type": "integer", "description": "Passed the safety checks and were eligible for reclaiming"}, "already_gone": {"type": "integer", "description": "Marked unrestorable but the payload was already absent"}, "bytes_freed": {"type": "integer", "description": "Bytes freed"}, "keep_seconds": {"type": "integer"}, "minimum": {"type": "integer"}, "maximum": {"type": "integer"}}, ["converted", "eligible", "already_gone", "bytes_freed", "keep_seconds", "minimum", "maximum"]),
    },
]


def handle_tools_list(params: dict, req_id) -> dict:
    if not isinstance(params, dict):
        return _err(-32602, "tools/list params must be an object", req_id)
    return _ok({"tools": [
        {key: spec[key] for key in ("name", "description", "inputSchema", "outputSchema")}
        for spec in TOOL_SPECS
    ]}, req_id)


def _validate_value(value, schema: dict, path: str) -> None:
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    valid = any(
        (kind == "string" and isinstance(value, str))
        or (kind == "boolean" and isinstance(value, bool))
        or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (kind == "array" and isinstance(value, list))
        or (kind == "object" and isinstance(value, dict))
        or (kind == "null" and value is None)
        for kind in expected_types
    )
    if not valid:
        raise ValueError(f"argument {path} has invalid type")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"argument {path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"argument {path} exceeds maximum")
    if isinstance(value, list) and schema.get("items"):
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"missing required arguments at {path}: " + ", ".join(missing))
        extra = sorted(set(value) - set(properties)) if schema.get("additionalProperties") is False else []
        if extra:
            raise ValueError(f"additional arguments at {path}: " + ", ".join(extra))
        for key, child in value.items():
            _validate_value(child, properties[key], f"{path}.{key}")


def _validate_tool_arguments(name: str, arguments) -> None:
    spec = next((item for item in TOOL_SPECS if item["name"] == name), None)
    if spec is None:
        raise ValueError(f"unknown tool: {name}")
    _validate_value(arguments, spec["inputSchema"], "arguments")
    if name == "tc_save" and arguments.get("paths"):
        raise ValueError("paths is reserved and must be empty until scoped capture is implemented")
    if name == "tc_restore":
        has_index = "index" in arguments
        has_drift = "drift_id" in arguments
        if has_index == has_drift:
            raise ValueError("tc_restore requires exactly one of index or drift_id")


def handle_tools_call(params: dict, req_id) -> dict:
    if not isinstance(params, dict):
        return _err(-32602, "tools/call params must be an object", req_id)
    name = params.get("name", "")
    arguments = params.get("arguments", {})
    if name not in TOOLS:
        return _err(-32601, f"unknown tool: {name}", req_id)
    try:
        _validate_tool_arguments(name, arguments)
    except ValueError as e:
        return _err(-32602, str(e), req_id)
    try:
        result = dispatch_tool(name, arguments)
        return _ok(_tool_result([_text_content(json.dumps(result, ensure_ascii=False))], structured=result), req_id)
    except ValueError as e:
        return _err(-32602, str(e), req_id)
    except NotImplementedError as e:
        return _ok(_tool_result([_text_content(json.dumps({
            "error": str(e), "isError": True, "not_implemented": True,
        }))], is_error=True), req_id)
    except PermissionError as e:
        return _ok(_tool_result([_text_content(json.dumps({"error": str(e), "isError": True}))], is_error=True), req_id)
    except (FileNotFoundError, FileExistsError, RuntimeError) as e:
        payload = {"error": str(e), "isError": True}
        return _ok(_tool_result([_text_content(json.dumps(payload, ensure_ascii=False))], True, payload), req_id)
    except Exception as e:
        tb = traceback.format_exc()
        try:
            store_str = arguments.get("root", "")
            store_p = Path(store_str) / ".checkpoints" if store_str else None
            if store_p:
                init_logger(store_p)
            get_logger().error(f"unhandled error in {name}: {e}\n{tb}")
        except Exception:
            pass
        return _err(-32603, f"internal error: {e}", req_id)


def dispatch_tool(name: str, args: dict) -> dict:
    root = args.get("root", "")
    if not root:
        raise ValueError("root is required")

    api = TaskCheckpoint(root)

    if name == "tc_init":
        store = args.get("store")
        return api.init(root, args["name"], store=store,
                        goal=args.get("goal", ""), constraints=args.get("constraints", ""),
                        exclude=args.get("exclude"))

    if name == "tc_switch":
        return api.switch(root, args["task_id"])

    if name == "tc_save":
        return api.save(root, args["title"],
                        task_id=args.get("task_id"),
                        description=args.get("description", ""),
                        conclusion=args.get("conclusion", ""),
                        verified=args.get("verified", []),
                        open_questions=args.get("open_questions", []),
                        next_step=args.get("next", ""),
                        close=args.get("close", False),
                        paths=args.get("paths", []))

    if name == "tc_capture":
        return api.capture(root, force=True)

    if name == "tc_show":
        return api.show(root,
                        task_id=args.get("task_id"),
                        index=args.get("index"),
                        include_drift=args.get("include_drift", False),
                        limit=min(int(args.get("limit", 50)), 200),
                        cursor=args.get("cursor"))

    if name == "tc_restore":
        return api.restore(root,
                           index=args.get("index"),
                           drift_id=args.get("drift_id"),
                           apply=args.get("apply", False))

    if name == "tc_resume":
        return api.resume(root, task_id=args.get("task_id"))

    if name == "tc_export":
        return api.export(root, args["to"])

    if name == "tc_import":
        return api.import_handoff(args["package"], root)

    if name == "tc_compress":
        return api.compress(keep_seconds=int(args.get("keep_seconds", 7 * 24 * 3600)),
                            minimum=int(args.get("minimum", 20)),
                            maximum=int(args.get("maximum", 50)))

    raise NotImplementedError(f"tool not implemented: {name}")


# ============================================================
# stdio 主循环
# ============================================================

_WATCH_ERRORS: dict[str, dict] = {}


def _watch_once() -> None:
    if os.environ.get("TC_WATCH", "on") == "off":
        return
    for root, watch in list(tc._WATCH.items()):
        root = watch.get("root", root)
        try:
            api = tc.TaskCheckpoint(root)
            if not api._active_id():
                continue
        except Exception:
            continue
        try:
            api.tick()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            seen = _WATCH_ERRORS.get(root) or {"detail": None, "count": 0}
            count = seen["count"] + 1
            changed = seen["detail"] != detail
            _WATCH_ERRORS[root] = {"detail": detail, "count": count}
            if changed or _reports_at(count):
                suffix = "" if count == 1 else f" ({count}x)"
                sys.stderr.write(f"task-checkpoint watch skipped{suffix}: {detail}\n")
        else:
            seen = _WATCH_ERRORS.pop(root, None)
            if seen and seen["count"] > 1:
                sys.stderr.write(f"task-checkpoint watch recovered after {seen['count']} failed ticks\n")


def _reports_at(count: int) -> bool:
    """首次、以及 10/100/1000… 汇报一次：错误持续时 stderr 只按对数增长。"""
    return count == 1 or (count > 1 and str(count).rstrip("0") == "1")


def _force_utf8_streams() -> None:
    """MCP 要求 UTF-8；Windows 管道下默认是本地代码页，必须显式固定。"""
    for stream, errors in ((sys.stdout, "strict"), (sys.stdin, "replace"), (sys.stderr, "backslashreplace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors, newline="\n")
        except (AttributeError, ValueError, OSError):
            pass


SKILL_PACKAGE_DIR = Path("share") / "task-checkpoint-mcp"

USAGE = """\
task-checkpoint-mcp —— 长任务的步骤存档与回退（stdio MCP 服务器）

不带参数启动时按 MCP 协议读 stdin / 写 stdout，这是客户端用的方式。
下面这些开关是给人用的：

  --print-skill            把 SKILL.md 打到 stdout
  --skill-path             打印 SKILL.md 的路径（配合 cp 用）
  --install-skill <目录>    把 SKILL.md 写进 <目录>/SKILL.md（已存在时报错）
      --force              配合 --install-skill，允许覆盖
  --version                打印版本号
  -h, --help               打印这段说明

SKILL.md 是功能的一半：MCP 服务器负责存取，它负责让模型每完成一步
真的去调 tc_save。装进客户端扫描的技能目录才生效，例如：

  tc-mcp --install-skill ~/.claude/skills/task-checkpoint
"""


def _skill_md_path() -> Path:
    """SKILL.md 在哪。

    装成包时它是包数据，落在 <data>/share/task-checkpoint-mcp/SKILL.md；
    从源码目录直接跑时就在仓库根目录。两边都找不到才算错。
    """
    candidates: list[Path] = []
    try:
        import sysconfig
        data = sysconfig.get_path("data")
        if data:
            candidates.append(Path(data) / SKILL_PACKAGE_DIR / "SKILL.md")
    except Exception:
        pass
    candidates.append(Path(sys.prefix) / SKILL_PACKAGE_DIR / "SKILL.md")
    candidates.append(Path(__file__).resolve().parent.parent / "SKILL.md")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("SKILL.md 找不到，找过：" + "、".join(str(c) for c in candidates))


def _copy_skill(destination: str, force: bool) -> int:
    target_dir = Path(destination).expanduser()
    target = target_dir / "SKILL.md"
    if target.exists() and not force:
        sys.stderr.write(f"{target} 已存在；要覆盖再加 --force\n")
        return 1
    try:
        payload = _skill_md_path().read_bytes()
    except FileNotFoundError as e:
        sys.stderr.write(f"{e}\n")
        return 1
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    sys.stderr.write(f"已写入 {target}\n")
    sys.stderr.write("重启客户端后生效。\n")
    return 0


def _run_cli(argv: list[str]) -> int | None:
    """处理命令行开关。

    返回退出码表示"这是一次 CLI 调用，别进 MCP 模式"；返回 None 表示
    没给参数（或给的是认不出来的参数）—— 照旧进 stdio MCP，避免把
    客户端多传的参数变成启动失败。
    """
    if not argv:
        return None
    first = argv[0]
    if first in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    if first == "--version":
        sys.stdout.write(_server_version() + "\n")
        return 0
    if first == "--skill-path":
        try:
            sys.stdout.write(str(_skill_md_path()) + "\n")
        except FileNotFoundError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        return 0
    if first == "--print-skill":
        try:
            sys.stdout.write(_skill_md_path().read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        return 0
    if first == "--install-skill":
        rest = argv[1:]
        targets = [a for a in rest if not a.startswith("-")]
        if not targets:
            sys.stderr.write("--install-skill 需要一个目标目录\n")
            return 2
        return _copy_skill(targets[0], "--force" in rest)
    sys.stderr.write(f"认不出的参数：{first}（当作 MCP stdio 启动；--help 看用法）\n")
    return None


def main() -> int | None:
    code = _run_cli(sys.argv[1:])
    if code is not None:
        return code
    _force_utf8_streams()
    logger = None
    try:
        store = os.environ.get("TC_STORE")
        if store:
            try:
                init_logger(Path(store) / ".checkpoints")
                logger = get_logger()
                logger.info("task-checkpoint-mcp starting")
            except Exception:
                logger = None
        interval = float(os.environ.get("TC_WATCH_INTERVAL", "15"))
        incoming: queue.Queue[str | None] = queue.Queue()

        def read_input() -> None:
            while True:
                line = sys.stdin.readline()
                incoming.put(None if not line else line)
                if not line:
                    return

        threading.Thread(target=read_input, daemon=True).start()

        def watch_loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    _watch_once()
                except Exception as exc:
                    sys.stderr.write(f"task-checkpoint watcher failed: {type(exc).__name__}: {exc}\n")

        threading.Thread(target=watch_loop, daemon=True).start()
        while True:
            try:
                line = incoming.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                sys.stdout.write(json.dumps(_err(-32700, f"parse error: {e}")) + "\n")
                sys.stdout.flush()
                continue
            if not isinstance(msg, dict):
                sys.stdout.write(json.dumps(_err(-32600, "request must be an object")) + "\n")
                sys.stdout.flush()
                continue
            if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                if "id" in msg:
                    sys.stdout.write(json.dumps(_err(-32600, "invalid JSON-RPC request", msg.get("id"))) + "\n")
                    sys.stdout.flush()
                continue
            method = msg["method"]
            params = msg.get("params", {})
            if "id" not in msg:
                continue
            req_id = msg.get("id")
            try:
                if method == "initialize":
                    resp = handle_initialize(params, req_id)
                elif method == "tools/list":
                    resp = handle_tools_list(params, req_id)
                elif method == "tools/call":
                    resp = handle_tools_call(params, req_id)
                elif method in {"notifications/initialized", "notifications/cancelled"}:
                    continue
                else:
                    resp = _err(-32601, f"unknown method: {method}", req_id)
            except ValueError as e:
                resp = _err(-32602, str(e), req_id)
            except Exception as e:
                resp = _err(-32603, f"internal error: {e}", req_id)
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception as e:
        if logger:
            logger.error(f"fatal: {e}\n{traceback.format_exc()}")
        else:
            sys.stderr.write(f"task-checkpoint fatal: {e}\n")
    finally:
        if logger:
            logger.flush()


if __name__ == "__main__":
    sys.exit(main())
