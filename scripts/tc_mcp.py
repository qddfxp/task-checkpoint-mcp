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


SKIPPED_PATHS = {"type": "array", "items": {"type": "string"}, "description": "skipped paths (e.g. symlinks pointing outside the workspace)"}
GIT_INFO = {"type": "object", "description": "这一步的 git 指针。git 是增强：ok 为 false 或 reason 说明没建 ref（不是仓库、TC_GIT=off、ref 重名等）时存档仍然有效",
            "properties": {"ok": {"type": "boolean"}, "ref": {"type": ["string", "null"]},
                           "reason": {"type": ["string", "null"]}, "error": {"type": "string"},
                           "unchanged": {"type": ["boolean", "null"]}},
            "required": ["ok"]}


ROOT = _string("工作区根目录")
TASK_ID = _string("任务 id。省略时使用当前活动任务")
TOOL_SPECS = [
    {
        "name": "tc_init",
        "description": "创建任务。已有活动任务会挂起而不是关闭。",
        "inputSchema": _schema({"root": ROOT, "name": _string("任务名"), "goal": _string("任务目标"), "constraints": _string("约束"), "store": _string("存档目录；省略时用工作区内 .checkpoints"), "exclude": {"type": "array", "items": {"type": "string"}, "description": "额外的排除模式，按单个名字段匹配（目录名或路径第一段），如 [\"config\", \"*.local\"]"}}, ["root", "name"]),
        "outputSchema": _schema({"task_id": _string("新任务 id"), "name": _string("任务名"), "store": _string("存档目录"), "store_is_external": {"type": "boolean"}, "prev_suspended": {"type": ["string", "null"]}, "exclude": {"type": "array", "items": {"type": "string"}}, "note": _string("存档位置提示")}, ["task_id", "name", "store", "store_is_external", "note"]),
    },
    {
        "name": "tc_switch",
        "description": "切换活动任务。closed 任务会被拒绝。",
        "inputSchema": _schema({"root": ROOT, "task_id": _string("要激活的任务 id")}, ["root", "task_id"]),
        "outputSchema": _schema({"task_id": TASK_ID, "status": _string("open")}, ["task_id", "status"]),
    },
    {
        "name": "tc_save",
        "description": "保存一个任务层步骤。close=true 时关闭任务且不再接受保存。",
        "inputSchema": _schema({"root": ROOT, "title": _string("步骤标题"), "task_id": TASK_ID, "description": _string("为什么这么做"), "conclusion": _string("这一步的结论"), "verified": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}, "next": _string("下一步"), "close": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}, "description": "预留路径范围参数；当前必须为空数组"}}, ["root", "title"]),
        "outputSchema": _schema({"task_id": TASK_ID, "index": {"type": "integer"}, "title": _string("步骤标题"), "groups": {"type": "array"}, "skipped_paths": SKIPPED_PATHS, "idempotent": {"type": "boolean"}, "closed": {"type": "boolean"}, "status": _string("open / suspended / closed"), "active_task_changed": {"type": "boolean", "description": "本次调用是否把活动任务切走了（传了 task_id 且不是当前活动任务时会发生）"}, "git": GIT_INFO}, ["task_id", "index", "title", "groups", "skipped_paths", "idempotent", "closed", "status", "active_task_changed", "git"]),
    },
    {
        "name": "tc_capture",
        "description": "立即记录当前文件变化，不等静默期。",
        "inputSchema": _schema({"root": ROOT}, ["root"]),
        "outputSchema": _schema({"captured": {"type": "boolean"}, "waiting": {"type": "boolean"}, "drift_id": {"type": ["string", "null"]}, "partial": {"type": "boolean"}, "reads": {"type": "integer"}, "skipped_paths": SKIPPED_PATHS}, ["captured", "drift_id", "partial", "reads", "waiting", "skipped_paths"]),
    },
    {
        "name": "tc_show",
        "description": "列出任务层步骤；include_drift=true 时同时列出变更层。index 可精确查看单步。",
        "inputSchema": _schema({"root": ROOT, "task_id": TASK_ID, "index": {"type": "integer", "minimum": 1}, "include_drift": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "integer", "minimum": 0}}, ["root"]),
        "outputSchema": _schema({"steps": {"type": "array"}, "drifts": {"type": "array"}, "head": {"type": "integer"}, "next_cursor": {"type": ["integer", "null"]}}, ["steps", "head"]),
    },
    {
        "name": "tc_restore",
        "description": "按任务层序号或变更层编号恢复。index 与 drift_id 二选一；apply=false 只预览。",
        "inputSchema": _schema({"root": ROOT, "index": {"type": "integer", "description": "任务层序号"}, "drift_id": _string("变更层编号，例如 d0001"), "apply": {"type": "boolean", "description": "true 才写工作区"}}, ["root"]),
        "outputSchema": _schema({"applied": {"type": "boolean"}, "recovered": {"type": ["string", "null"], "description": "本次自动保全的变更层编号"}, "plan": {"type": "object"}, "skipped_paths": SKIPPED_PATHS}, ["applied", "recovered", "plan", "skipped_paths"]),
    },
    {
        "name": "tc_resume",
        "description": "只读返回目标、当前步骤、未落档路径和下一步。",
        "inputSchema": _schema({"root": ROOT, "task_id": TASK_ID}, ["root"]),
        "outputSchema": _schema({"goal": _string("任务目标"), "constraints": _string("约束"), "head": {"type": "integer"}, "last": {"type": "object"}, "open_questions": {"type": "array"}, "drift_paths": {"type": "array"}, "next": _string("下一步"), "handoff": _string("可粘贴给新会话的文本"), "health": {"type": "object"}, "store": _string("存档目录"), "active_task": _string("活动任务"), "skipped": {"type": "array"}}, ["goal", "head", "drift_paths", "handoff", "health", "active_task"]),
    },
    {
        "name": "tc_export",
        "description": "导出交接包到一个已存在的目录。敏感文件内容不会导出。",
        "inputSchema": _schema({"root": ROOT, "to": _string("已存在的导出父目录")}, ["root", "to"]),
        "outputSchema": _schema({"path": _string("交接包目录"), "filtered": {"type": "array"}, "files": {"type": "array"}}, ["path", "filtered"]),
    },
    {
        "name": "tc_import",
        "description": "导入 tc_export 生成的交接包，在另一个目录或机器上接续任务。会校验任务 id 与工作区路径，拒绝重复任务、.. 穿越与符号链接目标；失败时撤销已写入的文件。",
        "inputSchema": _schema({"root": ROOT, "package": _string("交接包目录，即 tc_export 返回的 path")}, ["root", "package"]),
        "outputSchema": _schema({"task_id": _string("导入后的任务 id")}, ["task_id"]),
    },
    {
        "name": "tc_compress",
        "description": "回收存档空间：删掉超过保留期的变更层内容。被回收的变更层不能再 restore，步骤基线不受影响。",
        "inputSchema": _schema({
            "root": ROOT,
            "keep_seconds": {"type": "integer", "minimum": 0, "description": "保留最近多少秒内的变更层，默认 7 天"},
            "minimum": {"type": "integer", "minimum": 0, "description": "每条路径至少保留几份较旧的变更层，默认 20"},
            "maximum": {"type": "integer", "minimum": 1, "description": "每条路径最多保留几份变更层，默认 50"},
        }, ["root"]),
        "outputSchema": _schema({"converted": {"type": "integer", "description": "本次真的删掉的 payload 数"}, "eligible": {"type": "integer", "description": "通过了保护检查、可以考虑回收的数量"}, "already_gone": {"type": "integer", "description": "已标为不可回退但 payload 本来就不在的数量"}, "bytes_freed": {"type": "integer", "description": "释放的字节数"}, "keep_seconds": {"type": "integer"}, "minimum": {"type": "integer"}, "maximum": {"type": "integer"}}, ["converted", "eligible", "already_gone", "bytes_freed", "keep_seconds", "minimum", "maximum"]),
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


def main():
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
    main()
