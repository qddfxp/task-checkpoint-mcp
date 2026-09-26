# Task Checkpoint MCP

[![PyPI](https://img.shields.io/pypi/v/task-checkpoint-mcp.svg)](https://pypi.org/project/task-checkpoint-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](https://modelcontextprotocol.io)

给长任务做"做一步、存一步"的存档：中断后新会话能接着做，做错了能退回任意一步。

一个 stdio MCP 服务器，**只用 Python 标准库**，不装任何第三方包。

## 快速开始

**一、装**

```bash
pipx install task-checkpoint-mcp
```

**二、在客户端里接上**（Claude Code、Codex、Cursor 等都是同一段配置）

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

**三、把 `SKILL.md` 装进技能目录**，不装的话 Agent 不会主动存档

```bash
mkdir -p ~/.claude/skills/task-checkpoint
curl -fsSL -o ~/.claude/skills/task-checkpoint/SKILL.md \
  https://raw.githubusercontent.com/qddfxp/task-checkpoint-mcp/main/SKILL.md
```

（技能目录按客户端而定，见下面"把 SKILL.md 装进 Agent 的技能目录"。如果 `raw.githubusercontent.com` 访问不了，先按下面"安装"里的方式二把仓库 clone 下来，再 `cp SKILL.md` 过去。）

**四、交给 Agent 用。** 一个任务就四步：

```
tc_init    开任务
tc_save    每完成一步存一次（带 conclusion 和 next）
tc_resume  新会话开头先调这个接上进度
tc_restore 退回去（先 apply:false 预览，再 apply:true）
```

中途被打断、隔天换个模型重开，新会话调一次 `tc_resume` 拿到的是这个：

```
任务：重构认证模块
目标：把 session 改成 JWT
当前步骤：3
下一步：把三个视图函数切到新入口，删旧函数
未落档：src/views/auth.py, src/views/user.py
```

`handoff` 字段就是这段文本，可以直接粘给新会话。它同时告诉你哪几个文件改了但还没落档。

## 为什么不直接用 `git stash` 或随手 commit 一下

| 做法 | 缺什么 |
|---|---|
| `git stash` | 一次性的，不能命名、不能跨会话交接，也没有"这一步在做什么、结论是什么" |
| 随手一个 commit | 会污染真实历史；半成品未必允许 commit；还得你记得先 commit |
| 手写进度笔记 | 和文件状态脱钩，回退时要自己对着时间线拼 |
| **本工具** | 文件变化后台自动记（变更层），步骤语义由模型声明（任务层）；只额外加 `refs/checkpoints/...`，不动你的 git 历史 |

值得装：多步、可能被中断、需要能退回的编码或文档任务。
不值得装：一次能做完、不需要回退也不需要交接的改动。

## 装之前先知道

- 需要 Python 3.10 或更新，只用标准库。
- **不碰你的 git 历史**：不 commit、不 reset、不 clean、不动已有分支和 HEAD，只额外加 `refs/checkpoints/...` 引用。
- **不是 git 仓库也能用**，功能一样，只是少了那层 git 引用。
- **不保存聊天记录**，只保存工作区文件和模型主动声明的步骤说明。
- 存档默认写在工作区的 `.checkpoints/`；指定外部 `store` 时，工作区仅留一个不含文件内容的 `.task-checkpoint-store.json` 路径指针。
- **敏感文件识别是路径黑名单，盖不全。** 别把凭据放在被扫路径里 —— 详见"使用限制"。

## 安装

### 方式一：装成命令（推荐）

```bash
pipx install task-checkpoint-mcp
# 或
pip install task-checkpoint-mcp
```

也可以直接从 git 装，跟着仓库走、不走 PyPI 的版本号：

```bash
pipx install "git+https://github.com/qddfxp/task-checkpoint-mcp"
```

装完在客户端里用 `tc-mcp` 启动。包里有两个等价的入口点：`tc-mcp` 和 `task-checkpoint-mcp`（后者是给 `uvx` 这类按包名找命令的 runner 用的）：

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

### 方式二：直接用源码（不用安装）

```bash
git clone https://github.com/qddfxp/task-checkpoint-mcp
```

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/task-checkpoint-mcp/scripts/tc_mcp.py"]
    }
  }
}
```

`command` 要写解释器的**绝对路径**，不要写 `python` —— 客户端不一定能解析到你要的那个。装过之后也可以直接 `python -m tc_mcp`。

## 怎么用

**最重要的一条**：文件变化由后台线程自动记录，但"这一步在做什么、结论是什么、下一步干什么"只有模型主动调 `tc_save` 才会留下。

所以要让 Agent 每完成一步就存一步 —— 这份约束写在 `SKILL.md` 里，得把它装进 Agent 的技能目录（见下面"把 SKILL.md 装进 Agent 的技能目录"）。不装它，文件回退点照常产生，但没人知道当初要干什么、下一步该干什么。

四条铁律：

1. 开长任务前先 `tc_init`，一个任务一个名。已有活动任务会被**挂起而不是关闭**，可以 `tc_switch` 切回去。
2. 每完成一步就 `tc_save`，并且**必须填 `conclusion` 和 `next`**。不填，新会话就只看得到一堆文件路径。
3. 新会话、或中断后继续时，**第一件事是 `tc_resume`**，不要从零猜进度。
4. **回退前先预览**：先 `apply: false` 看清单，确认了再 `apply: true`。

一步的粒度：能独立说清"做完了、结论是 X"的单元。太细（每个文件一次）会淹没有效信息，太粗（整个任务一次）就失去了回退的意义。

### 工具

10 个。第一个参数都是 `root`，指工作区目录（通常是当前项目的绝对路径）。

| 工具 | 什么时候用 | 必填 |
|---|---|---|
| `tc_init` | 长任务开工。已有活动任务会被挂起而不是关闭 | `root`, `name` |
| `tc_save` | **每完成一步** | `root`, `title` |
| `tc_resume` | **新会话开头**、接手别人中断的工作（只读） | `root` |
| `tc_show` | 想看有哪些步骤 / 变更记录 | `root` |
| `tc_restore` | 退回某一步或某条变更记录。先预览再执行 | `root`，加 `index` 或 `drift_id` |
| `tc_capture` | 想立刻记一次文件变化（不等后台线程） | `root` |
| `tc_switch` | 回到之前挂起的任务 | `root`, `task_id` |
| `tc_export` | 导出交接包给另一个目录 / 另一台机器 | `root`, `to` |
| `tc_import` | 导入别人的交接包，在这里接续任务 | `root`, `package` |
| `tc_compress` | 存档太大了，回收旧变更层空间 | `root` |

`tc_save` 的完整参数：

- `title`（必填）——这一步一句话标题，写"做了什么"，不写"改了什么"
- `conclusion`——这一步的结论。**最有价值的字段**
- `next`——下一步要干什么。**接续工作的关键**
- `description`——为什么这么做（决策理由，事后没人记得）
- `verified`——已验证项列表（跑了什么测试、确认了什么事实）
- `open_questions`——还没解决、留给后面的问题
- `close: true`——收尾时用。关闭后不能再存档，但还能读

回退操作本身也会被记成一条变更记录，返回里的 `recovered` 就是它的 `drift_id` —— 退错了可以再用它退回来。

### 返回值里几个值得看的字段

- `tc_resume.handoff` —— 一段现成的交接文本，可以直接粘给新会话；`drift_paths` 是工作区里**还没落档**的改动；`health.errors` 是存档自身的问题
- `tc_save.idempotent` —— 同一步（标题、文件摘要、结论、下一步、已验证项都一样）重复存，不会产生重复步骤
- `tc_save.active_task_changed` —— 传了别人的 `task_id` 时活动任务会被切过去（原任务变 suspended），这个字段就是在提示这件事
- `tc_restore.recovered` —— 回退本身也会被记成一条变更记录，这就是它的 `drift_id`
- `tc_restore.plan.unrestorable` / `skipped_paths` —— 目标里没能还原的文件；预览和执行给的是同一份清单
- `tc_capture.waiting` / `partial` —— 还在静默期，以及中间那条不完整的记录
- `tc_init.exclude` —— 回读你这次设进去的排除模式
- `tc_export.path` —— 交接包目录，里面有一个 `HANDOFF.md`，是给接手方的提示词

## 把 SKILL.md 装进 Agent 的技能目录

`SKILL.md` 是这个项目的另一半功能，不是可选文档：**MCP 服务器负责存取，`SKILL.md` 负责让模型每完成一步真的去调 `tc_save`。**

复制到客户端扫描的技能目录，目录名即技能名：

| 客户端 | 放这里 |
|---|---|
| Claude Code | `~/.claude/skills/task-checkpoint/SKILL.md` |
| Codex CLI | `~/.codex/skills/task-checkpoint/SKILL.md` |
| ZCode | `~/.zcode/skills/task-checkpoint/SKILL.md` |
| 跨客户端共用 | `~/.agents/skills/task-checkpoint/SKILL.md` |

`~/.agents/skills/` 是多个客户端共用的技能目录；客户端专属目录优先于它 —— 同名技能以客户端专属目录里的那份为准。OpenCode 等其它客户端放到它自己文档里的技能目录即可，规则不变。

```bash
mkdir -p ~/.claude/skills/task-checkpoint
cp SKILL.md ~/.claude/skills/task-checkpoint/SKILL.md
```

Windows 路径形如 `C:\Users\<你>\.claude\skills\task-checkpoint\SKILL.md`。各客户端的技能目录可能不同，以它自己的文档为准；要求只有一条：文件落在技能目录下的 `<技能名>/SKILL.md`。重启客户端后生效。

## 自检（可选）

在克隆下来的仓库里跑一遍回归测试：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

最后一行是 `OK` 就对了。117 个用例整批约 5 分钟 —— 本机 Windows 实测 340 秒，开销集中在 git 子进程（`refs/checkpoints/` 相关的用例占约一分钟）和静默期等待，所以磁盘和 git 的速度对总时长影响很大。全部只用标准库，不需要 pytest；在 Python 3.10 上也能跑，只有一条读 `pyproject.toml` 的打包契约检查会因为 `tomllib` 被跳过（3.11+ 全跑）。

想要真机端到端演练（起真 MCP 子进程、真杀进程、建约 190 MB 的重工作区、逐项核对"不动你 git"的承诺）：

```bash
python tools/drill.py
```

它跑完默认把场地删掉；想看场地就 `TC_DRILL_KEEP=1 python tools/drill.py`（演练有未通过项时也会自动保留，方便排查）。

确认服务器能起来 —— 会回一行带 `serverInfo` 的 JSON：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' | python scripts/tc_mcp.py
```

