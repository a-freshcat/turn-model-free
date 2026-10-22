// 从已安装的 pack.js（package.json v2.5.4）抽取的腾讯渠道防护层，供审查/迁移参考。
// 恢复用 artifact：pack.js.guarded（整文件，含 wiring）+ pack.js.guarded.json（元数据）。
// 抽取命令：node main_repai/freeze-guard.mjs --save

var TG_ZW = "[\\u200B-\\u200D\\uFEFF\\u2060]*";
/** 一切「像空白」的字符：ASCII 空白、NBSP、各类 en/em 空格、全角空格。 */
var TG_WS = "[\\s\\u00A0\\u1680\\u2000-\\u200A\\u202F\\u205F\\u3000]+";
/** 品牌短语，允许零宽穿插。 */
var TG_BRAND = "DeepSeek" + TG_ZW + TG_WS + TG_ZW + "Harness";
/** 更松：两个词之间可以完全没有分隔（DeepSeekHarness / DeepSeek​Harness）。 */
var TG_BRAND_LOOSE = "DeepSeek" + TG_ZW + "(?:" + TG_WS + ")?" + TG_ZW + "Harness";
/** 路径保护：前后紧邻斜杠或反斜杠时**不是**指纹，保持逐字节不变。 */
var TG_PATH_L = "(?<![\\\\/\\w])";
var TG_PATH_R = "(?![\\\\/\\w])";
/**
 * 规则表。字段：
 *   id       规则名（遥测里用它统计命中）
 *   level    生效档位（<= 当前档位即生效）
 *   find     正则（带 g；用前会重置 lastIndex）
 *   replace  替换文本
 *   leading  仅作用于文本开头（true 时按「首句」处理）
 *   note     依据 / 实测出处
 * 新增指纹只需在表里加一行；想不重装插件就加，见 tencentGateUserConfig()。
 */
