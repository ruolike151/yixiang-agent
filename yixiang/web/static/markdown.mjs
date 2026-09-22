/* 以湘控制台 · Markdown 子集 → DOM（纯函数模块，无依赖、无构建、不碰 innerHTML）
 *
 * 三条设计决定：
 *   1. 解析与渲染分开：parseMarkdown 只吐 token（可在 Node 里直接断言），
 *      renderMarkdown 拿着 app.js 的 h() 建节点——**任何数据都只会进
 *      textContent / createTextNode**，模型输出永远当文本，不当代码；
 *   2. 只支持对话里真会出现的子集：标题、段落、粗体、斜体、行内代码、
 *      围栏代码块、无序 / 有序列表、引用、链接、软换行。刻意不做表格与
 *      内嵌 HTML：前者要宽度计算，后者是我们最不想要的东西；
 *   3. 链接只放行 http(s)：javascript: / data: 这类协议一律降级成纯文本，
 *      把 URL 整个丢掉——降级成文本而不是"渲染成不可点的链接"，是因为
 *      用户看到的字要跟模型写的一致。
 */

"use strict";

const FENCE_RE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^\s*([-*_])\s*\1\s*\1[\s-*_]*$/;
const BULLET_RE = /^\s*[-*+]\s+(.*)$/;
const NUMBERED_RE = /^\s*(\d+)[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
// URL 里允许一层圆括号：`[点我](javascript:alert(1))` 这类写法如果只认到第一个 ")"，
// 多出来的那个 ")" 会变成正文里的一段垃圾文本（实测）。
const LINK_RE = /^\[([^\]]*)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)/;
const STRONG_RE = /^\*\*([\s\S]+?)\*\*/;
const EM_RE = /^\*([^*\n][\s\S]*?)\*/;

/** 只有最朴素的两个协议放行；其余（javascript: / data: / file: / //host）一律返回空串。 */
export function safeHref(raw) {
  const value = String(raw ?? "").trim();
  return /^https?:\/\/[^\s]+$/i.test(value) ? value : "";
}

/** 行内解析：先认行内代码（反引号里的内容不再看别的记号），再认粗体 / 斜体 / 链接。 */
function parseInline(text) {
  const tokens = [];
  let buffer = "";
  let index = 0;
  const flush = () => {
    if (buffer) tokens.push({ type: "text", text: buffer });
    buffer = "";
  };
  while (index < text.length) {
    const rest = text.slice(index);
    if (rest.startsWith("`")) {
      const end = rest.indexOf("`", 1);
      if (end > 0) {
        flush();
        tokens.push({ type: "code", text: rest.slice(1, end) });
        index += end + 1;
        continue;
      }
    }
    const strong = rest.match(STRONG_RE);
    if (strong) {
      flush();
      tokens.push({ type: "strong", children: parseInline(strong[1]) });
      index += strong[0].length;
      continue;
    }
    const em = rest.match(EM_RE);
    if (em) {
      flush();
      tokens.push({ type: "em", children: parseInline(em[1]) });
      index += em[0].length;
      continue;
    }
    const link = rest.match(LINK_RE);
    if (link) {
      flush();
      const href = safeHref(link[2]);
      const label = parseInline(link[1]);
      // 协议不合法：只留链接文字（等价于原样的文本框，不是可点的 <a>）
      tokens.push(href ? { type: "link", href, children: label } : { type: "text", text: link[1] });
      index += link[0].length;
      continue;
    }
    buffer += rest[0];
    index += 1;
  }
  flush();
  return tokens;
}

function isBlockStart(line) {
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    HR_RE.test(line) ||
    BULLET_RE.test(line) ||
    NUMBERED_RE.test(line) ||
    QUOTE_RE.test(line)
  );
}

export function parseMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(FENCE_RE);
    if (fence) {
      const closer = new RegExp(`^\\s*${fence[1][0]}{3,}\\s*$`);
      const body = [];
      index += 1;
      while (index < lines.length && !closer.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // 跳过收尾围栏；流式输出里它可能还没到，缺了也不吞后面的正文
      blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    const heading = line.match(HEADING_RE);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        inline: parseInline(heading[2].trim()),
      });
      index += 1;
      continue;
    }
    if (HR_RE.test(line)) {
      blocks.push({ type: "hr" });
      index += 1;
      continue;
    }
    const bullet = line.match(BULLET_RE);
    const numbered = line.match(NUMBERED_RE);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      const items = [];
      while (index < lines.length) {
        const item = ordered ? lines[index].match(NUMBERED_RE) : lines[index].match(BULLET_RE);
        if (!item) break;
        items.push(parseInline((ordered ? item[2] : item[1]).trim()));
        index += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    const quote = line.match(QUOTE_RE);
    if (quote) {
      const body = [];
      while (index < lines.length) {
        const current = lines[index].match(QUOTE_RE);
        if (!current) break;
        body.push(current[1]);
        index += 1;
      }
      blocks.push({ type: "quote", inline: parseInline(body.join("\n")) });
      continue;
    }
    // 走到这里说明当前行不是任何块的起头，所以循环体至少进一次，不会空转
    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ type: "paragraph", inline: parseInline(paragraph.join("\n")) });
  }
  return blocks;
}

/** 文本里的软换行渲染成 <br>：DOM 会把 "\n" 当空白吞掉，不转就丢行。 */
function renderText(text, h) {
  const nodes = [];
  text.split("\n").forEach((part, position) => {
    if (position > 0) nodes.push(h("br"));
    if (part) nodes.push(part);
  });
  return nodes;
}

function renderInline(tokens, h) {
  return tokens.map((token) => {
    if (token.type === "text") return renderText(token.text, h);
    if (token.type === "code") return h("code", { text: token.text });
    if (token.type === "strong") return h("strong", {}, ...renderInline(token.children, h));
    if (token.type === "em") return h("em", {}, ...renderInline(token.children, h));
    if (token.type === "link") {
      // noopener noreferrer：新标签页拿不到 window.opener
      return h(
        "a",
        { href: token.href, target: "_blank", rel: "noopener noreferrer" },
        ...renderInline(token.children, h)
      );
    }
    return token.text ?? "";
  });
}

function renderBlock(block, h) {
  if (block.type === "heading") {
    // 页面大纲已经被 h1/h2 占了（侧栏标题、面板标题），正文标题整体下移两级
    return h(`h${Math.min(block.level + 2, 6)}`, {}, ...renderInline(block.inline, h));
  }
  if (block.type === "code") {
    return h("pre", { class: "md-code" }, h("code", { text: block.text }));
  }
  if (block.type === "list") {
    return h(
      block.ordered ? "ol" : "ul",
      {},
      ...block.items.map((item) => h("li", {}, ...renderInline(item, h)))
    );
  }
  if (block.type === "quote") {
    return h("blockquote", {}, h("p", {}, ...renderInline(block.inline, h)));
  }
  if (block.type === "hr") return h("hr");
  return h("p", {}, ...renderInline(block.inline, h));
}

export function renderMarkdown(text, { h, document: doc = globalThis.document } = {}) {
  if (typeof h !== "function") {
    throw new TypeError("renderMarkdown 需要一个节点工厂：传 app.js 的 h（{ h }）");
  }
  void doc; // 节点只经 h 创建：这里不 new 任何 DOM 类，Node 里跑用例也不需要 document
  return h("div", { class: "md" }, ...parseMarkdown(text).map((block) => renderBlock(block, h)));
}