从 pip 装的版本没有 `scripts/`，用 `python -m tc_mcp` 代替。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TC_STORE` | `<工作区>/.checkpoints` | 存档目录 |
| `TC_WATCH` | `on` | 设成 `off` 关掉后台自动记录 |
| `TC_WATCH_INTERVAL` | `15` | 后台检查间隔（秒） |
| `TC_DRIFT_MAX_WAIT` | `60` | 静默期上限（秒） |
| `TC_GIT` | `on` | 设成 `off` 就完全不建 `refs/checkpoints/...` 引用 |
| `TC_GIT_VERIFY` | `on` | 建引用前后对比 git 指纹，自证"没动过用户 git"。设成 `off` 跳过自证，每次 `tc_save` 少几趟 git 子进程；跳过后返回的 `git.unchanged` 是 `null` |
| `TC_SHA_REUSE` | `off` | mtime+size 没变的文件不再重读、直接复用索引里的 sha。风险见"使用限制" |

## 使用限制

### 敏感文件：黑名单，盖不全

敏感文件**不存内容、连哈希都不存**，只能看出 mtime 和 size 变了没有。判定按路径匹配，所以两头都会漏：

- **漏判**：名字无辜的凭据抓不到。`config/database.yaml`、`docker-compose.yml`、`config.json` 会被当普通文件明文存进 `payload/`。**别把凭据放在被扫路径里**；避不开时用 `tc_init(exclude=[...])` 把整个目录排掉。
- **误拦**：被误判为敏感的**源码**永远拿不到内容副本，回退时会静默跳过（只出现在 `plan.unrestorable` 里）。名单分两层就是为压低误拦。

拦的范围：

- **目录名**（整个目录都不存）
  `.ssh` `.aws` `.gnupg` `.docker` `.kube` `.env` `.envdir` `.secrets` `.tokens`，或名字含 `secret` / `credential` / `token` 的目录。
- **无歧义文件名**
  - dotenv：`.env` `.env.*` `*.env` `*.env.*` `.envrc`
  - 私钥：`*.pem` `*.key` `*.p12` `*.pfx` `*.p8` `*.jks` `*.keystore` `*.kdbx` `*.ppk` `*.ovpn` `*_rsa` `*_dsa` `*_ed25519` `*_ecdsa` `*_key`
  - 凭据：`.netrc` `.git-credentials` `.npmrc` `.pypirc` `.pgpass` `.htpasswd` `.my.cnf` `my.cnf` `passwd` `shadow`
  - 云与基础设施：`kubeconfig` `*.kubeconfig` `*.tfstate` `terraform.tfvars` `*.tfvars` `*.jwt`
  - 通配：`credentials*` `creds*` `secret*` `secrets.json` `token` `token.json` `token.txt` `*.token` `*_token` `auth.json`
  - 框架配置：`settings.py` `local_settings.py` `settings.xml` `wp-config.php` `web.config` `wrangler.toml` `.dev.vars`
- **高歧义子串**，只在文件名以配置类后缀结尾时才拦
  - 子串：`*secret*` `*password*` `*passwd*` `*credential*` `*api_key*` `*apikey*` `*token*` `*auth_token*`
  - 后缀：`.json .yaml .yml .toml .ini .cfg .conf .config .properties .xml .txt .env .envrc .cnf .sh .ps1 .bat .cmd .sql .php`
  - 所以 `prod_credentials.json` 拦；`src/password_policy.py`、`src/tokenizer.py`、`docs/secrets.md` **不拦**。

`exclude` 的匹配方式和内置名单一致：按**单个名字段**做 glob（目录名，或路径的第一段）。所以 `exclude=["config"]` 会排掉任意层级的 `config/` 目录，但**不能只排某个具体文件**（要排 `config/database.yaml` 就得连 `config/` 一起排）。带 `/` 的模式会被直接拒绝，不会静默失效。

**这条能力不构成隐私保护或合规保证。** 存档目录的访问权限由你自己管；别把未排除的凭据、令牌、私钥、个人资料放进被扫路径。

### 时间与粒度

- 静默期最长约 60 秒。文件一直在写时，中间那条记录标 `partial: true`。
- 进程突然死掉后、下次 `tc_save` / `tc_capture` 之前，最后那段改动只在 `tc_resume` 的未落档路径里，还不是一条变更层记录。

### 变更层只看 mtime+size

`tc_capture` 和后台线程用 **mtime+size** 判断变化。内容改了但 mtime 和长度都没变时（`cp -p`、`rsync -t`、`tar -x` 会保留 mtime），它**不会觉得文件变了**。

任务层 `tc_save` 默认按内容算 sha，不受此限；但 `TC_SHA_REUSE=on` 会把任务层也拉到同一判据上 —— 见下面那条。

### 回退

- 会一并还原文件权限位（POSIX 可执行位），**不还原 mtime** —— 把旧时间戳写回去会让 make 类工具误以为文件没变。注意：只改权限、内容没变时不会触发重写（回退只重写内容不同的文件），那种情况下权限位也回不去。
- **不能还原的文件会被跳过**（敏感文件、超过 100 MB 未存内容、payload 已丢），其余照退；跳过的列在返回的 `plan.unrestorable` 里。预览和实际执行用同一份清单，所以一个 `.env` 不会让整次回退失效。
- 二进制能还原，但不能做行级 diff；不合并并发编辑。

### 大文件

不超过 100 MB 的文件原样按 sha 存进 `payload/`，相同内容只存一份；超过 100 MB 只记流式哈希和元数据，不保存内容，因而不能回退内容。

### 存档不会自动缩

旧变更层的内容会一直留着，用 `tc_compress` 手动回收。它的默认参数是**保留 7 天**，所以小工作区上默认调用往往是 `converted: 0`（什么都没回收）—— 返回值里带 `keep_seconds` / `minimum` / `maximum` / `eligible` / `bytes_freed`，看得出"为什么没回收"。想真的清就用小一点的 `keep_seconds`（步骤基线和 `recovered` 层永远不动）。

### `TC_SHA_REUSE=on` 的风险（默认关的原因）

任务层默认每次都按内容算 sha，不信任 mtime，所以一次 save 会读完工作区里所有非敏感文件（不保留内容，内存占用与工作区大小无关）。

开了 `TC_SHA_REUSE=on` 之后，mtime+size 没变的文件不再重读、直接复用索引里的 sha。遇到 mtime 被还原（`os.utime`）、文件系统 mtime 粒度粗（FAT、部分网络盘）、或有工具保留 mtime（`cp -p`、`rsync -t`、`tar -x`、部分生成器）时，内容改动会被**完全漏掉**：不读盘、不存新内容，却把旧 sha 记进步骤，回退时静默给出旧内容。默认关闭时这条路径不存在。

`tc_resume` 和 `tc_show` 无论哪种模式都不读文件内容（只看 mtime+size），因此也可能漏掉"保留 mtime 的改动"。

### 参数与状态约束

- `tc_restore` 必须且只能传 `index` 或 `drift_id` 其中一个；`apply=true` 不允许修改已关闭任务。
- `tc_save` 传了 `task_id`（而且不是当前活动任务）时，会**先把活动任务切过去**（原任务置为 suspended）再存这一步；返回值里的 `active_task_changed: true` 就是告诉你这件事发生了，想切回去用 `tc_switch`。
- `tc_save` 在已有基线且检测到未落档变化时，会先把该变化挂到本次 save 的 manifest 上，再写任务步骤。
- `tc_export` 要求目标父目录已存在；`tc_import` 会校验任务 id 和工作区路径，拒绝重复任务、`..` 穿越与符号链接目标；导入失败会撤销已写入的目标文件。
- `store` 不得等于工作区根目录；自定义 `store` 位于工作区内时会被扫描器自动排除。

### git 相关

在 git 仓库里每次 `tc_save` 会多花几百毫秒（建引用的 git 子进程开销）。不想付这笔开销就 `TC_GIT=off` 或 `TC_GIT_VERIFY=off`。

## 实现备注

只有改这份代码的人才需要关心：

- 归档目录里的 `.git/index` 字节数会被 git 自己刷新（`status` / `ls-files` 会更新 stat cache），但 HEAD、分支、暂存内容都不会变 —— 自证就是拿 `status` / `ls-files` 的输出前后对比的。
- `refs/checkpoints/` 下已有同名引用时拒绝覆盖。
- `tc_show` / `tc_resume` 只在读取状态快照时持有短锁；工作区扫描和文件读取不长期阻塞写入操作。
- manifest 是增量链，每 20 份强制一个全量锚点。

## 存储

```
<工作区>/.checkpoints/
├── current.json          活动任务指针，按工作区 root 分键（同一个 store 可服务多个工作区）
├── state.lock            并发锁
├── server.log            日志（不会写到 stdout，stdout 只走 MCP 帧）
├── payload/<sha前两位>/  内容寻址的文件副本（多个任务/步骤共享，按 sha 去重）
└── tasks/<task_id>/
    ├── state.json
    ├── index.json            基线索引：每个文件的 mtime/size/sha256
    ├── steps/sNNNN.json      任务层：每一步的说明
    ├── drifts/dNNNN.json     变更层：文件变化
    └── manifest/mNNNN.json   一致性清单（增量链，每 20 份一个全量锚点）