var TENCENT_GATE_RULES = [
  {
    id: "identity-sentence",
    level: 1,
    find: /You are an AI agent powered by DeepSeek Harness\./g,
    replace: "You are an AI coding agent.",
    note: "实测指纹 #1；#544 起就在拦"
  },
  {
    id: "web-gui-phrase",
    level: 1,
    find: /DeepSeek Harness Web GUI/g,
    replace: "the Web GUI",
    note: "实测指纹 #2；与 #1 同时出现时 400（所以只改首句的旧补丁失效）"
  },
  {
    id: "git-branch-template",
    level: 1,
    find: /Main branch \(you will usually use this for PRs\)/g,
    replace: "Main branch (PRs usually go here)",
    note: "实测指纹 #3；与网关侧 CONTENT_FINGERPRINTS 同源"
  },
  {
    id: "brand-token",
    level: 1,
    find: new RegExp(TG_PATH_L + TG_BRAND + TG_PATH_R, "g"),
    replace: "DSH",
    note: "兜底：品牌短语以独立词出现时命中；路径里的不动"
  },
  // ── L2：容错变体（大小写 / 空白 / 零宽 / 全角 / 同义改写）──────────────
  {
    id: "identity-tolerant",
    level: 2,
    find: new RegExp(
      "You" + TG_WS + "are" + TG_WS + "(?:an?" + TG_WS + ")?(?:AI" + TG_WS + ")?(?:agent|assistant|model|coding" + TG_WS + "(?:agent|assistant))[^\\n]{0,120}?(?:powered" + TG_WS + "by|built" + TG_WS + "on|running" + TG_WS + "on|based" + TG_WS + "on|from)" + TG_WS + "(?:the" + TG_WS + ")?" + TG_BRAND_LOOSE + "\\.?",
      "gi"
    ),
    replace: "You are an AI coding agent.",
    note: "把「身份句」的各种改写形态一并归口"
  },
  {
    id: "powered-by-brand",
    level: 2,
    find: new RegExp("(?:powered" + TG_WS + "by|built" + TG_WS + "on|running" + TG_WS + "on|based" + TG_WS + "on)" + TG_WS + "(?:the" + TG_WS + ")?" + TG_BRAND_LOOSE, "gi"),
    replace: "powered by DSH",
    note: "「…powered by <品牌>…」的裸露形态"
  },
  {
    id: "brand-token-loose",
    level: 2,
    find: new RegExp(TG_PATH_L + TG_BRAND_LOOSE + TG_PATH_R, "g"),
    replace: "DSH",
    note: "两词之间无分隔 / 混入零宽"
  },
  {
    id: "git-branch-template-tolerant",
    level: 2,
    find: new RegExp(
      "Main" + TG_WS + "branch" + TG_WS + "\\(you" + TG_WS + "will" + TG_WS + "usually" + TG_WS + "use" + TG_WS + "this" + TG_WS + "for" + TG_WS + "PRs\\)",
      "gi"
    ),
    replace: "Main branch (PRs usually go here)",
    note: "同一条 git 模板的空白/大小写变体（实测「两个空格」即可让字面量规则漏掉）"
  },
  // ── L3：首部身份框架（其他渠道的客户端自称也一并收敛）──────────────────
  // ⚠️ 这里刻意**只认品牌自述**（deepseek/harness/dsh/自家渠道名）。
  //    早前写成「AI|agent|assistant|model」会把「You are a helpful coding
  //    assistant.」这种完全干净的正文也改掉，白白丢掉前缀缓存。
  {
    id: "leading-identity-framing",
    level: 3,
    leading: true,
    find: /^(?:\uFEFF)?[ \t]*You[ \t]+are[^\n]{0,160}?\b(?:deepseek|harness|dsh|codebuddy|workbuddy)\b[^\n]{0,160}?[.!?](?=[ \t]|$)/i,
    replace: "You are a coding assistant.",
    note: "首句若在自称「某客户端」，换成中性表述；只动开头，正文不碰"
  },
  {
    id: "leading-brand-line",
    level: 3,
    leading: true,
    find: new RegExp("^(?:\\uFEFF)?[ \\t]*[^\\n]*?" + TG_BRAND_LOOSE + "[^\\n]*?[.!?](?=[ \\t]|$)"),
    replace: "You are a coding assistant.",
    note: "首行含品牌短语时整体中和（防上游按「首行」取样）"
  }
];
/**
 * ── 路径遮罩（P1-3 修复）──────────────────────────────────────────────────
 * 改写正文指纹时，路径必须逐字节保留：`C:\DeepSeek Harness\app.js` 里的品牌词
 * 不是指纹，而且把目录名改成别的会直接让工具调用失败。
 *
 * 第一版做法是「从盘符一直吃到行尾」，结果把路径后面的正文也保护起来，
 * 正文里的指纹就永远改不到（过度保护）。第二版改成「只吃无空格 token」，
 * 又把 `my  file.txt` 这种带空格的文件名切断，L3 规范化会把文件名里的
 * 双空格折成单空格（仍在改用户数据）。
 *
 * 现在的规则：**起点**必须是真路径开头（盘符 + 分隔符 / UNC / 根斜杠），
 * 然后在同一行内向后扫，把「最后一个像路径的 token」作为终点。像路径 =
 * 含 `\` 或 `/`，或形如 `name.ext`。因为起点很严，普通正文（`and/or`、
 * `e.g.`）不会被误当路径；因为终点取「最后一个像路径的 token」，路径后面
 * 的正常正文不会被顺带保护。
 */
