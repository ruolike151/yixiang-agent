/* 以湘控制台 · 前端（手写、无构建、无 CDN）
 *
 * 三条约束决定了这份代码的形状：
 *   1. 只用浏览器原生能力（fetch + ReadableStream 读 SSE），离线也能跑；
 *   2. 一律用 DOM API 建节点，**不用 innerHTML 拼数据**——对话内容、记忆正文、
 *      错误信息都会进页面，拼字符串就是把模型的输出当代码执行；
 *   3. 面板数据按需拉取并缓存，发送 / 保存类的操作走"禁用按钮 + 就地反馈"，
 *      不整页刷新（测试时要能看到中间状态）。
 */

"use strict";

// Markdown 渲染器是独立模块（`markdown.mjs`），解析与建节点分开：
// 它只往 textContent / createTextNode 写数据，第 5 行的那条约束照样成立。
import { renderMarkdown } from "./markdown.mjs";

const SVG_NS = "http://www.w3.org/2000/svg";

const PANELS = [
  {
    id: "chat",
    label: "对话",
    icon: "chat",
    desc: "与命令行同一条链路：同一份 session、同一份 trace、同一份成本账。",
  },
  {
    id: "history",
    label: "历史对话",
    icon: "clock",
    desc: "按会话翻旧账：每轮的提问、回复、调了哪些工具、花了多少时间。",
  },
  {
    id: "persona",
    label: "人设与记忆",
    icon: "user",
    desc: "S1~S4 的正文都在这三个文件里；改完立刻影响下一轮，超限一个字节都不会写进去。",
  },
  {
    id: "config",
    label: "模型配置",
    icon: "sliders",
    desc: "写回 .env 并热更新运行时（data_dir 例外，需要重启服务）。",
  },
  {
    id: "prompt",
    label: "提示词",
    icon: "layers",
    desc: "本轮的 S1~S8 实况，以及已注册的工具与技能——模型看到了什么，这里就显示什么。",
  },
  {
    id: "qq",
    label: "QQ 设置",
    icon: "link",
    desc: "QQ 网关（OneBot v11 反向 WS）的开关与白名单；空白名单 = 拒绝一切外部消息。",
  },
  {
    id: "traces",
    label: "链路 trace",
    icon: "activity",
    desc: "每轮一行：门控做了什么、调了哪些工具、花了多少 token 与钱——面试时照这里讲。",
  },
];

const ICONS = {
  chat: ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"],
  activity: ["M22 12h-4l-3 9L9 3l-3 9H2"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7.5v5l3 2"],
  user: ["M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2", "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
  sliders: ["M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3", "M1 14h6M9 8h6M17 16h6"],
  layers: ["M12 2 2 7l10 5 10-5-10-5z", "M2 17l10 5 10-5", "M2 12l10 5 10-5"],
  link: [
    "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L12 5",
    "M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19",
  ],
  upload: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "M17 8l-5-5-5 5", "M12 3v12"],
  send: ["M22 2 11 13", "M22 2 15 22l-4-9-9-4z"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "M20.5 20.5 16 16"],
  refresh: [
    "M23 4v6h-6",
    "M1 20v-6h6",
    "M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15",
  ],
  save: ["M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z", "M17 21v-8H7v8", "M7 3v5h8"],
  check: ["M20 6 9 17l-5-5"],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
  plus: ["M12 5v14M5 12h14"],
  file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6"],
  tool: [
    "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z",
  ],
  book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"],
  spark: ["M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"],
};

/* ------------------------------------------------------------------ DOM */
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name, size = 18) {
  const svg = document.createElementNS(SVG_NS, "svg");
  const attrs = {
    viewBox: "0 0 24 24",
    width: String(size),
    height: String(size),
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.8",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  };
  for (const [key, value] of Object.entries(attrs)) svg.setAttribute(key, value);
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function button(label, { iconName, className = "", onClick, title, disabled = false } = {}) {
  const node = h(
    "button",
    {
      type: "button",
      class: className,
      title: title || label,
      disabled,
      onclick: onClick,
    },
    iconName ? icon(iconName, 16) : null,
    label ? h("span", { text: label }) : null
  );
  if (!label && iconName) node.classList.add("icon-only");
  return node;
}

function field(labelText, control, hint) {
  const id = control.id;
  return h(
    "div",
    { class: "field" },
    h("label", { for: id, text: labelText }),
    control,
    hint ? h("p", { class: "field-hint", text: hint }) : null
  );
}

function badge(text, tone = "") {
  return h("span", { class: `badge ${tone}`.trim(), text });
}

/* ------------------------------------------------------------------ API */
class ApiError extends Error {
  constructor(message, payload, status) {
    super(message);
    this.payload = payload || {};
    this.status = status;
  }
}

async function api(path, { method = "GET", body, formData } = {}) {
  const init = { method, headers: {} };
  if (formData) init.body = formData;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { message: text.slice(0, 400) };
    }
  }
  if (!response.ok) {
    throw new ApiError(
      (payload && payload.message) || `${response.status} ${response.statusText}`,
      payload,
      response.status
    );
  }
  return payload;
}

function toast(message, tone = "") {
  const box = document.getElementById("toasts");
  const node = h("div", { class: `toast ${tone}`.trim(), text: message });
  box.append(node);
  setTimeout(() => node.remove(), tone === "bad" ? 8000 : 4200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板");
  } catch {
    toast("浏览器不允许写剪贴板，手动复制吧", "bad");
  }
}

/* ------------------------------------------------------------------ 状态 */
const store = {
  panel: "chat",
  arg: "",
  summary: null,
  sessions: null,
  // 历史面板的搜索框内容：render() 会把面板整块换掉，搜索词得活在 store 上
  sessionQuery: "",
  transcript: null,
  transcriptSession: "",
  // Task 16 的 trace 详情面板要用；这一轮只占位，渲染还没接
  traces: null,
  trace: null,
  persona: null,
  memoryData: null,
  config: null,
  qq: null,
  prompt: null,
  tools: null,
  skills: null,
  uploads: [],
  // 输入框草稿：render() 会把整块面板连 textarea 一起换掉，草稿只活在 DOM 上就会丢
  draft: "",
  streaming: false,
  // 当前这一轮的 job_id 与中断句柄（停止生成要用它们）
  streamJob: "",
  abort: null,
  // 用户刚按过「停止」：读流已经断了，收尾要靠它把 chip 补上
  stopped: false,
  loading: new Set(),
};

const $main = document.getElementById("main");

function searching(key) {
  return store.loading.has(key);
}

/** 拉取失败时数据仍是 null：这时候显示骨架屏，而不是渲染一个空表格骗人。 */
function pending(...values) {
  return values.some((value) => value === null || value === undefined);
}

async function load(key, fetcher, { force = false } = {}) {
  if (searching(key)) return;
  store.loading.add(key);
  try {
    await fetcher();
  } catch (error) {
    toast(error.message || String(error), "bad");
  } finally {
    store.loading.delete(key);
  }
}

function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function fmtNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN") : String(value);
}

function fmtBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

function meter(used, limit, tone = "") {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  const level = ratio >= 0.95 ? "bad" : ratio >= 0.8 ? "warn" : tone;
  return h(
    "div",
    { class: `meter ${level}`.trim(), role: "img", "aria-label": `${used} / ${limit}` },
    h("span", { style: `width:${(ratio * 100).toFixed(1)}%` })
  );
}

function panelHead(title, desc, ...actions) {
  return h(
    "div",
    { class: "panel-head" },
    h("div", {}, h("h2", { text: title }), h("p", { text: desc })),
    actions.length ? h("div", { class: "row" }, ...actions) : null
  );
}

function skeleton(lines = 3) {
  return h(
    "div",
    { class: "card" },
    h("div", { class: "skeleton", style: "width:40%" }),
    ...Array.from({ length: lines }, () => h("div", { class: "skeleton block" }))
  );
}

/* ------------------------------------------------------------------ 路由 */
function route() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
  const [panel, ...rest] = (raw || "chat").split("/");
  const known = PANELS.some((item) => item.id === panel);
  store.panel = known ? panel : "chat";
  store.arg = rest.join("/");
  return store.panel;
}

function navigate(panel, arg = "") {
  const target = arg ? `#${panel}/${encodeURIComponent(arg)}` : `#${panel}`;
  if (location.hash === target) {
    render();
    return;
  }
  location.hash = target;
}

/* ------------------------------------------------------------------ 外壳 */
function renderNav() {
  const list = document.getElementById("nav-list");
  const counts = {
    chat: store.summary ? `${store.summary.session.turns} 轮` : "",
    history: store.sessions ? `${store.sessions.sessions.length}` : "",
    config: store.summary ? `${store.summary.counters.tools} 工具` : "",
    qq: store.summary && store.summary.qq.fields.qq_enabled ? "已开" : "",
  };
  list.replaceChildren(
    ...PANELS.map((panel) =>
      h(
        "li",
        {},
        h(
          "a",
          {
            class: "nav-item",
            href: `#${panel.id}`,
            "aria-current": store.panel === panel.id ? "page" : null,
            onclick: (event) => {
              event.preventDefault();
              navigate(panel.id);
            },
          },
          icon(panel.icon, 17),
          h("span", { text: panel.label }),
          counts[panel.id] ? h("span", { class: "nav-count", text: counts[panel.id] }) : null
        )
      )
    )
  );
}

function renderStatus() {
  const strip = document.getElementById("status-strip");
  const summary = store.summary;
  if (!summary) {
    strip.replaceChildren(h("div", {}, h("dt", { text: "状态" }), h("dd", { text: "连接中…" })));
    return;
  }
  const items = [
    ["主模型", summary.models.main],
    ["密钥", summary.provider.api_key_set ? summary.provider.api_key_mask : "未配置"],
    ["嵌入", summary.provider.embed_backend],
    ["今日成本", `¥${Number(summary.cost.total.cost_cny || 0).toFixed(4)}`],
  ];
  strip.replaceChildren(
    ...items.map(([term, value]) =>
      h("div", {}, h("dt", { text: term }), h("dd", { text: String(value) }))
    )
  );
  const foot = document.getElementById("foot-session");
  foot.textContent = `会话：${summary.session.id}（${summary.session.turns} 轮 · source=${summary.session.source}）`;
  document.getElementById("foot-paths").textContent = `data: ${summary.app.data_dir}`;
}

function renderBrandMark() {
  const mark = document.getElementById("brand-mark");
  mark.replaceChildren(icon("spark", 20));
}

/* ------------------------------------------------------------------ 对话 */
function toolChips(tools) {
  if (!tools || !tools.length) return null;
  return h(
    "div",
    { class: "chips" },
    ...tools.map((item) =>
      h("span", {
        class: `chip ${item.ok ? "ok" : "bad"}`,
        text: `${item.tool || item.name || "tool"} ${item.ok ? "ok" : "失败"} ${
          item.ms ? `${item.ms}ms` : ""
        }`.trim(),
        title: JSON.stringify(item.args || {}),
      })
    )
  );
}

function turnToNodes(turn, { withUser = true } = {}) {
  const nodes = [];
  if (withUser) {
    nodes.push(
      h(
        "article",
        { class: "bubble user" },
        h("div", { class: "bubble-meta" }, h("span", { class: "role", text: "you" })),
        h("p", { text: turn.user || "" })
      )
    );
  }
  const meta = [h("span", { class: "role", text: "yixiang" }), h("span", { text: fmtTime(turn.at) })];
  nodes.push(
    h(
      "article",
      { class: "bubble assistant" },
      h("div", { class: "bubble-meta" }, ...meta),
      // 模型正文按 Markdown 子集渲染；用户气泡与工具输出保持纯文本
      renderMarkdown(turn.reply || "（这一轮没有正文）", { h }),
      toolChips(turn.tools)
    )
  );
  return nodes;
}

/** 把一段 Markdown 渲染进已有容器：流式气泡用同一套渲染，不再吐原始记号。 */
function renderInto(box, text) {
  box.replaceChildren(...renderMarkdown(text || "", { h }).childNodes);
  return box;
}

function chatScroll() {
  const box = h("div", { class: "chat-scroll", id: "chat-scroll" });
  const turns = store.transcript ? store.transcript.turns : [];
  if (!turns.length) {
    box.append(
      h("p", {
        class: "empty",
        text:
          "还没有往来记录。先在「模型配置」里确认 API key，然后在下面写一句试试；" +
          "要让它读文件，点「上传文件」把文件放进 data/uploads/ 再让它 read_file。",
      })
    );
  } else {
    for (const turn of turns) box.append(...turnToNodes(turn));
  }
  return box;
}

function uploadTray() {
  if (!store.uploads.length) return null;
  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-head" }, h("h3", { text: "本次已上传" })),
    ...store.uploads.map((item) =>
      h(
        "div",
        { class: "row" },
        icon("file", 16),
        h("span", { class: "grow mono", text: `${item.path} · ${fmtBytes(item.bytes)}` }),
        button("复制 read_file 调用", {
          iconName: "check",
          className: "ghost",
          onClick: () => copyText(item.hint),
        }),
        button("填进输入框", {
          className: "ghost",
          onClick: () => {
            fillComposer(item.hint);
            const box = document.getElementById("chat-input");
            if (box) box.focus();
          },
        })
      )
    )
  );
}

/** 往输入框里追加一段文字（并记住草稿）。render() 换掉的是节点，草稿得存在 store 里。 */
function fillComposer(text) {
  store.draft = `${store.draft.trim()} ${text}`.trim();
  const box = document.getElementById("chat-input");
  if (box) box.value = store.draft;
}

/** 一次上传：文件选择框、拖拽、粘贴三条路都走这里。
 *
 * `input` 传了就把 read_file 调用直接填进输入框——省掉 uploadTray 里那一次多余的点击。
 * 失败交给 load() 统一弹 toast（413 / 空文件 / bad_multipart 都有自己的说法）。
 */
