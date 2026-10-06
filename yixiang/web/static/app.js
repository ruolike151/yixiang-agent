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
import { chooseMode, DEFAULT_THEME, findLineIndex, formatTime, modeChangeRequiresReload, modeOptions, pollDelay, stateToFoliaEvents, stageMessage, visibleLineIndexes } from "./music.mjs";

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
  {
    id: "music",
    label: "音乐",
    icon: "music",
    desc: "读系统「正在播放」、反向控制播放器、放本机曲库与歌词舞台——音频只在这台机器上放。",
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
  music: [
    "M9 18V5l12-2v13",
    "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "M18 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  ],
  play: ["M6 4l14 8-14 8z"],
  pause: ["M8 4v16", "M16 4v16"],
  prev: ["M19 5 9 12l10 7z", "M6 5v14"],
  next: ["M5 5l10 7-10 7z", "M18 5v14"],
  /* 全屏一对：expand 是"撑开"，collapse 是"收回来" */
  expand: [
    "M8 3H5a2 2 0 0 0-2 2v3",
    "M16 3h3a2 2 0 0 1 2 2v3",
    "M8 21H5a2 2 0 0 1-2-2v-3",
    "M16 21h3a2 2 0 0 0 2-2v-3",
  ],
  collapse: [
    "M3 8h3a2 2 0 0 0 2-2V3",
    "M21 8h-3a2 2 0 0 1-2-2V3",
    "M3 16h3a2 2 0 0 1 2 2v3",
    "M21 16h-3a2 2 0 0 0-2 2v3",
  ],
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

/* 会话来源：各入口各聊各的（TECH §10.2.2），列表里得看得出来这一条是从哪进来的。
   网页自己不用标——在网页上看到的多半就是它，标了反而吵；
   零轮的 ``source``（老库 / 异常数据）也不猜，宁可不标。 */
const SOURCE_LABELS = { qq: "QQ", cli: "命令行" };

function sourceBadge(session) {
  const label = SOURCE_LABELS[session.source];
  return label ? badge(label) : null;
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
/* 五套色调（skin）：只换 CSS 变量，不动 DOM 结构，也不落盘——
   和歌词舞台的模式一样，会话内切换、刷新回到默认。 */
const SKINS = [
  { id: "aurora", label: "极光" },
  { id: "violet", label: "霓虹紫" },
  { id: "sunset", label: "落日" },
  { id: "ocean", label: "深海" },
  { id: "graphite", label: "石墨" },
];

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
  // 工作区文件（`data/uploads/` 的**服务端**列表：刷新后还在，所以能一行一行删）。
  // null = 还没拉过，渲染托盘时先显示骨架屏，而不是"空工作区"这个假结论。
  uploads: null,
  uploadLimit: 0,
  // 图片的另一条上限：超过它的图仍能传、仍会记进历史，但模型看不到像素
  inlineLimit: 0,
  // 本轮要随消息发出去的图片（点击「附上」加入，发送后清空）
  attached: [],
  // 输入框草稿：render() 会把整块面板连 textarea 一起换掉，草稿只活在 DOM 上就会丢
  draft: "",
  streaming: false,
  // 当前这一轮的 job_id 与中断句柄（停止生成要用它们）
  streamJob: "",
  abort: null,
  // 用户刚按过「停止」：读流已经断了，收尾要靠它把 chip 补上
  stopped: false,
  loading: new Set(),
  // 音乐面板（Task 8）：状态 / 曲库 / 搜索结果 / 选中的平台与关键词，
  // 以及**活过一次重画**的两样东西——本机 `<audio>` 元素与"舞台开着没"。
  // audio 必须挂在 store 上：render() 整块换掉 #main，挂在 DOM 上等于一刷新就从头播。
  music: null,
  musicLibrary: null,
  musicResults: null,
  musicProvider: "",
  musicQuery: "",
  musicStage: false,
  musicStageFullscreen: false,   // P0：歌词舞台全屏（只改类名，绝不搬 iframe）
  musicEngine: "original",   // Task 10：'original'（MIT，Task 9）| 'folia'（AGPL，Task 10）
  musicMode: "",             // 空串 = 自动（照歌名哈希挑）；填了就是用户在舞台里点名的那个模式
  musicAudio: null,
  musicSong: null,
  skin: "aurora",
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
    music: store.music && store.music.track ? "在放" : "",
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

/* --------------------------------------------------------------- 色调切换 */
/** 只改 `<html data-skin>`：所有颜色都是 token，这一下等于换了整套色板。 */
function applySkin(id) {
  const skin = SKINS.find((item) => item.id === id) || SKINS[0];
  store.skin = skin.id;
  if (store.skin === "aurora") delete document.documentElement.dataset.skin;
  else document.documentElement.dataset.skin = skin.id;
  const picker = document.getElementById("skin-picker");
  if (!picker) return;
  for (const chip of picker.querySelectorAll(".skin-chip")) {
    const active = chip.dataset.skinId === store.skin;
    chip.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

function renderSkinPicker() {
  const host = document.getElementById("topbar-tools");
  if (!host) return;
  const picker = h(
    "div",
    {
      class: "skin-picker",
      id: "skin-picker",
      role: "group",
      "aria-label": "界面色调",
    },
    h("span", { class: "skin-picker-label", text: "色调" }),
    ...SKINS.map((skin) =>
      h("button", {
        type: "button",
        class: "skin-chip",
        "data-skin-id": skin.id,
        title: skin.label,
        "aria-label": skin.label,
        "aria-pressed": store.skin === skin.id ? "true" : "false",
        onclick: () => applySkin(skin.id),
      })
    )
  );
  host.replaceChildren(picker);
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

/** 工作区文件列表：**服务端**的 `data/uploads/` 里现在有什么。
 *
 * 上一版只记在内存里，刷新一次界面就空了、磁盘上的文件却还在——用户看不到行，
 * 自然也没法删，工作区只会越堆越满。所以列表、删除、清空三个动作都对着服务端。
 */
async function refreshUploads() {
  const data = await api("/api/uploads");
  store.uploads = data.items || [];
  store.uploadLimit = data.limit || 0;
  store.inlineLimit = data.inline_limit || 0;
  return store.uploads;
}

/** 上传件的原文地址（<img> 缩略图与它同一条路：/api/uploads/<name>/raw）。 */
function rawUrl(path) {
  const name = String(path || "").split("/").pop();
  return `/api/uploads/${encodeURIComponent(name)}/raw`;
}

function uploadLimitLabel() {
  return store.uploadLimit ? `单个上限 ${fmtBytes(store.uploadLimit)}` : "";
}

/** 输入框下面那行提示。两条上限都由服务端给，别在这里各写一遍数字。 */
function uploadHint() {
  const parts = ["文件直接拖进来或截图直接粘贴也行"];
  if (store.uploadLimit) {
    parts.push(`单个上限 ${fmtBytes(store.uploadLimit)}`);
  }
  parts.push(
    store.inlineLimit
      ? `图片会随这一轮发给模型、它能真的看到；单张 ${fmtBytes(store.inlineLimit)} 以上的只留路径`
      : "图片会随这一轮发给模型，它能真的看到"
  );
  parts.push("普通文件落在 data/uploads/，让模型 read_file 读它");
  return `${parts.join("；")}。`;
}

/** 附图那一行与后端 ``_with_image_note`` 同一口径：本地与刷新后的历史对得上。 */
function imageNote(images) {
  return `（附图：${images.join("、")}）`;
}

function isAttached(path) {
  return store.attached.includes(path);
}

/** 「附上 / 取消附图」：切换一张图是否随下一轮发出去。 */
function toggleAttach(item, repaint) {
  store.attached = isAttached(item.path)
    ? store.attached.filter((path) => path !== item.path)
    : [...store.attached, item.path];
  repaint();
}

async function deleteUpload(item, repaint) {
  await load("delete-upload", async () => {
    const result = await api(`/api/uploads/${encodeURIComponent(item.name)}`, {
      method: "DELETE",
    });
    store.uploads = result.items || [];
    // 刚删掉的那张图不该还挂在待发列表里：否则下一轮会回一个 bad_image
    store.attached = store.attached.filter((path) => path !== item.path);
    repaint();
    toast(`已删除 ${item.name}`, "ok");
  });
}

async function clearUploads(repaint) {
  const count = store.uploads ? store.uploads.length : 0;
  if (!count) return;
  if (!window.confirm(`清空 data/uploads/ 里的 ${count} 个文件？删了就找不回来了。`)) return;
  await load("clear-uploads", async () => {
    const result = await api("/api/uploads/clear", { method: "POST" });
    store.uploads = result.items || [];
    store.attached = [];
    repaint();
    toast(`清空了 ${result.deleted} 个文件`, "ok");
  });
}

function uploadRow(item, repaint) {
  const pending = isAttached(item.path);
  const preview =
    item.kind === "image"
      ? h("img", {
          class: `thumb${pending ? " attached" : ""}`,
          src: item.url || rawUrl(item.path),
          alt: item.name,
          loading: "lazy",
        })
      : icon("file", 16);
  const actions =
    item.kind === "image"
      ? [
          button(pending ? "取消附图" : "附上", {
            iconName: pending ? "check" : "plus",
            className: pending ? "primary" : "ghost",
            title: pending ? "这一轮不带上它" : "让这一轮带上它（模型会真的看到这张图）",
            onClick: () => toggleAttach(item, repaint),
          }),
        ]
      : [
          button("填进输入框", {
            className: "ghost",
            onClick: () => {
              fillComposer(item.hint);
              const box = document.getElementById("chat-input");
              if (box) box.focus();
            },
          }),
          button("复制 read_file 调用", {
            iconName: "check",
            className: "ghost",
            onClick: () => copyText(item.hint),
          }),
        ];
  return h(
    "div",
    { class: "row upload-row" },
    preview,
    h("span", {
      class: "grow mono upload-name",
      title: item.path,
      text: `${item.name} · ${fmtBytes(item.bytes)}`,
    }),
    h("span", { class: "badge", text: item.kind === "image" ? "图片" : "文件" }),
    ...actions,
    button("删除", {
      iconName: "alert",
      className: "ghost",
      title: `从 data/uploads/ 删掉 ${item.name}`,
      onClick: () => deleteUpload(item, repaint),
    })
  );
}

function uploadTray(repaint) {
  if (store.uploads === null) {
    return h(
      "div",
      { class: "card upload-tray" },
      h("div", { class: "card-head" }, h("h3", { text: "工作区文件" })),
      h("p", { class: "card-note", text: "正在读 data/uploads/ …" })
    );
  }
  const items = store.uploads;
  const total = items.reduce((sum, item) => sum + (item.bytes || 0), 0);
  const note = items.length
    ? `${items.length} 个 · 共 ${fmtBytes(total)} · ${uploadLimitLabel()}`
    : `还没有文件 · ${uploadLimitLabel()}`;
  return h(
    "div",
    { class: "card upload-tray" },
    h(
      "div",
      { class: "card-head" },
      h("h3", { text: "工作区文件" }),
      h("span", { class: "card-note", text: note }),
      h("span", { class: "grow" }),
      items.length
        ? button("清空", {
            iconName: "alert",
            className: "ghost",
            title: "清空 data/uploads/（一次删掉全部上传件）",
            onClick: () => clearUploads(repaint),
          })
        : null
    ),
    ...items.map((item) => uploadRow(item, repaint))
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
    await refreshUploads();
    if (uploaded.kind === "image") {
      // 图片不走 read_file（它读不了二进制）：直接挂进待发列表，发送时随消息发出去
      if (!isAttached(uploaded.path)) store.attached = [...store.attached, uploaded.path];
      toast(`已上传 ${uploaded.name}（${fmtBytes(uploaded.bytes)}）：发送时会带上它`, "ok");
    } else {
      if (input) fillComposer(uploaded.hint);
      toast(`已上传 ${uploaded.name}（${fmtBytes(uploaded.bytes)}）`, "ok");
    }
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

async function sendMessage(text, { input, submit, stop, scroll, images = [], onSent }) {
  store.streaming = true;
  const controller = new AbortController();
  store.streamJob = "";
  store.abort = controller;
  store.stopped = false;
  if (stop) stop.hidden = false;
  submit.disabled = true;
  input.disabled = true;

  const note = images.length ? imageNote(images) : "";
  const userBubble = h(
    "article",
    { class: "bubble user" },
    h("div", { class: "bubble-meta" }, h("span", { class: "role", text: "you" })),
    text ? h("p", { text }) : null,
    // 附图直接摆出来：这一轮模型看到的到底是哪几张，发出去之前就看得见
    images.length
      ? h(
          "div",
          { class: "row" },
          ...images.map((path) =>
            h("img", { class: "thumb attached", src: rawUrl(path), alt: path })
          )
        )
      : null
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
        addChip(
          h("span", {
            class: "chip info",
            text:
              event.reason === "truncated"
                ? "到输出上限了，关掉思考重说一遍…"
                : "这轮要查资料，撤回草稿",
          })
        );
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
      body: JSON.stringify({ text, images }), // 图片只在这一轮发出去，不进历史
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
        // 附图那一行跟后端 ``_with_image_note`` 同一口径：本地这一轮与刷新后
        // 从磁盘读回来的历史要长得一模一样，否则刷新一次"带了哪张图"就没了。
        user: [text, note].filter(Boolean).join("\n"),
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

  // 附图只发这一轮：成功了就把待发列表清掉。失败时留着——再按一次发送就能重试，
  // 不用回工作区把每张图重新点一遍「附上」。
  if (onSent && !failure) onSent();

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
  // 工作区列表来自服务端：只差这一次拉取，刷新之后文件还在、行也还在
  if (store.uploads === null) await load("uploads", refreshUploads);
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
  // 托盘自己一格 DOM：删一个文件不该把整块面板（连同正在流式的气泡与滚动位置）
  // 一起重画——所以给它一个只换自己的重绘口。
  const traySlot = h("div", { class: "tray-slot" });
  const repaintTray = () => traySlot.replaceChildren(uploadTray(repaintTray));
  repaintTray();
  const fire = () => {
    const text = input.value.trim();
    const images = [...store.attached];
    // 只带图不带字是合法的一轮："看看这张"这句话本身可以省掉
    if ((!text && !images.length) || store.streaming) return;
    input.value = "";
    store.draft = ""; // 发出去就清草稿：render() 之后不会再把这句话捞回来
    sendMessage(text, {
      input,
      submit,
      stop,
      scroll,
      images,
      onSent: () => {
        store.attached = [];
        repaintTray(); // 缩略图上的「取消附图」要跟着变回「附上」
      },
    });
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
        h("span", { class: "card-note", text: uploadHint() }),
        h("span", { class: "grow" }),
        stop,
        submit
      ),
      traySlot
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
          sourceBadge(session),
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

/* ------------------------------------------------------------------ 音乐 */

/* 面板上有两条**不相干**的播放通道，别混在一起：

   A.「系统正在播放」（Windows SMTC）：只读状态 + 控制按钮（play / pause / next /
      previous / seek）→ `POST /api/music/control`，动的是**别的**播放器。
   B.「本机播放」（本地曲库 / 在线直链）：页面自己的 `<audio>`，由浏览器直接放，
      音频字节从不经过 yixiang、也从不进模型上下文（用户口径 10）。

   两块各自标清是哪一个：合成一块的话，"我按的暂停到底暂停了谁"就成了玄学。
   歌词舞台（Task 9 的 iframe）跟的是 A 的状态 JSON——它要跟系统里那首歌滚。 */

const MUSIC_PROVIDERS = [
  { name: "qq", label: "QQ" },
  { name: "netease", label: "网易云" },
  { name: "kugou", label: "酷狗" },
];

const MUSIC_ACTION_LABELS = {
  play: "播放",
  pause: "暂停",
  next: "下一首",
  previous: "上一首",
  seek: "定位",
};

const MUSIC_CONNECTION_LABELS = {
  disabled: "音乐没开",
  offline: "没找到正在播放",
  local: "本机播放",
  connected: "系统正在播放",
};

let musicPollTimer = null;

/** 离开音乐面板就别再问了：一次多余的请求也是请求。 */
function stopMusicPolling() {
  if (musicPollTimer === null) return;
  clearTimeout(musicPollTimer);
  musicPollTimer = null;
}

/** 只有"看得见的变化"才值得重画一整块面板；进度每秒都在变，但不重画。 */
function musicFingerprint(state) {
  if (!state) return "none";
  const track = state.track || {};
  const playback = state.playback || {};
  const lyrics = state.lyrics || {};
  return [
    state.connection,
    state.source,
    state.note || "",
    track.id,
    track.title,
    track.artist,
    playback.isPlaying,
    lyrics.status,
    Array.isArray(lyrics.lines) ? lyrics.lines.length : 0,
  ].join("|");
}

async function refreshMusicState() {
  const before = musicFingerprint(store.music);
  try {
    store.music = await api("/api/music/state");
  } catch (error) {
    // 服务没起来 / 接口报错：把话说清楚，别让面板停在"加载中"骗人
    store.music = {
      ok: false,
      connection: "offline",
      source: "",
      track: null,
      playback: { isPlaying: false, positionMs: 0, controls: [] },
      lyrics: { status: "empty", source: "", lines: [] },
      note: error.message || String(error),
    };
  }
  syncMediaSession();
  if (musicFingerprint(store.music) !== before) render();
  scheduleMusicPoll();
}

function scheduleMusicPoll() {
  stopMusicPolling();
  if (store.panel !== "music") return;
  musicPollTimer = setTimeout(() => {
    void refreshMusicState();
  }, pollDelay(store.music));
}

/* ------------------------------------------------------- 通道 B：本机播放 */
function ensureMusicAudio() {
  if (store.musicAudio) return store.musicAudio;
  const audio = h("audio", {
    id: "music-audio",
    class: "music-audio",
    controls: true,
    preload: "metadata",
  });
  // src 只会被设成 /api/audio/<name> 或在线直链，两处都由浏览器自己拉，
  // 服务端不代理音频——这一页本身就是播放器。
  audio.addEventListener("play", () => syncMediaSession());
  audio.addEventListener("pause", () => syncMediaSession());
  audio.addEventListener("ended", () => syncMediaSession());
  store.musicAudio = audio;
  return audio;
}

function playMusicSource({ url, title = "", artist = "", album = "", artworkUrl = "" }) {
  if (!url) return;
  const audio = ensureMusicAudio();
  store.musicSong = { url, title, artist, album, artworkUrl };
  audio.src = url;
  audio.play().catch((error) => {
    // 浏览器拦自动播放是常见事：说清楚"自己点一下播放键"，别静默失败
    toast(`浏览器没让播（${error.message || error}）：点下面的播放键试试`, "bad");
  });
  syncMediaSession();
  render();
}

/** Media Session 只属于通道 B——系统媒体键控的就是页面里这个 `<audio>`。 */
function syncMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const now = store.musicSong;
  if (typeof MediaMetadata === "function") {
    navigator.mediaSession.metadata = now
      ? new MediaMetadata({
          title: now.title || "未知歌曲",
          artist: now.artist || "未知歌手",
          album: now.album || "",
          artwork: now.artworkUrl ? [{ src: now.artworkUrl }] : [],
        })
      : null;
  }
  const playing = Boolean(store.musicAudio) && !store.musicAudio.paused;
  navigator.mediaSession.playbackState = playing ? "playing" : now ? "paused" : "none";
}

function bindMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const handlers = {
    play: () => {
      if (store.musicAudio) void store.musicAudio.play();
    },
    pause: () => {
      if (store.musicAudio) store.musicAudio.pause();
    },
    stop: () => {
      if (!store.musicAudio) return;
      store.musicAudio.pause();
      store.musicAudio.currentTime = 0;
    },
  };
  for (const [action, handler] of Object.entries(handlers)) {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch {
      // 这个浏览器不认这个动作：跳过它，别为了一个媒体键把整个面板拖挂
    }
  }
}

/* ------------------------------------------------------- 通道 A：系统播放 */
async function sendMusicControl(action, positionMs = 0) {
  const payload = { action };
  if (action === "seek") payload.positionMs = Math.max(0, Math.round(positionMs));
  try {
    const result = await api("/api/music/control", { method: "POST", body: payload });
    if (result && result.applied === false) {
      // 200 但没生效（比如这台播放器不认 seek）：如实说，不假装成功
      toast(result.reason || `播放器没接受「${MUSIC_ACTION_LABELS[action] || action}」`, "bad");
    } else {
      toast(`已发送：${MUSIC_ACTION_LABELS[action] || action}`, "ok");
    }
    return result;
  } catch (error) {
    toast(error.message || String(error), "bad");
    return null;
  }
}

/* ------------------------------------------------------------------ 画 */
async function renderMusic() {
  if (!store.music) {
    await load("music", async () => {
      store.music = await api("/api/music/state");
    });
  }
  if (pending(store.music)) return [panelHead("音乐", PANELS[7].desc), skeleton()];

  bindMediaSession();
  scheduleMusicPoll();
  const state = store.music;
  if (state.connection === "disabled") {
    return [panelHead("音乐", PANELS[7].desc), musicDisabledCard(state)];
  }
  const [library, search] = await Promise.all([musicLibraryCard(), musicSearchCard()]);
  return [
    panelHead(
      "音乐",
      PANELS[7].desc,
      button("立刻刷新", {
        iconName: "refresh",
        className: "ghost",
        onClick: () => {
          store.music = null;
          render();
        },
      })
    ),
    musicNowCard(state),
    library,
    search,
    musicStageCard(state),
  ];
}

function musicDisabledCard(state) {
  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("h3", { text: "音乐没开" }),
      badge(MUSIC_CONNECTION_LABELS.disabled)
    ),
    h("p", {
      class: "empty",
      text: "把 .env 里的 YIXIANG_MUSIC_ENABLED 改成 1，再刷新这一页就能读系统「正在播放」了。",
    }),
    h("p", {
      class: "card-note",
      text: state.note || "开着也不影响别的：音乐链路不注册工具、不写 chat_log、不进模型上下文。",
    })
  );
}

function musicNowCard(state) {
  const track = state.track;
  const playback = state.playback || {};
  const lyrics = state.lyrics || {};
  const label = MUSIC_CONNECTION_LABELS[state.connection] || state.connection || "未知";
  const head = (extra) =>
    h("div", { class: "card-head" }, h("h3", { text: "系统正在播放" }), badge(label), extra || null);

  if (!track) {
    return h(
      "section",
      { class: "card" },
      head(null),
      h("p", {
        class: "empty",
        text: "没听到正在放的歌：在任意播放器（QQ 音乐 / 网易云 / 浏览器 / 播放器本体）里点播放，这里会自己跟上。",
      }),
      h("p", {
        class: "card-note",
        text: state.note || "读的是 Windows「正在播放」（SMTC）：不挑播放器，也不挑会员。",
      })
    );
  }

  const durationMs = Number(track.durationMs || 0);
  const positionMs = Number(playback.positionMs || 0);
  const allowed =
    Array.isArray(playback.controls) && playback.controls.length
      ? playback.controls
      : ["play", "pause", "next", "previous"];
  const can = (action) => allowed.includes(action);

  const seek = h("input", {
    type: "range",
    id: "music-seek",
    class: "music-seek",
    min: "0",
    max: String(Math.max(durationMs, 1000)),
    step: "1000",
    value: String(Math.min(Math.max(positionMs, 0), Math.max(durationMs, 1000))),
    "aria-label": "播放位置",
  });
  seek.addEventListener("change", () => {
    void sendMusicControl("seek", Number(seek.value));
  });

  const control = (action, iconName, className) =>
    button(MUSIC_ACTION_LABELS[action], {
      iconName,
      className,
      disabled: !can(action),
      onClick: () => {
        void sendMusicControl(action).then(() => refreshMusicState());
      },
    });

  const lines = Array.isArray(lyrics.lines) ? lyrics.lines : [];
  const current = findLineIndex(lines, positionMs);
  let lyricNode = null;
  if (!lines.length) {
    lyricNode = h("p", {
      class: "empty",
      text:
        lyrics.note ||
        (lyrics.status === "loading"
          ? "正在找歌词…"
          : "这首歌没找到歌词：已问过 QQ 音乐和 LRCLib，也可以把 .lrc 放进 music_lyrics_dir。"),
    });
  } else {
    const list = h("ol", { class: "music-lines" });
    for (const index of visibleLineIndexes(current, lines.length)) {
      list.append(
        h("li", {
          class: `music-line${index === current ? " current" : ""}`,
          text: lines[index].text || "♪",
        })
      );
    }
    lyricNode = list;
  }

  return h(
    "section",
    { class: "card" },
    head(badge(playback.isPlaying ? "在放" : "暂停", playback.isPlaying ? "ok" : "")),
    h(
      "div",
      { class: "music-track" },
      h("p", { class: "music-title", text: track.title || "未知歌曲" }),
      h("p", {
        class: "card-note",
        text: [track.artist || "未知歌手", track.album || ""].filter(Boolean).join(" · "),
      })
    ),
    h(
      "div",
      { class: "music-transport" },
      control("previous", "prev", "ghost"),
      playback.isPlaying ? control("pause", "pause", "primary") : control("play", "play", "primary"),
      control("next", "next", "ghost")
    ),
    h(
      "div",
      { class: "music-progress" },
      h("span", { class: "card-note", text: formatTime(positionMs) }),
      seek,
      h("span", { class: "card-note", text: formatTime(durationMs) })
    ),
    h("p", {
      class: "card-note",
      text: `歌词来源：${lyrics.source || "无"}。这几个按钮动的是系统里那个播放器，不是这一页。`,
    }),
    lyricNode
  );
}

async function musicLibraryCard() {
  if (!store.musicLibrary) {
    await load("music-library", async () => {
      store.musicLibrary = await api("/api/music/library");
    });
  }
  if (pending(store.musicLibrary)) return skeleton(2);

  const items = store.musicLibrary.items || [];
  const list = h("div", { class: "music-library" });
  if (!items.length) {
    list.append(
      h("p", {
        class: "empty",
        text: "曲库是空的：把音频放进 music_audio_dir（默认 data/music/audio/）再刷新。",
      })
    );
  }
  for (const item of items) {
    list.append(
      h(
        "div",
        { class: "row" },
        h("span", { class: "music-library-name", text: item.name }),
        h("span", { class: "card-note", text: fmtBytes(item.sizeBytes) }),
        button("播放", {
          iconName: "play",
          className: "ghost",
          onClick: () => playMusicSource({ url: item.url, title: item.name }),
        })
      )
    );
  }
  return h(
    "section",
    { class: "card" },
    h("div", { class: "card-head" }, h("h3", { text: "本机播放" })),
    h("p", {
      class: "card-note",
      text: "这一块放的是你自己盘里的音频：字节只从本机服务流到这台浏览器，谁都不经过。",
    }),
    ensureMusicAudio(),
    list
  );
}

function musicSearchCard() {
  const select = h(
    "select",
    { id: "music-provider", class: "music-provider" },
    MUSIC_PROVIDERS.map((item) => h("option", { value: item.name, text: item.label }))
  );
  select.value = store.musicProvider || MUSIC_PROVIDERS[0].name;
  select.addEventListener("change", () => {
    store.musicProvider = select.value;
  });

  const keyword = h("input", {
    type: "search",
    id: "music-query",
    spellcheck: "false",
    placeholder: "歌名 / 歌手",
  });
  keyword.value = store.musicQuery || "";
  keyword.addEventListener("change", () => {
    store.musicQuery = keyword.value.trim();
  });

  const results = h("div", { class: "music-results" });
  for (const song of store.musicResults || []) {
    results.append(
      h(
        "div",
        { class: "row" },
        h("span", {
          class: "music-result-title",
          text: `${song.title} — ${song.artist || "未知歌手"}`,
        }),
        badge(song.playable ? "可播" : "要会员", song.playable ? "ok" : "warn"),
        button("看看", {
          iconName: "search",
          className: "ghost",
          onClick: () => {
            void openOnlineSong(select.value, song.id);
          },
        })
      )
    );
  }
  if (store.musicResults && !store.musicResults.length) {
    results.append(h("p", { class: "empty", text: "没搜到：换个关键词，或者换一家平台。" }));
  }

  const run = button("搜歌", {
    iconName: "search",
    className: "primary",
    onClick: () => {
      void searchOnlineMusic(select.value, keyword.value.trim());
    },
  });

  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("h3", { text: "在线搜歌" }),
      badge("直链交给浏览器")
    ),
    h("p", {
      class: "card-note",
      text:
        "平台走自建 API（YIXIANG_NETEASE_API_BASE / KUGOU / QQ）：搜到的直链由浏览器自己拉，" +
        "yixiang 不代理音频，也不把搜过什么记进任何对话。",
    }),
    h(
      "div",
      { class: "row" },
      h("div", { class: "field" }, h("label", { for: "music-provider", text: "平台" }), select),
      h("div", { class: "field" }, h("label", { for: "music-query", text: "关键词" }), keyword),
      run
    ),
    results
  );
}

async function searchOnlineMusic(provider, keyword) {
  if (!keyword) {
    toast("先写个歌名", "bad");
    return;
  }
  store.musicProvider = provider;
  store.musicQuery = keyword;
  await load("music-search", async () => {
    const query = `provider=${encodeURIComponent(provider)}&q=${encodeURIComponent(keyword)}`;
    const payload = await api(`/api/music/search?${query}`);
    store.musicResults = payload.items || [];
  });
  render();
}

async function openOnlineSong(provider, songId) {
  await load("music-song", async () => {
    const query = `provider=${encodeURIComponent(provider)}&id=${encodeURIComponent(songId)}`;
    const payload = await api(`/api/music/song?${query}`);
    const song = payload.song || {};
    playMusicSource({
      url: payload.audioUrl,
      title: song.title || "",
      artist: song.artist || "",
      album: song.album || "",
      artworkUrl: song.artworkUrl || "",
    });
  });
}

// —— Task 10：两个引擎靠 store.musicEngine 分叉，两条协议**不合并** ——
// original（MIT，Task 9）：父页发 {type:"midori-lyrics-state", state, mode, theme}
// folia（AGPL，Task 10）  ：父页发 {type:"midori-lyrics-events", events:[…4 条…]}
// 舞台侧各认各的，父页按当前引擎挑信封；改成"统一发一条"会直接让一个引擎黑屏。
const LYRIC_ENGINES = [
  { engine: "original", label: "Original（6 模式）" },
  { engine: "folia", label: "Folia（12 模式）" },
];

function frameUrlFor(engine, mode) {
  if (engine === "folia") {
    // 上游 spotify-lyrics-stage.js:334-340 的形状：folia 的模式走 URL 查询串。
    return `./lyrics-stage/folia/index.html?lyricStage=1&visualizer=${encodeURIComponent(mode)}`;
  }
  // original 与 Task 8 逐字一致：模式**不走 URL**，靠 postMessage 送（换歌不重载 iframe）。
  return "./lyrics-stage/original/index.html";
}

// folia 的产物会注册 scope 含 /lyrics-stage/folia/ 的 service worker；不先注销，
// 换引擎 / 换版本时会命中旧缓存（上游 spotify-lyrics-stage.js:47-53 的原样做法）。
function foliaServiceWorkerReset() {
  if (!("serviceWorker" in navigator)) return Promise.resolve();
  return navigator.serviceWorker
    .getRegistrations()
    .then((registrations) => Promise.all(
      registrations
        .filter((registration) => (registration.scope || "").includes("/lyrics-stage/folia/"))
        .map((registration) => registration.unregister()),
    ))
    .catch(() => {});
}

// 舞台这一秒该用哪个模式：`store.musicMode` 点名优先，空串就照歌名哈希自动挑。
function stageMode() {
  return chooseMode(store.music && store.music.track, store.musicMode, store.musicEngine);
}

// 徽章上除了模式名，还要看得出来这是不是手动点的——不然"怎么这首歌不是自动那个模式"没人解释。
function stageModeLabel(mode) {
  return store.musicMode ? `${mode} · 手动` : mode;
}

function musicStageEngineBar() {
  const bar = document.createElement("div");
  bar.className = "music-stage-engines";
  for (const engine of LYRIC_ENGINES) {
    bar.append(button(engine.label, {
      className: store.musicEngine === engine.engine ? "primary" : "ghost",
      onClick: () => switchLyricEngine(engine.engine),
    }));
  }

  // 模式下拉框：模式池跟着引擎走（original 6 / folia 12，含手动才选得到的 tempera）。
  // 第一项是"自动（<自动会挑到的那个>）"，选中它 = 回到按歌名算。
  const options = modeOptions(store.musicEngine, chooseMode(store.music && store.music.track, "", store.musicEngine));
  const known = options.some((option) => option.value === store.musicMode);
  if (!known) store.musicMode = "";   // 换引擎带过来的残留值：当场清掉，别让下拉框空着

  const label = document.createElement("label");
  label.className = "music-stage-mode";
  label.append(document.createTextNode("模式"));
  const select = document.createElement("select");
  select.id = "music-stage-mode-select";
  for (const option of options) select.append(new Option(option.label, option.value));
  select.value = store.musicMode;
  select.addEventListener("change", () => switchLyricStageMode(select.value));
  label.append(select);
  bar.append(label);
  return bar;
}

function switchLyricEngine(engine) {
  if (engine === store.musicEngine) return;
  store.musicEngine = engine;
  store.musicMode = "";   // 模式池换了，回到"自动"，别把上一个引擎点名的模式带过去
  reloadLyricStageFrame();
  render();
}

// folia 的模式只从 URL 查询串读（`?visualizer=…`），换模式不重载就是"选了没反应"；
// original 的模式走 postMessage，原地换掉即可，重载反而会白屏一下。
function switchLyricStageMode(mode) {
  if (mode === store.musicMode) return;
  store.musicMode = mode;
  if (modeChangeRequiresReload(store.musicEngine)) {
    reloadLyricStageFrame();
  } else {
    const frame = document.querySelector(".music-stage-frame");
    if (frame) postStateToFrame(frame, store.music, stageMode());
  }
  const badgeNode = document.getElementById("music-stage-mode-badge");
  if (badgeNode) badgeNode.textContent = stageModeLabel(stageMode());
}

function reloadLyricStageFrame() {
  const frame = document.querySelector(".music-stage-frame");
  if (!frame) return;
  // 顺序有讲究（上游 spotify-lyrics-stage.js:375-379）：
  // 先把 iframe 甩到 about:blank 断开旧引擎，复位 service worker，再赋真 src。
  // 直接换 src 会命中旧 SW 的缓存，表现为"切过去了但还是旧引擎的画面"。
  frame.src = "about:blank";
  foliaServiceWorkerReset().then(() => {
    frame.src = frameUrlFor(store.musicEngine, stageMode());
  });
}

/* ------------------------------------------------- 歌词舞台 · 全屏（P0） */
// 全屏靠"给卡片加一个类名"实现（样式见 style.css 的 `.music-stage-card.is-fullscreen`）：
// **不把 iframe 挪进别的容器**——DOM 一挪 iframe 就重载，台上那份歌词进度与逐字动画全没了。
// 所以这里只做三件事：改类名、换按钮文案、把焦点送到该在的位置。
function stageFullscreenActive() {
  return Boolean(store.musicStageFullscreen && store.musicStage && store.panel === "music");
}

function setStageFullscreenButton(active) {
  const node = document.getElementById("music-stage-fullscreen");
  if (!node) return;
  node.replaceChildren(
    icon(active ? "collapse" : "expand", 16),
    h("span", { text: active ? "退出全屏" : "全屏播放" })
  );
  node.title = active ? "退出全屏（Esc）" : "全屏播放歌词舞台";
  node.setAttribute("aria-pressed", active ? "true" : "false");
}

/** 把 store 上的全屏状态刷到 DOM：类名 + 按钮文案。不重渲染 = iframe 不重载。 */
function syncStageFullscreenDom() {
  const active = stageFullscreenActive();
  document.body.classList.toggle("stage-fullscreen", active);
  const card = document.querySelector(".music-stage-card");
  if (card) card.classList.toggle("is-fullscreen", active);
  setStageFullscreenButton(active);
}

// 浏览器那层真全屏是"锦上添花"：拿不到（不给权限 / 旧浏览器）也没关系，
// CSS 覆盖层已经把画面铺满了。两个都失败才是真问题，那时按钮文案仍然是对的。
function requestNativeStageFullscreen() {
  const card = document.querySelector(".music-stage-card");
  if (!card || typeof card.requestFullscreen !== "function") return;
  try {
    const result = card.requestFullscreen();
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch {
    // 忽略：覆盖层已经生效
  }
}

function releaseNativeStageFullscreen() {
  if (!document.fullscreenElement) return;
  try {
    const result = document.exitFullscreen();
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch {
    // 忽略
  }
}

function setStageFullscreen(on) {
  const next = Boolean(on);
  if (next === store.musicStageFullscreen) {
    syncStageFullscreenDom();
    return;
  }
  store.musicStageFullscreen = next;
  syncStageFullscreenDom();
  if (next) {
    requestNativeStageFullscreen();
  } else {
    releaseNativeStageFullscreen();
  }
  // 焦点跟着按钮走：进全屏时坐在「退出全屏」上（一按 Esc / 回车就能回来），
  // 退出来还回「全屏播放」——键盘用户不会掉到页首。
  const toggle = document.getElementById("music-stage-fullscreen");
  if (toggle) toggle.focus();
}

window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !store.musicStageFullscreen) return;
  setStageFullscreen(false);
});

// 浏览器用 Esc 关掉它那层全屏时，这里同步收掉覆盖层，别留一屏黑在那儿。
document.addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement || !stageFullscreenActive()) return;
  store.musicStageFullscreen = false;
  syncStageFullscreenDom();
  const toggle = document.getElementById("music-stage-fullscreen");
  if (toggle) toggle.focus();
});

