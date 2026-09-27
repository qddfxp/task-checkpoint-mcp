"""发布一致性：版本号、注册表元数据、README 里的所有权标记。

这些以前全靠人记得一起改，结果真出过漂移 —— MCP Registry 停在 0.1.3 而
仓库已经是 0.1.4，客户端从注册表读到的是旧版本。钉成测试之后，改漏一处就红。
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # 3.10 没有 tomllib；服务器本体仍支持 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "io.github.qddfxp/task-checkpoint-mcp"
REPO_URL = "https://github.com/qddfxp/task-checkpoint-mcp"


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def _npm_json() -> dict:
    return json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))


@unittest.skipIf(tomllib is None, "tomllib 需要 Python 3.11+")
class ReleaseConsistency(unittest.TestCase):
    def test_the_three_version_numbers_agree(self):
        """pyproject / npm/package.json / server.json 必须是同一个版本。

        发版要同时动这三处，任何一处漏改都会让某个渠道停在旧版本上。
        """
        version = _project()["version"]
        self.assertEqual(version, _npm_json()["version"], "pyproject 与 npm/package.json 版本不一致")

        server = _server_json()
        self.assertEqual(version, server["version"], "pyproject 与 server.json 版本不一致")
        self.assertTrue(server["packages"], "server.json 至少要有一个 package")
        for package in server["packages"]:
            self.assertEqual(version, package["version"],
                             "server.json 里 packages[].version 与项目版本不一致")

    def test_server_json_points_at_the_real_distributions(self):
        server = _server_json()
        project = _project()

        self.assertEqual(server["name"], SERVER_NAME)
        self.assertTrue(server["name"].startswith("io.github."),
                        "GitHub 认证要求命名空间形如 io.github.<用户名>/")
        self.assertEqual(server["repository"], {"url": REPO_URL, "source": "github"})
        # CI 里 check-jsonschema 用的就是这一条；钉住它，schema 换代时能发现
        self.assertEqual(server["$schema"],
                         "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json")

        description = server["description"]
        self.assertEqual(description, description.strip(), "description 首尾不该有空白")
        # 注册表对 description 的要求是「具体、说清做什么」，宁短勿长
        self.assertLessEqual(len(description), 100, "server.json 的 description 建议不超过 100 字符")

        pypi = [p for p in server["packages"] if p["registryType"] == "pypi"]
        self.assertTrue(pypi, "server.json 里必须有 pypi 包，否则装不上")
        self.assertEqual(pypi[0]["identifier"], project["name"],
                         "server.json 的 pypi identifier 必须等于 PyPI 包名")
        for package in server["packages"]:
            self.assertEqual(package["transport"]["type"], "stdio")

    def test_readme_carries_the_registry_ownership_marker(self):
        """注册表靠 README 里这行注释证明 PyPI 包归这个 server 所有。

        删了它，`mcp-publisher publish` 会以所有权校验失败被拒。
        """
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"<!-- mcp-name: {SERVER_NAME} -->", readme)

    def test_npm_launcher_matches_the_pypi_package(self):
        """npm 包与 PyPI 包同名是有意的 —— 它是启动器，不是另一份实现。"""
        npm = _npm_json()
        self.assertEqual(npm["name"], _project()["name"])
        self.assertIn("github.com/qddfxp/task-checkpoint-mcp", npm["repository"]["url"])
        self.assertEqual(npm["bin"], {"task-checkpoint-mcp": "cli.js"})
        self.assertEqual(sorted(npm["files"]), ["README.md", "cli.js"])
        self.assertEqual(npm["license"], "MIT")


class PortabilityPromises(unittest.TestCase):
    """pyproject 承诺的 Python 版本，代码得真的能在那上面跑。（这个类不依赖 tomllib）"""

    def test_no_backslash_inside_fstring_expressions(self):
        """3.10 / 3.11 不允许 f-string 的表达式部分出现反斜杠。

        含 `\\uXXXX` 转义的兜底字面量写进 `{...}` 里，在 3.12+ 编译得过，但
        在 3.10 和 3.11 上整个模块 import 就炸 —— `requires-python = ">=3.10"`
        会变成一句空话。CI 的 3.10 job 是最终闸门，这条是本地闸门。
        """
        offenders = []
        sources = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
        for path in sources:
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if not isinstance(node, ast.JoinedStr):
                    continue
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        segment = ast.get_source_segment(source, value.value) or ""
                        if "\\" in segment:
                            offenders.append(f"{path.name}:{value.value.lineno}  {segment.strip()}")
        self.assertEqual(offenders, [],
                         "f-string 表达式里出现反斜杠，3.10 / 3.11 上会 SyntaxError：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
