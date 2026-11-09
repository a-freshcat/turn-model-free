// 给 dsh-our-free-model 的 vendor/channel-pack/pack.js 打华为 CodeArts 补丁。
//
// 修的是四类「账号本身没问题，插件却报错」的缺陷：
//
//   1. credits.balances 直接拿存储里的旧凭据去查账户信息，从不检查 expires_at。
//      后台续期漏一次，这个号就会一直报 HTTP 400 / APIG.0602。
//   2. 适配器的按需续期 `refresh()` 里又调了一次 getAvailableAccount("")，
//      双号时永远拿到第一个健康号，第二个过期号不会被续期。
//   3. 探活漏了 RACCOON/LOOMY 分支，掉进 CodeArtsAdapter，
//      报「codearts: no usable credential; log in first」。
//   4. 权益（benefit）模型被网关回 APIG.0101「该 API 未发布」时直接失败，
//      而同一请求去掉 `maas_type: benefit` 头就能走普通通道。
//
// 补丁只在原文精确匹配时替换，不匹配就整体失败退出，绝不半打。
import { readFileSync, writeFileSync, copyFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { homedir } from 'node:os'

const PACK = process.env.DSH_PACK_FILE || path.join(
  homedir(), '.dsh', 'profiles', 'desktop', 'node_modules',
  'dsh-our-free-model', 'vendor', 'channel-pack', 'pack.js')

const DRY = process.argv.includes('--dry-run')

// ── 补丁 1：余额查询前按账号续期 ───────────────────────────────────────────
const BALANCE_OLD = `        if (req.provider === "codearts") {
          const values2 = await collectCreditBalances(accounts, void 0, {
            resolve: (ref) => ctx.credentials.resolve(ref),`

const BALANCE_NEW = `        if (req.provider === "codearts") {
          // ⚠️ 补丁：先按账号续期，再读余额。
          //
          // 华为 CodeArts 的 security token 只有约 2 小时寿命，过期后余额接口
          // 回 HTTP 400 / APIG.0602。原实现直接拿存储里的旧凭据去查，于是后台
          // 续期只要漏过一次，这个号就会一直显示「账户信息查询失败：HTTP 400」，
          // 直到别的路径（对话请求或手动刷新）替它续上。
          //
          // 续期失败不能影响其它账号，也不能把余额错误换成续期错误：这里只
          // 尽力续期，失败就把原因写进日志，随后照原样去查——让用户看到真实的
          // 上游报错，而不是被我们吞掉。
          for (const entry of accounts) {
            try {
              const resolved = await ctx.credentials.resolve(credentialRef13(entry.credentialRef));
              if (!resolved) continue;
              const credential = JSON.parse(resolved.value);
              const expiresAt = credential?.expires_at ? Date.parse(credential.expires_at) : Number.NaN;
              if (Number.isFinite(expiresAt) && expiresAt - Date.now() > 60 * 60 * 1000) continue;
              if (!credential?.refresh_token) continue;
              await codearts.refreshAccountCredential(entry.credentialRef, pool, entry.id);
            } catch (error) {
              ctx.logger?.warn?.(
                \`[channel-pack] credits.balances 账号 \${entry.id} 续期失败（仍尝试查询）：\${error instanceof Error ? error.message : String(error)}\`
              );
            }
          }
          const values2 = await collectCreditBalances(accounts, void 0, {
            resolve: (ref) => ctx.credentials.resolve(ref),`

// ── 补丁 2：按需续期绑定被选中的账号 ───────────────────────────────────────
const REFRESH_OLD = `    refresh: async () => {
      const available2 = await pool.getAvailableAccount("codearts", "");
      if (available2) await service.refreshAccountCredential(available2.entry.credentialRef, pool, available2.entry.id);
    },`

const REFRESH_NEW = `    // ⚠️ 补丁：续期绑定**当前这次请求正在用的账号**。
    //
    // 原实现这里又调了一次 getAvailableAccount("codearts", "")，而它永远
    // 返回账号池里的第一个可用号。双号场景下，第二个号过期时对话请求会拿
    // 第一个号的凭据去续期（白续一次），第二个号永远轮不到续期，于是它的
    // 余额/配额面板一直是 APIG.0602。
    //
    // 现在优先用调用方传入的 credential（适配器 stream() 已按 AK 反查出账号），
    // 拿不到才回退到原行为。
    refresh: async (credential) => {
      let entry;
      if (credential?.access_key_id) {
        try {
          const id = await pool.findAccountIdByCredential("codearts", credential.access_key_id);
          if (id) entry = (await pool.listAccounts("codearts")).find((a) => a.id === id);
        } catch {
          entry = void 0;
        }
      }
      if (!entry) {
        const available2 = await pool.getAvailableAccount("codearts", "");
        entry = available2?.entry;
      }
      if (entry) await service.refreshAccountCredential(entry.credentialRef, pool, entry.id);
    },`

// ── 补丁 3：按需续期把凭据传下去 ───────────────────────────────────────────
const ONDEMAND_OLD = `      refresh: async () => {
        await this.options.refresh();
        return await this.options.resolveCredential();
      },`

const ONDEMAND_NEW = `      refresh: async () => {
        // 补丁：把本次要续期的凭据传下去，让 refresh() 续对的账号。
        await this.options.refresh(credential);
        return await this.options.resolveCredential();
      },`

// ── 补丁 4：探活补齐 Raccoon / Loomy 适配器 ─────────────────────────────────
const PROBE_ADAPTER_OLD = `  } else if (entry.provider === GEMINI.id) {
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
  }`

const PROBE_ADAPTER_NEW = `  } else if (entry.provider === GEMINI.id) {
    adapter = new GeminiAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      },
      sessionLane: GEMINI_SESSION_LANE_SMOKE,
      project: GEMINI_DEFAULT_PROJECT,
      product: GEMINI
    });
  } else if (entry.provider === RACCOON.id) {
    // ⚠️ 补丁：探活支持 Raccoon 渠道。
    // 原实现未对 RACCOON 做分支判断，导致 raccoon 账号探活时落入 else，
    // 被错误地构造成 CodeArtsAdapter，进而报「codearts: no usable credential; log in first」。
    adapter = new RaccoonAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      },
      product: RACCOON
    });
  } else if (entry.provider === LOOMY.id) {
    adapter = new LoomyAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      },
      product: LOOMY
    });
  } else {
    adapter = new CodeArtsAdapter({
      credentialRef: ref,
      resolveCredential: async () => credential,
      refresh: async () => {
      }
    });
  }`

// ── 补丁 5：APIG.0101（接口未发布）判据 ────────────────────────────────────
const UNPUBLISHED_HELPER_OLD = `function isAuthError(status, body) {
  if (isCodeArtsNonAuthRejection(body)) return false;`

const UNPUBLISHED_HELPER_NEW = `/**
 * 是否为「该 API 在网关里没有发布」的拒绝（华为云 APIG.0101）。
 *
 * 原文：\`The API does not exist or has not been published in the environment\`。
 * 这不是鉴权问题（那是 APIG.0602/0301），也不是额度问题：请求到了云网关就被
 * 路由层退掉，业务后端根本没收到。
 *
 * 实测（2026-11-09）：\`glm-5.3-flash\`、\`deepseek-v4.1-flash\` 两个**权益模型**
 * 恒报此错，而 \`GLM-5.2\`/\`glm-5.3\`/\`kimi-k3\`/\`openpangu-*\` 全部 200 —— 因为
 * 权益模型会额外带 \`maas_type: benefit\` 签名头，走的是另一条未对当前租户发布
 * 的 API。去掉该头即可回到普通通道，故此处只做判据，由调用方决定重试。
 */
function isCodeArtsApiUnpublished(body) {
  if (typeof body !== "string" || body.length === 0) return false;
  return /does not exist or has not been published|APIG\\.0101/i.test(body);
}
function isAuthError(status, body) {
  if (isCodeArtsNonAuthRejection(body)) return false;`

// ── 补丁 6：SSE 内嵌错误给出专用错误码 ─────────────────────────────────────
const UNPUBLISHED_RAISE_OLD = `            if (data.error_code === CODEARTS_BENEFIT_NOT_FOUND_ERROR_CODE) {
              throw new LlmError2(\`codearts: \${message2}\`, BENEFIT_NOT_FOUND_CODE, { status: 200 });
            }
            throw new LlmError2(\`codearts: \${message2}\`, "INVALID_REQUEST", { status: 200 });`

const UNPUBLISHED_RAISE_NEW = `            if (data.error_code === CODEARTS_BENEFIT_NOT_FOUND_ERROR_CODE) {
              throw new LlmError2(\`codearts: \${message2}\`, BENEFIT_NOT_FOUND_CODE, { status: 200 });
            }
            // 补丁：区分「接口未发布」与「请求格式不对」。
            // 上游把 APIG.0101 塞进 SSE 的 error 事件（HTTP 仍是 200），旧实现一律
            // 归成 INVALID_REQUEST，于是用户看到一句英文原文、也没有任何重试机会。
            if (isCodeArtsApiUnpublished(message2)) {
              throw new LlmError2(\`codearts: \${message2}\`, "BENEFIT_API_UNPUBLISHED", { status: 200 });
            }
            throw new LlmError2(\`codearts: \${message2}\`, "INVALID_REQUEST", { status: 200 });`

// ── 补丁 7：权益模型先丢 maas_type 头重试，仍失败再给人话 ──────────────────
const UNPUBLISHED_RETRY_OLD = `          } else if (error instanceof LlmError2 && error.code === BENEFIT_NOT_FOUND_CODE && isBenefitModel && !benefitHeaderDropped) {
            benefitHeaderDropped = true;
            continue;`

const UNPUBLISHED_RETRY_NEW = `          } else if (error instanceof LlmError2 && (error.code === BENEFIT_NOT_FOUND_CODE || error.code === "BENEFIT_API_UNPUBLISHED") && isBenefitModel && !benefitHeaderDropped) {
            // 补丁：权益通道未发布（APIG.0101）时先摘掉 maas_type 头走普通通道。
            // 与「权益额度用尽」同一套回退逻辑：权益通道不可用不等于模型不可用。
            benefitHeaderDropped = true;
            continue;
          } else if (error instanceof LlmError2 && error.code === "BENEFIT_API_UNPUBLISHED") {
            // 摘头之后仍然未发布：说明该模型在当前账号所属环境确实没有对应 API，
            // 这时给一句可执行的中文，而不是让英文网关原文直接冒到对话里。
            throw new LlmError2(
              \`codearts: 上游接口未发布（APIG.0101）——模型 \${options.model} 在当前账号所属环境没有对应 API。\` +
                "该模型属于权益（benefit）通道，需账号已开通权益；否则请换用普通模型（如 GLM-5.2 / glm-5.3 / kimi-k3）。",
              "INVALID_REQUEST",
              { cause: error }
            );`

const patches = [
  { name: 'credits.balances 续期', from: BALANCE_OLD, to: BALANCE_NEW,
    mark: '补丁：先按账号续期' },
  { name: 'refresh 绑定账号', from: REFRESH_OLD, to: REFRESH_NEW,
    mark: '续期绑定**当前这次请求正在用的账号**' },
  { name: 'on-demand 传凭据', from: ONDEMAND_OLD, to: ONDEMAND_NEW,
    mark: 'this.options.refresh(credential);' },
  { name: 'probe Raccoon/Loomy 适配器', from: PROBE_ADAPTER_OLD, to: PROBE_ADAPTER_NEW,
    mark: 'entry.provider === RACCOON.id' },
  { name: 'APIG.0101 判据', from: UNPUBLISHED_HELPER_OLD, to: UNPUBLISHED_HELPER_NEW,
    mark: 'function isCodeArtsApiUnpublished' },
  { name: 'APIG.0101 专用错误码', from: UNPUBLISHED_RAISE_OLD, to: UNPUBLISHED_RAISE_NEW,
    mark: 'BENEFIT_API_UNPUBLISHED' },
  { name: 'APIG.0101 丢 benefit 头重试', from: UNPUBLISHED_RETRY_OLD, to: UNPUBLISHED_RETRY_NEW,
    mark: '未发布（APIG.0101）' },
]

let source = readFileSync(PACK, 'utf8')
const origin = source
const applied = []

for (const patch of patches) {
  // 判「已打过」要看补丁**独有**的痕迹，不能用 `to` 的开头——
  // 那几十个字符往往和原文相同（上下文锚点），会把未打过的文件误判成已打。
  if (source.includes(patch.mark)) {
    applied.push(`${patch.name}: 已打过，跳过`)
    continue
  }
  const hits = source.split(patch.from).length - 1
  if (hits !== 1) {
    console.error(`[patch] ${patch.name}: 原文出现 ${hits} 次（期望 1 次），放弃`)
    process.exit(1)
  }
  source = source.replace(patch.from, patch.to)
  applied.push(`${patch.name}: 已打`)
}

if (source === origin) {
  console.log('[patch] 无需改动')
  for (const line of applied) console.log('  ' + line)
  process.exit(0)
}

if (DRY) {
  console.log('[patch] dry-run，未写入')
  for (const line of applied) console.log('  ' + line)
  process.exit(0)
}

const backup = `${PACK}.bak-codearts-refresh-${new Date().toISOString().replace(/[:.]/g, '-')}`
if (!existsSync(backup)) copyFileSync(PACK, backup)

// 语法自检：宁可这里失败，也不要把一个坏文件留给运行时。
const { execFileSync } = await import('node:child_process')
const tmp = `${PACK}.patched-check.mjs`
writeFileSync(tmp, source, 'utf8')
try {
  execFileSync(process.execPath, ['--check', tmp], { stdio: 'pipe' })
} catch (error) {
  console.error('[patch] 语法检查失败，未写入:', String(error?.stderr ?? error))
  process.exit(1)
} finally {
  const { unlinkSync } = await import('node:fs')
  try { unlinkSync(tmp) } catch {}
}

writeFileSync(PACK, source, 'utf8')
console.log('[patch] 已写入', PACK)
console.log('[patch] 备份', backup)
for (const line of applied) console.log('  ' + line)