function musicStageCard(state) {
  const mode = chooseMode(state.track, store.musicMode, store.musicEngine);
  const toggle = button(store.musicStage ? "收起歌词舞台" : "打开歌词舞台", {
    iconName: "layers",
    className: store.musicStage ? "ghost" : "primary",
    onClick: () => {
      store.musicStage = !store.musicStage;
      // 收起舞台就等于退出全屏：不然下次点开会"直接跳进全屏"，像是坏了
      if (!store.musicStage) store.musicStageFullscreen = false;
      render();
    },
  });
  const fullscreen = button("全屏播放", {
    iconName: "expand",
    className: "ghost",
    title: "全屏播放歌词舞台",
    onClick: () => setStageFullscreen(!stageFullscreenActive()),
  });
  fullscreen.id = "music-stage-fullscreen";
  fullscreen.setAttribute("aria-pressed", "false");
  let body = h("p", {
    class: "empty",
    text: "点「打开歌词舞台」，它会在这一页里开一块画布，跟着系统里那首歌滚。",
  });
  if (store.musicStage) {
    const engineBar = musicStageEngineBar();
    const frame = h("iframe", {
      class: "music-stage-frame",
      title: "歌词舞台",
      loading: "lazy",
      allowfullscreen: true,
      src: frameUrlFor(store.musicEngine, mode),
    });
    // 用 stageMode() 现取而不是捕获 mode：换模式不重渲染，靠的就是这条回调送新模式。
    frame.addEventListener("load", () => postStateToFrame(frame, store.music, stageMode()));
    body = h("div", { class: "music-stage" }, engineBar, frame);
  }
  const modeBadge = badge(stageModeLabel(mode));
  modeBadge.id = "music-stage-mode-badge";   // 换模式时原地改这一枚，不整页重渲染
  return h(
    "section",
    {
      class: `card music-stage-card${stageFullscreenActive() ? " is-fullscreen" : ""}`,
    },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "row" }, h("h3", { text: "歌词舞台" }), modeBadge),
      h(
        "div",
        { class: "row" },
        h("span", { class: "stage-fullscreen-hint", text: "Esc 退出全屏" }),
        toggle,
        store.musicStage ? fullscreen : null
      )
    ),
    h("p", {
      class: "card-note",
      text:
        "两套渲染器可切：original（MIT，6 个模式、逐句高亮）与 Folia（AGPL，12 个模式、卡拉 OK 逐字）" +
        "。模式默认照歌名算（同一首歌永远同一个），右边下拉框可以手动点名；换引擎会回到自动。",
    }),
    // 嵌入模式下 Folia 自己的界面是收起来的（上游就是这么写的）：舞台只认父页推过去的曲目。
    // 不写这一句，用户会以为"按钮坏了"。换歌走上面那张「在线搜歌」卡片。
    store.musicEngine === "folia"
      ? h("p", {
          class: "card-note",
          text:
            "Folia 走的是嵌入模式（?lyricStage=1），它自带的搜索与设置按钮在这一页里收起来了，" +
            "点了不会有反应。换歌请用上面的「在线搜歌」卡片：搜到之后点播放，曲目就会推给舞台。",
        })
      : null,
    body
  );
}

