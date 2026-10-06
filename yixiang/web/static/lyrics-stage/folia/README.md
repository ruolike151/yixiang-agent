# Lyric Stage（Folia 引擎）——来源、许可与范围

这个目录里的 105 个文件**不是**本项目写的，也不是本项目构建的：它们是从下面这个
项目里**原样搬运**的构建产物，一个字节都没有改动（`README.md` 是唯一的新增文件）。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/tosya301/midori-companion-avatar |
| commit | `0f35fbeab379d7ddb85df875d8c9fe38fdc74e7a`（2026-09-21） |
| 上游目录 | `lyrics-stage/folia/**`（105 个文件 / 12,917,606 B） |
| 上游引擎 | `chthollyphile/folia-major`：base **0.6.16** @ `446e9038b8e43f44d4ed0628ce5b8fc8ad22538c`，backport **v0.7.3** @ `04d222779b2bd63fd04436cb7477c7de5dd5f008` |
| 凭据清单 | `docs/FOLIA_SOURCE_MANIFEST.json`（105 条 `{path, bytes, sha256}`） |
| 许可 | **AGPL-3.0**（见 `../licenses/folia-AGPL-3.0.txt`） |

## 为什么是"搬产物"而不是"自己构建"

上游 `docs/FOLIA_BUILD.md` 写明构建需要 **Node ≥24**；本机是 Node 22。为了这一个引擎
升一次 Node 并不可怕，可怕的是**重新构建会让"逐字节等于上游"这条可验证性消失**——
所以这里选择搬产物，并用 `docs/FOLIA_SOURCE_MANIFEST.json` 的 105 条 SHA256 把它钉死。
要改 Folia 的行为时再升 Node 重打，那是另一件事。

## 12 个模式

`classic` / `cadenza` / `partita` / `tilt` / `fume` / `monet` /
`claddagh` / `pendolo` / `diorama` / `sonnet` / `cappella` / `tempera`。

`tempera` 是 **opt-in**：它不在自动轮换池里（自动池 17 个），只有显式点名才会用。
`cappella` 自带角色"明前奶绿"的两张立绘与三个贴纸——见下面的许可边界。

## 许可边界（照实写，别含混）

- **代码**：这一棵子树是 **AGPL-3.0**。它**只覆盖 `lyrics-stage/folia/` 及其许可文件**；
  仓库其余部分（宿主、`lyrics-stage/original/`）仍是 MIT。别把 AGPL 声明挪到仓库根部，
  也别给这棵树套上宿主的 MIT。
- **对应源码**：上游 `lyrics-stage/THIRD_PARTY.md` 声明 "Corresponding source is included"，
  本目录同时保留它的凭据清单（`docs/FOLIA_SOURCE_MANIFEST.json`：105 条路径 + 字节数 + SHA256）
  与来源坐标。**本仓库是私有本机项目、不对外分发**；如果将来要分发这些产物，
  必须先补齐一份完整的对应源码包，而不是只给清单。
- **美术 / 音乐 / 歌词 / 封面**：上游 `THIRD_PARTY.md` 写明**代码许可不授予**这些权利；
  角色立绘、贴纸、logo 不在 AGPL 授权范围内。它们只是 vendored 产物里的字节，
  **不要复制到仓库其他位置，也不要单独拿出来当素材用**。