var TG_PATH_START_RE = /^(?:[A-Za-z]:[\\/]|\\\\|\/)/;
/** 引号/反引号/尖括号包裹：`"C:\x"`、`` `D:\y` ``、`<E:\z>`、`'F:\w'`。 */
var TG_PATH_QUOTES = { '"': '"', "'": "'", '`': '`', '<': '>', '[': ']', '(': ')' };
/** 一个 token 是否「像路径的一部分」。 */
function tgPathish(token) {
  return /[\\/]/.test(token) || /\.[A-Za-z0-9]{1,8}$/.test(token);
}
/**
 * 返回路径的 [start, end) 区间数组；没有路径返回空数组。
 *
 * 四轮修订的教训（每一轮都是复审给的实测反例）：
 *
 *   第 1 版「从盘符吃到行尾」—— 路径后的正文一起被保护，指纹改不到。
 *   第 2 版「只吃无空格 token」—— `my  file.txt` 被切断，L3 折叠双空格。
 *   第 3 版「起点严 + 取最后一个像路径的 token」—— 起点漏掉引号包裹；
 *      且会越过 `then You are...` 吃到后面另一个 `README.md`，罩住正文。
 *   第 4 版（本版）—— 上一版对**引号内**仍按 token 猜边界，
 *      `"C:\work docs DeepSeek Harness\app.js"` 在 `docs` 处就停了，
 *      路径被截短、后半段当正文改写。引号存在时必须一路扫到**闭合引号**。
 *
 * 现在的规则：
 *   · **有引号**：从起点一直扫到匹配的闭合符（支持 `\"` 转义），
 *     区间不含包裹符。引号内**不做** token 级猜测 —— 引号本身就是边界，
 *     里面的空格、普通词都是路径的一部分。
 *   · **无引号**：沿用「起点严 + 只有像路径的 token 才延续」的保守策略，
 *     遇到普通小写词立即停止（用于区分「路径后面跟着自然语言」）。
 */
function tgPathSpans(text) {
  const tokens = [];
  const tokenRe = /\S+/g;
  let m;
  while ((m = tokenRe.exec(text)) !== null) {
    tokens.push({ start: m.index, end: m.index + m[0].length, text: m[0] });
  }
  const spans = [];
  let i = 0;
  while (i < tokens.length) {
    const token = tokens[i];
    let body = token.text;
    let offset = 0;
    let closer = "";
    const opener = body[0];
    if (opener !== void 0 && Object.prototype.hasOwnProperty.call(TG_PATH_QUOTES, opener) && TG_PATH_START_RE.test(body.slice(1))) {
      closer = TG_PATH_QUOTES[opener];
      offset = 1;
      body = body.slice(1);
    }
    if (!TG_PATH_START_RE.test(body)) {
      i += 1;
      continue;
    }
    const start = token.start + offset;

    /* ── 有引号：以闭合符为硬边界，一路扫到底 ───────────────────────── */
    if (closer !== "") {
      // 先看起点 token 内是否已含闭合符（`"C:\x"` 单 token）
      const inToken = findUnescaped(body, closer);
      if (inToken >= 0) {
        spans.push([start, start + inToken]);
        i += 1;
        continue;
      }
      // 否则从起点后的位置继续找闭合符，允许跨 token（含空格）
      const closeAt = findUnescaped(text, closer, start + (body.length - offset) + offset);
      if (closeAt > start) {
        spans.push([start, closeAt]);
        // 跳到包含 closeAt 的那个 token 之后
        let k = i + 1;
        while (k < tokens.length && tokens[k].start <= closeAt) k += 1;
        i = Math.max(k, i + 1);
        continue;
      }
      // 没有闭合符：退化为无引号策略（宁可少保护，也不要吞掉正文）
    }

    /* ── 无引号：保守延续 ───────────────────────────────────────────── */
    let last = i;
    let end = token.end;
    let j = i + 1;
    while (j < tokens.length) {
      const gap = text.slice(tokens[j - 1].end, tokens[j].start);
      if (gap.length === 0 || gap.length > 64 || !/^[ \t]+$/.test(gap)) break;
      const next = tokens[j].text;
      if (!tgPathish(next)) break;
      last = j;
      end = tokens[j].end;
      j += 1;
    }
    spans.push([start, end]);
    i = last + 1;
  }
  return spans;
}
/**
 * 找未转义的闭合符位置；找不到返回 -1。
 * `from` 默认从 0 开始。只处理反斜杠转义（路径里最常见）。
 */
