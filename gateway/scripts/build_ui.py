"""Build the channel runtime from the untouched local free-m-clone snapshot.

Only channel metadata changes: replace the Gemini card with Antigravity.
The original card components, hooks, CSS, design tokens and layout stay intact.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web/vendor/free-m-clone/dist/demo.js"
TARGET = ROOT / "web/channel-runtime.js"

source = SOURCE.read_text(encoding="utf-8")
old = "{ id: 'gemini', name: 'Gemini', org: 'Google Code Assist', accent: '#4285F4', note: '本地回调 OAuth 免费线', login: 'browser' }"
new = "{ id: 'antigravity', name: 'Antigravity', org: 'Google', accent: '#4285F4', note: 'Google OAuth · 账号与模型配额', login: 'browser' }"
credit = "gemini: { unit: 'quota', quota: true },"
for marker in (old, credit):
    if source.count(marker) != 1:
        raise RuntimeError("Upstream channel metadata changed; review before rebuilding")
source = source.replace(old, new).replace(credit, "antigravity: { unit: 'quota', quota: true },")
# Do not display the old Gemini logo as an Antigravity logo. The upstream
# component's standard two-letter fallback uses the same original icon frame.
source = source.replace("'gemini': 'data:image/", "'legacy-gemini': 'data:image/")
# Keep the original components/styles, replacing only the quota rendering and
# the Antigravity catalog's default visibility.
state_marker = "const [models, setModels] = useState(undefined)"
list_marker = "h('div', { className: 'ofm_modellist' }, models.map(model => h('div', {"
quota_marker = "const reading = creditCapability.quota"
for marker in (state_marker, list_marker, quota_marker):
    if source.count(marker) != 1:
        raise RuntimeError("Upstream UI seam changed; review before rebuilding")
source = source.replace(state_marker, state_marker + "\n      const [showAllModels, setShowAllModels] = useState(false)")
source = source.replace(list_marker,
    "channel.id === 'antigravity' ? h('button', { type: 'button', className: 'ofm_minibtn', "
    "onClick: () => setShowAllModels(value => !value) }, showAllModels ? '收起其他型号' : "
    "'其他 Gemini / 内部型号（' + models.filter(model => model.pool === 'gemini_shared').length + '）') : null,\n"
    "h('div', { className: 'ofm_modellist' }, models.filter(model => channel.id !== 'antigravity' || "
    "showAllModels || model.pool !== 'gemini_shared').map(model => h('div', {")
source = source.replace(quota_marker,
    "if (creditCapability.quota) return h('a', { className: 'ofm_creditbadge strong', "
    "href: '/quota?account=' + encodeURIComponent(row.id), target: '_blank', rel: 'noopener', "
    "title: '查看三个独立配额池' }, '查看配额 · 三个池');\n                    " + quota_marker)
model_name_marker = "h('span', { className: 'ofm_id', title: model.id }, model.name ?? model.id),"
if source.count(model_name_marker) != 1:
    raise RuntimeError("Upstream model label seam changed; review before rebuilding")
source = source.replace(model_name_marker, model_name_marker +
    "\n                model.pool ? h('span', { className: 'ofm_tagpill' }, "
    "({argon:'Argon 池',claude_gpt:'Claude / GPT 池',gemini_shared:'Gemini / 内部共享池'})[model.pool]) : null,")

# The upstream page renders credit buttons from a static table that describes
# the plugin's 13 fully-implemented channels. This side implements a subset, so
# the page must defer to what `provider.status` reports for the current channel,
# otherwise it paints buttons that can only ever fail.
cap_state_marker = "const creditCapability = CHANNEL_CREDITS[channel.id]"
if source.count(cap_state_marker) != 1:
    raise RuntimeError("Upstream credit capability seam changed; review before rebuilding")
source = source.replace(cap_state_marker, cap_state_marker +
    "\n      // 网关实际实现的能力优先于插件的静态表：表里有、这边没做的东西，"
    "\n      // 按钮渲染出来只会点成一句「操作失败」。缺失时回落到静态表。"
    "\n      const declared = status?.capabilities"
    "\n      const can = key => declared === undefined ? CHANNEL_CREDITS[channel.id]?.[key] === true"
    "\n        : declared[key] === true", 1)

for old_expr, new_expr in (
    ("creditCapability?.claim ? h('button'", "can('claim') ? h('button'"),
):
    if source.count(old_expr) != 1:
        raise RuntimeError(f"Upstream capability gate seam changed: {old_expr}")
    source = source.replace(old_expr, new_expr)

# 「今日已签到」徽标：本机网关没有签到状态，`credits.status` 恒为空数组。
credit_status_gate = "if (!CHANNEL_CREDITS[channel.id]?.status) return"
if source.count(credit_status_gate) != 1:
    raise RuntimeError("Upstream credit status gate seam changed; review before rebuilding")
source = source.replace(credit_status_gate,
    "if (status?.capabilities ? status.capabilities.creditStatus !== true"
    " : !CHANNEL_CREDITS[channel.id]?.status) return")
credits_deps = "      }, [rpc, channel.id])\n      const loadBalances = useCallback"
if source.count(credits_deps) != 1:
    raise RuntimeError("Upstream loadCredits deps seam changed; review before rebuilding")
source = source.replace(credits_deps,
    "      }, [rpc, channel.id, status])\n      const loadBalances = useCallback")

# 余额相关的四处渲染：卡片汇总、"刷新余额"按钮、错误提示行、账号行内的读数。
for old_expr, new_expr in (
    ("creditCapability ? h('span', { className: 'ofm_credittotal' },",
     "can('balances') ? h('span', { className: 'ofm_credittotal' },"),
    ("creditCapability ? h('button', {\n            type: 'button', className: 'ofm_btn ghost', disabled: balanceLoading || busy !== '' || !rpc,",
     "can('balances') ? h('button', {\n            type: 'button', className: 'ofm_btn ghost', disabled: balanceLoading || busy !== '' || !rpc,"),
    ("balanceError !== '' ? h('p', { className: 'ofm_balanceerror', role: 'alert' },",
     "!can('balances') ? null : balanceError !== '' ? h('p', { className: 'ofm_balanceerror', role: 'alert' },"),
    ("                    if (!creditCapability) return null",
     "                    if (!can('balances')) return null"),
):
    if source.count(old_expr) != 1:
        raise RuntimeError(f"Upstream balance render seam changed: {old_expr[:48]}")
    source = source.replace(old_expr, new_expr)

# 把「为什么这里没有余额」写在卡片上。只对**已有账号**的渠道显示：没有账号的
# 卡片没东西可解释，12 张空卡各挂一句提示只会变成噪音。
notice_anchor = "        notice !== '' ? h('p', { className: 'ofm_note' }, notice) : null,"
if source.count(notice_anchor) != 1:
    raise RuntimeError("Upstream notice seam changed; review before rebuilding")
source = source.replace(notice_anchor, notice_anchor +
    "\n        accountsCount > 0 && !can('balances') ? h('p', { className: 'ofm_note', 'data-gateway': 'no-balance' },"
    "\n          t('chan.balance.absent')) : null,")

# 余额按钮与心跳轮询只在真有余额接口时存在。
balance_gate = "if (!CHANNEL_CREDITS[channel.id] || !rpc) return Promise.resolve()"
if source.count(balance_gate) != 1:
    raise RuntimeError("Upstream balance loader seam changed; review before rebuilding")
source = source.replace(balance_gate,
    "const enabled = status?.capabilities\n"
    "          ? status.capabilities.balances === true : Boolean(CHANNEL_CREDITS[channel.id])\n"
    "        if (!enabled || !rpc) return Promise.resolve()")

balance_deps = "      }, [rpc, channel.id])\n      useEffect(() => {\n        setBalances(undefined); setBalanceError('')"
if source.count(balance_deps) != 1:
    raise RuntimeError("Upstream loadBalances deps seam changed; review before rebuilding")
source = source.replace(balance_deps,
    "      }, [rpc, channel.id, status])\n      useEffect(() => {\n        setBalances(undefined); setBalanceError('')")

heartbeat_gate = """      useEffect(() => {
        if (!CHANNEL_CREDITS[channel.id]) return undefined
        const timer = setInterval(() => { void loadBalances() }, 90_000)
        return () => clearInterval(timer)
      }, [channel.id, loadBalances])"""
if source.count(heartbeat_gate) != 1:
    raise RuntimeError("Upstream balance heartbeat seam changed; review before rebuilding")
source = source.replace(heartbeat_gate, """      useEffect(() => {
        const enabled = status?.capabilities
          ? status.capabilities.balances === true : Boolean(CHANNEL_CREDITS[channel.id])
        if (!enabled) return undefined
        const timer = setInterval(() => { void loadBalances() }, 90_000)
        return () => clearInterval(timer)
      }, [channel.id, loadBalances, status])""")

# 登录弹窗文案按「这个账号从哪来」分三档：
#   antigravity  → Google OAuth（本进程自己的 OAuth）
#   导入窗口     → import 文案（用户自己填兼容上游）
#   其余 12 家   → 厂商登录（插件 channel-pack 在后台轮询）
# 导入窗口用 `request` 查询参数区分：只有它才带 ?request=。
mode_marker = "const creditCapability = CHANNEL_CREDITS[channel.id]"
for key in ("title", "step1", "step2", "open", "waiting"):
    original = "t('chan.login." + key + "')"
    alternate = ("(channel.id === 'antigravity' ? t('chan.oauth." + key + "')"
                 " : importing ? t('chan.import." + key + "')"
                 " : " + original + ")")
    if source.count(original) < 1:
        raise RuntimeError(f"Upstream login label seam missing: {original}")
    source = source.replace(original, alternate)

# `importing`：当前登录窗口是不是导入页。
import_flag = "      const loginAttempt = useRef(null)"
if source.count(import_flag) != 1:
    raise RuntimeError("Upstream loginAttempt seam changed; review before rebuilding")
source = source.replace(import_flag,
    "      // 只有导入窗口的 loginUrl 带 `?request=`；厂商登录页不带。\n"
    "      const importing = typeof login?.url === 'string' && login.url.includes('/connect?request=')\n"
    + import_flag)

# 每日自动签到开关：本机没有签到引擎，`usage.autoCheckin` 读回 null，页面本来
# 就不渲染它。这条注入只是把「读失败」与「没有该功能」区分开，避免开关在错误
# 时闪一下就消失。
auto_marker = "const value = await rpc('usage.autoCheckin', {}, 30_000); setAuto(value?.autoCheckin ?? null)"
if source.count(auto_marker) != 1:
    raise RuntimeError("Upstream autoCheckin read seam changed; review before rebuilding")
source = source.replace(auto_marker,
    "const value = await rpc('usage.autoCheckin', {}, 30_000); setAuto(value?.autoCheckin ?? null)"
    " /* null = 本机没有签到引擎，不是读失败 */")

TARGET.write_text(source, encoding="utf-8", newline="\n")
print("Built web/channel-runtime.js; upstream source and CSS remain unchanged")