async function uploadFile(file, { input, fallbackName } = {}) {
  const name = file.name || fallbackName || "upload.bin";
  let uploaded = null;
  await load("upload", async () => {
    const data = new FormData();
    data.append("file", file, name);
    uploaded = await api("/api/upload", { method: "POST", formData: data });
    store.uploads.push(uploaded);
    if (input) fillComposer(uploaded.hint);
    toast(`已上传 ${uploaded.name}（${fmtBytes(uploaded.bytes)}）`, "ok");
  });
  return uploaded;
}

/** 把一个容器变成拖放目标：拖进来就是上传，松手后输入框里已经填好 read_file。 */
function attachDropTarget(node, input) {
  node.addEventListener("dragover", (event) => {
    if (!event.dataTransfer) return;
    event.preventDefault(); // 不拦：浏览器会直接把文件当页面打开，界面状态全丢
    event.dataTransfer.dropEffect = "copy";
    node.classList.add("dropping");
  });
  node.addEventListener("dragleave", (event) => {
    // 在子元素之间挪动也会触发 dragleave，relatedTarget 还在容器里就不算走
    if (node.contains(event.relatedTarget)) return;
    node.classList.remove("dropping");
  });
  node.addEventListener("drop", async (event) => {
    event.preventDefault();
    node.classList.remove("dropping");
    const files = Array.from(event.dataTransfer ? event.dataTransfer.files : []);
    if (!files.length) {
      toast("拖进来的东西里没有文件：拖一个文件，别拖文件夹或网址", "bad");
      return;
    }
    for (const file of files) await uploadFile(file, { input });
    render();
  });
}

/** 截图粘贴：只接剪贴板里的**文件**，纯文本粘贴一个字都不拦。 */
function attachPasteUpload(input) {
  input.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData ? event.clipboardData.files : []);
    if (!files.length) return;
    event.preventDefault(); // 必须同步调：异步里再调已经晚了
    void (async () => {
      for (const file of files) {
        await uploadFile(file, { input, fallbackName: `clipboard-${Date.now()}.png` });
      }
      render();
    })();
  });
}

async function refreshSummary() {
  store.summary = await api("/api/state");
  renderStatus();
  renderNav();
  return store.summary;
}

/** 停止生成：两件事都要做——断开读流（停止"看"）+ 调取消接口（停止"算"）。 */
async function stopStreaming() {
  const jobId = store.streamJob;
  store.stopped = true; // 读流马上要断：让 sendMessage 收尾时把 chip 补上
  if (store.abort) {
    store.abort.abort();
    store.abort = null;
  }
  if (!jobId) return;
  try {
    await api(`/api/chat/${jobId}/cancel`, { method: "POST" });
    toast("已停止这一轮", "ok");
  } catch (error) {
    // 这一轮刚好跑完了：不是错误，别弹红
    if (error.status !== 404) toast(error.message || "停止失败", "bad");
  }
}

async function sendMessage(text, { input, submit, stop, scroll }) {
  store.streaming = true;
  const controller = new AbortController();
  store.streamJob = "";
  store.abort = controller;
  store.stopped = false;
  if (stop) stop.hidden = false;
  submit.disabled = true;
  input.disabled = true;

  const userBubble = h(
    "article",
    { class: "bubble user" },
    h("div", { class: "bubble-meta" }, h("span", { class: "role", text: "you" })),
    h("p", { text })
  );
  // 正文容器跟历史轮次一样是 div.md（不能是 <p>：渲染器要往里塞 h*/ul/pre 这些块级节点）
  const body = h("div", { class: "md" });
  const chips = h("div", { class: "chips" });
  const meta = h("div", {
    class: "bubble-meta",
    text: "正在连接模型…",
  });
  const assistant = h(
    "article",
    { class: "bubble assistant streaming" },
    meta,
    body,
    chips
  );
  const cursor = h("span", { class: "caret", "aria-hidden": "true" });
  body.append(cursor);
  const empty = scroll.querySelector(".empty");
  if (empty) empty.remove();
  scroll.append(userBubble, assistant);
  scroll.scrollTop = scroll.scrollHeight;

  const pending = new Map();
  let replyText = "";
  let result = null;
  let failure = null;
  let stopped = false;

  const addChip = (node) => {
    chips.append(node);
    return node;
  };
  const markStopped = () => {
    if (stopped) return;
    stopped = true;
    addChip(h("span", { class: "chip warn", text: "已停止生成" }));
  };
  const handle = (event) => {
    switch (event.kind) {
      case "text_delta":
        replyText += event.text || "";
        renderInto(body, replyText).append(cursor);
        break;
      case "text_revoke":
        replyText = "";
        renderInto(body, "").append(cursor);
        addChip(h("span", { class: "chip info", text: "这轮要查资料，撤回草稿" }));
        break;
      case "notice":
        addChip(h("span", { class: "chip", text: event.text || "" }));
        break;
      case "tool_start": {
        const chip = addChip(
          h("span", { class: "chip info", text: `调用 ${event.tool} …` })
        );
        pending.set(event.tool, chip);
        break;
      }
      case "tool_end": {
        const chip = pending.get(event.tool);
        const text = `${event.tool} ${event.ok ? "ok" : "失败"} ${event.ms || 0}ms`;
        if (chip) {
          chip.textContent = text;
          chip.className = `chip ${event.ok ? "ok" : "bad"}`;
        } else {
          addChip(h("span", { class: `chip ${event.ok ? "ok" : "bad"}`, text }));
        }
        break;
      }
      case "error":
        failure = event;
        assistant.classList.add("error");
        break;
      case "start":
        // 服务端把这一轮的 job_id 交过来：停止按钮靠它找到"要停谁"
        store.streamJob = event.job_id || "";
        break;
      case "cancelled":
        markStopped();
        break;
      case "result":
        result = event;
        break;
      default:
        break;
    }
    scroll.scrollTop = scroll.scrollHeight;
  };

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: controller.signal, // 停止时先断开"看"的这一侧
    });
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({}));
      throw new ApiError(payload.message || `HTTP ${response.status}`, payload, response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const line = chunk.split("\n").find((item) => item.startsWith("data:"));
        if (line) {
          try {
            handle(JSON.parse(line.slice(5).trim()));
          } catch {
            /* 半行 / 非 JSON 的注释行（心跳）直接跳过 */
          }
        }
        index = buffer.indexOf("\n\n");
      }
    }
  } catch (error) {
    if (error.name === "AbortError") {
      // 用户按了停止：这一轮由服务端收尾（trace / chat_log 照落），界面不用报错
      if (store.stopped) markStopped();
    } else {
      failure = { message: error.message || String(error) };
      assistant.classList.add("error");
    }
  }

  cursor.remove();
  assistant.classList.remove("streaming");
  if (stopped && !result && !failure) meta.textContent = "已停止";

  if (result) {
    const tokens = result.usage || {};
    const parts = [
      result.model || "",
      `${result.latency_ms ? result.latency_ms.total : 0}ms`,
      `${result.iterations || 0} 轮迭代`,
      `in ${fmtNumber(tokens.in)} / out ${fmtNumber(tokens.out)} token`,
    ].filter(Boolean);
    meta.textContent = parts.join(" · ");
    if (result.reply) {
      replyText = result.reply;
      renderInto(body, replyText);
    }
    for (const tool of result.tools || []) {
      const chip = pending.get(tool.tool);
      if (chip) {
        chip.textContent = `${tool.tool} ${tool.ok ? "ok" : "失败"} ${tool.ms || 0}ms`;
        chip.className = `chip ${tool.ok ? "ok" : "bad"}`;
      }
    }
    if (result.cancelled) {
      markStopped();
    } else if (result.error) {
      assistant.classList.add("error");
      chips.append(h("span", { class: "chip bad", text: `error: ${result.error}` }));
    }
    if (result.memory_write_failed) {
      chips.append(h("span", { class: "chip bad", text: "记忆没写进去" }));
    }
    if (store.transcript) {
      store.transcript.turns.push({
        id: result.turn_id,
        user: text,
        reply: replyText,
        tools: result.tools || [],
        at: new Date().toISOString(),
      });
    }
  }
  if (failure) {
    meta.textContent = "这一轮没跑完";
    if (!replyText) body.remove();
    chips.append(h("p", { class: "field-error", text: failure.message || "未知错误" }));
  }

  store.streaming = false;
  store.streamJob = "";
  store.abort = null;
  store.stopped = false;
  if (stop) stop.hidden = true;
  submit.disabled = false;
  input.disabled = false;
  input.focus();
  try {
    await load("summary", refreshSummary, { force: true });
    store.sessions = null; // 新会话 / 新轮次：历史列表下次进面板时重拉
  } catch {
    /* 状态刷新失败不影响这一轮的结论 */
  }
}

