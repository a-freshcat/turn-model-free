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
for key in ("title", "step1", "step2", "open", "waiting"):
    original = "t('chan.login." + key + "')"
    alternate = "(channel.id === 'antigravity' ? t('chan.oauth." + key + "') : " + original + ")"
    source = source.replace(original, alternate)
TARGET.write_text(source, encoding="utf-8", newline="\n")
print("Built web/channel-runtime.js; upstream source and CSS remain unchanged")