```

活动指针和基线索引都按范围隔离：同一个 `store` 被几个工作区共用时，互不串味。

## 目录结构

```
.
├── README.md        本文件
├── SKILL.md         给 Agent 看的使用约束（功能的一部分，不是可选文档）
├── LICENSE          MIT
├── pyproject.toml   打包配置，源码不改成包目录
├── MANIFEST.in      sdist 包含哪些文件
├── server.json      官方 MCP Registry 的发布元数据
├── scripts/
│   ├── tc.py        业务核心
│   └── tc_mcp.py    stdio MCP 适配层
├── tools/
│   └── drill.py     真机端到端演练探针（不进测试套件，手动跑）
└── tests/
    ├── test_tc.py                完整验收测试（回退安全、并发锁、git ref、返回字段与打包契约）
    └── test_tc_regressions.py    针对已修缺陷的定向回归
```

构建产物不进仓库。发布走两条：PyPI 上的 `task-checkpoint-mcp`（`pipx install task-checkpoint-mcp`）是主渠道，wheel 和 sdist 同时挂在 GitHub Releases 上。

---

# Task Checkpoint MCP (English)

Checkpoint and resume for long agent tasks: save each step as you go, pick the work back up in a new session, roll back to any earlier step.

A stdio MCP server written in **pure Python standard library** — no third-party packages. Python 3.10+.

## Quick start

**1. Install**

```bash
pipx install task-checkpoint-mcp
```

**2. Wire it into your client** (same block for Claude Code, Codex, Cursor and the rest)

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

**3. Install `SKILL.md` into the skill directory.** Without it the agent won't checkpoint on its own.

```bash
mkdir -p ~/.claude/skills/task-checkpoint
curl -fsSL -o ~/.claude/skills/task-checkpoint/SKILL.md \
  https://raw.githubusercontent.com/qddfxp/task-checkpoint-mcp/main/SKILL.md
