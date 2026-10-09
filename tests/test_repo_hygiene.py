#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库卫生检查。

这几条不是「功能对不对」，是「交出去会不会出事」：

1. 没有 Token 落盘 —— 文档里写 `export MCD_MCP_TOKEN=...` 可以，写真的不行
2. 只依赖标准库 —— 评委 clone 完直接能跑，不用 pip install
3. 写接口一个都不碰 —— create-order / cancel-order / mall-create-order
4. 用户数据不进仓库 —— memory.json / mock/images / .env
5. 必交文件都在
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import sysconfig
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"

sys.path.insert(0, str(SCRIPTS))

WRITE_TOOLS = ("create-order", "cancel-order", "mall-create-order")

# 纯文本、值得扫的文件类型。图片和二进制跳过。
TEXT_SUFFIXES = {".py", ".md", ".json", ".html", ".txt", ".cfg", ".toml",
                 ".yml", ".yaml", ".gitignore", ".sh", ""}

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "images"}


def iter_text_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico",
                                   ".woff", ".woff2", ".pdf"}:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.suffix:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        yield path


# --------------------------------------------------------------------------
# 1. 没有 Token 落盘
# --------------------------------------------------------------------------


class TestNoTokenOnDisk(unittest.TestCase):
    def test_no_token_assignment_anywhere(self):
        """全仓库都不许出现 `MCD_MCP_TOKEN=<16 位以上真值>`。"""
        pattern = re.compile(r"MCD_MCP_TOKEN\s*=\s*([A-Za-z0-9]{16,})")
        for path in iter_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for value in pattern.findall(text):
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.fail(f"疑似写死 Token：{value[:6]}…（{len(value)} 位）")

    def test_no_bearer_with_a_real_looking_token(self):
        """`Bearer ${VAR}` 和 `Bearer YOUR_MCP_TOKEN` 可以，`Bearer <32位>` 不行。"""
        pattern = re.compile(r"Bearer\s+([A-Za-z0-9_\-]{16,})")
        allowed = re.compile(r"^\$\{|\$[A-Z_]|^YOUR_|^<|^your", re.I)
        for path in iter_text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for value in pattern.findall(text):
                if allowed.match(value):
                    continue
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.fail(f"疑似真的 Bearer Token：{value[:6]}…")

    def test_no_32_char_secret_string_literal(self):
        """32 位、大小写+数字混合的字符串字面量，长得就像 Token。"""
        pattern = re.compile(r"""["']([A-Za-z0-9]{28,40})["']""")
        for path in iter_text_files():
            if path.suffix.lower() != ".py":
                continue
            text = path.read_text(encoding="utf-8")
            for value in pattern.findall(text):
                if not (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)
                        and re.search(r"\d", value)):
                    continue
                if value.isalnum() and value.lower() in {"abcdefghijklmnopqrstuvwxyz"}:
                    continue
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.fail(f"疑似 Token 字面量：{value[:6]}…")

    def test_client_reads_only_from_environ(self):
        text = (SCRIPTS / "mcp_client.py").read_text(encoding="utf-8")
        self.assertIn("MCD_MCP_TOKEN", text)
        self.assertRegex(text, r"environ\.get\(\s*[\"']MCD_MCP_TOKEN")

    def test_example_config_has_no_real_token(self):
        path = ROOT / "mcp-config.example.json"
        self.assertTrue(path.exists(), "mcp-config.example.json 应该存在")
        text = path.read_text(encoding="utf-8")
        self.assertIn("${MCD_MCP_TOKEN}", text)
        # 解析一遍，确保 JSON 本身没问题（注释字段用 _ 开头的键，合法）
        data = json.loads(text)
        headers = data["mcpServers"]["mcd-mcp"]["headers"]
        self.assertNotRegex(headers["Authorization"], r"Bearer\s+[A-Za-z0-9]{16,}")


# --------------------------------------------------------------------------
# 2. 零第三方依赖
# --------------------------------------------------------------------------


