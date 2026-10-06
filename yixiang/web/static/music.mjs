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

/** 同一首歌永远挑同一个模式——不是随机的，所以刷新页面不会闪来闪去。
 *
 * 追加的两个参数都是**可选**的：`requestedMode` 是点名要的模式（`"folia:tempera"` 或
 * 简写 `"tempera"`），`engine` 决定从哪个池子里挑（original 6 / folia 12）。都不传时
 * 行为与 Task 8 逐字相同；引擎名不认识也退回 original 那 6 个。
 */
export function chooseMode(track, requestedMode = "", engine = "original") {
  const pool = MODES_BY_ENGINE[engine] || ORIGINAL_MODES;
  const automatic = pool.filter((mode) => mode !== "tempera");
  const exact = pool.find((mode) => `${engine}:${mode}` === requestedMode);
  if (exact) return exact;
  const shorthand = pool.find((mode) => mode === requestedMode);
  if (shorthand) return shorthand;
  const identity = [track?.id, track?.title, track?.artist, track?.durationMs]
    .filter(Boolean)
    .join("|");
  if (!identity) return automatic[0];
  return automatic[hashText(identity) % automatic.length];
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

// —— Folia 引擎（AGPL-3.0 构建产物，来源与许可见 lyrics-stage/folia/README.md）——
// 模式名的顺序照上游 spotify-lyrics-stage.js:3-25 的 SPOTIFY_LYRIC_MODES 逐字抄：
// original 6 个在前、folia 12 个在后。顺序没有语义，但换顺序会让 chooseMode 的
// 自动挑选结果整体漂移（Task 8 有一条用例钉了 "pixel"），所以别动。

export const FOLIA_MODES = Object.freeze([
  "classic", "cadenza", "partita", "tilt", "fume", "monet",
  "claddagh", "pendolo", "diorama", "sonnet", "cappella", "tempera",
]);

export const LYRIC_MODES = Object.freeze([...ORIGINAL_MODES, ...FOLIA_MODES]);

export const MODES_BY_ENGINE = Object.freeze({
  original: ORIGINAL_MODES,
  folia: FOLIA_MODES,
});

// tempera 是 opt-in（上游原话：New full-scene mode is opt-in; preserve the existing
// per-track automatic selection.）：进 LYRIC_MODES、但不进自动轮换池。
export const AUTOMATIC_LYRIC_MODES = Object.freeze(
  LYRIC_MODES.filter((mode) => mode !== "tempera"),
);

/**
 * 舞台模式下拉框的选项表：第一项永远是"自动"（`value` 为空串 = 不点名，照歌名哈希挑），
 * 后面按 `MODES_BY_ENGINE[engine]` 的顺序把该引擎的每个模式都列出来。
 *
 * `tempera` 是 opt-in（不进自动轮换池），但它**必须**在这一列里——它存在的意义就是手动选。
 * 引擎名不认识时退回 original 的 6 个，与 `chooseMode` 同一个口径。
 */
export function modeOptions(engine, autoMode = "") {
  const pool = MODES_BY_ENGINE[engine] || ORIGINAL_MODES;
  const auto = String(autoMode || "");
  return [
    { value: "", label: auto ? `自动（${auto}）` : "自动" },
    ...pool.map((mode) => ({ value: mode, label: mode })),
  ];
}

/**
 * 换模式要不要重载 iframe：folia 的 visualizer 只从 URL 查询串读（`?visualizer=…`），
 * 不重载就是"选了没反应"；original 的模式走 postMessage，重载反而会白屏一下。
 */
export function modeChangeRequiresReload(engine) {
  return engine === "folia";
}

// 上游的时间戳**自带方括号**：`[mm:ss.cc]`（**厘秒**，不是毫秒）。整首 LRC 是
// 行与行 join("\n")、**没有尾换行**。方括号在这一层，`lyricLinesToLrc` 里**不再包**。
export function formatLrcTimestamp(timeMs) {
  const safeTime = Math.max(0, Math.floor(Number(timeMs) || 0));
  const minutes = Math.floor(safeTime / 60_000);
  const seconds = Math.floor((safeTime % 60_000) / 1_000);
  const centiseconds = Math.floor((safeTime % 1_000) / 10);
  return `[${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(centiseconds).padStart(2, "0")}]`;
}

export function lyricLinesToLrc(lines) {
  return (Array.isArray(lines) ? lines : [])
    .filter((line) => line && String(line.text || "").trim())
    .map((line) => `${formatLrcTimestamp(line.timeMs)}${String(line.text).trim()}`)
    .join("\n");
}

// 照上游 spotify-lyrics-stage.js:114-177 与 nowPlayingSource.ts 的 NowPlayingLyricPayload 抄。
// 三条纪律（写错任何一条都是"舞台没歌词"或"进度条乱跳"，且不报错）：
//   ① 内层信封的键是 **`event`** 不是 `type`（外层消息才是 type；见 Step 1.2 的提示框）；
//   ② Track.duration / Lyric.duration 是**秒**，PlayerPauseState.seekbarCurrentPosition
//      与 PlayerProgress.progress 是**毫秒**；
//   ③ karaokeLyric 是**原始 QRC 明文串**，原样透传，不解析、不塞进 lines[].words。
export function stateToFoliaEvents(state, nowMs = Date.now()) {
  const track = state?.track;
  const playback = state?.playback || {};
  const lrc = lyricLinesToLrc(state?.lyrics?.lines || []);
  const karaokeLyric = String(state?.lyrics?.karaokeLyric || "");
  if (!track) {
    return [
      { event: "Track", data: {} },
      { event: "Lyric", data: {} },
      { event: "PlayerPauseState", data: { hasSong: false, isPaused: true, seekbarCurrentPosition: 0 } },
      { event: "PlayerProgress", data: { progress: 0 } },
    ];
  }
  const durationMs = Math.max(0, Number(track.durationMs) || 0);
  const sampledAtMs = Math.max(0, Number(playback.sampledAtMs) || 0);
  // 后端采样的是"墙上时钟"，页面要把它到这一刻的流逝补上，否则播放中进度条会一顿一顿。
  const sampleAgeMs = playback.isPlaying && sampledAtMs
    ? Math.max(0, nowMs - sampledAtMs)
    : 0;
  const progressMs = Math.min(
    durationMs || Number.POSITIVE_INFINITY,
    Math.max(0, Number(playback.positionMs) || 0) + sampleAgeMs,
  );
  return [
    {
      event: "Track",
      data: {
        id: track.id,
        title: track.title,
        author: track.artist,          // 上游字段名是 author
        album: track.album,
        cover: track.artworkUrl,       // 上游字段名是 cover
        duration: durationMs / 1000,   // ← 秒
      },
    },
    {
      event: "Lyric",
      data: {
        source: state?.lyrics?.source || "lrclib",
        title: track.title,
        author: track.artist,
        duration: durationMs / 1000,   // ← 秒
        hasLyric: Boolean(lrc),
        hasTranslatedLyric: false,     // 翻译 v1 不做，但要如实报 false
        hasKaraokeLyric: Boolean(karaokeLyric.trim()),
        lrc,
        translatedLyric: "",
        karaokeLyric,
      },
    },
    {
      event: "PlayerPauseState",
      data: {
        hasSong: true,
        isPaused: !playback.isPlaying,
        seekbarCurrentPosition: progressMs,   // ← 毫秒
      },
    },
    { event: "PlayerProgress", data: { progress: progressMs } },   // ← 毫秒
  ];
}
