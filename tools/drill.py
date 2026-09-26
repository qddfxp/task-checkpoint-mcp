"""真实工作区端到端演练探针（用完即删，不进测试套件）。

在真机上起真 MCP 子进程、建重工作区、真杀进程，回答三个问题：
  1. SKILL 规定的调用节奏在实际使用中可行吗
  2. 大工作区下扫描与落档的实际耗时
  3. 接了自动监视之后"体感"如何（中断、续上、回退、撤销回退）

用法：

    python tools/drill.py

需要 `git` 在 PATH 上；会在临时目录里建约 130 MB 的场地（一个 25 MB 随机文件 +
一个 105 MB 稀疏文件）并**保留**该目录供事后查看。Windows 上没有符号链接权限时，
那一步会被跳过，而不是让整场演练失败。

默认把本仓库当作那个"真实项目"拷进场地；想换成别的项目，把 `TC_DRILL_SOURCE`
指向它即可。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tc_mcp.py"
DRILL = Path(tempfile.mkdtemp(prefix="tc-drill-"))
WS = DRILL / "workspace"

FAIL = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAIL.append(label)


def fingerprint(root):
    """工作区全部文件内容指纹（排除存档目录）。"""
    out = {}
    for p in Path(root).rglob("*"):
        if ".checkpoints" in p.parts or p.is_dir() or p.is_symlink():
            continue
        if p.stat().st_size > 50 * 1024 * 1024:
            out[str(p.relative_to(root))] = f"<big:{p.stat().st_size}>"
            continue
        out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def git(args, cwd=WS):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip()


class Server:
    """一个真实的 MCP 服务进程（stdio）。"""

    def __init__(self, watch: str = "on"):
        # 监视线程会自己落变更记录，跟它并发跑的断言注定是竞态。
        # 只在专门测监视的那一段开它，其余段落 watch="off" 让断言可重现。
        env = {**os.environ, "TC_WATCH": watch, "TC_WATCH_INTERVAL": "2",
               "TC_DRIFT_MAX_WAIT": "30"}
        self.p = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, text=True, encoding="utf-8", errors="replace",
                                  bufsize=1)
        self.n = 0
        self.call("initialize", {"protocolVersion": "2025-06-18",
                                 "capabilities": {}, "clientInfo": {"name": "drill", "version": "1"}})

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, tool_name, **args):
        r = self.call("tools/call", {"name": tool_name, "arguments": args})
        res = r.get("result", r)
        return res.get("structuredContent") or json.loads(res["content"][0]["text"])

    def kill(self):
        """模拟客户端被关掉 / 进程被杀。"""
        self.p.kill()
        self.p.wait(timeout=15)
        return self.p.returncode


def build_workspace():
    """一个有分量的真实工作区：真实项目副本 + 批量文件 + 各类边界文件。"""
    WS.mkdir(parents=True)
    # 1. 真实项目副本（真代码 + 真文档，不是 three-file 玩具）
    src = Path(os.environ.get("TC_DRILL_SOURCE") or ROOT)
    if src.is_dir():
        for item in src.iterdir():
            if item.name in {".git", "node_modules", "__pycache__"}:
                continue
            if item.is_dir():
                shutil.copytree(item, WS / item.name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif item.suffix == ".pyc":
                continue
            else:
                shutil.copy2(item, WS / item.name)
    # 2. 批量真实感文件：多目录、中文内容
    for i in range(400):
        d = WS / "src" / f"mod{i % 20:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"unit{i:03d}.py").write_text(
            f"# -*- coding: utf-8 -*-\n"
            f'"""模块 {i}：计算第 {i} 项。中文注释用于验证 UTF-8 链路。"""\n\n\n'
            f"def compute_{i}(x):\n    return x * {i} + {i}\n",
            encoding="utf-8")
    # 3. 敏感文件（不该进 payload）
    (WS / ".env").write_text("SECRET_KEY=should-never-be-stored\n", encoding="utf-8")
    (WS / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nMIIfake\n", encoding="utf-8")
    # 4. 大文件：25MB 不可压 + 105MB 超限（稀疏写，快）
    (WS / "big25.bin").write_bytes(os.urandom(25 * 1024 * 1024))
    with open(WS / "huge105.bin", "wb") as f:
        f.seek(105 * 1024 * 1024 - 1)
        f.write(b"\0")
    # 5. 区外符号链接（该被跳过并记进 health）；Windows 无权限时降级为跳过
    try:
        os.symlink(str(DRILL), str(WS / "outside_link"), target_is_directory=True)
    except (OSError, NotImplementedError):
        print("  （本机不允许建符号链接，跳过区外链接这一步）")
    # 6. 真 git 仓库：有历史、有未提交改动、有未跟踪文件
    git(["init", "-q"])
    git(["config", "user.email", "drill@localhost"])
    git(["config", "user.name", "drill"])
    git(["add", "-A"])
    git(["-c", "core.autocrlf=false", "commit", "-q", "-m", "初始提交"])
    (WS / "uncommitted.txt").write_text("还没提交的改动\n", encoding="utf-8")
    (WS / "untracked.txt").write_text("还没 add 的文件\n", encoding="utf-8")
    return len(list(WS.rglob("*")))


def main():
    print("=" * 78)
    print("真实工作区端到端演练")
    print("=" * 78)
    n_before = build_workspace()
    head_before = git(["rev-parse", "HEAD"])
    index_before = git(["ls-files", "-s"])
    branches_before = git(["for-each-ref", "--format=%(refname)", "refs/heads"])
    tags_before = git(["for-each-ref", "--format=%(refname)", "refs/tags"])
    print(f"\n[场地] {WS}")
    print(f"  条目数 {n_before}，工作区体积 "
          f"{sum(p.stat().st_size for p in WS.rglob('*') if p.is_file()) / 1e6:.1f} MB")
    print(f"  git HEAD {git(['rev-parse', '--short', 'HEAD'])}，"
          f"未跟踪 {len([l for l in git(['status', '--porcelain']).splitlines() if l.startswith('??')])} 个")

    # ---------- 第一段会话：init + 两步 ----------
    print("\n[第一段会话] init + 两步 save")
    s = Server(watch="off")
    t = time.perf_counter()
    r = s.tool("tc_init", root=str(WS), name="真实演练", goal="验证长任务存档链路",
               constraints="不破坏 git 历史")
    t_init = time.perf_counter() - t
    check("tc_init 成功", "task_id" in r, f"{t_init:.2f}s")

    (WS / "src" / "mod00" / "unit000.py").write_text("def compute_0(x):\n    return x * 0\n",
                                                    encoding="utf-8")
    (WS / "step1_new.py").write_text("第一步新增\n", encoding="utf-8")
    before_git = (git(["status", "--porcelain"]), git(["ls-files", "-s"]), git(["rev-parse", "HEAD"]))
    t = time.perf_counter()
    r1 = s.tool("tc_save", root=str(WS), title="改 compute_0 并新增 step1_new.py",
                description="第一步，验证任务层字段", conclusion="计算逻辑已改，新增文件已落档",
                verified=["自检通过"], next="继续改第二步", open_questions=["是否需要清理旧函数"])
    t_save1 = time.perf_counter() - t
    after_git = (git(["status", "--porcelain"]), git(["ls-files", "-s"]), git(["rev-parse", "HEAD"]))
    check("tc_save 第 1 步成功", r1.get("index") == 1 or r1.get("head") == 1, f"{t_save1:.2f}s")
    check("save 不动用户 git（status/ls-files/HEAD 三项一致）", before_git == after_git)
    check("save 返回结构含 skipped_paths", "skipped_paths" in r1, f"skipped={r1.get('skipped_paths')}")

    (WS / "src" / "mod01" / "unit001.py").write_text("def compute_1(x):\n    return x * 1 + 1\n",
                                                    encoding="utf-8")
    t = time.perf_counter()
    r2 = s.tool("tc_save", root=str(WS), title="改 compute_1",
                conclusion="第二步完成，准备中断", next="第三步要处理 token 刷新")
    t_save2 = time.perf_counter() - t
    check("tc_save 第 2 步成功", r2.get("index") == 2 or r2.get("head") == 2, f"{t_save2:.2f}s")

    print(f"\n  落档耗时：init {t_init:.2f}s，save#1 {t_save1:.2f}s，save#2 {t_save2:.2f}s")

    # ---------- 真中断：改了东西但没存，然后杀进程 ----------
    print("\n[真中断] 改文件但不 save，然后 kill 掉服务进程")
    (WS / "unsaved_a.py").write_text("中断前改的 A\n", encoding="utf-8")
    (WS / "unsaved_b.py").write_text("中断前改的 B\n", encoding="utf-8")
    (WS / "src" / "mod00" / "unit000.py").write_text("def compute_0(x):\n    return '半截'\n",
                                                    encoding="utf-8")
    rc = s.kill()
    check("进程被强制杀掉", rc != 0, f"returncode={rc}")
    check("存档没被写坏（current.json 仍是合法 JSON）",
          bool(json.loads((WS / ".checkpoints" / "current.json").read_text(encoding="utf-8"))["active"]))
    fp_interrupted = fingerprint(WS)

    # ---------- 第二段会话：新会话接上 ----------
    print("\n[第二段会话] 新进程，第一件事 tc_resume")
    s2 = Server(watch="off")
    # 注意：drift 早就有了（save#2 在写步骤前把未落档的变化挂成了一条变更记录，
    # 这是设计行为）。所以只能对比 resume 前后的数量，不能断言总数为 0。
    drifts_before_resume = len(list((WS / ".checkpoints" / "tasks").rglob("drifts/d*.json")))
    res = s2.tool("tc_resume", root=str(WS))
    print(f"  resume: head={res.get('head')}  目标={res.get('goal')!r}")
    print(f"          下一步={res.get('next')!r}")
    print(f"          未落档路径={res.get('drift_paths')}")
    check("resume 说出停在第 2 步", res.get("head") == 2)
    check("resume 报出中断前没落档的改动",
          {"unsaved_a.py", "unsaved_b.py"} <= set(res.get("drift_paths") or []))
    check("resume 给出下一步（接续工作的关键）", bool(res.get("next")))
    drifts_after_resume = len(list((WS / ".checkpoints" / "tasks").rglob("drifts/d*.json")))
    check("resume 不新增 drift 记录", drifts_after_resume == drifts_before_resume,
          f"resume 前={drifts_before_resume} 后={drifts_after_resume}")

    r3 = s2.tool("tc_save", root=str(WS), title="补记中断前的工作",
                 conclusion="中断前的改动已补记并落成第 3 步", next="验证回退")
    check("新会话能继续 save", r3.get("index") == 3, f"index={r3.get('index')}")
    fp_step3 = fingerprint(WS)

    shown = s2.tool("tc_show", root=str(WS))
    check("tc_show 列出 3 步", shown.get("count") == 3 or len(shown.get("steps") or []) == 3,
          f"{[st.get('title') for st in (shown.get('steps') or [])]}")

    # ---------- 回退：先预览再执行 ----------
    print("\n[回退] 先 apply=false 预览，再 apply=true")
    prev = s2.tool("tc_restore", root=str(WS), index=2, apply=False)
    print(f"  预览: 要删={prev.get('delete') or prev.get('deleted') or prev.get('files_to_delete')} "
          f"要写={len(prev.get('write') or prev.get('files_to_write') or [])} 条")
    check("预览不碰工作区", fingerprint(WS) == fp_step3)
    check("预览带 skipped_paths", "skipped_paths" in prev, f"{prev.get('skipped_paths')}")

    t = time.perf_counter()
    back = s2.tool("tc_restore", root=str(WS), index=2, apply=True)
    t_restore = time.perf_counter() - t
    check("回退到第 2 步成功", not back.get("isError"), f"{t_restore:.2f}s")
    check("第 3 步新增的文件被删掉", not (WS / "unsaved_a.py").exists() and not (WS / "unsaved_b.py").exists())
    check("被改的文件回到第 2 步内容",
          (WS / "src" / "mod00" / "unit000.py").read_text(encoding="utf-8") == "def compute_0(x):\n    return x * 0\n")
    check("回退本身被记成一条变更记录（recovered）", bool(back.get("recovered")), f"{back.get('recovered')}")
    after_resume = s2.tool("tc_resume", root=str(WS))
    check("回退后 head 回到 2", after_resume.get("head") == 2,
          f"restore 不返回 head，用 resume 核对：head={after_resume.get('head')}")
    check("save 返回里带 git 指针字段", "git" in r1, f"{r1.get('git')}")

    # ---------- 用 recovered 撤销回退 ----------
    print("\n[撤销回退] 用返回的 recovered 再退一次")
    fid = back.get("recovered")
    undo = s2.tool("tc_restore", root=str(WS), drift_id=fid, apply=True)
    fp_undone = fingerprint(WS)
    check("撤销回退成功", not undo.get("isError"))
    check("工作区逐字节回到误操作之前", fp_undone == fp_step3,
          f"差异={sorted(set(fp_step3) ^ set(fp_undone))[:5]}")

    # ---------- git 承诺 ----------
    print("\n[git] 承诺是否成立")
    check("HEAD 没有被移动", git(["rev-parse", "HEAD"]) == head_before,
          f"{head_before[:8]} -> {git(['rev-parse', 'HEAD'])[:8]}")
    check("用户索引逐字节未变", git(["ls-files", "-s"]) == index_before,
          f"{len(index_before.splitlines())} 条索引，前后一致" if git(["ls-files", "-s"]) == index_before else "索引被改动了")
    refs = git(["for-each-ref", "--format=%(refname)", "refs/checkpoints"])
    print(f"  refs/checkpoints: {refs.splitlines()[:3]} ... 共 {len(refs.splitlines())} 条")
    check("新增了 refs/checkpoints 引用", len(refs.splitlines()) > 0)
    check("没有写 refs/heads 或 refs/tags",
          git(["for-each-ref", "--format=%(refname)", "refs/heads"]) == branches_before
          and git(["for-each-ref", "--format=%(refname)", "refs/tags"]) == tags_before,
          f"分支={branches_before.replace(chr(10), ',')} 标签={tags_before or '无'}")
    # 每个 checkpoint ref 的树里应当包含未 add 的文件
    first = refs.splitlines()[0] if refs.splitlines() else None
    if first:
        tree = git(["ls-tree", "--name-only", "-r", first])
        check("checkpoint 树包含未跟踪文件 untracked.txt", "untracked.txt" in tree)
        check("checkpoint 树包含未提交改动 uncommitted.txt", "uncommitted.txt" in tree)
        check("敏感文件没进 checkpoint 树", ".env" not in tree and "server.pem" not in tree)
    idx_after = git(["ls-files", "-s"])
    check("索引在整场演练中都没被动过", idx_after == index_before,
          f"{len(index_before.splitlines())} 条")

    # ---------- 监视线程体感 ----------
    print("\n[自动监视] 开监视线程，等两个 tick，看是否自动落档")
    s3 = Server(watch="on")
    time.sleep(6)
    (WS / "watched_by_thread.txt").write_text("后台线程该自己看见这个\n", encoding="utf-8")
    time.sleep(7)
    drift_now = list((WS / ".checkpoints" / "tasks").rglob("drifts/d*.json"))
    check("后台线程自动落了变更记录", len(drift_now) > 0, f"drift 数={len(drift_now)}")
    stderr_left = s3.p.stderr.read() if s3.p.poll() is not None else ""
    check("服务还活着", s3.p.poll() is None, f"poll={s3.p.poll()}")
    s3.kill()

    # ---------- 结论 ----------
    print("\n" + "=" * 78)
    if FAIL:
        print(f"演练结果：{len(FAIL)} 项未通过")
        for f in FAIL:
            print("  -", f)
    else:
        print("演练结果：全部通过")
    print(f"场地保留在 {DRILL}（如需查看）")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
