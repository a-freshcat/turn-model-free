"""华为 CodeArts 续期补丁：锚点唯一性、幂等性、语法自检。

补丁脚本改的是 `pack.js`（三百万字节的单文件产物）。这类改动最容易出的两个
问题都不是「逻辑写错」，而是**锚点漂移**和**重复打补丁**：

* 上游 `pack.js` 换了实现，锚点匹配不到 —— 必须失败退出，绝不能半打；
* 脚本被跑第二次 —— 必须识别「已打过」，而不是把注释再套一层。

所以这里用一份**形状正确的最小夹具**跑真实脚本，而不是把它当纯函数测。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "gateway" / "scripts" / "patch-codearts-refresh.mjs"

# 夹具：三个锚点各自的最小上下文。脚本按原文精确匹配，所以这里必须
# 与 pack.js 里的缩进/换行一致。
FIXTURE = '''function registerChannelPackRpc(ctx, pool, codearts) {
  ctx.inject(["connection"], (connectionCtx) => {
    async function handle(payload) {
      switch (payload.method) {
        case "credits.balances": {
          const req = payload;
          const accounts = await pool.listAccounts(req.provider);
        if (req.provider === "codearts") {
          const values2 = await collectCreditBalances(accounts, void 0, {
            resolve: (ref) => ctx.credentials.resolve(ref),
            fetchBalanceDetailed: async (credential) => ({ balance: null }),
          });
            return { ok: true, value: { accounts: values2 } };
          }
          break;
        }
      }
    }
    return handle;
  });
}

function registerCodeArtsLlm(ctx, options) {
  return options;
}

function buildOptions(pool, service) {
  return {
    resolveCredential: async () => undefined,
    refresh: async () => {
      const available2 = await pool.getAvailableAccount("codearts", "");
      if (available2) await service.refreshAccountCredential(available2.entry.credentialRef, pool, available2.entry.id);
    },
  };
}

class Adapter {
  constructor(options) {
    this.options = options;
  }
  async refreshCredentialOnDemand(credential) {
    const result = await codeartsRefreshWithReread({
      refresh: async () => {
        await this.options.refresh();
        return await this.options.resolveCredential();
      },
      reread: async () => await this.options.resolveCredential(),
      attempted: credential,
    });
    return result.credential;
  }
}

function isCodeArtsNonAuthRejection(body) {
  return typeof body === "string" && body.includes("TM.00000001");
}
function isAuthError(status, body) {
  if (isCodeArtsNonAuthRejection(body)) return false;
  return status === 401 || status === 403;
}

async function consumeSse(stream, options) {
  for (;;) {
    const data = JSON.parse(await stream.next());
    if (typeof data.error_code === "string" && data.error_code.length > 0) {
      const message2 = data.error_msg ?? data.error_code;
            if (data.error_code === CODEARTS_BENEFIT_NOT_FOUND_ERROR_CODE) {
              throw new LlmError2(`codearts: ${message2}`, BENEFIT_NOT_FOUND_CODE, { status: 200 });
            }
            throw new LlmError2(`codearts: ${message2}`, "INVALID_REQUEST", { status: 200 });
    }
  }
}

async function consumeWithRetry(adapter, isBenefitModel) {
  let benefitHeaderDropped = false;
  for (;;) {
    try {
      await adapter.read();
    } catch (error) {
      if (error instanceof LlmError2 && error.code === "SSE_QUEUE_RETRY") {
          } else if (error instanceof LlmError2 && error.code === BENEFIT_NOT_FOUND_CODE && isBenefitModel && !benefitHeaderDropped) {
            benefitHeaderDropped = true;
            continue;
      } else {
        throw error;
      }
    }
    break;
  }
}

function probeFixture(entry, ref, credential) {
  let adapter;
  if (entry.provider === "something") {
    adapter = null;
  } else if (entry.provider === GEMINI.id) {
    adapter = new GeminiAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      },
      sessionLane: GEMINI_SESSION_LANE_SMOKE,
      project: GEMINI_DEFAULT_PROJECT,
      product: GEMINI
    });
  } else {
    adapter = new CodeArtsAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      }
    });
  }
  return adapter;
}
'''


def node_available():
    return shutil.which("node") is not None


@unittest.skipUnless(node_available(), "需要 node 才能跑补丁脚本")
class CodeArtsRefreshPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pack = Path(self.temp.name) / "pack.js"
        # newline="\n" 必须显式给：Windows 文本模式会把 \n 写成 \r\n，
        # 而 pack.js 是 LF，锚点就会一条都匹配不上（夹具问题，不是补丁问题）。
        self.pack.write_text(FIXTURE, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_patch(self, *args):
        env = {**os.environ, "DSH_PACK_FILE": str(self.pack)}
        return subprocess.run(
            ["node", str(SCRIPT), *args],
            capture_output=True, text=True, env=env, encoding="utf-8",
            timeout=120,
        )

    def test_patches_all_three_sites_and_keeps_syntax_valid(self):
        done = self.run_patch()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        text = self.pack.read_text(encoding="utf-8")

        # 1) 余额查询前按账号续期
        self.assertIn("补丁：先按账号续期", text)
        self.assertIn("await codearts.refreshAccountCredential(entry.credentialRef, pool, entry.id)", text)
        # 续期窗口必须是 1 小时——与 pack.js 的 REFRESH_LEAD_MS 同语义。
        self.assertIn("60 * 60 * 1000", text)

        # 2) 续期绑定被选中的账号，而不是重新 getAvailableAccount
        self.assertIn("findAccountIdByCredential(\"codearts\", credential.access_key_id)", text)
        # 3) 按需续期把凭据传下去
        self.assertIn("await this.options.refresh(credential);", text)

        # 4) 探活支持 Raccoon 渠道，避免被错误构造成 CodeArtsAdapter 报 400
        self.assertIn("entry.provider === RACCOON.id", text)
        self.assertIn("adapter = new RaccoonAdapter", text)

        # 5) APIG.0101（接口未发布）必须能被识别，而不是当成请求格式错误
        self.assertIn("function isCodeArtsApiUnpublished", text)
        self.assertIn("BENEFIT_API_UNPUBLISHED", text)
        # 6) 权益模型遇到未发布时先摘 maas_type 头重试
        self.assertIn("benefitHeaderDropped = true;", text)

        # 语法必须仍然合法：脚本自己做过一次 --check，这里再独立验一遍。
        checked = subprocess.run(["node", "--check", str(self.pack)],
                                 capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_second_run_is_idempotent(self):
        self.assertEqual(self.run_patch().returncode, 0)
        after_first = self.pack.read_text(encoding="utf-8")
        again = self.run_patch()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("已打过", again.stdout)
        self.assertEqual(self.pack.read_text(encoding="utf-8"), after_first,
                         "重复执行不该再改文件")

    def test_missing_anchor_fails_without_writing(self):
        """锚点漂移必须整体失败，不能留下半打的文件。"""
        self.pack.write_text(FIXTURE.replace(
            'await this.options.refresh();',
            'await this.options.refresh(alreadyChanged);'), encoding="utf-8", newline="\n")
        before = self.pack.read_text(encoding="utf-8")
        done = self.run_patch()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("放弃", done.stderr)
        self.assertEqual(self.pack.read_text(encoding="utf-8"), before)

    def test_dry_run_does_not_touch_the_file(self):
        before = self.pack.read_text(encoding="utf-8")
        done = self.run_patch("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.pack.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