class TestStdlibOnly(unittest.TestCase):
    """评委 clone 完直接 `python3 scripts/tour.py --demo` 就该能跑。"""

    @classmethod
    def setUpClass(cls):
        cls.stdlib_dir = Path(sysconfig.get_paths()["stdlib"]).resolve()
        cls.local_modules = {p.stem for p in SCRIPTS.glob("*.py")}
        cls.local_modules |= {p.stem for p in TESTS.glob("*.py")}

    @classmethod
    def module_names(cls, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # 相对导入
                    continue
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    @classmethod
    def is_stdlib(cls, name: str) -> bool:
        if name in cls.local_modules:
            return True
        names = getattr(__import__("sys"), "stdlib_module_names", None)
        if names is not None:           # Python 3.10+
            return name in names
        try:                            # Python 3.9 兜底：看文件在不在标准库里
            spec = __import__("importlib.util", fromlist=["util"]).find_spec(name)
        except (ImportError, ValueError):
            return False
        if spec is None:
            return False
        origin = spec.origin
        if not origin or origin in ("built-in", "frozen"):
            return True
        try:
            return Path(origin).resolve().is_relative_to(cls.stdlib_dir)
        except (ValueError, OSError):
            return False

    def test_scripts_import_only_stdlib(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            for name in sorted(self.module_names(path)):
                with self.subTest(file=path.name, module=name):
                    self.assertTrue(
                        self.is_stdlib(name),
                        f"{path.name} 引入了非标准库依赖：{name}")

    def test_tests_import_only_stdlib(self):
        for path in sorted(TESTS.glob("*.py")):
            for name in sorted(self.module_names(path)):
                with self.subTest(file=path.name, module=name):
                    self.assertTrue(
                        self.is_stdlib(name),
                        f"{path.name} 引入了非标准库依赖：{name}")

    def test_no_requirements_file(self):
        """连 requirements.txt 都不该有 —— 有就说明有依赖。"""
        for name in ("requirements.txt", "pyproject.toml", "setup.py",
                     "Pipfile", "poetry.lock"):
            self.assertFalse((ROOT / name).exists(),
                             f"不该有 {name}（本项目零依赖）")


# --------------------------------------------------------------------------
# 3. 写接口一个都不碰
# --------------------------------------------------------------------------


class TestNoWriteToolsCalled(unittest.TestCase):
    def test_scripts_never_invoke_write_tools(self):
        """写接口只准待在 `mcd.py` 的 `cmd_order()` 里面，别处一个都不许有。

        为什么留这个口子：下单那道闸门就是「用户自己敲 `mcd.py order …`」——
        不敲就永远走不进那个函数，也没有任何一处会自动调它。
        所以静态检查退一步，改成「写接口必须被关在 cmd_order 里，前后都不能漏出去」。
        """
        order_entry = SCRIPTS / "mcd.py"
        for path in sorted(SCRIPTS.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            inside = ""
            if path == order_entry and "def cmd_order(" in text:
                inside = text[text.index("def cmd_order("):text.index("def main(")]
            for tool in WRITE_TOOLS:
                patterns = [
                    rf"call_tool\(\s*[\"']{re.escape(tool)}",
                    rf"_call\(\s*[\"']{re.escape(tool)}",
                    rf"call_tool_or_mock\(\s*[\"']{re.escape(tool)}",
                ]
                for pattern in patterns:
                    with self.subTest(file=path.name, tool=tool):
                        self.assertEqual(
                            len(re.findall(pattern, text)),
                            len(re.findall(pattern, inside)),
                            f"{path.name} 里的 {tool} 跑到 cmd_order 外面去了")

    def test_readonly_whitelist_excludes_write_tools(self):
        from mcp_client import READONLY_TOOLS
        for tool in WRITE_TOOLS:
            self.assertNotIn(tool, READONLY_TOOLS)

    def test_readonly_whitelist_has_the_seven_we_use(self):
        from mcp_client import READONLY_TOOLS
        self.assertEqual(len(READONLY_TOOLS), 7)
        for name in ("now-time-info", "query-nearby-stores", "query-meals",
                     "query-meal-detail", "list-nutrition-foods",
                     "query-store-coupons", "calculate-price"):
            self.assertIn(name, READONLY_TOOLS)


# --------------------------------------------------------------------------
# 4. 用户数据不进仓库
# --------------------------------------------------------------------------


class TestUserDataIgnored(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    def test_user_store_lives_outside_the_repo(self):
        """门店记在 `~/.mcd-buddy.json` —— 用户数据放仓库外面。"""
        from mcd import STORE_FILE
        self.assertEqual(STORE_FILE.parent, Path.home())
        self.assertNotIn(str(ROOT), str(STORE_FILE))

    def test_real_combo_cache_never_touches_mock(self):
        """真实模式拉的套餐目录缓存到用户目录，绝不写回 mock/。

        mock/combo-catalog.json 是全部测试的数据源 —— 曾经被真实接口
        的回写一冲，二十多万行的 mock 变成一家门店的 58 个套餐，
        test_recommend 当场瞎了俩。这条测试就是那次污染留下的。
        """
        import menu_engine
        source = (SCRIPTS / "menu_engine.py").read_text(encoding="utf-8")
        # 写操作只准指向用户级缓存
        self.assertNotIn("COMBO_CACHE.write_text", source)
        self.assertIn("COMBO_CACHE_USER.write_text", source)
        self.assertEqual(menu_engine.COMBO_CACHE.parent, menu_engine.MOCK_DIR)
        self.assertEqual(menu_engine.COMBO_CACHE_USER.parent, Path.home())

    def test_image_cache_is_ignored(self):
        self.assertIn("mock/images/", self.ignore)

    def test_env_is_ignored(self):
        self.assertRegex(self.ignore, r"(?m)^\.env\s*$")
        self.assertRegex(self.ignore, r"(?m)^\.env\.\*\s*$")

    def test_image_cache_is_never_tracked_by_git(self):
        if shutil.which("git") is None:
            self.skipTest("本机没有 git")
        if subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                          capture_output=True).returncode != 0:
            self.skipTest("不是一个 git 仓库")
        probed = subprocess.run(
            ["git", "check-ignore", "-q", "mock/images/whatever.png"],
            cwd=ROOT, capture_output=True)
        self.assertEqual(probed.returncode, 0,
                         "mock/images/ 里的图没被 .gitignore 挡住")

    def test_example_config_json_is_valid(self):
        json.loads((ROOT / "mcp-config.example.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 5. 必交文件都在
# --------------------------------------------------------------------------


class TestDeliverables(unittest.TestCase):
    def test_required_files_exist(self):
        for name in ("README.md", "SKILL.md", "MCP_INTEGRATION.md",
                     "mcp-config.example.json", "CONTEST_DECLARATION.md"):
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).exists(), f"缺 {name}")

    def test_readme_shows_the_three_commands(self):
        """README 得让人一眼看到怎么用 —— 就三条命令。"""
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for cmd in ("mcd.py where", "mcd.py want", "mcd.py order"):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, text)

    def test_scripts_stay_small(self):
        """脚本不许再长回去。

        这个项目的核心就三件事：定门店、查价格热量、下单。
        14 个脚本、一万行的版本被用户当面骂过「点餐 CLI 怎么写的这么重」——
        这条测试是那顿骂留下的。
        """
        scripts = sorted(SCRIPTS.glob("*.py"))
        self.assertLessEqual(len(scripts), 4,
                             f"脚本又变多了：{[p.name for p in scripts]}")
        total = sum(len(p.read_text(encoding="utf-8").splitlines())
                    for p in scripts)
        self.assertLessEqual(total, 2500, f"脚本总共 {total} 行，太长了")

    def test_readme_has_a_one_line_pitch(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("你在哪儿，说一声", text)

    def test_readme_shows_the_three_commands(self):
        """README 得让人一眼看到怎么用 —— 就三条命令。"""
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for cmd in ("mcd.py where", "mcd.py want", "mcd.py order",
                    "python3 -m unittest discover -s tests", "MCD_MCP_TOKEN"):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, text)

    def test_skill_md_has_the_five_redlines(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for keyword in ("不下单不确认", "不引导不健康饮食", "不比较、不贬低",
                        "不编数据", "Token 只从环境变量读"):
            with self.subTest(rule=keyword):
                self.assertIn(keyword, text)

    def test_declaration_is_flagged_as_unfilled(self):
        """声明原文没填之前，文件里必须有醒目的警告，别让人误交。"""
        text = (ROOT / "CONTEST_DECLARATION.md").read_text(encoding="utf-8")
        self.assertIn("待填", text)


# --------------------------------------------------------------------------
# 6. 卡片 HTML 不进仓库
# --------------------------------------------------------------------------


class TestGeneratedArtifacts(unittest.TestCase):
    def test_no_leftover_build_output(self):
        """不该留下构建产物 —— 这项目没有构建步骤，跑的就是源码。"""
        for name in ("assets", "data", "build", "dist"):
            with self.subTest(dir=name):
                self.assertFalse((ROOT / name).exists(), f"{name}/ 是残留产物")


if __name__ == "__main__":
    unittest.main(verbosity=2)
