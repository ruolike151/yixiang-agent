/* 以湘控制台 · 音乐面板纯函数的用例（node --test yixiang/web/static/music.test.mjs） */

import assert from "node:assert/strict";
import test from "node:test";

import {
  AUTOMATIC_LYRIC_MODES,
  DEFAULT_POLL_MS,
  FOLIA_MODES,
  LYRIC_MODES,
  MIN_POLL_MS,
  ORIGINAL_MODES,
  chooseMode,
  findLineIndex,
  formatLrcTimestamp,
  formatTime,
  hashText,
  lyricLinesToLrc,
  pollDelay,
  stageMessage,
  stateToFoliaEvents,
  trackIdentity,
  visibleLineIndexes,
} from "./music.mjs";

test("六个模式名和顺序都钉死：改顺序等于换掉所有人的观感", () => {
  assert.deepEqual(ORIGINAL_MODES, [
    "luminous",
    "blueprint",
    "cinema",
    "pixel",
    "cloud",
    "mindscape",
  ]);
  assert.equal(new Set(ORIGINAL_MODES).size, 6);
});

test("hashText 是 FNV-1a：三个已知值一个都不能漂", () => {
  assert.equal(hashText(""), 2166136261);
  assert.equal(hashText("a"), 3826002220);
  assert.equal(hashText("query:abc"), 361255333);
});

test("hashText 回无符号整数：不收敛符号的话 % 6 会算出负数", () => {
  for (const text of ["", "a", "query:abc", "告别前要跳舞"]) {
    const value = hashText(text);
    assert.ok(Number.isInteger(value), `${text} 不是整数`);
    assert.ok(value >= 0 && value <= 0xffffffff, `${text} 超出 32 位无符号范围：${value}`);
  }
  assert.equal(hashText(undefined), hashText(""));
  assert.equal(hashText(2026), hashText("2026"));
});

test("trackIdentity 拼 id/title/artist/durationMs，空的一律不占位", () => {
  assert.equal(trackIdentity(null), "");
  assert.equal(trackIdentity({}), "");
  assert.equal(
    trackIdentity({ id: "query:abc", title: "告别前要跳舞", artist: "汪苏泷", durationMs: 0 }),
    "query:abc|告别前要跳舞|汪苏泷"
  );
  assert.equal(
    trackIdentity({ id: "smtc:QQMusic.exe", title: "One Last Kiss", durationMs: 317000 }),
    "smtc:QQMusic.exe|One Last Kiss|317000"
  );
});

test("chooseMode 是确定的：同一首永远同一个模式，没歌时给第一个", () => {
  assert.equal(chooseMode(null), "luminous");
  assert.equal(chooseMode({}), "luminous");
  const track = { id: "query:abc", title: "告别前要跳舞", artist: "汪苏泷", durationMs: 0 };
  assert.equal(chooseMode(track), "pixel");
  assert.equal(chooseMode({ ...track }), "pixel");
  assert.ok(ORIGINAL_MODES.includes(chooseMode({ id: "smtc:QQMusic.exe", title: "One Last Kiss" })));
});

test("pollDelay 四档：没歌最省、暂停次之、在放按 playing", () => {
  assert.equal(pollDelay(null), DEFAULT_POLL_MS.idle);
  assert.equal(
    pollDelay({ track: { durationMs: 200000 }, playback: { isPlaying: false } }),
    DEFAULT_POLL_MS.paused
  );
  assert.equal(
    pollDelay({ track: { durationMs: 200000 }, playback: { isPlaying: true, positionMs: 1000 } }),
    DEFAULT_POLL_MS.playing
  );
});

test("pollDelay 只对「还在放」的歌加密：离结尾不到 12 秒才转 nearEnd", () => {
  const nearEnd = (positionMs) =>
    pollDelay({ track: { durationMs: 200000 }, playback: { isPlaying: true, positionMs } });
  assert.equal(nearEnd(195000), DEFAULT_POLL_MS.nearEnd);
  assert.equal(nearEnd(188000), DEFAULT_POLL_MS.playing); // 正好剩 12 000：不算"快完了"
  assert.equal(nearEnd(200000), DEFAULT_POLL_MS.playing); // 已经放完：等下一次状态
  assert.equal(nearEnd(400000), DEFAULT_POLL_MS.playing); // 位置超过时长：别把自己绕进去
  assert.equal(
    pollDelay({ track: { durationMs: 0 }, playback: { isPlaying: true, positionMs: 195000 } }),
    DEFAULT_POLL_MS.playing
  );
});

