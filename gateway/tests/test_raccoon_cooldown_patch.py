"""Raccoon 冷却退避补丁的守护测试。

用户报的现象：「就这个厂商会经常冷却」。根因是 Raccoon 是唯一用指数退避的
渠道（30s→60s→…→10min），而退避档位只在收到 200 时归零；账号一旦被标冷却
就不再被号池选中，于是拿不到 200，档位永远停在高位，两个账号还会交替累积。

补丁让档位「每拿到一次响应降一级」。这组测试守住两件事：
  1. 补丁仍然能正确应用到当前已安装的 pack.js 上（上游变了要立刻发现）；
  2. 降级行为本身正确（借助 test-raccoon-cooldown.mjs 跑真代码）。
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
PATCH_SCRIPT = SCRIPT_DIR / "patch-raccoon-cooldown.mjs"
TEST_SCRIPT = SCRIPT_DIR / "test-raccoon-cooldown.mjs"
BEHAVIOUR_SCRIPT = SCRIPT_DIR / "test-raccoon-cooldown.mjs"
PATCHED = ROOT / ".pack" / "pack.raccoon-cooldown.js"
DEFAULT_PACK_HOME = (
    Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-our-free-model"
)
PACK_SOURCE = DEFAULT_PACK_HOME / "vendor" / "channel-pack"


def _node():
    """找 node：优先 PATH，其次 DSH 自带的运行时。"""
    found = shutil.which("node")
    if found:
        return found
    runtime = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
    return str(runtime) if runtime.is_file() else None


def _run(script, *args):
    node = _node()
    if node is None:
        raise unittest.SkipTest("node 不可用，跳过补丁检查")
    return subprocess.run(
        [node, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=180,
    )


class RaccoonCooldownPatchTests(unittest.TestCase):
    def test_patch_script_exists(self):
        self.assertTrue(PATCH_SCRIPT.is_file(), "补丁脚本丢失")

    def test_patch_still_applies_to_installed_pack(self):
        """补丁串是否还能匹配当前插件包。

        补丁默认**不再加载**（副本目录缺同级 wasm 会打断 Qoder），这里只校验
        脚本本身没和上游脱节。没有生成副本时跳过：产物是实验用的临时文件。
        """
        if not PATCHED.is_file():
            self.skipTest("未生成补丁副本（默认不启用）")
        result = _run(PATCH_SCRIPT, "--check")
        if result.returncode == 2:
            # 没装插件 —— 只跑本机 Antigravity 的用户本来就没有 pack.js。
            self.skipTest("本机未安装 channel-pack 插件")
        self.assertEqual(
            result.returncode, 0,
            f"补丁校验失败（退出码 {result.returncode}）：\n{result.stdout}\n{result.stderr}",
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(payload.get("ok"))
        self.assertIn("patched_sha256", payload)

    def test_stepwise_recovery_behaviour(self):
        """跑真代码验证「降一级」：攒到高位的档位能一步步降回 30 秒起点。"""
        if not PATCHED.is_file():
            self.skipTest("副本尚未生成（先跑 patch-raccoon-cooldown.mjs）")
        result = _run(TEST_SCRIPT)
        self.assertEqual(
            result.returncode, 0,
            f"行为验证失败：\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("全部通过", result.stdout)

    def test_bridge_defaults_to_the_original_pack(self):
        """默认必须加载插件目录里的原件。

        曾经自动把补丁副本放到 `gateway/.pack/` 再加载，结果 pack.js 按**自身
        目录**读取同级 `qoder-auth-wasm.wasm`，副本目录没有该文件，Qoder 直接
        报 ENOENT。所以默认路径只认原件，补丁副本只能显式通过环境变量指定。
        """
        bridge = (ROOT / "scripts" / "pack_bridge.mjs").read_text(encoding="utf-8")
        self.assertIn("const explicit = process.env.DSH_PACK_PATCHED", bridge)
        self.assertNotIn("spawnSync(process.execPath", bridge)
        # 备用网关不再被注入补丁副本路径。
        self.assertNotIn("DSH_PACK_PATCHED: patchedPackPath()", bridge)

    def test_patch_script_still_produces_expected_logic(self):
        """补丁脚本本身保持可用（供隔离实验），但不再被默认加载。"""
        if not PATCHED.is_file():
            self.skipTest("未生成补丁副本")
        patch_script = (ROOT / "scripts" / "patch-raccoon-cooldown.mjs").read_text(encoding="utf-8")
        self.assertIn("raccoonCooldownStep", patch_script)
        patched = PATCHED.read_text(encoding="utf-8")
        self.assertIn("if (isRateLimitError) raccoonCooldownStep(accountId, options.model)", patched)

    def test_original_pack_is_never_modified(self):
        """补丁只能产出副本，绝不能改插件自己的文件。"""
        source = ROOT / "scripts" / "patch-raccoon-cooldown.mjs"
        text = source.read_text(encoding="utf-8")
        # 写文件只允许写到 .pack/ 下的副本。
        self.assertIn("writeFileSync(OUT_FILE", text)
        self.assertNotIn("writeFileSync(PACK", text)


class IsolatedCopyTests(unittest.TestCase):
    """`--isolated-dir` 产出的副本必须**真的能加载**。

    这是 v1.3.1 的真实故障：只把 pack.js 复制到 `gateway/.pack/`，Qoder 一用就
    `ENOENT ... qoder-auth-wasm.wasm` —— 因为 pack.js 里是
    `WASM_PATH = resolve(dirname(import.meta.url), "qoder-auth-wasm.wasm")`。
    因此隔离副本必须是**整目录复制**，同级资源一个都不能少。
    """

    def setUp(self):
        if not (PACK_SOURCE / "pack.js").is_file():
            self.skipTest("本机未安装 channel-pack 插件")
        if _node() is None:
            self.skipTest("node 不可用")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "lab"

    def _patch_into_isolated(self):
        result = _run(PATCH_SCRIPT, "--isolated-dir", str(self.target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_isolated_copy_contains_sibling_assets(self):
        result = _run(PATCH_SCRIPT, "--isolated-dir", str(self.target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.target / "pack.js").is_file(), "副本 pack.js 不存在")
        wasm = self.target / "qoder-auth-wasm.wasm"
        self.assertTrue(wasm.is_file(), "同级 qoder-auth-wasm.wasm 没被复制 —— Qoder 会 ENOENT")
        self.assertEqual(wasm.read_bytes(),
                         (PACK_SOURCE / "qoder-auth-wasm.wasm").read_bytes(),
                         "复制过去的 wasm 与原件不一致")
        # 备份文件不该跟着搬（每个都近 3MB）。
        backups = [p.name for p in self.target.iterdir() if ".bak" in p.name]
        self.assertEqual(backups, [], f"隔离目录里混进了备份：{backups}")

    def test_isolated_patch_differs_from_the_original(self):
        original = (PACK_SOURCE / "pack.js").read_bytes()
        result = _run(PATCH_SCRIPT, "--isolated-dir", str(self.target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        patched = (self.target / "pack.js").read_bytes()
        self.assertNotEqual(original, patched, "副本与原件逐字节相同，说明补丁没打上")
        # 原件必须原封不动：隔离模式的整个意义就在这里。
        self.assertEqual((PACK_SOURCE / "pack.js").read_bytes(), original,
                         "补丁写回插件原件了")

    def test_patched_behaviour_passes_against_the_isolated_copy(self):
        """行为验证要对着**真正会加载的那一份**跑，而不是另抄一份逻辑。"""
        result = _run(PATCH_SCRIPT, "--isolated-dir", str(self.target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        behaviour = _run(TEST_SCRIPT, str(self.target / "pack.js"))
        self.assertEqual(behaviour.returncode, 0,
                         f"行为验证失败：\n{behaviour.stdout}\n{behaviour.stderr}")
        self.assertIn("全部通过", behaviour.stdout)

    def test_isolated_dir_refuses_the_plugin_directory_itself(self):
        """隔离目录不能就是插件目录：那等于把补丁写回原件。"""
        result = _run(PATCH_SCRIPT, "--isolated-dir", str(PACK_SOURCE))
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