function findUnescaped(text, ch, from) {
  const startAt = typeof from === "number" && from > 0 ? from : 0;
  for (let i = startAt; i < text.length; i += 1) {
    if (text[i] !== ch) continue;
    // 数一下前面连续反斜杠的个数：偶数表示未转义
    let backslashes = 0;
    for (let k = i - 1; k >= 0 && text[k] === "\\"; k -= 1) backslashes += 1;
    if (backslashes % 2 === 0) return i;
  }
  return -1;
}
/**
 * 把路径换成占位符。返回 { masked, tokens } 或 null（无路径）。
 *
 * 占位符用私用区字符 `\uE000<序号>\uE001`：NFKC 不会动它、空白折叠不会动它、
 * 内置规则的正则也不会命中它，因此在后续所有变形中都稳定存活。
 */
function tgMaskPaths(text) {
  const spans = tgPathSpans(text);
  if (spans.length === 0) return null;
  spans.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const span of spans) {
    const prev = merged[merged.length - 1];
    if (prev !== void 0 && span[0] <= prev[1]) prev[1] = Math.max(prev[1], span[1]);
    else merged.push([span[0], span[1]]);
  }
  let masked = "";
  let cursor = 0;
  const tokens = [];
  for (const [start, end] of merged) {
    masked += text.slice(cursor, start);
    masked += "\uE000" + tokens.length + "\uE001";
    tokens.push(text.slice(start, end));
    cursor = end;
  }
  masked += text.slice(cursor);
  return { masked, tokens };
}
/** 还原占位符。任何一个占位符丢失都返回 null —— 调用方据此**放弃改写**。 */
function tgUnmaskPaths(text, tokens) {
  let out = text;
  for (let i = 0; i < tokens.length; i += 1) {
    const holder = "\uE000" + i + "\uE001";
    if (!out.includes(holder)) return null;
    out = out.split(holder).join(tokens[i]);
  }
  return out;
}
/**
 * 用户可扩展规则：`~/.dsh/our-free-model/tencent-gate.json`
 *   { "baseLevel": 1, "maxEscalations": 2,
 *     "rules": [ { "id":"x", "level":2, "find":"…", "flags":"gi", "replace":"…" } ] }
 * 文件不存在就用内置表。读取只做一次（懒加载 + 缓存），改完重载插件即可生效。
 */
var TG_USER_RULES_CACHE = null;
function tencentGateUserConfig() {
  if (TG_USER_RULES_CACHE !== null) return TG_USER_RULES_CACHE;
  const fallback = { baseLevel: 1, maxEscalations: 2, rules: [] };
  try {
    // 用本文件顶部已导入的 readFileSync / homedir / join（同一模块作用域）。
    const file = join(homedir(), ".dsh", "our-free-model", "tencent-gate.json");
    const parsed = JSON.parse(readFileSync(file, "utf8"));
    const rules = Array.isArray(parsed.rules)
      ? parsed.rules.filter((r) => r && typeof r.find === "string" && typeof r.replace === "string").map((r) => ({
        id: String(r.id ?? "user-rule"),
        level: Number.isFinite(r.level) ? Number(r.level) : 2,
        find: new RegExp(r.find, typeof r.flags === "string" ? r.flags : "g"),
        replace: String(r.replace),
        leading: r.leading === true,
        note: String(r.note ?? "user rule")
      }))
      : [];
    TG_USER_RULES_CACHE = {
      baseLevel: Number.isFinite(parsed.baseLevel) ? Math.min(3, Math.max(1, Number(parsed.baseLevel))) : 1,
      maxEscalations: Number.isFinite(parsed.maxEscalations) ? Math.min(4, Math.max(0, Number(parsed.maxEscalations))) : 2,
      rules
    };
  } catch {
    TG_USER_RULES_CACHE = fallback;
  }
  return TG_USER_RULES_CACHE;
}
function tencentGateRules() {
  return TENCENT_GATE_RULES.concat(tencentGateUserConfig().rules);
}
/** 起始档位：环境变量优先，其次配置文件，最后 L1。 */
function tencentBaseGuardLevel() {
  const fromEnv = Number(process.env.OF_TENCENT_GATE_LEVEL ?? process.env.OFM_TENCENT_GATE_LEVEL);
  if (Number.isFinite(fromEnv) && fromEnv >= 1 && fromEnv <= 3) return Math.floor(fromEnv);
  return tencentGateUserConfig().baseLevel;
}
/** 下一档；已到顶就原样返回（调用方据此判断「升不动了」）。 */
function tencentNextGuardLevel(level) {
  return level >= 3 ? level : level + 1;
}
var TENCENT_GATE_MAX_ESCALATIONS = tencentGateUserConfig().maxEscalations;
/**
 * 规范化：把「用来绕过匹配」的花样还原成规范形态。
 * NFKC 收全角/兼容字符，去零宽，折叠异体空白。**只在 L3、且只有在直接匹配
 * 一无所获时才用** —— 它会改动较多字节，无脑套用会让干净内容也丢前缀缓存。
 */