test("pollDelay 读后端下发的 pollMs，并有 250ms 下限", () => {
  const state = (pollMs, positionMs = 1000) => ({
    track: { durationMs: 200000 },
    playback: { isPlaying: true, positionMs },
    pollMs,
  });
  assert.equal(pollDelay(state({ playing: 1000 })), 1000);
  assert.equal(pollDelay(state({ playing: 1000, nearEnd: 800 }, 195000)), 800); // 自定义的那档也生效
  assert.equal(pollDelay(state({ playing: 10 })), MIN_POLL_MS);
  assert.equal(pollDelay(state({ playing: 0 })), DEFAULT_POLL_MS.playing); // 0 = 没给，用默认
  assert.equal(pollDelay(state({ playing: "很久" })), DEFAULT_POLL_MS.playing);
  assert.equal(
    pollDelay({ track: { durationMs: 0 }, playback: { isPlaying: false }, pollMs: { paused: 400 } }),
    400
  );
});

test("findLineIndex 空数组回 -1：调用方据此说「这首歌没有歌词」", () => {
  assert.equal(findLineIndex([], 0), -1);
  assert.equal(findLineIndex(null, 1234), -1);
});

test("findLineIndex 找最后一个 timeMs <= 位置的行", () => {
  const lines = [
    { timeMs: 0, text: "第一行" },
    { timeMs: 1000, text: "第二行" },
    { timeMs: 2000, text: "第三行" },
  ];
  assert.equal(findLineIndex(lines, 0), 0);
  assert.equal(findLineIndex(lines, 999), 0);
  assert.equal(findLineIndex(lines, 1000), 1);
  assert.equal(findLineIndex(lines, 1999), 1);
  assert.equal(findLineIndex(lines, 2000), 2);
  assert.equal(findLineIndex(lines, 99999), 2);
});

test("findLineIndex 对还没开唱、负位置、乱序位置都不炸", () => {
  const late = [{ timeMs: 5000, text: "前奏很长" }, { timeMs: 9000, text: "第二行" }];
  assert.equal(findLineIndex(late, 0), 0); // 没到第一行给 0，不是 -1
  assert.equal(findLineIndex(late, -500), 0);
  assert.equal(findLineIndex(late, undefined), 0);
  assert.equal(findLineIndex(late, Number.NaN), 0);
  assert.equal(findLineIndex([{ timeMs: 100 }, { timeMs: 200 }], 150), 0); // timeMs 缺字段当 0
});

test("visibleLineIndexes 是当前行前后两行，且两头都夹得住", () => {
  assert.deepEqual(visibleLineIndexes(5, 10), [3, 4, 5, 6, 7]);
  assert.deepEqual(visibleLineIndexes(0, 10), [0, 1, 2]);
  assert.deepEqual(visibleLineIndexes(9, 10), [7, 8, 9]);
  assert.deepEqual(visibleLineIndexes(-3, 10), [0, 1, 2]);
  assert.deepEqual(visibleLineIndexes(99, 10), [7, 8, 9]);
  assert.deepEqual(visibleLineIndexes(5, 10, 1), [4, 5, 6]);
  assert.deepEqual(visibleLineIndexes(0, 1), [0]);
  assert.deepEqual(visibleLineIndexes(0, 0), []);
  assert.deepEqual(visibleLineIndexes(0, null), []);
});

test("formatTime 是 mm:ss，负值与非数一律 0:00", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(-1), "0:00");
  assert.equal(formatTime(undefined), "0:00");
  assert.equal(formatTime(Number.NaN), "0:00");
  assert.equal(formatTime(999), "0:00");
  assert.equal(formatTime(1000), "0:01");
  assert.equal(formatTime(59999), "0:59");
  assert.equal(formatTime(61000), "1:01");
  assert.equal(formatTime(317000), "5:17");
  assert.equal(formatTime(3600000), "60:00");
});

test("stageMessage 的形状就是参考项目那条契约，多一个字段都不要", () => {
  const state = { connection: "connected", track: { title: "告别前要跳舞" } };
  assert.deepEqual(Object.keys(stageMessage(state, "pixel")).sort(), ["mode", "state", "theme", "type"]);
  assert.deepEqual(stageMessage(state, "pixel"), {
    type: "midori-lyrics-state",
    state,
    mode: "pixel",
    theme: "night",
  });
  assert.equal(stageMessage(state, "cloud", "day").theme, "day");
});
test("LYRIC_MODES 是 18 个：original 6 + folia 12，顺序照上游", () => {
  assert.equal(LYRIC_MODES.length, 18);
  assert.deepEqual(LYRIC_MODES.slice(6), FOLIA_MODES);
  assert.deepEqual(FOLIA_MODES, [
    "classic", "cadenza", "partita", "tilt", "fume", "monet",
    "claddagh", "pendolo", "diorama", "sonnet", "cappella", "tempera",
  ]);
  assert.equal(AUTOMATIC_LYRIC_MODES.length, 17);
  assert.ok(!AUTOMATIC_LYRIC_MODES.includes("tempera"), "tempera 是 opt-in，不进自动轮换");
});