async function renderChat() {
  if (!store.summary) await refreshSummary();
  if (!store.transcript) {
    await load("transcript", async () => {
      store.transcript = await api("/api/session?limit=200");
      store.transcriptSession = store.transcript.session_id;
    });
  }
  const scroll = chatScroll();
  const input = h("textarea", {
    id: "chat-input",
    rows: "3",
    placeholder: "写一句给以湘。回车发送，Shift+回车换行。",
    "aria-label": "发消息",
  });
  input.value = store.draft; // render() 换掉的是节点：草稿从 store 里接回来
  input.addEventListener("input", () => {
    store.draft = input.value;
  });
  const fileInput = h("input", {
    type: "file",
    id: "chat-file",
    multiple: true,
    hidden: true,
    onchange: async (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = ""; // 允许连续两次选同一个文件
      for (const file of files) await uploadFile(file, { input });
      render();
    },
  });
  const submit = button("发送", {
    iconName: "send",
    className: "primary",
    disabled: store.streaming,
    title: store.streaming ? "上一轮还在跑" : "发送（回车）",
    onClick: () => fire(),
  });
  const stop = button("停止", {
    iconName: "alert",
    className: "ghost",
    title: "停止这一轮（已生成的部分与这一轮都会留在历史里）",
    onClick: () => stopStreaming(),
  });
  // 跟着 store.streaming 走：每次 render() 都会重建这个按钮
  stop.hidden = !store.streaming;
  const fire = () => {
    const text = input.value.trim();
    if (!text || store.streaming) return;
    input.value = "";
    store.draft = ""; // 发出去就清草稿：render() 之后不会再把这句话捞回来
    sendMessage(text, { input, submit, stop, scroll });
  };
  attachPasteUpload(input);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      fire();
    }
  });

  const head = panelHead(
    `对话 · ${store.transcript ? store.transcript.session_id : "…"}`,
    PANELS[0].desc,
    button("新会话", {
      iconName: "plus",
      className: "ghost",
      onClick: async () => {
        await load("new-session", async () => {
          const result = await api("/api/session/new", { method: "POST", body: {} });
          store.sessions = result;
          store.transcript = null;
          await refreshSummary();
          render();
        });
      },
    }),
    button("刷新", {
      iconName: "refresh",
      className: "ghost",
      onClick: () => {
        store.transcript = null;
        render();
      },
    })
  );

  const card = h(
    "div",
    { class: "card drop-zone" },
    scroll,
    h(
      "div",
      { class: "composer" },
      input,
      h(
        "div",
        { class: "row" },
        button("上传文件", {
          iconName: "upload",
          className: "ghost",
          onClick: () => fileInput.click(),
        }),
        fileInput,
        h("span", {
          class: "card-note",
          text: "上传后落在 data/uploads/，模型用 read_file 读它（≤2MB）；文件直接拖进来或截图直接粘贴也行",
        }),
        h("span", { class: "grow" }),
        stop,
        submit
      ),
      uploadTray()
    )
  );
  attachDropTarget(card, input);

  return [
    head,
    card,
    ...(store.summary.errors.length
      ? [
          h(
            "div",
            { class: "notice bad" },
            icon("alert", 18),
            h("div", {}, ...store.summary.errors.map((item) => h("p", { text: item })))
          ),
        ]
      : []),
  ];
}

/* ------------------------------------------------------------------ 历史 */
/* --------------------------------------------------------------- 历史动作 */
async function renameSession(session) {
  const title = window.prompt("给这个会话起个名字（留空 = 恢复默认标题）", session.title || "");
  if (title === null) return; // 取消：什么都不做
  await load("rename", async () => {
    store.sessions = await api("/api/session/rename", {
      method: "POST",
      body: { session_id: session.session_id, title },
    });
    toast("改好了", "ok");
    render();
  });
}

