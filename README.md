# Task Checkpoint MCP

给长任务做"做一步、存一步"的存档。中断后新会话能接着做，做错了能退回任意一步。

一个 stdio 的 MCP 服务器，**只用 Python 标准库**，不装任何第三方包。

## 装之前先知道

- 需要 Python 3.10 或更新。
- 它**不碰你的 git 历史**：不 commit、不 reset、不 clean、不动已有分支和 HEAD。只额外加 `refs/checkpoints/...` 形式的引用。
- **不是 git 仓库也能用**，功能一样，只是少了那层 git 引用。
- **不保存聊天记录**，只保存工作区文件和模型主动声明的步骤说明。
- 存档默认写在工作区的 `.checkpoints/`；指定外部 `store` 时，工作区仅留一个不含文件内容的 `.task-checkpoint-store.json` 路径指针。

## 安装

### 方式一：直接用源码（不用安装）

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

`python.exe` 要写绝对路径，不要写 `python`——客户端不一定能解析到你要的那个解释器。

### 方式二：pipx

```bash
pipx install "E:/task-checkpoint-mcp"
```

装完可以直接用 `tc-mcp`：

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "tc-mcp",
      "args": []
    }
  }
}
```

### 方式三：装进当前环境

```bash
pip install "E:/task-checkpoint-mcp"
```

也可以直接用模块方式跑：

```bash
python -m tc_mcp
```

安装后也可以用 `tc-mcp` 入口启动。构建发布前应在干净环境中分别安装 wheel 和 sdist，并运行上面的 MCP 自检。

## 自检

```bash
cd E:/task-checkpoint-mcp
python -m unittest discover -s tests -p "test_*.py" -v
```

当前目录包含 `scripts/` 源码和 `tests/` 回归测试，也可以直接从目录安装：

```bash
python -m pip install .
# 或
pipx install .
```

测试套件本身需要 Python 3.11+（用了 `tomllib`）；**服务器本体只需要 3.10+**。

预期最后一行是 `OK`。测试同样只用标准库（`unittest`），不需要 pytest。

手动确认服务器能起来：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' | python E:/task-checkpoint-mcp/scripts/tc_mcp.py
```

## 怎么用

**最重要的一条**：文件变化由后台线程自动记录，但"这一步在做什么、结论是什么、下一步干什么"只有模型主动调 `tc_save` 才会留下。所以要让 Agent 每完成一步就存一步——这份约束写在 `SKILL.md` 里，把它装进 Agent 的技能目录即可。

典型流程：

```
tc_init    开任务
tc_save    每完成一步存一次（带 conclusion 和 next）
tc_resume  新会话开头先调这个接上进度
tc_restore 退回去（先 apply:false 预览，再 apply:true）
```

## 工具

9 个。第一个参数都是 `root`，指工作区目录。

| 工具 | 用途 |
|---|---|
| `tc_init` | 建任务。已有活动任务会被挂起而不是关闭 |
| `tc_save` | 存一步（标题、结论、下一步、已验证项、未决问题） |
| `tc_capture` | 立刻记一次文件变化（不等后台线程） |
| `tc_show` | 列出各步骤，可选带出变更记录 |
| `tc_restore` | 退回某一步或某条变更记录。先预览再执行 |
| `tc_resume` | 只读：目标、当前进度、未落档路径、下一步 |
| `tc_switch` | 切回之前挂起的任务 |
| `tc_export` | 导出交接包，给别的目录或别的机器 |
| `tc_compress` | 回收存档空间：删掉过期变更层内容，步骤基线不动 |

