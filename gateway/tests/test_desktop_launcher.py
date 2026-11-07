"""免控制台启动器（TurnModelFree.exe）的守护测试。

用户的原话是「像 app 端那样用快捷方式启动，其他隐藏在后端」。这里守住三件事：

1. **没有黑框**：编译目标是 `winexe`，拉起 `pythonw.exe`，并带 `CreateNoWindow`。
   任何一步退回控制台子系统，用户就会看到一个 cmd 窗口。
2. **exe 在包里的任何落法都能开**：发布包根目录 / `launcher\\` 里 / 被拖进
   `gateway\\` 里。找错一层就是「双击没反应」。
3. **失败要说话**：没有控制台时 MessageBox 是唯一出口，同时必须指路
   `launch-desktop.cmd`。`.cmd` 本身永远保留作排障入口。

编译相关的用例在没有 csc / 没有 Pillow 时跳过，而不是假装通过。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1]
ROOT = GATEWAY.parent
LAUNCHER_DIR = ROOT / "launcher"
CS_SOURCE = LAUNCHER_DIR / "TurnModelFreeLauncher.cs"
ICON = LAUNCHER_DIR / "app.ico"
BUILD_SCRIPT = ROOT / "scripts" / "build-launcher.ps1"
SHORTCUT_SCRIPT = ROOT / "scripts" / "install-shortcut.ps1"
ICON_SCRIPT = ROOT / "scripts" / "make-icon.py"


def _csc() -> str | None:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for part in ("Framework64", "Framework"):
        candidate = windir / "Microsoft.NET" / part / "v4.0.30319" / "csc.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class LauncherSourceTests(unittest.TestCase):
    """静态约束：这些行没了，用户立刻会看到黑框或「双击没反应」。"""

    @classmethod
    def setUpClass(cls):
        cls.source = CS_SOURCE.read_text(encoding="utf-8")

    def test_source_and_icon_are_tracked_files(self):
        self.assertTrue(CS_SOURCE.is_file(), "缺少启动器源码")
        self.assertTrue(ICON.is_file(), "缺少启动器图标")

    def test_it_is_built_as_a_window_subsystem_binary(self):
        """`/target:winexe` 是「没有黑框」的唯一来源。"""
        script = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("/target:winexe", script)
        self.assertIn("/win32icon:", script)
        # 源码里有中文，编译器必须按 UTF-8 读，否则弹窗文案是乱码。
        self.assertIn("/codepage:65001", script)

    def test_it_starts_pythonw_not_python(self):
        self.assertIn("pythonw.exe", self.source)
        # pythonw 是控制台子系统的反面；这里不能回退到 python.exe。
        self.assertNotIn('"python.exe"', self.source)
        self.assertIn("CreateNoWindow", self.source)
        self.assertIn("WindowStyle = ProcessWindowStyle.Hidden", self.source)

    def test_desktop_launcher_is_the_process_it_starts(self):
        self.assertIn("launch_desktop.py", self.source)
        # 端口默认值必须与 launch_web.DEFAULT_PORT 同源（8199），否则快捷方式
        # 开在 8199、文档写 8199、exe 起在别的端口。
        self.assertIn("DefaultPort = 8199", self.source)

    def test_gateway_directory_is_found_from_three_layouts(self):
        """包根 / launcher 子目录 / gateway 里，三种落法都得能找到脚本。"""
        self.assertIn('Path.Combine(baseDir, "gateway")', self.source)
        self.assertIn('Path.Combine(baseDir, "..", "gateway")', self.source)
        self.assertIn("launch_desktop.py", self.source)

    def test_failure_path_explains_itself_and_points_at_the_cmd(self):
        self.assertIn("MessageBox.Show", self.source)
        self.assertIn("launch-desktop.cmd", self.source)
        # 自动化测试要能静音弹窗，否则 CI 会卡在对话框上。
        self.assertIn("TMF_LAUNCHER_NO_DIALOG", self.source)

    def test_shortcut_script_targets_the_exe_and_keeps_arguments(self):
        script = SHORTCUT_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("WScript.Shell", script)
        self.assertIn("TurnModelFree.exe", script)
        self.assertIn("$shortcut.Arguments = $Arguments", script)
        # 换版本后重跑一次就能改指向，所以不能"只在不存在时创建"。
        self.assertNotIn("if (Test-Path $link) { return }", script)

    def test_powershell_scripts_start_with_a_utf8_bom(self):
        """带中文的 .ps1 必须是 UTF-8 **带 BOM**。

        Windows PowerShell 5.1 按 ANSI 解码无 BOM 的脚本，中文立刻变乱码，而且
        乱码会吃掉行尾引号 —— 报错是看不懂的 `Unexpected token`，看起来像语法
        错误。这条被踩过一次，所以固化成测试。
        """
        for script in (BUILD_SCRIPT, SHORTCUT_SCRIPT):
            with self.subTest(script=script.name):
                raw = script.read_bytes()
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                                f"{script.name} 缺少 UTF-8 BOM（PowerShell 5.1 会乱码）")

    def test_shortcut_can_point_at_another_install(self):
        """`-Exe` 必须能用：仓库里编的 exe 缺 `.venv`，真正在跑的是用户目录那份。"""
        script = SHORTCUT_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("[string]$Exe = ''", script)
        self.assertIn("$shortcut.WorkingDirectory = $working", script)


class LauncherBuildTests(unittest.TestCase):
    """真编一次：编不出来就说明用户双击会失败。"""

    def test_csc_compiles_the_launcher(self):
        if _csc() is None:
            self.skipTest("没有 csc.exe（.NET Framework 4.x 未安装）")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "TurnModelFree.exe"
            result = subprocess.run([
                _csc(), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
                "/codepage:65001", f"/win32icon:{ICON}", f"/out:{target}",
                "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                str(CS_SOURCE),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(target.is_file())
            # PE 头里的子系统字段：2 = GUI（无控制台），3 = 控制台。
            data = target.read_bytes()
            pe = int.from_bytes(data[0x3C:0x40], "little")
            optional = pe + 24
            self.assertEqual(int.from_bytes(data[optional:optional + 2], "little"), 0x10B,
                             "预期 32 位 PE 可选头")
            subsystem = int.from_bytes(data[optional + 68:optional + 70], "little")
            self.assertEqual(subsystem, 2, "子系统不是 GUI —— 双击会弹出控制台窗口")

    def test_icon_is_reproducible_from_its_script(self):
        """图标是原创绘制，随时可重建；不一致说明有人手改了二进制。"""
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow，跳过图标复现校验")
        result = subprocess.run([sys.executable, str(ICON_SCRIPT), "--check"],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_icon_has_the_sizes_windows_asks_for(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 Pillow，跳过图标尺寸校验")
        with Image.open(ICON) as image:
            sizes = set(image.info.get("sizes") or [])
        for size in ((16, 16), (32, 32), (48, 48), (256, 256)):
            self.assertIn(size, sizes, f"图标缺少 {size} 尺寸：{sorted(sizes)}")


class HiddenChildProcessTests(unittest.TestCase):
    """「exe 已经藏起来了，后台还挂着一个黑框」的回归测试。

    根因不在启动器，在 Python 侧：`pythonw.exe` 自己没有控制台，但
    `create_subprocess_exec` 起 node 桥时没传 `creationflags`，Windows 就给
    node 新开一个控制台。用户看到的是那个黑框，启动器再干净也挡不住它。
    """

    @classmethod
    def setUpClass(cls):
        cls.bridge = (GATEWAY / "bridge_client.py").read_text(encoding="utf-8")
        cls.web = (GATEWAY / "launch_web.py").read_text(encoding="utf-8")

    def test_bridge_spawns_node_without_console(self):
        self.assertIn("_hidden_child_kwargs", self.bridge)
        self.assertIn("**_hidden_child_kwargs()", self.bridge)
        self.assertIn("CREATE_NO_WINDOW", self.bridge)
        # 不能退回「什么都不传」：那正是缺陷本身。
        self.assertNotIn(
            "stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ROOT),\n            )",
            self.bridge)

    def test_helper_is_a_noop_off_windows(self):
        """非 Windows 上不能传 creationflags —— 那个参数不存在，会 TypeError。"""
        self.assertIn('if os.name != "nt":', self.bridge)
        self.assertIn("return {}", self.bridge)

    def test_one_shot_commands_are_also_hidden(self):
        """netstat / tasklist / taskkill / icacls 在 pythonw 下也会各闪一个黑框。"""
        self.assertIn("def _hidden(", self.web)
        for command in ('"netstat"', '"tasklist"', '"taskkill"', '"icacls"'):
            self.assertIn(command, self.web)
        self.assertEqual(self.web.count("**_hidden()"), 4,
                         "四个一次性子进程都要带隐藏标志")


class ReleasePackagingTests(unittest.TestCase):
    """发版脚本必须把启动器带上，且绝不夹带本机凭据。"""

    @classmethod
    def setUpClass(cls):
        cls.release = (ROOT / "scripts" / "prepare_release.py").read_text(encoding="utf-8")

    def test_launcher_assets_are_required_in_the_git_index(self):
        for name in ("launcher/TurnModelFreeLauncher.cs", "launcher/app.ico",
                     "scripts/build-launcher.ps1", "scripts/install-shortcut.ps1",
                     "scripts/make-icon.py"):
            self.assertIn(f"'{name}'", self.release, f"发行必需清单缺少 {name}")

    def test_desktop_zip_places_the_exe_at_the_package_root(self):
        self.assertIn("f'{label}/TurnModelFree.exe'", self.release)

    def test_launcher_is_compiled_not_committed(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("launcher/*.exe", ignore,
                      "编译产物不该入库：exe 由 build-launcher.ps1 现编")
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.exe binary", attributes)
        self.assertIn("*.ico binary", attributes)


if __name__ == "__main__":
    unittest.main()