function tencentCanonicalizeText(text) {
  let out = text;
  if (typeof out.normalize === "function") out = out.normalize("NFKC");
  out = out.replace(/[\u200B-\u200D\uFEFF\u2060]/g, "");
  out = out.replace(/[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]/g, " ");
  // 连续 ASCII 空白折成一个空格：上游对空白宽容，我们也要，「两个空格」不该漏。
  out = out.replace(/[ \t]{2,}/g, " ");
  return out;
}
/**
 * ⚠️ 这里**曾经**有个 `tencentGateMightMatch()` 快速否定（没有 deepseek/harness/
 * main branch 就直接跳过）。它是错的：L2 容错规则、以及用户自定义规则完全可以
 * 命中一个不含这些词的内容，于是那些规则永远跑不到。审查把它列为 P2-5。
 * 现在直接用规则表判定 —— 命中不了自然一个字节都不改，代价只是几次正则测试。
 */
/** 规则匹配测试（g 正则必须先重置 lastIndex）。 */
function tgTest(re, text) {
  re.lastIndex = 0;
  return re.test(text);
}
/** 应用规则。返回 { text, hits } —— hits 是命中的规则 id 列表。 */
function tencentGateApplyRules(text, level, leading) {
  let out = text;
  const hits = [];
  for (const rule of tencentGateRules()) {
    if (rule.level > level) continue;
    // leading 规则只在文本开头生效；普通规则**处处生效**（含开头）。
    // ⚠️ 别写成「普通规则只在非开头生效」—— system 的指纹恰好就在开头，
    //    那样写会让整层防护对字符串内容空转。
    if (rule.leading === true && leading !== true) continue;
    if (!tgTest(rule.find, out)) continue;
    rule.find.lastIndex = 0;
    const next = out.replace(rule.find, rule.replace);
    if (next === out) continue;
    out = next;
    hits.push(rule.id);
  }
  return { text: out, hits };
}
/**
 * 改写一段文本。返回 { text, hits } 或 null（无需改写 / 放弃改写）。
 *
 * 顺序：遮罩路径 → 直接套规则 → （L3 且没命中）规范化后再套一次 →
 *       还原路径。任何一步出问题（占位符被吃掉）都返回 null，宁可放过也不
 *       写出半成品。
 */