```

(Skill directories differ per client — see "Install SKILL.md into the agent's skill directory" below.)

**4. Let the agent use it.** A task is four calls:

```
tc_init    start a task
tc_save    once per completed step (with conclusion and next)
tc_resume  first call in a new session, to pick up where you left off
tc_restore roll back (apply:false to preview, then apply:true)
```

Interrupted mid-task, resumed the next day on another model — one `tc_resume` in the new session gives you this:

```
任务：重构认证模块
目标：把 session 改成 JWT
当前步骤：3
下一步：把三个视图函数切到新入口，删旧函数
未落档：src/views/auth.py, src/views/user.py
```

That text is the `handoff` field; paste it straight into a new session. It also names the files that changed but haven't been recorded yet.

## Why not `git stash`, or just commit?

| Approach | What it's missing |
|---|---|
| `git stash` | One-shot: no name, no cross-session handoff, and nothing about *what this step was* or *what it concluded* |
| A quick commit | Pollutes real history; half-finished work isn't always committable; you have to remember to commit first |
| A hand-written progress note | Decoupled from file state — rolling back means reconciling it against a timeline by hand |
| **This tool** | A background thread records file changes (drift layer); the model declares step semantics (task layer). It only adds `refs/checkpoints/...` and never touches your git history |

Worth installing for multi-step coding or writing tasks that can get interrupted and need to be reversible. Not worth it for changes you'll finish in one sitting with no rollback or handoff need.

## Before you install

- Python 3.10 or newer, standard library only.
- **Your git history is untouched**: no commit, reset, clean, branch or HEAD changes. It only adds `refs/checkpoints/...` refs.
- **Works outside a git repo too** — same features, minus that ref layer.
- **No chat logs are stored** — only workspace files and the step notes the model declares.
- The store defaults to `<workspace>/.checkpoints/`. With an external `store`, the workspace keeps only a `.task-checkpoint-store.json` path pointer containing no file contents.
- **Sensitive-file detection is a path blacklist and does not cover everything.** Keep credentials out of scanned paths — see Limits.

## Install

### Option 1 — from PyPI (recommended)

```bash
pipx install task-checkpoint-mcp
# or
pip install task-checkpoint-mcp
```

Or install straight from git, tracking the repo and skipping PyPI versioning:

```bash
pipx install "git+https://github.com/qddfxp/task-checkpoint-mcp"
```

Then point your client at `tc-mcp`. The package ships two equivalent entry points — `tc-mcp` and `task-checkpoint-mcp` (the latter for runners like `uvx` that look up a command by package name):

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

### Option 2 — run from source (nothing to install)

```bash
git clone https://github.com/qddfxp/task-checkpoint-mcp
```

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/task-checkpoint-mcp/scripts/tc_mcp.py"]
    }
  }
}
```

