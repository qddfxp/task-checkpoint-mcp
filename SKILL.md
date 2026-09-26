---
name: task-checkpoint
description: 长任务的步骤存档与回退。多步任务开工前用 tc_init 建任务，每完成一步用 tc_save 落一步（带结论和下一步），会话中断或换新会话时先用 tc_resume 接上进度，需要退回去时用 tc_restore（先预览再执行，误操作可用返回的 drift_id 撤销）。文件变化由后台线程自动记录，不需要模型调用；但"这一步在做什么、结论是什么、下一步干什么"只有模型主动调 tc_save 才会留下。适用场景：需要连续多步、可能被中断、需要能回退的编码或文档任务。
---

# Task Checkpoint

给长任务做"做一步、存一步"的存档。中断后新会话能接着做，做错了能退回任意一步。

## 铁律

这四条是使用这个服务器的最低要求。前三条不做，存档就是空壳。

1. **开长任务前先 `tc_init`。** 一个任务一个名。已有活动任务时，`tc_init` 会把它挂起而不是关闭，之后可以切回去。
2. **每完成一步就 `tc_save`，并且必须填 `conclusion` 和 `next`。** 这是最重要的一条。`tc_resume` 直接读这两个字段来回答"做到哪了、下一步干什么"。不填，新会话就只能看到一堆文件路径，接不上思路。
3. **新会话、或中断后继续时，第一件事是 `tc_resume`。** 不要从零开始猜进度。
4. **回退前先预览。** `tc_restore` 先带 `apply: false` 跑一次看清单，确认了再 `apply: true`。

## 为什么必须主动调 tc_save

这个服务器分两层，触发者不同：

| 层 | 记什么 | 谁触发 |
|---|---|---|
| 变更层 | 文件变了（内容副本） | **后台线程自动**，不需要模型做什么 |
| 任务层 | 这一步在做什么、结论、下一步 | **只有模型主动 `tc_save`** |

后台线程看得见磁盘，所以文件回退点会自动产生。但服务器看不到模型的思考和 Write/Edit 调用，永远不知道"刚做完一步"——那只能由模型声明。

所以：**只调文件工具不调 `tc_save`，等于只有尸体没有病历。** 文件能退回去，但没人知道当初要干什么、下一步该干什么。

## 工具

所有工具的第一个参数都是 `root`，指工作区目录（通常是当前项目的绝对路径）。

| 工具 | 什么时候用 | 必填 |
|---|---|---|
| `tc_init` | 长任务开工 | `root`, `name` |
| `tc_save` | **每完成一步** | `root`, `title` |
| `tc_resume` | **新会话开头**、接手别人中断的工作 | `root` |
| `tc_show` | 想看有哪些步骤 / 变更记录 | `root` |
| `tc_restore` | 退回某一步 | `root`, 加 `index` 或 `drift_id` |
| `tc_capture` | 想立刻记一次文件变化（不等后台） | `root` |
| `tc_switch` | 回到之前挂起的任务 | `root`, `task_id` |
| `tc_export` | 导出交接包给另一个目录 / 另一台机器 | `root`, `to` |
| `tc_compress` | 存档太大了，回收旧变更层空间 | `root` |

`tc_save` 的完整参数：

- `title`（必填）——这一步一句话标题，写"做了什么"，不写"改了什么"
- `conclusion`——这一步的结论。**最有价值的字段**
- `next`——下一步要干什么。**接续工作的关键**
- `description`——为什么这么做（决策理由，事后没人记得）
- `verified`——已验证项列表（跑了什么测试、确认了什么事实）
- `open_questions`——还没解决、留给后面的问题
- `close: true`——收尾时用。关闭后不能再存档，但还能读

## 典型流程

**开工**

```
tc_init(root="/abs/path/to/project", name="重构认证模块",
        goal="把 session 改成 JWT", constraints="不改公开 API")
```

**每做完一步**

```
tc_save(root="/abs/path/to/project",
        title="抽离 token 签发逻辑到 auth/token.py",
        description="原逻辑散在三个视图函数里，先集中再改",
        conclusion="签发已集中，旧调用点还在用旧路径，双轨并存",
        verified=["pytest tests/test_auth.py 12 passed"],
        next="把三个视图函数切到新入口，删旧函数",
        open_questions=["refresh token 是否也要一起迁"])
```

**被中断后（新会话）**

```
tc_resume(root="/abs/path/to/project")
```