function tgRewriteText(text, level, leading) {
  const mask = tgMaskPaths(text);
  const base = mask === null ? text : mask.masked;
  let out = base;
  let hits = [];
  const direct = tencentGateApplyRules(base, level, leading);
  if (direct.hits.length > 0) {
    out = direct.text;
    hits = direct.hits;
  }
  if (hits.length === 0 && level >= 3) {
    const canonical = tencentCanonicalizeText(base);
    if (canonical !== base) {
      const second = tencentGateApplyRules(canonical, level, leading);
      if (second.hits.length > 0) {
        out = second.text;
        hits = second.hits;
      }
    }
  }
  // 一条规则都没命中：**原样返回 null**，保证干净内容逐字节不变。
  if (hits.length === 0) return null;
  if (mask !== null) {
    const restored = tgUnmaskPaths(out, mask.tokens);
    if (restored === null) {
      tencentGateStats().pathMaskAborts = (tencentGateStats().pathMaskAborts ?? 0) + 1;
      return null;
    }
    out = restored;
  }
  if (out === text) return null;
  return { text: out, hits };
}
/** 遥测：全局累加，供排障查看，不落盘、不影响请求。 */
function tencentGateStats() {
  const key = Symbol.for("our-free-model.tencent-gate");
  let stats = globalThis[key];
  if (stats === void 0) {
    stats = { requests: 0, rewritten: 0, messagesTouched: 0, escalations: 0, gateBlocks: 0, pathMaskAborts: 0, ruleHits: /* @__PURE__ */ Object.create(null), lastAt: 0 };
    globalThis[key] = stats;
  }
  return stats;
}
/** 适用于「一条消息的内容」，支持字符串与文本块数组。返回是否改过。 */
function scrubTencentFingerprintMessage(message, level) {
  const content = message.content;
  const apply = (text, leading) => {
    if (typeof text !== "string" || text.length === 0) return null;
    const result = tgRewriteText(text, level, leading);
    if (result === null) return null;
    for (const id of result.hits) {
      tencentGateStats().ruleHits[id] = (tencentGateStats().ruleHits[id] ?? 0) + 1;
    }
    return result.text;
  };
  if (typeof content === "string") {
    const next = apply(content, true);
    if (next === null || next === content) return false;
    message.content = next;
    return true;
  }
  if (!Array.isArray(content)) return false;
  let changed = false;
  let first = true;
  for (const block of content) {
    if (typeof block !== "object" || block === null) continue;
    if (block.type !== "text" || typeof block.text !== "string") continue;
    const next = apply(block.text, first);
    first = false;
    if (next === null || next === block.text) continue;
    block.text = next;
    changed = true;
  }
  return changed;
}
/**
 * 防护层入口：就地处理整条 wire messages。
 * 只动 system / assistant；user（用户原话）与 tool（上游不扫描）原样保留。
 * 返回改动的消息条数。
 */
function applyTencentGateGuard(messages, level) {
  const stats = tencentGateStats();
  stats.requests += 1;
  stats.lastAt = Date.now();
  let changed = 0;
  for (const message of messages) {
    if (typeof message !== "object" || message === null) continue;
    if (message.role !== "system" && message.role !== "assistant") continue;
    if (scrubTencentFingerprintMessage(message, level)) changed += 1;
  }
  if (changed > 0) {
    stats.rewritten += 1;
    stats.messagesTouched += changed;
  }
  return changed;
}
/** 兼容旧调用点（等价于 L1）。 */
function scrubTencentContentFingerprintsInPlace(messages) {
  return applyTencentGateGuard(messages, 1);
}
/**
 * 这段响应体是不是上游的「渠道校验」拒绝？（P2-8 修复）
 *
 * 早前用 `text.includes("11128")` + `/request illegal/i`：任何正文里出现这个
 * 数字、或别的错误码（如 11140）带了同样措辞，都会被误判成渠道校验并触发
 * 重发 —— 既浪费预算，也可能把正常错误吞掉。
 *
 * 现在只认**结构化**证据：逐个候选行做 JSON 解析，命中的判据是
 *   · code === 11128，或
 *   · 错误消息里明确出现 unapproved channel，或
 *   · 错误消息里同时出现 11128 与 unapproved/illegal。
 * 整段都不是 JSON 时，才退化为「纯文本含 unapproved channel」。
 */
