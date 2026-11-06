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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
PATCH_SCRIPT = SCRIPT_DIR / "patch-raccoon-cooldown.mjs"
TEST_SCRIPT = SCRIPT_DIR / "test-raccoon-cooldown.mjs"
PATCHED = ROOT / ".pack" / "pack.raccoon-cooldown.js"


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
        """上游 pack.js 一变，补丁串就可能匹配不上。

        这种情况必须**明确失败**而不是静默跳过：静默意味着补丁悄悄失效，
        而用户又会重新遇到「只有 Raccoon 老冷却」。
        """
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

    def test_bridge_generates_patch_and_chat_gateway_loads_it(self):
        """桥和真正发送对话的备用网关都必须使用本补丁。"""
        bridge = (ROOT / "scripts" / "pack_bridge.mjs").read_text(encoding="utf-8")
        gateway = (ROOT / "scripts" / "pack_gateway.mjs").read_text(encoding="utf-8")
        self.assertIn("patch-raccoon-cooldown.mjs", bridge)
        self.assertIn("spawnSync(process.execPath", bridge)
        self.assertIn("DSH_PACK_PATCHED: patchedPackPath() || PACK", bridge)
        self.assertIn("process.env.DSH_PACK_PATCHED ||", gateway)
        patched = PATCHED.read_text(encoding="utf-8")
        self.assertIn("if (isRateLimitError) raccoonCooldownStep(accountId, options.model)", patched)

    def test_original_pack_is_never_modified(self):
        """补丁只能产出副本，绝不能改插件自己的文件。"""
        source = ROOT / "scripts" / "patch-raccoon-cooldown.mjs"
        text = source.read_text(encoding="utf-8")
        # 写文件只允许写到 .pack/ 下的副本。
        self.assertIn("writeFileSync(OUT_FILE", text)
        self.assertNotIn("writeFileSync(PACK", text)


if __name__ == "__main__":
    unittest.main()