async function deleteSession(session) {
  const label = session.title || session.session_id;
  if (
    !window.confirm(
      `删掉「${label}」的 ${session.turns} 轮往来？\n长期记忆（facts / episodes）不会被删。`
    )
  ) {
    return;
  }
  await load("delete", async () => {
    store.sessions = await api(`/api/session/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
    });
    toast("已删除这个会话的往来记录", "ok");
    render();
  });
}

async function exportSession(session) {
  await load("export", async () => {
    const payload = await api(`/api/session/${encodeURIComponent(session.session_id)}/export`);
    const blob = new Blob([sessionToMarkdown(payload)], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: `${session.session_id}.md` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("导出好了（看浏览器的下载）", "ok");
  });
}

/** 会话 → Markdown：直接给用户存档看的，所以标题层级从 # 开始（不是正文渲染）。 */
function sessionToMarkdown(payload) {
  const lines = [`# 会话 ${payload.session_id}`, "", `导出时间：${payload.exported_at}`, ""];
  for (const turn of payload.turns) {
    lines.push(`## #${turn.id} · ${turn.at}`, "", `**你**：${turn.user}`, "");
    lines.push(`**以湘**：${turn.reply || "（空）"}`, "");
    if (turn.tools && turn.tools.length) lines.push(`工具：${turn.tools.join(", ")}`, "");
  }
  return lines.join("\n");
}

async function searchSessions(query) {
  store.sessionQuery = query;
  await load("sessions", async () => {
    const suffix = query ? `?q=${encodeURIComponent(query)}` : "";
    store.sessions = await api(`/api/sessions/search${suffix}`);
    render();
  });
}

async function renderHistory() {
  if (!store.sessions) {
    await load("sessions", async () => {
      store.sessions = await api("/api/sessions?limit=50");
    });
  }
  if (!store.transcript) {
    await load("transcript", async () => {
      store.transcript = await api("/api/session?limit=200");
    });
  }
  if (pending(store.sessions, store.transcript)) {
    return [panelHead("历史对话", PANELS[1].desc), skeleton()];
  }
  const list = h("div", { class: "scroll-list" });
  if (!store.sessions.sessions.length) {
    list.append(h("p", { class: "empty", text: "还没有任何历史会话：去「对话」那栏聊一句就有了。" }));
  }
  for (const session of store.sessions.sessions) {
    const active = session.session_id === store.sessions.current;
    list.append(
      h(
        "div",
        { class: "session-row" },
        h(
          "button",
          {
            class: "session-item",
            type: "button",
            "aria-pressed": active ? "true" : "false",
            onclick: async () => {
              await load("switch", async () => {
                const result = await api("/api/session/switch", {
                  method: "POST",
                  body: { session_id: session.session_id },
                });
                store.sessions = result;
                store.transcript = await api(
                  `/api/session/${encodeURIComponent(session.session_id)}?limit=200`
                );
                await refreshSummary();
                render();
              });
            },
          },
          icon("chat", 16),
          h(
            "span",
            { class: "session-body" },
            h("span", { class: "session-title", text: session.title || "（没有标题）" }),
            h("span", {
              class: "session-sub",
              text: `${session.session_id} · ${session.turns} 轮 · ${fmtTime(session.last_at)}`,
            })
          ),
          active ? badge("当前", "ok") : null
        ),
        h(
          "div",
          { class: "session-tools" },
          button("改名", { className: "ghost", onClick: () => renameSession(session) }),
          button("导出", { className: "ghost", onClick: () => exportSession(session) }),
          button("删除", {
            className: "ghost danger",
            title: active ? "正在用的会话不能删" : "删掉这个会话的往来记录",
            disabled: active,
            onClick: () => deleteSession(session),
          })
        )
      )
    );
  }

  const turns = h("div", { class: "turns" });
  for (const turn of store.transcript.turns) {
    turns.append(
      h(
        "article",
        { class: "turn" },
        h(
          "div",
          { class: "row" },
          h("span", { class: "mono", text: `#${turn.id}` }),
          h("span", { class: "card-note", text: fmtTime(turn.at) })
        ),
        h(
          "dl",
          {},
          h("dt", { text: "你" }),
          h("dd", { text: turn.user || "" }),
          h("dt", { text: "以湘" }),
          h("dd", { text: turn.reply || "（空）" })
        ),
        toolChips(turn.tools)
      )
    );
  }

  return [
    panelHead(
      "历史对话",
      PANELS[1].desc,
      button("刷新", {
        iconName: "refresh",
        className: "ghost",
        onClick: () => {
          store.sessions = null;
          store.transcript = null;
          store.sessionQuery = "";
          render();
        },
      })
    ),
    h(
      "div",
      { class: "grid-2" },
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h("h3", { text: "会话列表" }),
          h(
            "div",
            { class: "session-search-row" },
            h("input", {
              type: "search",
              class: "session-search",
              placeholder: "搜标题或正文…",
              value: store.sessionQuery,
              onkeydown: (event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void searchSessions(event.target.value.trim());
                }
              },
            }),
            button("搜索", {
              iconName: "search",
              className: "ghost",
              onClick: () => {
                const box = document.querySelector(".session-search");
                void searchSessions(box ? box.value.trim() : "");
              },
            })
          )
        ),
        h("p", {
          class: "card-note",
          text: "点一条就切过去；改名 / 导出 / 删除在每条的右边（正在用的那条不能删）。",
        }),
        list
      ),
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h("h3", { text: `往来记录 · ${store.transcript.session_id}` }),
          h("span", {
            class: "card-note",
            text: `${store.transcript.turns.length} 轮（不套 history_turns 窗口）`,
          })
        ),
        turns.childElementCount
          ? turns
          : h("p", { class: "empty", text: "这个会话还没有往来记录。" })
      )
    ),
  ];
}

/* ------------------------------------------------------------------ 链路 */
function traceRow(row) {
  const tokens = row.tokens || {};
  const chips = (row.tools || []).map((name) => h("span", { class: "chip", text: name }));
  if (((row.rag || {}).embed || "") === "unavailable") {
    chips.push(h("span", { class: "chip warn", text: "检索已降级（纯 FTS5）" }));
  }
  return h(
    "article",
    {
      class: "turn trace-row",
      onclick: () => navigate("traces", row.turn_id),
    },
    h(
      "div",
      { class: "row" },
      h("span", { class: "mono", text: row.turn_id }),
      h("span", { class: "card-note", text: fmtTime(row.ts) }),
      row.error ? badge("出错", "bad") : badge(row.finish_reason || "stop", "ok")
    ),
    h(
      "dl",
      {},
      h("dt", { text: "会话" }),
      h("dd", { text: row.session || "—" }),
      h("dt", { text: "迭代 / tokens" }),
      h("dd", {
        text: `${row.iterations} 轮 · ${fmtNumber(tokens.in)} in（${fmtNumber(
          tokens.cached_in
        )} cached）/ ${fmtNumber(tokens.out)} out`,
      }),
      h("dt", { text: "成本" }),
      h("dd", { text: `¥${Number(row.cost_cny || 0).toFixed(4)}` })
    ),
    chips.length ? h("div", { class: "chips" }, ...chips) : null
  );
}

function traceDetail(record) {
  const tools = record.tool_calls || [];
  const latency = record.latency_ms || {};
  const rag = record.rag || {};
  // 检索降级（D-24）：列表行用 chip 标出来了，点进来也得能看见——不然"这一轮
  // 的召回是纯 FTS5 的"这句话只在列表上闪一下，详情里反而没有
  const degraded = (rag.embed || "") === "unavailable";
  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("h3", { text: "这一轮的完整记录" }),
      h("span", { class: "card-note", text: record.turn_id || "" })
    ),
    h(
      "dl",
      {},
      h("dt", { text: "时间" }),
      h("dd", { text: fmtTime(record.ts) }),
      h("dt", { text: "会话 / 来源" }),
      h("dd", { text: `${record.session || "—"} / ${record.source || "—"}` }),
      h("dt", { text: "用户" }),
      h("dd", { text: record.user_text || "" }),
      h("dt", { text: "模型" }),
      h("dd", { text: record.model || "" }),
      h("dt", { text: "耗时" }),
      h("dd", {
        text: `模型 ${latency.llm || 0}ms · 工具 ${latency.tools || 0}ms · 总计 ${
          latency.total || 0
        }ms`,
      }),
      h("dt", { text: "结束原因" }),
      h("dd", { text: record.finish_reason || "" }),
      h("dt", { text: "检索" }),
      h("dd", {
        text: rag.embed ? (degraded ? "已降级（纯 FTS5）" : `${rag.facts || 0} 条事实`) : "—",
      })
    ),
    h(
      "div",
      { class: "card-head" },
      h("h3", { text: "工具调用" }),
      h("span", { class: "card-note", text: tools.length ? `${tools.length} 次` : "这一轮没调工具" })
    ),
    tools.length
      ? h(
          "div",
          { class: "chips" },
          ...tools.map((call) =>
            h("span", {
              class: `chip ${call.ok ? "ok" : "bad"}`,
              text: `[${call.iter}] ${call.tool} ${call.ms || 0}ms`,
              title: JSON.stringify(call.args || {}),
            })
          )
        )
      : null,
    degraded
      ? h(
          "div",
          { class: "chips" },
          h("span", { class: "chip warn", text: "检索已降级（纯 FTS5）" })
        )
      : null,
    h("p", { class: "card-note", text: `回复预览：${record.reply_preview || "（空）"}` }),
    h("p", {
      class: "card-note",
      text: `命令行同款：yixiang ops show-trace ${record.turn_id || ""}`,
    })
  );
}