`command` must be the **absolute path** to the interpreter, not bare `python` — the client may not resolve the one you meant. Once installed you can also use `python -m tc_mcp`.

## Usage

**The one rule that matters**: file changes are recorded automatically by the background thread, but "what this step was, what it concluded, what comes next" only exists if the model calls `tc_save`.

So the agent has to checkpoint after every completed step. That contract lives in `SKILL.md`, which you install into the agent's skill directory (see below). Without it you still get file rollback points, but nobody knows what you were doing or what's next.

Four rules:

1. Call `tc_init` before a long task, one name per task. An existing active task is **suspended, not closed**; use `tc_switch` to return to it.
2. Call `tc_save` after every completed step, and **always fill in `conclusion` and `next`**. Without them the next session sees a pile of file paths and nothing else.
3. In a new session, or after an interruption, **call `tc_resume` first**. Don't guess your progress from scratch.
4. **Preview before rolling back**: `apply: false` to see the list, then `apply: true`.

Step granularity: a unit you can describe as "done, and the conclusion is X". Too fine (one per file) drowns the signal; too coarse (one per task) loses the point of rolling back.

### Tools

Ten. Every one takes `root` first — the workspace directory (usually the absolute path of the project).

| Tool | When to use | Required |
|---|---|---|
| `tc_init` | Starting a long task. An existing active task gets suspended, not closed | `root`, `name` |
| `tc_save` | **After every completed step** | `root`, `title` |
| `tc_resume` | **Start of a new session**, or taking over interrupted work (read-only) | `root` |
| `tc_show` | Inspect steps / drift records | `root` |
| `tc_restore` | Roll back to a step or drift record. Preview before applying | `root`, plus `index` or `drift_id` |
| `tc_capture` | Force a file-change capture now, without waiting for the thread | `root` |
| `tc_switch` | Return to a previously suspended task | `root`, `task_id` |
| `tc_export` | Export a handoff package to another directory / machine | `root`, `to` |
| `tc_import` | Import someone's handoff package and continue here | `root`, `package` |
| `tc_compress` | Reclaim space from old drift layers | `root` |

