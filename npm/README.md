# task-checkpoint-mcp (npm launcher)

这是 [task-checkpoint-mcp](https://github.com/qddfxp/task-checkpoint-mcp) 的 **npm 启动器**，不是服务器本体。

服务器本体是纯标准库的 Python 包。**推荐直接装 Python 包**，不需要这个 npm 包：

```bash
pipx install task-checkpoint-mcp
```

装完把 MCP 客户端的 `command` 指向 `tc-mcp` 即可。

## 什么时候用这个包

只在你已经用不上 `pipx` 的场景：

```bash
npx -y task-checkpoint-mcp
```

它会找一个装了 `tc_mcp` 的 Python 解释器（依次试 `python` / `py -3` / `python3`，可用 `TC_PYTHON` 指定），然后把 stdio 透传给 `python -m tc_mcp`。

**它不会替你装 Python 包。** 本机没有 `pipx install task-checkpoint-mcp` 过的话，它会打印上面那句安装命令然后退出码 1 —— 这是有意的，免得你在运行时拿到一堆看不懂的 traceback。

## 客户端配置

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "npx",
      "args": ["-y", "task-checkpoint-mcp"]
    }
  }
}
```

如果本机已经装了 Python 包，用下面这段更直接，少一层 Node 中转：

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

许可：MIT。

---

# task-checkpoint-mcp (npm launcher)

An **npm launcher** for [task-checkpoint-mcp](https://github.com/qddfxp/task-checkpoint-mcp), not the server itself.

The server is a Python package using only the standard library. **Install the Python package instead** — you don't need this npm package:

```bash
pipx install task-checkpoint-mcp
```

Then point your MCP client's `command` at `tc-mcp`.

## When to use this package

Only where `pipx` isn't an option:

```bash
npx -y task-checkpoint-mcp
```

It locates a Python interpreter that has `tc_mcp` importable (trying `python`, `py -3`, `python3` in turn; override with `TC_PYTHON`) and hands stdio through to `python -m tc_mcp`.

**It will not install the Python package for you.** If `pipx install task-checkpoint-mcp` has never been run, it prints that command and exits 1 — deliberately, so you don't get an opaque traceback at runtime.

## Client config

```json
{
  "mcpServers": {
    "task-checkpoint": {
      "command": "npx",
      "args": ["-y", "task-checkpoint-mcp"]
    }
  }
}
```

If the Python package is already installed, this is more direct — one less Node hop:

```json
{
  "mcpServers": {
    "task-checkpoint": { "command": "tc-mcp", "args": [] }
  }
}
```

License: MIT.