function postStateToFrame(frame, state, mode) {
  const target = frame?.contentWindow;
  if (!target || !state) return;
  if (store.musicEngine === "folia") {
    target.postMessage(
      { type: "midori-lyrics-events", events: stateToFoliaEvents(state) },
      window.location.origin,
    );
    return;
  }
  // Task 8 原来那一条，一个字不改（theme 沿用盘上的取值）。
  target.postMessage(
    { type: "midori-lyrics-state", state, mode, theme: store.musicTheme ?? DEFAULT_THEME },
    window.location.origin,
  );
}

// 舞台加载完会回一条 midori-lyrics-ready：等它自报家门之后父页再补送一次状态，
// 免得"iframe 还没挂上监听"的那一次 postMessage 白发。
window.addEventListener("message", (event) => {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== "midori-lyrics-ready") return;
  if (data.engine !== store.musicEngine) return;   // 只认当前引擎的自报家门
  if (!store.musicStage || !store.music) return;
  const frame = document.querySelector(".music-stage-frame");
  if (frame && frame.contentWindow === event.source) {
    postStateToFrame(frame, store.music, stageMode());
  }
});

/* ------------------------------------------------------------------ 渲染 */
const RENDERERS = {
  chat: renderChat,
  history: renderHistory,
  persona: renderPersona,
  config: renderConfig,
  prompt: renderPrompt,
  qq: renderQQ,
  traces: renderTraces,
  music: renderMusic,
};

let renderToken = 0;

async function render() {
  const token = ++renderToken;
  if (store.panel !== "music") {
    stopMusicPolling();
    // 换面板就退出全屏：覆盖层是整页 fixed，跟着走到别的面板会挡住人
    store.musicStageFullscreen = false;
  }
  route();
  renderNav();
  renderStatus();
  const nodes = await RENDERERS[store.panel]();
  if (token !== renderToken) return; // 渲染期间用户又切了面板：这一份丢掉
  $main.replaceChildren(...nodes.filter(Boolean));
  // 重渲染会把全屏类名一并刷掉（iframe 是新建的），这里按 store 补回来
  syncStageFullscreenDom();
  document.title = `${PANELS.find((item) => item.id === store.panel).label} · 以湘控制台`;
}

async function boot() {
  // 拖到卡片外面松手：浏览器默认会导航去打开这个文件，整个控制台就没了
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", (event) => event.preventDefault());
  renderBrandMark();
  renderSkinPicker();
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