`tc_save` fields:

- `title` (required) — one line about what you *did*, not what you changed
- `conclusion` — what this step concluded. **The most valuable field**
- `next` — what to do next. **What makes the work resumable**
- `description` — why you did it (the reasoning nobody remembers later)
- `verified` — what you actually ran and confirmed
- `open_questions` — unresolved questions to hand forward
- `close: true` — to finish a task. Closed tasks can still be read

A rollback is itself recorded as a drift entry; the returned `recovered` is its `drift_id`, so **a mistaken rollback can be rolled back**.

### Response fields worth reading

- `tc_resume.handoff` — ready-made handoff text you can paste straight into a new session; `drift_paths` are changes on disk **not yet recorded**; `health.errors` are problems with the store itself
- `tc_save.idempotent` — saving the same step again (same title, file digest, conclusion, next, verified) won't duplicate it
- `tc_save.active_task_changed` — passing another task's `task_id` switches the active task (the old one becomes suspended); this field tells you it happened
- `tc_restore.recovered` — the rollback's own `drift_id`
- `tc_restore.plan.unrestorable` / `skipped_paths` — files that couldn't be restored; preview and execution use the same list
- `tc_capture.waiting` / `partial` — still inside the quiet period, and the incomplete intermediate record
- `tc_init.exclude` — echoes back the exclusion patterns you set
- `tc_export.path` — the package directory, containing a `HANDOFF.md` prompt for whoever picks the work up

## Install SKILL.md into the agent's skill directory

`SKILL.md` is the other half of this project, not optional documentation: **the MCP server does the storing, `SKILL.md` is what makes the model actually call `tc_save` after each step.**

Copy it into a directory your client scans; the directory name is the skill name.

| Client | Path |
|---|---|
| Claude Code | `~/.claude/skills/task-checkpoint/SKILL.md` |
| Codex CLI | `~/.codex/skills/task-checkpoint/SKILL.md` |
| ZCode | `~/.zcode/skills/task-checkpoint/SKILL.md` |
| Shared across clients | `~/.agents/skills/task-checkpoint/SKILL.md` |

`~/.agents/skills/` is shared by several clients; a client-specific directory takes precedence over it when the same skill name exists in both. For OpenCode and others, use the skill directory documented by that client — the rule is unchanged.

```bash
mkdir -p ~/.claude/skills/task-checkpoint
cp SKILL.md ~/.claude/skills/task-checkpoint/SKILL.md
```

On Windows the path looks like `C:\Users\<you>\.claude\skills\task-checkpoint\SKILL.md`. Clients may scan different directories — check their docs. The only requirement is that the file lands at `<skill-dir>/<skill-name>/SKILL.md`. Restart the client for it to take effect.

## Verify (optional)

Run the regression suite inside a cloned repo:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