test("chooseMode 按引擎分池：folia 的自动池排除 tempera，显式请求照收", () => {
  assert.equal(chooseMode(null, "", "folia"), "classic");
  assert.ok(FOLIA_MODES.includes(chooseMode({ id: "smtc:QQMusic.exe", title: "One Last Kiss" }, "", "folia")));
  assert.notEqual(chooseMode({ id: "x", title: "y" }, "", "folia"), "tempera");
  assert.equal(chooseMode({ id: "x" }, "tempera", "folia"), "tempera");
  assert.equal(chooseMode({ id: "x" }, "folia:cappella", "folia"), "cappella");
  // 引擎不认识 / 没传 → 退回 original 的 6 个，行为与 Task 8 逐字一致。
  assert.ok(["luminous", "blueprint", "cinema", "pixel", "cloud", "mindscape"]
    .includes(chooseMode({ id: "smtc:QQMusic.exe", title: "One Last Kiss" })));
});

test("formatLrcTimestamp 是 [mm:ss.cc]（厘秒，不是毫秒）", () => {
  assert.equal(formatLrcTimestamp(0), "[00:00.00]");
  assert.equal(formatLrcTimestamp(1_234), "[00:01.23]");
  assert.equal(formatLrcTimestamp(75_450), "[01:15.45]");
  assert.equal(formatLrcTimestamp(3_723_990), "[62:03.99]");
  // 上游是 join("\n")，**没有尾换行**；空文本行丢掉。
  assert.equal(lyricLinesToLrc([{ timeMs: 1_234, text: "嗨" }, { timeMs: 2_000, text: "" }]),
    "[00:01.23]嗨");
});

test("stateToFoliaEvents 无歌时四条事件都要在，形状照上游一个字不改", () => {
  const events = stateToFoliaEvents({ connection: "offline", track: null }, 1_000);
  assert.deepEqual(events.map((envelope) => envelope.event),
    ["Track", "Lyric", "PlayerPauseState", "PlayerProgress"]);
  assert.deepEqual(events[0].data, {});
  assert.deepEqual(events[1].data, {});
  assert.deepEqual(events[2].data, { hasSong: false, isPaused: true, seekbarCurrentPosition: 0 });
  assert.deepEqual(events[3].data, { progress: 0 });
});

test("stateToFoliaEvents：duration 是秒、progress 是毫秒、采样会补上", () => {
  const state = {
    track: { id: "q:1", title: "歌", artist: "人", album: "辑", durationMs: 240_000, artworkUrl: "" },
    playback: { isPlaying: true, positionMs: 10_000, sampledAtMs: 1_000 },
    lyrics: { source: "lrclib", lines: [{ timeMs: 0, text: "第一句" }], karaokeLyric: "" },
  };
  const [track, lyric, pause, progress] = stateToFoliaEvents(state, 2_500);

  assert.equal(track.data.duration, 240);      // 秒
  assert.equal(track.data.author, "人");        // artist → author
  assert.equal(lyric.data.duration, 240);
  assert.equal(lyric.data.hasLyric, true);
  assert.equal(lyric.data.hasKaraokeLyric, false);
  assert.equal(lyric.data.lrc, "[00:00.00]第一句");
  assert.equal(pause.data.seekbarCurrentPosition, 11_500);   // 10_000 + 1_500
  assert.equal(progress.data.progress, 11_500);

  // 暂停时不再补时间；进度封顶在 durationMs。
  const paused = stateToFoliaEvents(
    { ...state, playback: { isPlaying: false, positionMs: 10_000, sampledAtMs: 1_000 } }, 9_000);
  assert.equal(paused[3].data.progress, 10_000);
  const over = stateToFoliaEvents(
    { ...state, playback: { isPlaying: true, positionMs: 999_000, sampledAtMs: 1 } }, 2);
  assert.equal(over[3].data.progress, 240_000);
});

test("stateToFoliaEvents 把 QRC 原串透传给卡拉 OK，不塞进 words", () => {
  const karaokeLyric = "[0,1000](0,500,0)嗨(500,500,0)呀";
  const [, lyric] = stateToFoliaEvents({
    track: { id: "q:2", title: "歌", artist: "人", durationMs: 1_000 },
    playback: { isPlaying: false, positionMs: 0, sampledAtMs: 0 },
    lyrics: { source: "qq", lines: [{ timeMs: 0, text: "嗨呀" }], karaokeLyric },
  }, 0);
  assert.equal(lyric.data.karaokeLyric, karaokeLyric);
  assert.equal(lyric.data.hasKaraokeLyric, true);
  assert.equal(lyric.data.hasTranslatedLyric, false);
  assert.equal(lyric.data.translatedLyric, "");
});
