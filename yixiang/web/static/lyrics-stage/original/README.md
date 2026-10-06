# Lyric Stage（original 渲染器）——来源与改动

这个目录里的渲染器**不是**本项目写的，是从下面这个 MIT 项目原样移植过来的。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/tosya301/midori-companion-avatar |
| commit | `0f35fbeab379d7ddb85df875d8c9fe38fdc74e7a`（2026-09-21） |
| 上游目录 | `lyrics-stage/original/` |
| 许可 | MIT，`Copyright (c) 2026 Tim and contributors`（见 `../licenses/original-MIT.txt`） |

## 改动：只有两处文案

除下面这两行外，五个文件与上游**逐字节相同**（`index.html` 连标题都没改）。

```diff
--- a/lyrics-stage/original/lyric-presentation.mjs
+++ b/lyrics-stage/original/lyric-presentation.mjs
-    title: connected ? (state.track.title || '未知歌曲') : '等待 Spotify',
-    artist: connected ? (state.track.artist || '未知歌手') : '打开 Spotify 播放音乐',
+    title: connected ? (state.track.title || '未知歌曲') : '等待播放',
+    artist: connected ? (state.track.artist || '未知歌手') : '打开任意播放器放音乐',
```

**为什么改**：本项目的曲源是 Windows SMTC 全局"正在播放"（网易云 / 酷狗 / QQ / 浏览器
播放器都算），上游写死 Spotify 会误导用户。只改文案，不碰渲染逻辑。

## 校验表

| 文件 | 字节 | SHA256 |
|---|---|---|
| `app.js` | 13937 | `62036006D764CE5572EB0516304F38C5ABC4A03D23D944DDB5DA4FE9F3C8E98F` |
| `index.html` | 1508 | `20385BC72FE23B2257E4BC2B8C388F4A891B80DFEDC315D5D1CDE01D14B2291A` |
| `lyric-layout.mjs` | 3650 | `96FA1BE798F75EE5123C23C259D4C09B8F6A0AADEAEA218C028E4917018A8D92` |
| `lyric-presentation.mjs` | 638 | `2AFC5B5E50F9BD2522C088C869B0B8CC7B5AE881E7FC2467D34428189EBFDD1D`（改后） |
| `styles.css` | 17990 | `3FBEC341F93C93631C98B2919EA4DA4A958E56802A67FB502DDC01B6A9B4912D` |

## 另一个引擎：Folia

参考项目里还有一套 Folia 渲染器（12 个模式、卡拉 OK 逐字），它是 **AGPL-3.0**。
本仓库在 Task 10 里**也把它搬进来了**，但**单独放在隔壁的 [`../folia/`](../folia/) 子树**，
与这里的 MIT original 物理隔离：许可、来源与校验表都写在那棵树自己的 `README.md` 里。
两个引擎由宿主按 `engine` 分叉（父页对 original 发 `midori-lyrics-state`、
对 Folia 发 `midori-lyrics-events`），互不影响。

角色美术 / 语音 / 贴纸 / logo 不在**任何**代码许可范围内——两个引擎都不含可单独复用的素材；
详见 [`../THIRD_PARTY.md`](../THIRD_PARTY.md)。