async function renderTraces() {
  if (store.arg) {
    if (!store.trace || store.trace.turn_id !== store.arg) {
      await load("trace", async () => {
        store.trace = await api(`/api/trace/${encodeURIComponent(store.arg)}`);
      });
    }
    if (pending(store.trace)) {
      return [panelHead("链路 trace", PANELS[6].desc), skeleton()];
    }
    return [
      panelHead(
        "链路 trace",
        PANELS[6].desc,
        button("返回列表", {
          iconName: "clock",
          className: "ghost",
          onClick: () => navigate("traces"),
        })
      ),
      traceDetail(store.trace),
    ];
  }
  if (!store.traces) {
    await load("traces", async () => {
      store.traces = await api("/api/traces?limit=50");
    });
  }
  if (pending(store.traces)) {
    return [panelHead("链路 trace", PANELS[6].desc), skeleton()];
  }
  const list = h("div", { class: "turns" });
  if (!store.traces.traces.length) {
    list.append(h("p", { class: "empty", text: "还没有 trace：先去「对话」聊一句。" }));
  }
  for (const row of store.traces.traces) list.append(traceRow(row));
  return [
    panelHead(
      "链路 trace",
      PANELS[6].desc,
      button("刷新", {
        iconName: "refresh",
        className: "ghost",
        onClick: () => {
          store.traces = null;
          store.trace = null;
          render();
        },
      })
    ),
    h("p", {
      class: "card-note",
      text: `最近 ${store.traces.count} 轮，点一条看详情；每轮的原文也在 data/traces/。`,
    }),
    list,
  ];
}

/* ------------------------------------------------------------ 人设与记忆 */
async function renderPersona() {
  await load("persona", async () => {
    store.persona = await api("/api/persona");
  });
  await load("memory", async () => {
    store.memoryData = await api("/api/memory");
  });
  if (pending(store.persona, store.memoryData)) {
    return [panelHead("人设与记忆", PANELS[2].desc), skeleton()];
  }

  const cards = store.persona.files.map((file) => {
    const box = h("textarea", { id: `persona-${file.name}`, rows: "12", spellcheck: "false" });
    box.value = file.text;
    const counter = h("span", { class: "card-note" });
    const update = () => {
      const used = box.value.trim().length;
      counter.textContent = `${used} / ${file.limit} ${file.unit}`;
      meterNode.replaceChildren(meter(used, file.limit));
    };
    const meterNode = h("div", {});
    box.addEventListener("input", update);
    update();
    const save = button("保存", {
      iconName: "save",
      className: "primary",
      onClick: async () => {
        save.disabled = true;
        await load("save-persona", async () => {
          const result = await api("/api/persona", {
            method: "PUT",
            body: { name: file.name, text: box.value },
          });
          store.persona = { files: result.files };
          toast(`${file.name} 已保存`, "ok");
          render();
        });
        save.disabled = false;
      },
    });
    return h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h("h3", { text: file.label }),
        h(
          "div",
          { class: "row" },
          badge(file.source === "data" ? "来自 data/" : "来自模板", file.source === "data" ? "" : "warn"),
          counter,
          save
        )
      ),
      meterNode,
      box,
      h("p", {
        class: "field-hint",
        text: file.over
          ? "已经超过上限：保存会被拒绝，先删减。"
          : "上限由 core_files 定义，与命令行写入同一套校验。",
      })
    );
  });

  const memory = store.memoryData;
  const memBox = h("textarea", { id: "memory-text", rows: "16", spellcheck: "false" });
  memBox.value = memory.text;
  const memCounter = h("span", { class: "card-note" });
  const memMeter = h("div", {});
  const updateMem = () => {
    const used = memBox.value.split("\n").filter((line) => line.trim() && !line.startsWith("<!--")).length;
    memCounter.textContent = `约 ${used} 活跃行 / ${memory.max_lines} 行`;
    memMeter.replaceChildren(meter(used, memory.max_lines));
  };
  memBox.addEventListener("input", updateMem);
  updateMem();

  const saveMemory = button("保存 memory.md", {
    iconName: "save",
    className: "primary",
    onClick: async () => {
      saveMemory.disabled = true;
      await load("save-memory", async () => {
        const result = await api("/api/memory", { method: "PUT", body: { text: memBox.value } });
        store.memoryData = result;
        toast("memory.md 已保存；记得同步进数据库", "ok");
        render();
      });
      saveMemory.disabled = false;
    },
  });
  const syncMemory = button("同步进数据库", {
    iconName: "refresh",
    className: "ghost",
    onClick: async () => {
      await load("sync-memory", async () => {
        const result = await api("/api/memory/sync", { method: "POST", body: {} });
        toast(`同步完成：${result.summary}`, "ok");
        store.memoryData = await api("/api/memory");
        render();
      });
    },
  });

  const entries = h(
    "table",
    {},
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", { text: "#" }),
        h("th", { text: "段" }),
        h("th", { text: "内容" }),
        h("th", { text: "钉住" })
      )
    ),
    h(
      "tbody",
      {},
      ...(memory.entries.length
        ? memory.entries.map((entry) =>
            h(
              "tr",
              {},
              h("td", { class: "mono", text: String(entry.line) }),
              h("td", { text: entry.section || "—" }),
              h("td", { text: entry.content }),
              h("td", { text: entry.pinned ? "是" : "否" })
            )
          )
        : [h("tr", {}, h("td", { colspan: "4", text: "还没有条目。" }))])
    )
  );

  return [
    panelHead(
      "人设与记忆",
      PANELS[2].desc,
      button("重新读取", {
        iconName: "refresh",
        className: "ghost",
        onClick: () => {
          store.persona = null;
          store.memoryData = null;
          render();
        },
      })
    ),
    ...(memory.problems.length
      ? [
          h(
            "div",
            { class: "notice bad" },
            icon("alert", 18),
            h("div", {}, ...memory.problems.map((item) => h("p", { text: item })))
          ),
        ]
      : []),
    h("div", { class: "grid-2" }, ...cards),
    h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h("h3", { text: "长期记忆（memory.md）" }),
        h("div", { class: "row" }, memCounter, saveMemory, syncMemory)
      ),
      memMeter,
      memBox,
      h("p", { class: "card-note mono", text: memory.path }),
      h(
        "details",
        {},
        h("summary", { text: `解析出的条目（${memory.entries.length}）` }),
        entries
      )
    ),
  ];
}