The last line should be `OK`. All 117 tests take about 5 minutes — 340 s measured on Windows, with the cost concentrated in git subprocesses (the `refs/checkpoints/` cases account for roughly a minute) and quiet-period waits, so disk and git speed matter a lot. Standard library only (`unittest`), no pytest. It runs on Python 3.10 too; the one packaging-contract check that reads `pyproject.toml` is skipped there because `tomllib` is 3.11+.

For an end-to-end drill on a real workspace (spawns a real MCP subprocess, kills it mid-flight, builds a ~190 MB heavy workspace, and verifies the "your git is untouched" promise item by item):

```bash
python tools/drill.py
```

It deletes the workspace when it finishes; use `TC_DRILL_KEEP=1 python tools/drill.py` to keep it (it's also kept automatically when the drill has failures, to help diagnose).

Confirm the server starts — it answers with one JSON line containing `serverInfo`:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' | python scripts/tc_mcp.py
```

A pip-installed copy has no `scripts/`; use `python -m tc_mcp` instead.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TC_STORE` | `<workspace>/.checkpoints` | Store directory |
| `TC_WATCH` | `on` | `off` disables the background recorder |
| `TC_WATCH_INTERVAL` | `15` | Background poll interval (seconds) |
| `TC_DRIFT_MAX_WAIT` | `60` | Quiet-period ceiling (seconds) |
| `TC_GIT` | `on` | `off` stops creating `refs/checkpoints/...` entirely |
| `TC_GIT_VERIFY` | `on` | Fingerprints git before and after to prove your repo was untouched. `off` saves a few git subprocesses per `tc_save`; `git.unchanged` then returns `null` |
| `TC_SHA_REUSE` | `off` | Reuse the indexed sha when mtime+size are unchanged, instead of re-reading. See Limits for the risk |

## Limits

### Sensitive files: a blacklist that doesn't cover everything

Sensitive files store **neither content nor hash** — only whether mtime and size changed. Detection matches on paths, so it fails in both directions:

- **False negatives**: credentials with innocent names are not caught. `config/database.yaml`, `docker-compose.yml`, `config.json` get stored as plaintext in `payload/`. **Keep credentials out of scanned paths**; if you can't, use `tc_init(exclude=[...])` to exclude whole directories.
- **False positives**: source files misclassified as sensitive never get a content copy, so a rollback silently skips them (they only show up in `plan.unrestorable`). That's why the list is two-tiered.

What's blocked:

- **Directory names** (the whole directory): `.ssh` `.aws` `.gnupg` `.docker` `.kube` `.env` `.envdir` `.secrets` `.tokens`, or any directory whose name contains `secret` / `credential` / `token`.
- **Unambiguous filenames**: `.env` `.env.*` `*.env` `*.envrc`; `*.pem` `*.key` `*.p12` `*.pfx` `*.p8` `*.jks` `*.keystore` `*.kdbx` `*.ppk` `*.ovpn` `*_rsa` `*_dsa` `*_ed25519` `*_ecdsa` `*_key`; `.netrc` `.git-credentials` `.npmrc` `.pypirc` `.pgpass` `.htpasswd` `.my.cnf` `my.cnf` `passwd` `shadow`; `kubeconfig` `*.kubeconfig` `*.tfstate` `terraform.tfvars` `*.tfvars` `*.jwt`; `credentials*` `creds*` `secret*` `secrets.json` `token` `token.json` `token.txt` `*.token` `*_token` `auth.json`; `settings.py` `local_settings.py` `settings.xml` `wp-config.php` `web.config` `wrangler.toml` `.dev.vars`.
- **Ambiguous substrings** (`*secret*` `*password*` `*passwd*` `*credential*` `*api_key*` `*apikey*` `*token*` `*auth_token*`): blocked only when the filename ends in a config-type suffix — `.json` `.yaml` `.yml` `.toml` `.ini` `.cfg` `.conf` `.config` `.properties` `.xml` `.txt` `.env` `.envrc` `.cnf` `.sh` `.ps1` `.bat` `.cmd` `.sql` `.php`. So `prod_credentials.json` is blocked, while `src/password_policy.py`, `src/tokenizer.py` and `docs/secrets.md` are not.

`exclude` patterns follow the same rule: a glob against a **single name segment** (a directory name, or the first segment of a path). `exclude=["config"]` excludes a `config/` directory at any depth, but you **cannot** exclude one specific file — excluding `config/database.yaml` means excluding `config/` entirely. Patterns containing `/` are rejected outright rather than silently ignored.

**This is not privacy protection or a compliance guarantee.** You manage the store's access permissions; don't leave unexcluded credentials, tokens, private keys or personal data inside scanned paths.

### Timing and granularity

- The quiet period is up to ~60 s. While a file keeps changing, the intermediate record is marked `partial: true`.
- If the process dies abruptly, the tail of your changes appears only in `tc_resume`'s not-yet-recorded paths until the next `tc_save` / `tc_capture`.

### The drift layer only looks at mtime+size

`tc_capture` and the background thread detect change by **mtime+size**. If content changed but mtime and length did not (`cp -p`, `rsync -t`, `tar -x` preserve mtime), it **won't see a change**.

`tc_save` hashes content by default and isn't affected — except under `TC_SHA_REUSE=on`, which pulls the task layer onto the same test.

### Rollback

- Restores permission bits (the POSIX executable bit) but **not mtime** — writing old timestamps back makes make-like tools think nothing changed. Note: a permissions-only change with identical content doesn't trigger a rewrite, so in that case the bits stay put.
- Files that **cannot be restored are skipped** (sensitive, >100 MB with no stored content, payload lost) while the rest proceed; skipped paths come back in `plan.unrestorable`. Preview and execution use the same list, so one `.env` doesn't fail a whole rollback.
- Binaries restore but get no line-level diff. Concurrent edits are not merged.

### Large files

Files up to 100 MB are stored verbatim in `payload/`, deduped by content. Above 100 MB only a streaming hash and metadata are kept — **no content, so no content rollback**.

### The store never shrinks on its own

Old drift content stays until you call `tc_compress`. Its default is **keep 7 days**, so on a small workspace a default call often reports `converted: 0` — `keep_seconds` / `minimum` / `maximum` / `eligible` / `bytes_freed` in the response explain why. Pass a smaller `keep_seconds` to actually reclaim. Step baselines and the `recovered` layer are never touched.

### Risk of `TC_SHA_REUSE=on` (why it defaults off)

By default the task layer hashes content on every save — it doesn't trust mtime — so one save reads every non-sensitive file in the workspace (no content is retained, so memory doesn't scale with workspace size).

