/* 以湘控制台 · Markdown 渲染器的用例（node --test yixiang/web/static/markdown.test.mjs） */

import assert from "node:assert/strict";
import test from "node:test";

import { parseMarkdown, renderMarkdown, safeHref } from "./markdown.mjs";

/** 记录版 h：不碰 DOM，只把 (tag, attrs, children) 记下来，好断言"到底建了什么节点"。 */
function fakeH() {
  const log = [];
  const h = (tag, attrs = {}, ...children) => {
    const node = { tag, attrs: attrs || {}, children: children.flat(Infinity) };
    log.push(node);
    return node;
  };
  return { h, log };
}

/** 把 fakeH 记下来的节点摊平成文本，用来断言"危险内容只可能是文本"。 */
function flatten(nodes) {
  const out = [];
  for (const node of nodes.flat(Infinity)) {
    if (node && typeof node === "object" && "tag" in node) out.push(flatten(node.children));
    else if (node !== null && node !== undefined && node !== false) out.push(String(node));
  }
  return out.join("");
}

test("标题 / 段落 / 列表 / 引用各归各的块", () => {
  const blocks = parseMarkdown("# 标题\n\n第一段\n第二段\n\n- 一\n- 二\n\n> 引用\n");
  assert.deepEqual(
    blocks.map((block) => block.type),
    ["heading", "paragraph", "list", "quote"]
  );
  assert.equal(blocks[0].level, 1);
  assert.equal(blocks[1].inline[0].text, "第一段\n第二段");
  assert.equal(blocks[2].ordered, false);
  assert.equal(blocks[2].items.length, 2);
});

test("有序列表认阿拉伯数字加点或右括号", () => {
  const [block] = parseMarkdown("1. 先读论文\n2) 再跑评测\n");
  assert.equal(block.type, "list");
  assert.equal(block.ordered, true);
  assert.equal(block.items.length, 2);
});

test("围栏代码块原样保留，内部不做行内解析", () => {
  const [block] = parseMarkdown("```python\nprint('**不是粗体**')\n```\n");
  assert.equal(block.type, "code");
  assert.equal(block.lang, "python");
  assert.equal(block.text, "print('**不是粗体**')");
});

test("行内代码优先于粗体：反引号里的星号是字面量", () => {
  const [block] = parseMarkdown("用 `**a**` 表示强调\n");
  assert.equal(block.inline[1].type, "code");
  assert.equal(block.inline[1].text, "**a**");
});

test("粗体 / 斜体 / 链接都解析成带 children 的 token", () => {
  const [block] = parseMarkdown("**粗**与*斜*，见 [文档](https://example.com/a?b=1)\n");
  const kinds = block.inline.map((token) => token.type);
  assert.deepEqual(kinds, ["strong", "text", "em", "text", "link"]);
  assert.equal(block.inline[4].href, "https://example.com/a?b=1");
});

test("href 白名单：只有 http(s) 放行", () => {
  assert.equal(safeHref("javascript:alert(1)"), "");
  assert.equal(safeHref("data:text/html,<script>alert(1)</script>"), "");
  assert.equal(safeHref("  https://example.com/x  "), "https://example.com/x");
  assert.equal(safeHref("//example.com/x"), "");
});

test("危险链接降级成纯文本：链接文字留着，URL 一个字都不进 DOM", () => {
  const [block] = parseMarkdown("[点我](javascript:alert(1))\n");
  assert.deepEqual(block.inline, [{ type: "text", text: "点我" }]);
  const { h, log } = fakeH();
  renderMarkdown("[点我](javascript:alert(1))\n", { h });
  assert.equal(log.filter((node) => node.tag === "a").length, 0);
  assert.ok(!flatten(log).includes("javascript:"));
});

test("HTML 当纯文本：一个元素都不建，只留文本", () => {
  const payload = '<img src=x onerror="alert(1)">';
  const { h, log } = fakeH();
  renderMarkdown(payload, { h });
  assert.equal(log.filter((node) => ["img", "script", "iframe", "style"].includes(node.tag)).length, 0);
  assert.ok(flatten(log).includes(payload));
});

test("渲染结果是 div.md 包着的块，且必须传 h", () => {
  const { h, log } = fakeH();
  const node = renderMarkdown("**粗**\n", { h });
  assert.equal(node.tag, "div");
  assert.equal(node.attrs.class, "md");
  assert.equal(log.filter((item) => item.tag === "strong").length, 1);
  assert.throws(() => renderMarkdown("**粗**\n"), /h/);
});
