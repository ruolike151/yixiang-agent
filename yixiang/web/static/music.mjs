/* 以湘控制台 · 音乐面板的纯函数（用例：`node --test yixiang/web/static/music.test.mjs`）
 *
 * 这里只放"给一份状态、算出一个数"的东西：下次该隔多久再问一次、这首歌用哪个
 * 歌词模式、现在该高亮第几行。**不碰 DOM、不发请求**——画界面在 app.js，
 * 取数在 /api/music/*。
 *
 * 与参考项目同源的部分都写在函数上：模式挑选照 `lyrics-stage/original/app.js`
 * 的 `hashText(身份) % 6`，歌词定位照它那条"最后一个 timeMs <= 播放位置"的二分。
 * 抄的是算法，不是文件——original 渲染器那份代码在 Task 9 原样移植。
 */

/** original 引擎的 6 个模式；顺序与参考项目 `SPOTIFY_LYRIC_MODES` 前 6 项一致。 */
export const ORIGINAL_MODES = [
  "luminous",
  "blueprint",
  "cinema",
  "pixel",
  "cloud",
  "mindscape",
];

/** 歌词舞台的主题：参考项目的 `data-host-theme` 只有 day / night 两档。 */
export const DEFAULT_THEME = "night";

/** 位置离结尾多近算"快放完了"：与参考项目父页的 12 000 一致。 */
export const NEAR_END_MS = 12_000;

/** 轮询间隔下限：后端 `MIN_POLL_MS` 也是 250，两边都不许把间隔压到这份上。 */
export const MIN_POLL_MS = 250;

/** 后端没给 `pollMs` 时的兜底，与 `Settings` 的四个默认值一一对应。 */
export const DEFAULT_POLL_MS = {
  playing: 5_000,
  nearEnd: 1_500,
  paused: 15_000,
  idle: 30_000,
};

/* FNV-1a（32 位）：按 UTF-16 码元滚，与参考项目一个字不差。
   `>>> 0` 是必须的——`Math.imul` 回的是**带符号** 32 位整数，不收敛成无符号的话
   `% 6` 会算出负数，模式就随机跳了。 */
export function hashText(text) {
  let hash = 2166136261;
  const source = typeof text === "string" ? text : String(text ?? "");
  for (let i = 0; i < source.length; i += 1) {
    hash ^= source.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

/** 一首歌的"身份串"：改这首歌的哪个字段都算换歌，只有播放位置不算。 */
export function trackIdentity(track) {
  if (!track) return "";
  return [track.id, track.title, track.artist, track.durationMs].filter(Boolean).join("|");
}

/** 同一首歌永远挑同一个模式——不是随机的，所以刷新页面不会闪来闪去。 */
export function chooseMode(track) {
  const identity = trackIdentity(track);
  if (!identity) return ORIGINAL_MODES[0];
  return ORIGINAL_MODES[hashText(identity) % ORIGINAL_MODES.length];
}

function pollValue(polls, key) {
  const raw = Number(polls[key]);
  const value = Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_POLL_MS[key];
  return Math.max(MIN_POLL_MS, Math.round(value));
}

/**
 * 下次隔多久再问一次 `/api/music/state`。
 *
 * 四档与参考项目的父页轮询同构：没歌最省（idle）、暂停次之、在放按 playing、
 * 离结尾不到 12 秒就加密到 nearEnd（不然歌词最后一句会迟到）。间隔本身**读后端
 * 下发的 `state.pollMs`**，前端不写死——改节奏只改 `.env` 一处。
 */
export function pollDelay(state) {
  const polls = { ...DEFAULT_POLL_MS, ...(state && state.pollMs ? state.pollMs : {}) };
  const track = state && state.track;
  const playback = (state && state.playback) || {};
  if (!track) return pollValue(polls, "idle");
  if (!playback.isPlaying) return pollValue(polls, "paused");
  const durationMs = Number(track.durationMs || 0);
  const positionMs = Number(playback.positionMs || 0);
  if (durationMs > 0) {
    const remaining = durationMs - positionMs;
    if (remaining > 0 && remaining < NEAR_END_MS) return pollValue(polls, "nearEnd");
  }
  return pollValue(polls, "playing");
}

/**
 * 当前该高亮第几行：最后一个 `timeMs <= positionMs` 的行。
 *
 * 两个边界是**故意**这么定的，照抄参考项目：还没到第一行时给 0（而不是 -1），
 * 这样"歌刚开头"和"第一行正好在 0 秒"看起来一样，不会闪一下空白；
 * 只有 `lines` 为空才回 -1，调用方据此显示"这首歌没有歌词"。
 */
export function findLineIndex(lines, positionMs) {
  if (!Array.isArray(lines) || lines.length === 0) return -1;
  const target = Number(positionMs) || 0;
  let low = 0;
  let high = lines.length - 1;
  let answer = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const at = Number(lines[mid] && lines[mid].timeMs) || 0;
    if (at <= target) {
      answer = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return Math.max(0, answer);
}

/** 舞台与面板都只画当前行的前后两行（参考项目 `index-2..index+2`）；空数组 = 没词。 */
export function visibleLineIndexes(index, total, radius = 2) {
  const count = Math.trunc(Number(total));
  if (!Number.isFinite(count) || count <= 0) return [];
  const current = Math.min(Math.max(Math.trunc(Number(index) || 0), 0), count - 1);
  const start = Math.max(0, current - radius);
  const end = Math.min(count - 1, current + radius);
  const indexes = [];
  for (let i = start; i <= end; i += 1) indexes.push(i);
  return indexes;
}

/** `mm:ss`（分钟不补零，秒补）。负数 / 非数 / 空值一律 `0:00`，不显示 `NaN:NaN`。 */
export function formatTime(ms) {
  const total = Number(ms);
  if (!Number.isFinite(total) || total <= 0) return "0:00";
  const seconds = Math.floor(total / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * 发给歌词舞台 iframe 的那条 `postMessage`。
 *
 * 形状是参考项目的契约，一个字都不能改：`{type, state, mode, theme}`。
 * 舞台那边认 `type === "midori-lyrics-state"` 才收（还要核 source 与 origin）。
 */
export function stageMessage(state, mode, theme = DEFAULT_THEME) {
  return { type: "midori-lyrics-state", state, mode, theme };
}
