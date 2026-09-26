#!/usr/bin/env node
'use strict';

const { spawn, spawnSync } = require('node:child_process');

// 这个包只是启动器：服务器本体是 Python，只用标准库。
// 找得到装了 tc_mcp 的解释器就透传 stdio 跑起来，找不到就说清楚怎么装。

const CANDIDATES = process.platform === 'win32'
  ? [['python'], ['py', '-3'], ['python3']]
  : [['python3'], ['python']];

function usable(cmd, args) {
  try {
    const probe = spawnSync(cmd, [...args, '-c', 'import tc_mcp'], { stdio: 'ignore', timeout: 20000 });
    return probe.status === 0;
  } catch {
    return false;
  }
}

function pick() {
  if (process.env.TC_PYTHON && usable(process.env.TC_PYTHON, [])) {
    return { cmd: process.env.TC_PYTHON, args: [] };
  }
  for (const [cmd, ...args] of CANDIDATES) {
    if (usable(cmd, args)) return { cmd, args };
  }
  return null;
}

const chosen = pick();

if (!chosen) {
  process.stderr.write(
    'task-checkpoint-mcp: 找不到已安装 tc_mcp 的 Python 解释器。\n\n' +
    '这个 npm 包只是启动器，服务器本体是纯标准库的 Python 包，请先装：\n\n' +
    '    pipx install task-checkpoint-mcp\n\n' +
    '装完把 MCP 客户端的 command 指向 `tc-mcp` 即可，或者重跑本启动器。\n' +
    '（Python 装在非默认位置时，用 TC_PYTHON 指向它。）\n' +
    '文档：https://github.com/qddfxp/task-checkpoint-mcp\n'
  );
  process.exit(1);
}

const child = spawn(chosen.cmd, [...chosen.args, '-m', 'tc_mcp'], { stdio: 'inherit' });

child.on('error', (error) => {
  process.stderr.write(`task-checkpoint-mcp: 启动 ${chosen.cmd} 失败：${error.message}\n`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code === null ? 1 : code);
});