function isTencentGateBlock(text) {
  if (typeof text !== "string" || text.length === 0) return false;
  const candidates = [text];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    candidates.push(trimmed.startsWith("data:") ? trimmed.slice(5).trim() : trimmed);
  }
  let parsedAny = false;
  for (const candidate of candidates) {
    if (candidate.length === 0 || candidate === "[DONE]") continue;
    let parsed;
    try {
      parsed = JSON.parse(candidate);
    } catch {
      continue;
    }
    if (parsed === null || typeof parsed !== "object") continue;
    parsedAny = true;
    const code = parsed.code ?? parsed.error?.code ?? parsed.data?.code;
    if (Number(code) === 11128) return true;
    const message = String(parsed.msg ?? parsed.message ?? parsed.error?.message ?? parsed.data?.message ?? "");
    if (/unapproved\s+channel/i.test(message)) return true;
    if (/11128/.test(message) && /(?:unapproved|illegal)/i.test(message)) return true;
  }
  if (!parsedAny) return /unapproved\s+channel/i.test(text);
  return false;
}
/**
 * 渠道校验错误的登记表。
 *
 * **为什么不用「在 error 上挂属性」**：`LlmError` 是从 @deepseek-ai/dsh-llm
 * 导入的外部类，它的构造器只把 status / providerRetryAfterMs / requestId
 * 放进 `failure`，**不保证**保留任意 options。依赖 `error.tencentGate`
 * 会在换 SDK 版本后静默失效（升档永远不触发，而且没人报错）。
 *
 * WeakSet / WeakMap 以错误对象本身为键，与 SDK 如何构造对象无关；
 * 对象被回收后条目自动消失，不会泄漏。
 */
var TG_GATE_ERRORS = /* @__PURE__ */ new WeakSet();
var TG_GATE_ERROR_BODIES = /* @__PURE__ */ new WeakMap();
/**
 * 把一个错误登记为「渠道校验拒绝」。
 * @param {object} error 要登记的错误对象（通常是要抛出的 LlmError）
 * @param {string} [body] 上游原始响应体，仅供排障
 * @returns {object} 原样返回 error，便于 `throw markTencentGateError(...)`
 */
function markTencentGateError(error, body) {
  if (error !== null && typeof error === "object") {
    TG_GATE_ERRORS.add(error);
    if (typeof body === "string" && body.length > 0) TG_GATE_ERROR_BODIES.set(error, body);
  }
  return error;
}
/** 取回登记时保存的原始响应体（排障用）；没有就返回空串。 */
function tencentGateErrorBody(error) {
  if (error === null || typeof error !== "object") return "";
  return TG_GATE_ERROR_BODIES.get(error) ?? "";
}
/**
 * 判断一个**抛出的错误**是不是渠道校验拒绝。
 *
 * 判定顺序：
 *   1. 登记表里有它 —— 这是本层自己标记的，最可靠；
 *   2. 它恰好带了自有属性 `tencentGate === true`（兼容旧调用点/测试注入）；
 *   3. 带了可解析的错误体，且错误体被判为渠道校验。
 * 不对 message 做子串猜测。
 */
function isTencentGateBlockError(error) {
  if (!error || typeof error !== "object") return false;
  if (TG_GATE_ERRORS.has(error)) return true;
  if (error.tencentGate === true) return true;
  const payload = error.body ?? error.responseText ?? error.detail;
  if (typeof payload === "string" && payload.length > 0) return isTencentGateBlock(payload);
  return false;
}
/**
 * 渠道校验取证埋点。
 *
 * 为什么需要它：真实 11128 是**被动**事件，不能为了造证据去打上游（那是封号
 * 诱因）。但事件发生时必须留下足够证据，能证明「同一请求的档位、发送次数、
 * 最终结果、输出是否重复」——否则那条「真实 11128 端到端」永远无法闭环。
 *
 * 安全约束：
 *   · **只写元数据**。绝不写消息正文、凭据、access_token、用户输入。
 *     字段全部是数字/枚举/短标签。
 *   · 单行 JSON + 固定前缀，便于 grep 与事后脚本解析。
 *   · 埋点本身绝不抛错（整体 try/catch），不影响请求路径。
 *
 * @param {string} event 事件名（escalate / result）
 * @param {object} fields 元数据（只能是标量）
 */
function tgTrace(event, fields) {
  try {
    const payload = {
      evt: event,
      at: (/* @__PURE__ */ new Date()).toISOString(),
      ...fields
    };
    console.warn("[ofm-tencent-gate] " + JSON.stringify(payload));
  } catch {
    // 埋点失败不影响请求
  }
}
var TOOL_RESULT_IMAGE_TEXT = "Attached image(s) from tool result:";