/* ------------------------------------------------------------ 模型配置 */
const CONFIG_META = {
  main_model: { label: "主对话模型", hint: "唯一必填的角色。" },
  gate_model: { label: "门控模型", hint: "留空回落到主模型。" },
  judge_model: { label: "评判模型", hint: "留空回落到主模型。" },
  utility_model: { label: "杂务模型", hint: "留空回落到评判 / 主模型。" },
  api_base: { label: "API Base", hint: "例如 https://api.deepseek.com/v1" },
  api_key: { label: "API Key", type: "password", hint: "留空 = 不改（只显示掩码）。" },
  judge_api_base: {
    label: "评判 API Base",
    hint: "judge / 杂务换家才填（本机 Ollama：http://127.0.0.1:11434/v1）；留空 = 跟主端点同一家。",
  },
  judge_api_key: {
    label: "评判 API Key",
    type: "password",
    hint: "换了家才填；留空且换了家 = 那一端不发密钥（本机端点正是这样）。",
  },
  no_think_models: {
    label: "关思考模型",
    hint: "逗号分隔，支持 qwen3.5-* 这样的前缀通配；命中就在请求里带 reasoning_effort=none。",
  },
  embed_backend: { label: "嵌入后端", type: "select-embed" },
  embed_model: { label: "嵌入模型", hint: "api 后端要填；hash 后端忽略。" },
  data_dir: { label: "数据目录", hint: "改了要重启服务才生效。" },
  consolidate_every: { label: "巩固间隔（轮）", type: "number" },
  history_turns: { label: "历史窗口（轮）", type: "number" },
  retrieve_top_k: { label: "检索条数", type: "number" },
  episode_top_k: { label: "片段条数", type: "number" },
  loop_max_iter: { label: "单轮最大迭代", type: "number", hint: "1~20" },
  tool_retry_max: { label: "工具重试次数", type: "number" },
  llm_timeout: { label: "模型超时（秒）", type: "number", step: "0.5" },
  gate_timeout: { label: "门控超时（秒）", type: "number", step: "0.5" },
  budget_cny_per_day: { label: "每日预算（元）", type: "number", step: "0.1" },
  log_level: { label: "日志级别", type: "select-log" },
  scheduler_enabled: { label: "启用定时任务", type: "checkbox" },
  brief_cron: { label: "日报 cron" },
  brief_catchup_until: { label: "日报补跑截止" },
  brief_sink: { label: "日报输出", hint: "例如 console" },
};

async function renderConfig() {
  await load("config", async () => {
    store.config = await api("/api/config");
  });
  if (pending(store.config)) return [panelHead("模型配置", PANELS[3].desc), skeleton()];

  const config = store.config;
  const controls = new Map();
  const form = h("div", { class: "grid-2" });

  for (const [name, meta] of Object.entries(CONFIG_META)) {
    const value = config.fields[name];
    let control;
    if (meta.type === "checkbox") {
      const box = h("input", { type: "checkbox", id: `cfg-${name}` });
      box.checked = Boolean(value);
      control = h(
        "div",
        { class: "switch" },
        box,
        h("label", { for: `cfg-${name}`, text: meta.label })
      );
      controls.set(name, box);
      form.append(h("div", { class: "field" }, control, hintOf(meta)));
      continue;
    }
    if (meta.type === "select-embed" || meta.type === "select-log") {
      const select = h("select", { id: `cfg-${name}` });
      const choices =
        meta.type === "select-embed" ? config.choices.embed_backend : config.choices.log_level;
      for (const choice of choices) {
        select.append(
          h("option", { value: choice, text: choice, selected: String(value) === choice })
        );
      }
      controls.set(name, select);
      form.append(field(meta.label, select, hintOf(meta)));
      continue;
    }
    const input = h("input", {
      type: meta.type === "password" ? "password" : meta.type === "number" ? "number" : "text",
      id: `cfg-${name}`,
      step: meta.step || null,
      autocomplete: "off",
      spellcheck: "false",
    });
    if (meta.type === "password") {
      input.value = "";
      input.placeholder = config.secret_mask.api_key || "还没有配置";
    } else {
      input.value = value === null || value === undefined ? "" : String(value);
    }
    controls.set(name, input);
    form.append(field(meta.label, input, hintOf(meta)));
  }

  const save = button("保存并热更新", {
    iconName: "save",
    className: "primary",
    onClick: async () => {
      const payload = {};
      for (const [name, control] of controls) {
        const meta = CONFIG_META[name];
        if (meta.type === "checkbox") payload[name] = control.checked;
        else if (meta.type === "number") payload[name] = control.value === "" ? "" : Number(control.value);
        else if (meta.type === "password") {
          if (control.value.trim()) payload[name] = control.value.trim();
        } else payload[name] = control.value;
      }
      save.disabled = true;
      await load("save-config", async () => {
        const result = await api("/api/config", { method: "PUT", body: payload });
        store.config = result.config;
        await refreshSummary();
        toast(
          result.restart_required
            ? "已写入 .env；data_dir 变了，重启服务才生效"
            : "已写入 .env 并热更新",
          result.restart_required ? "bad" : "ok"
        );
        render();
      });
      save.disabled = false;
    },
  });

  return [
    panelHead("模型配置", PANELS[3].desc, save),
    h(
      "div",
      { class: "notice" },
      icon("file", 18),
      h(
        "div",
        {},
        h("p", {
          text: `写入目标：${config.env_file || "（没有 .env，命令行 --env-file 为空）"}`,
        }),
        h("p", {
          class: "card-note",
          text: config.env_file_exists ? "文件已存在：命中的键原地替换，注释保留。" : "文件还不存在：保存时会新建。",
        })
      )
    ),
    ...(config.errors.length
      ? [
          h(
            "div",
            { class: "notice bad" },
            icon("alert", 18),
            h("div", {}, ...config.errors.map((item) => h("p", { text: item })))
          ),
        ]
      : []),
    form,
  ];
}

function hintOf(meta) {
  return meta.hint || "";
}