回退操作本身也会被记成一条变更记录，返回里的 `recovered` 就是它的 `drift_id`——退错了可以再用它退回来。

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

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TC_STORE` | `<工作区>/.checkpoints` | 存档目录 |
| `TC_WATCH` | `on` | 设成 `off` 关掉后台自动记录 |
| `TC_WATCH_INTERVAL` | `15` | 后台检查间隔（秒） |
| `TC_DRIFT_MAX_WAIT` | `60` | 静默期上限（秒） |
| `TC_GIT` | `on` | 设成 `off` 就完全不建 `refs/checkpoints/...` 引用 |
| `TC_GIT_VERIFY` | `on` | 建引用前后对比 git 指纹以自证“没动过用户 git”。设成 `off` 跳过自证，每次 `tc_save` 少几趟 git 子进程；跳过后返回的 `git.unchanged` 是 `null` |
| `TC_SHA_REUSE` | `off` | 设成 `on` 后，mtime+size 没变的文件不再重读、直接复用索引里的 sha。只在你确认工作区不会出现“保留 mtime 的改动”时开（见下面“已知边界”里的提醒） |

## 已知边界

- 进程突然死掉后、下次 `tc_save`/`tc_capture` 之前，最后那段改动只在 `tc_resume` 的未落档路径里，还不是一条变更层记录。
- 静默期最长约 60 秒。文件一直在写时，中间那条记录标 `partial: true`。
- **敏感文件**不存内容，**连哈希都不存**，只能看出 mtime 和 size 变了没有。
  这是**黑名单，盖不全**，两头都有坑：
  - **漏判**：名字无辜的凭据按名字根本抓不到（`config/database.yaml`、`docker-compose.yml`、`config.json`），会被当普通文件明文存进 `payload/`。别把凭据放在被扫路径里，或者用 `state.exclude` 自己加。
  - **误拦**：被误判为敏感的**源码**永远拿不到内容副本，所以回退时会静默跳过（只出现在 `plan.unrestorable` 里）。名单就是为此分两层设计的。

  拦的范围：
  - **目录名**：`.ssh` `.aws` `.gnupg` `.docker` `.kube` `.env` `.envdir` `.secrets` `.tokens`，或名字带 `secret`/`credential`/`token` 的目录（目录下全部内容）。
  - **无歧义文件名**：`.env` `.env.*` `*.env` `*.env.*` `.envrc`；`*.pem` `*.key` `*.p12` `*.pfx` `*.p8` `*.jks` `*.keystore` `*.kdbx` `*.ppk` `*.ovpn` `*_rsa` `*_dsa` `*_ed25519` `*_ecdsa` `*_key`；`.netrc` `.git-credentials` `.npmrc` `.pypirc` `.pgpass` `.htpasswd` `.my.cnf` `my.cnf` `passwd` `shadow`；`kubeconfig` `*.kubeconfig` `*.tfstate` `terraform.tfvars` `*.tfvars` `*.jwt`；`credentials*` `creds*` `secret*` `secrets.json` `token` `token.json` `token.txt` `*.token` `*_token` `auth.json`；`settings.py` `local_settings.py` `settings.xml` `wp-config.php` `web.config` `wrangler.toml` `.dev.vars`。
  - **高歧义子串**（`*secret*` `*password*` `*passwd*` `*credential*` `*api_key*` `*apikey*` `*token*` `*auth_token*`）：只在文件名以**配置类后缀**结尾时才拦（`.json/.yaml/.yml/.toml/.ini/.cfg/.conf/.config/.properties/.xml/.txt/.env/.envrc/.cnf/.sh/.ps1/.bat/.cmd/.sql/.php`）。所以 `prod_credentials.json` 拦，`src/password_policy.py`、`src/tokenizer.py`、`docs/secrets.md` **不拦**。
- 变更层（`tc_capture` 与后台自动记录）用的是 **mtime+size 判据**：内容改了但 mtime 和长度都没变时（有工具会保留 mtime：`cp -p`、`rsync -t`、`tar -x`），它**不会觉得文件变了**。任务层 `tc_save` 默认按内容算 sha，不受此限。
- 回退会一并还原文件权限位（POSIX 上的可执行位）；**不还原 mtime**——把旧时间戳写回去会让 make 类工具误以为文件没变。注意：**只改权限、内容没变时不会触发重写**（回退只重写内容不同的文件），那种情况下权限位也回不去。
- 目标里**不能还原的文件会被跳过**（敏感文件、超过 100 MB 未存内容、payload 已丢），其余照退；跳过的会列在返回的 `plan.unrestorable` 里。预览和实际执行用的是同一份清单。
- 自定义 `store` 如果位于工作区内，会被扫描器自动排除；`store` 不得等于工作区根目录。
- `tc_restore(apply=true)` 不允许修改已关闭任务；`tc_restore` 必须且只能传 `index` 或 `drift_id` 其中一个。
- `tc_export` 的交接包导入会校验任务 id 和工作区路径，拒绝重复任务、`..` 穿越与符号链接目标；导入失败会撤销已写入的目标文件。
- `tc_save` 在已有基线且检测到未落档变化时，会先把该变化挂到本次 save 的 manifest 上，再写任务步骤。
- `tc_save` 传了 `task_id`（而且不是当前活动任务）时，它会**先把活动任务切过去**（原任务置为 suspended）再存这一步；返回值里的 `active_task_changed: true` 就是告诉你这件事发生了，想切回去用 `tc_switch`。
- `tc_show` / `tc_resume` 只在读取状态快照时持有短锁，工作区扫描和文件读取不长期阻塞写入操作。
- 不超过 100 MB 的文件原样按 sha 存进 `payload/`，相同内容只存一份；超过 100 MB 只记流式哈希和元数据，不保存内容，因而不能回退内容。
- 任务层 `tc_save` 默认每次都按内容算 sha（不信任 mtime），所以内存占用跟工作区大小无关（不保留内容），但一次 save 会读完工作区里所有非敏感文件。工作区很大、又确定不会有“保留 mtime 的改动”时，可以开 `TC_SHA_REUSE=on` 只重读 mtime+size 变过的文件。`tc_resume` 和 `tc_show` 无论哪种模式都不读文件内容（它们只看 mtime+size，因此也可能漏掉“保留 mtime 的改动”，见上面变更层那条）。
- **`TC_SHA_REUSE=on` 的风险**（默认关的原因）：只看 mtime+size 判变化，遇到 mtime 被还原（`os.utime`）、文件系统 mtime 粒度粗（FAT、部分网络盘）、或有工具保留 mtime（`cp -p`、`rsync -t`、`tar -x`、部分生成器）时，内容改动会被完全漏掉：不读盘、不存新内容，却把旧 sha 记进步骤，回退时静默给出旧内容。默认关闭时这条路径不存在。
- 存档不会自动缩：旧变更层的内容会一直留着，用 `tc_compress` 手动回收。它的默认参数是**保留 7 天**，所以小工作区上默认调用往往是 `converted: 0`（什么都没回收）——返回值里带 `keep_seconds`/`minimum`/`maximum`/`eligible`/`bytes_freed`，看得出“为什么没回收”。想真的清就用小一点的 `keep_seconds`（步骤基线和 `recovered` 层永远不动）。
- 归档目录里的 `.git/index` 字节数会被 git 自己刷新（`status`/`ls-files` 会更新 stat cache），但 HEAD、分支、暂存内容都不会变——自证就是拿 `status`/`ls-files` 的输出前后对比的。
- 二进制能还原，不能做行级 diff。
- 不合并并发编辑。
- `refs/checkpoints/` 下已有同名引用时拒绝覆盖。
- 在 git 仓库里每次 `tc_save` 会多花几百毫秒（建引用的 git 子进程开销），不想付这笔开销就 `TC_GIT=off` 或 `TC_GIT_VERIFY=off`。

## 目录结构

```
E:/task-checkpoint-mcp/
├── SKILL.md          给 Agent 看的使用约束（这是功能的一部分，不是可选文档）
├── README.md         本文件
├── pyproject.toml    打包配置，源码不改成包目录
├── drill.py          真实工作区端到端演练探针（不进测试套件）
├── scripts/
│   ├── tc.py         业务核心
│   └── tc_mcp.py     stdio MCP 适配层
├── tests/
│   └── test_tc.py    验收测试，只用 unittest
├── build/  dist/     打包中间产物（已 gitignore）
└── pre-0.1.1-backup/  0.1.1 之前的手工备份（已 gitignore）
```