With `TC_SHA_REUSE=on`, files whose mtime+size are unchanged are not re-read and the indexed sha is reused. When mtime has been restored (`os.utime`), when the filesystem's mtime granularity is coarse (FAT, some network drives), or when a tool preserves mtime (`cp -p`, `rsync -t`, `tar -x`, some generators), content edits are **missed entirely**: nothing is read, nothing new is stored, the stale sha lands in the step record, and rollback silently returns old content. That path does not exist with the default.

`tc_resume` and `tc_show` never read file contents in either mode (mtime+size only), so they can also miss mtime-preserving edits.

### Argument and state constraints

- `tc_restore` takes exactly one of `index` or `drift_id`; `apply=true` refuses to modify a closed task.
- Passing another task's `task_id` to `tc_save` **switches the active task first** (the old one becomes suspended); `active_task_changed: true` tells you so. Use `tc_switch` to go back.
- If a baseline exists and unrecorded changes are detected, `tc_save` attaches them to that save's manifest before writing the step.
- `tc_export` requires the destination parent directory to exist; `tc_import` validates task id and workspace path, rejecting duplicate tasks, `..` traversal and symlink targets, and rolls back written files on failure.
- `store` may not equal the workspace root; a custom `store` inside the workspace is excluded from scanning automatically.

### git

Inside a git repo each `tc_save` costs a few hundred extra milliseconds (git subprocesses to create the ref). Avoid it with `TC_GIT=off` or `TC_GIT_VERIFY=off`.

## Storage layout

```
<workspace>/.checkpoints/
├── current.json          active-task pointer, keyed by workspace root (one store can serve many)
├── state.lock            concurrency lock
├── server.log            logs (never stdout; stdout carries MCP frames only)
├── payload/<sha2>/       content-addressed file copies, deduped by sha across tasks
└── tasks/<task_id>/
    ├── state.json
    ├── index.json            baseline index: mtime/size/sha256 per file
    ├── steps/sNNNN.json      task layer: one record per step
    ├── drifts/dNNNN.json     drift layer: file changes
    └── manifest/mNNNN.json   consistency manifests (incremental chain, full anchor every 20)
```

The active pointer and baseline indexes are scoped per workspace: when several workspaces share one `store`, they don't leak into each other.

## Repository layout

```
.
├── README.md        this file
├── SKILL.md         the agent-facing usage contract (part of the feature)
├── LICENSE          MIT
├── pyproject.toml   packaging; sources stay in scripts/, not a package dir
├── MANIFEST.in      what goes into the sdist
├── server.json      publish metadata for the official MCP Registry
├── scripts/
│   ├── tc.py        core
│   └── tc_mcp.py    stdio MCP adapter
├── tools/
│   └── drill.py     end-to-end probe against a real workspace (not in the test suite, run by hand)
└── tests/
    ├── test_tc.py                full acceptance suite (rollback safety, concurrency lock, git refs, return fields, packaging contract)
    └── test_tc_regressions.py    targeted regressions for fixed defects
```

Build artifacts stay out of the repo. There are two release channels: `task-checkpoint-mcp` on PyPI (`pipx install task-checkpoint-mcp`) is the primary one, with the wheel and sdist also attached to GitHub Releases.

Implementation notes for people changing this code (`.git/index` byte counts, ref collision behaviour, locking strategy, manifest anchoring) are in the Chinese section above.

<!-- mcp-name: io.github.qddfxp/task-checkpoint-mcp -->