/* ------------------------------------------------------------ 提示词 */
async function renderPrompt() {
  await load("prompt", async () => {
    store.prompt = await api("/api/prompt");
  });
  await load("tools", async () => {
    store.tools = await api("/api/tools");
    store.skills = await api("/api/skills");
  });
  if (pending(store.prompt, store.tools, store.skills)) {
    return [panelHead("提示词", PANELS[4].desc), skeleton()];
  }

  const used = store.prompt.used_chars;
  const budget = store.prompt.budget_chars;
  const blocks = store.prompt.blocks.map((block) =>
    h(
      "details",
      { open: block.id === "S1" || block.id === "S4" },
      h(
        "summary",
        {},
        h("span", { class: "mono", text: block.id }),
        ` ${block.label}`,
        h("span", { class: "card-note", text: ` · ${block.chars} 字 · ${block.source}` })
      ),
      block.text
        ? h("div", { class: "scroll-box", text: block.text })
        : h("p", { class: "empty", text: "这一段现在是空的（空闲状态下 S5 / S6 为空是正常的）。" })
    )
  );

  const tools = store.tools.tools.map((tool) =>
    h(
      "tr",
      {},
      h("td", {}, h("span", { class: "mono", text: tool.name })),
      h("td", { text: tool.description }),
      h("td", { text: tool.side_effect ? "有副作用" : "只读" }),
      h("td", { class: "mono", text: (tool.required || []).join(", ") || "—" })
    )
  );

  return [
    panelHead(
      "提示词",
      PANELS[4].desc,
      button("重新拼一次", {
        iconName: "refresh",
        className: "ghost",
        onClick: () => {
          store.prompt = null;
          render();
        },
      })
    ),
    h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h("h3", { text: `本轮上下文占用：${fmtNumber(used)} / ${fmtNumber(budget)} 字` }),
        h("span", { class: "card-note", text: store.prompt.note })
      ),
      meter(used, budget),
      ...blocks
    ),
    h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h("h3", { text: `已注册工具（${store.tools.count}）` }),
        h("span", {
          class: "card-note",
          text: `技能 ${store.skills.count} 个 · S1~S4 正文在「人设与记忆」里改`,
        })
      ),
      h(
        "table",
        {},
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", { text: "工具" }),
            h("th", { text: "说明" }),
            h("th", { text: "性质" }),
            h("th", { text: "必填参数" })
          )
        ),
        h("tbody", {}, ...tools)
      ),
      store.skills.skills.length
        ? h(
            "div",
            { class: "chips" },
            ...store.skills.skills.map((skill) =>
              h("span", {
                class: "chip",
                text: `${skill.name}（触发：${(skill.triggers || []).join(" / ") || "无"}）`,
              })
            )
          )
        : h("p", { class: "card-note", text: "data/skills/ 下还没有技能。" })
    ),
  ];
}

/* ------------------------------------------------------------ QQ 设置 */
async function renderQQ() {
  await load("qq", async () => {
    store.qq = await api("/api/qq");
  });
  if (pending(store.qq)) return [panelHead("QQ 设置", PANELS[5].desc), skeleton()];

  const qq = store.qq;
  const enabled = h("input", { type: "checkbox", id: "qq-enabled" });
  enabled.checked = Boolean(qq.fields.qq_enabled);
  const group = h("input", { type: "checkbox", id: "qq-group" });
  group.checked = Boolean(qq.fields.qq_group_enabled);
  const listen = h("input", { type: "text", id: "qq-listen", spellcheck: "false" });
  listen.value = qq.fields.qq_listen || "";
  listen.placeholder = "127.0.0.1:8766";
  const token = h("input", { type: "password", id: "qq-token", autocomplete: "off" });
  token.placeholder = qq.secret_mask.qq_token || "还没有配置";
  const allowed = h("textarea", { id: "qq-allowed", rows: "4", spellcheck: "false" });
  allowed.value = qq.fields.qq_allowed || "";

  const save = button("保存 QQ 设置", {
    iconName: "save",
    className: "primary",
    onClick: async () => {
      save.disabled = true;
      await load("save-qq", async () => {
        const payload = {
          qq_enabled: enabled.checked,
          qq_listen: listen.value.trim(),
          qq_allowed: allowed.value.trim(),
          qq_group_enabled: group.checked,
        };
        if (token.value.trim()) payload.qq_token = token.value.trim();
        const result = await api("/api/qq", { method: "PUT", body: payload });
        store.qq = result.qq;
        await refreshSummary();
        toast("QQ 设置已写入 .env", "ok");
        render();
      });
      save.disabled = false;
    },
  });

  return [
    panelHead("QQ 设置", PANELS[5].desc, save),
    h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h("h3", { text: "接入状态" }),
        badge(
          qq.status,
          qq.fields.qq_enabled && qq.allowed_count ? "ok" : qq.fields.qq_enabled ? "bad" : ""
        )
      ),
      h("p", { class: "card-note", text: qq.note }),
      h("p", {
        class: "card-note",
        text: `白名单 ${qq.allowed_count} 个：${qq.allowed_list.join("、") || "（空）"}`,
      })
    ),
    h(
      "div",
      { class: "grid-2" },
      h(
        "section",
        { class: "card" },
        h("div", { class: "card-head" }, h("h3", { text: "开关" })),
        h(
          "div",
          { class: "switch" },
          enabled,
          h("label", { for: "qq-enabled", text: "启用 QQ 网关" })
        ),
        h(
          "div",
          { class: "switch" },
          group,
          h("label", { for: "qq-group", text: "允许群聊（默认只接私聊）" })
        ),
        field(
          "监听地址",
          listen,
          "默认 127.0.0.1:8766：8765 留给 Web 控制台，两个监听不撞（P2 接网关时生效）"
        ),
        field("访问令牌", token, "留空 = 不改（只显示掩码）")
      ),
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h("h3", { text: "白名单" }),
          h("span", { class: "card-note", text: "逗号分隔；开了网关却没白名单会被拒绝" })
        ),
        field("允许的 QQ 号", allowed, "一行一个或用逗号分隔，例如 10001,10002")
      )
    ),
  ];
}

/* ------------------------------------------------------------------ 渲染 */
const RENDERERS = {
  chat: renderChat,
  history: renderHistory,
  persona: renderPersona,
  config: renderConfig,
  prompt: renderPrompt,
  qq: renderQQ,
  traces: renderTraces,
};

let renderToken = 0;

async function render() {
  const token = ++renderToken;
  route();
  renderNav();
  renderStatus();
  const nodes = await RENDERERS[store.panel]();
  if (token !== renderToken) return; // 渲染期间用户又切了面板：这一份丢掉
  $main.replaceChildren(...nodes.filter(Boolean));
  document.title = `${PANELS.find((item) => item.id === store.panel).label} · 以湘控制台`;
}

async function boot() {
  // 拖到卡片外面松手：浏览器默认会导航去打开这个文件，整个控制台就没了
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", (event) => event.preventDefault());
  renderBrandMark();
  try {
    await refreshSummary();
  } catch (error) {
    $main.replaceChildren(
      h(
        "div",
        { class: "notice bad" },
        icon("alert", 18),
        h(
          "div",
          {},
          h("p", { text: "连不上本地服务。" }),
          h("p", { class: "card-note", text: error.message || String(error) }),
          h("p", { class: "card-note", text: "确认 yixiang web 还在跑，然后刷新页面。" })
        )
      )
    );
    return;
  }
  window.addEventListener("hashchange", () => render());
  await render();
}

boot();
