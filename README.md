# Task Checkpoint MCP

给长任务做“做一步、存一步”的存档：中断后新会话能接着做，做错了能退回任意一步。

一个 stdio MCP 服务器，**只用 Python 标准库**，不装任何第三方包。

## 为什么不直接用 `git stash` 或随手 commit 一下

| 做法 | 缺什么 |
|---|---|
| `git stash` | 一次性的，不能命名、不能跨会话交接，也没有“这一步在做什么、结论是什么” |
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
- **敏感文件识别是路径黑名单，盖不全。** 别把凭据放在被扫路径里 —— 详见「使用限制」。

## 安装

### 方式一：装成命令（推荐）

```bash
pipx install "git+https://github.com/qddfxp/task-checkpoint-mcp"
# 或
pip install "git+https://github.com/qddfxp/task-checkpoint-mcp"
```

装完在客户端里用 `tc-mcp` 启动：

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

**最重要的一条**：文件变化由后台线程自动记录，但“这一步在做什么、结论是什么、下一步干什么”只有模型主动调 `tc_save` 才会留下。

所以要让 Agent 每完成一步就存一步 —— 这份约束写在 `SKILL.md` 里，得把它装进 Agent 的技能目录（见下面「把 SKILL.md 装进 Agent 的技能目录」）。不装它，文件回退点照常产生，但没人知道当初要干什么、下一步该干什么。

典型流程：

```
tc_init    开任务
tc_save    每完成一步存一次（带 conclusion 和 next）
tc_resume  新会话开头先调这个接上进度
tc_restore 退回去（先 apply:false 预览，再 apply:true）
```

### 工具

9 个。第一个参数都是 `root`，指工作区目录（通常是当前项目的绝对路径）。

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
| `tc_compress` | 存档太大了，回收旧变更层空间 | `root` |

`tc_save` 的完整参数：

- `title`（必填）——这一步一句话标题，写“做了什么”，不写“改了什么”
- `conclusion`——这一步的结论。**最有价值的字段**
- `next`——下一步要干什么。**接续工作的关键**
- `description`——为什么这么做（决策理由，事后没人记得）
- `verified`——已验证项列表（跑了什么测试、确认了什么事实）
- `open_questions`——还没解决、留给后面的问题
- `close: true`——收尾时用。关闭后不能再存档，但还能读

回退操作本身也会被记成一条变更记录，返回里的 `recovered` 就是它的 `drift_id` —— 退错了可以再用它退回来。

## 把 SKILL.md 装进 Agent 的技能目录

`SKILL.md` 是这个项目的另一半功能，不是可选文档：**MCP 服务器负责存取，`SKILL.md` 负责让模型每完成一步真的去调 `tc_save`。**

复制到客户端扫描的技能目录，目录名即技能名：

| 客户端 | 放这里 |
|---|---|
| Claude Code | `~/.claude/skills/task-checkpoint/SKILL.md` |
| 多客户端共用的 agents 目录 | `~/.agents/skills/task-checkpoint/SKILL.md` |
| ZCode | `~/.zcode/skills/task-checkpoint/SKILL.md` |

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

最后一行是 `OK` 就对了。测试同样只用标准库（`unittest`），不需要 pytest。

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
| `TC_GIT_VERIFY` | `on` | 建引用前后对比 git 指纹，自证“没动过用户 git”。设成 `off` 跳过自证，每次 `tc_save` 少几趟 git 子进程；跳过后返回的 `git.unchanged` 是 `null` |
| `TC_SHA_REUSE` | `off` | mtime+size 没变的文件不再重读、直接复用索引里的 sha。风险见「使用限制」 |

## 使用限制

### 敏感文件：黑名单，盖不全

敏感文件**不存内容、连哈希都不存**，只能看出 mtime 和 size 变了没有。判定按路径匹配，所以两头都会漏：

- **漏判**：名字无辜的凭据抓不到。`config/database.yaml`、`docker-compose.yml`、`config.json` 会被当普通文件明文存进 `payload/`。**别把凭据放在被扫路径里**，或者用 `state.exclude` 自己加。
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

旧变更层的内容会一直留着，用 `tc_compress` 手动回收。它的默认参数是**保留 7 天**，所以小工作区上默认调用往往是 `converted: 0`（什么都没回收）—— 返回值里带 `keep_seconds` / `minimum` / `maximum` / `eligible` / `bytes_freed`，看得出“为什么没回收”。想真的清就用小一点的 `keep_seconds`（步骤基线和 `recovered` 层永远不动）。

### `TC_SHA_REUSE=on` 的风险（默认关的原因）

任务层默认每次都按内容算 sha，不信任 mtime，所以一次 save 会读完工作区里所有非敏感文件（不保留内容，内存占用与工作区大小无关）。

开了 `TC_SHA_REUSE=on` 之后，mtime+size 没变的文件不再重读、直接复用索引里的 sha。遇到 mtime 被还原（`os.utime`）、文件系统 mtime 粒度粗（FAT、部分网络盘）、或有工具保留 mtime（`cp -p`、`rsync -t`、`tar -x`、部分生成器）时，内容改动会被**完全漏掉**：不读盘、不存新内容，却把旧 sha 记进步骤，回退时静默给出旧内容。默认关闭时这条路径不存在。

`tc_resume` 和 `tc_show` 无论哪种模式都不读文件内容（只看 mtime+size），因此也可能漏掉“保留 mtime 的改动”。

### 参数与状态约束

- `tc_restore` 必须且只能传 `index` 或 `drift_id` 其中一个；`apply=true` 不允许修改已关闭任务。
- `tc_save` 传了 `task_id`（而且不是当前活动任务）时，会**先把活动任务切过去**（原任务置为 suspended）再存这一步；返回值里的 `active_task_changed: true` 就是告诉你这件事发生了，想切回去用 `tc_switch`。
- `tc_save` 在已有基线且检测到未落档变化时，会先把该变化挂到本次 save 的 manifest 上，再写任务步骤。
- `tc_export` 的交接包导入会校验任务 id 和工作区路径，拒绝重复任务、`..` 穿越与符号链接目标；导入失败会撤销已写入的目标文件。
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
├── scripts/
│   ├── tc.py        业务核心
│   └── tc_mcp.py    stdio MCP 适配层
├── tests/
│   └── test_tc.py   回归测试，只用 unittest
└── dist/            构建产物（已 gitignore）；正式发行版在 GitHub Releases
```