它会给出：任务目标、当前第几步、最近几步的结论、下一步、以及工作区里**还没落档**的改动路径。先读它，再动手。

**做错了要退回去**

```
tc_restore(root="...", index=3, apply=false)   # 先看清单：要删什么、要写什么、要改什么
tc_restore(root="...", index=3, apply=true)    # 确认后执行
```

回退本身也会被记成一条变更记录，返回里的 `recovered` 就是它的 `drift_id`。**退错了可以再退回来**：

```
tc_restore(root="...", drift_id="<上一步返回的 recovered>", apply=true)
```

## 节奏建议

- **一步的粒度**：能独立说清"做完了、结论是 X"的单元。太细（每个文件一次）会淹没有效信息，太粗（整个任务一次）就失去了回退粒度。
- **中间大幅改动**：还没到一步的边界但改动很大时，可以主动 `tc_capture` 立刻落一次文件变化（后台线程本来也会落，主动调只是更快）。
- **收尾**：最后一步用 `close: true`，让任务变成 `closed`。

## 边界

- **不保存聊天记录**，只保存工作区文件 + 模型声明的步骤说明。
- 静默期最长约 60 秒：文件一直在写时，中间那条记录标 `partial: true`。
- 进程如果突然死掉，最后那段改动要等下次 `tc_save`/`tc_capture` 补记；在这之前它只出现在 `tc_resume` 的未落档路径里。
- **敏感文件**不存内容、连哈希都不存，只看得出 mtime 和 size 变了没有。判定看目录名和无歧义文件名（`.env`/`*.env`/`*.pem`/`*.key`/`*.p8`/`*_rsa`/`*_key`/`kubeconfig`/`*.tfstate`/`*.jwt`/`.npmrc`/`.pypirc`/`.pgpass`/`credentials*`/`secret*`/`token.json`/`auth.json`/`settings.py`/`wp-config.php`/.ssh/.aws/.kube/.secrets 等）；`*secret*`/`*password*`/`*credential*`/`*api_key*` 这类高歧义子串**只对配置类后缀**生效，所以 `src/password_policy.py`、`src/tokenizer.py` 不会被误拦。
  这是**黑名单，盖不全**（两头都会漏）：凭据别放在被扫路径里；名字无辜的凭据（如 `config/database.yaml`）根本抓不到。
- 变更层（`tc_capture` / 后台线程）用 **mtime+size** 判变化：内容改了但 mtime 与长度都没变时，它会漏记；任务层 `tc_save` 默认按内容算 sha，不受此限。
- 超过 100 MB 的文件只记元数据，不回退内容。
- 回退会还原权限位，但不还原 mtime；**只改权限、内容没变时不会触发重写**，那种情况权限也回不去。
- `tc_save` 带 `task_id` 指定别的任务时，会把活动任务切过去（原任务变 suspended）；返回值里 `active_task_changed: true` 就是提示这件事，想切回去用 `tc_switch`。
- 目标里不能还原的文件（敏感文件、>100 MB、payload 已丢）会被跳过，其余照退，跳过的列在 `plan.unrestorable` 里——所以一个 `.env` 不会让整个回退失效。
- 存档不会自动缩。旧变更层内容一直留着，需要时调 `tc_compress` 回收（被回收的变更层不能再回退，步骤基线不受影响）。它**默认保留 7 天**，所以不传参数常常是 `converted: 0`；返回值里的 `eligible`/`bytes_freed`/`keep_seconds` 说明为什么。
- 不做行级 diff 合并，不合并并发编辑。

## git 仓库里使用

- **不碰你的 git 历史**：不会 commit、不会 reset、不会 clean、不会动已有分支或 HEAD。
- 只额外新增 `refs/checkpoints/<任务>/s<步骤号>` 形式的引用，把每一步的工作区状态（含未 `git add` 的改动和未跟踪文件）固定成一个 git 对象。
- **非 git 目录完全可用**，功能一样，只是少了这层 git 引用。

## 接入客户端

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "C:/Path/To/python.exe",
      "args": ["E:/task-checkpoint-mcp/scripts/tc_mcp.py"]
    }
  }
}
```

装过之后（见 `README.md`）也可以直接用 `tc-mcp` 命令，或 `python -m tc_mcp`。

存档默认放在工作区的 `.checkpoints/` 目录里。想放到别处，在 `tc_init` 时用 `store` 参数指定。
