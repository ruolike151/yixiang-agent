"""``yixiang rag …`` 与 ``yixiang brief`` 的命令实现（TECH §18.1 / PART-3 §3、§6）。

命令层只做三件事：**解析参数 → 装配（连接 / 迁移 / 检索上下文）→ 把账目打印清楚**。
抓取、嵌入、检索、日报组装分别在 ``yixiang/rag`` 与 ``yixiang/tools`` 里，所以这里
没有业务判断，只有"把零件接起来、把结果如实说出来"。

两条纪律（改之前先读）：

  * **离线等价入口**：``--source local --file …`` 不联网、不下载模型也能把整条链路
    跑通（PART-3 §7），``evals/fixtures/media_sample.json`` 就是它的输入；
  * **嵌入拿不到就降级**：``_embedder()`` 失败只打印一行提示并返回 ``None``——入库照跑
    （只写 FTS 索引）、检索退纯 FTS5，命令不会因为模型没下下来而失败（D-24）。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from yixiang.config import Settings

# 仓库根：文档里的命令都是在仓库根跑的，相对路径找不到时回落到这里
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = PROJECT_ROOT / "evals" / "golden"
INGEST_FIXTURE = PROJECT_ROOT / "evals" / "fixtures" / "media_sample.json"

# 抓取口径与 rag.ingest 对齐（这里不 import 它，避免 CLI 启动时拉进整个检索栈）
PAGE_SIZE = 20
TMDB_ENV = "YIXIANG_TMDB_API_KEY"

OFFLINE_HINT = (
    "离线入口：yixiang rag ingest --source local --file evals/fixtures/media_sample.json"
)


# --------------------------------------------------------------------- 解析
def add_parsers(sub: Any) -> None:
    """把 ``rag`` / ``brief`` 挂到主解析器上（``__main__.build_parser`` 调用）。"""
    rag = sub.add_parser("rag", help="语料与推荐：ingest / reindex / eval")
    rag_sub = rag.add_subparsers(dest="rag_command", metavar="{ingest,reindex,eval}")

    ingest = rag_sub.add_parser("ingest", help="抓取 / 读本地语料并幂等入库")
    ingest.add_argument(
        "--source",
        choices=("bangumi", "tmdb", "local"),
        default="bangumi",
        help="语料来源（默认 bangumi；local 是离线入口）",
    )
    ingest.add_argument("--file", default="", help="本地语料文件（--source local；.json / .jsonl）")
    ingest.add_argument("--tags", default="", help="Bangumi 标签，逗号分隔（--source bangumi）")
    ingest.add_argument("--genres", default="", help="TMDb 类型 id，逗号分隔（--source tmdb）")
    ingest.add_argument("--pages", type=int, default=1, help="抓多少页（默认 1）")
    ingest.add_argument("--limit", type=int, default=PAGE_SIZE, help=f"每页条数（默认 {PAGE_SIZE}）")
    ingest.add_argument(
        "--since", default="", help="只要这之后的作品（YYYY-MM-DD，手动增量；不做自动追更）"
    )
    ingest.add_argument(
        "--sort",
        choices=("heat", "rank", "score", "match"),
        default="heat",
        help="Bangumi 排序（默认 heat；关键词为空时 rank / score 是无效排序）",
    )
    ingest.add_argument(
        "--min-rating",
        type=float,
        default=0.0,
        help="平均分下限（默认 0；>0 时同时写进服务端 filter，客户端再判一次）",
    )
    ingest.add_argument(
        "--min-votes",
        type=int,
        default=0,
        help="评分人数下限（默认 0；服务端没有这个过滤，只能客户端判 rating.total）",
    )
    ingest.add_argument(
        "--want",
        type=int,
        default=0,
        help="目标**实收**条数：过滤 + 去重后到数就停（默认 0 = 只按 --pages 抓）",
    )
    ingest.add_argument("--api-key", default="", help=f"TMDb API key（默认读环境变量 {TMDB_ENV}）")
    ingest.add_argument(
        "--resume", action="store_true", help="从 meta.ingest_cursor_<source> 接着跑"
    )
    ingest.add_argument(
        "--dry-run", action="store_true", help="只统计会写什么，一个字节都不落库"
    )

    reindex = rag_sub.add_parser(
        "reindex", help="全量重建 FTS / 向量索引（换嵌入模型或改分词策略后必跑）"
    )
    reindex.add_argument("--batch", type=int, default=32, help="嵌入批大小（默认 32）")

    evaluate = rag_sub.add_parser("eval", help="golden 集检索评测（top-3 命中率 ≥60% + MRR）")
    evaluate.add_argument(
        "--holdout", action="store_true", help="跑 media_holdout.jsonl（发版前防过拟合用）"
    )
    evaluate.add_argument(
        "--top-k", type=int, default=3, help="评测的 k（默认 3，产品指标就挂在 3 上）"
    )
    evaluate.add_argument(
        "--source",
        choices=("", "bangumi", "tmdb", "local"),
        default="",
        help="评测前先入库（--source local 配 --file 是离线入口）",
    )
    evaluate.add_argument("--file", default="", help="配合 --source local")
    evaluate.add_argument("--tags", default="", help="配合 --source bangumi")
    evaluate.add_argument("--genres", default="", help="配合 --source tmdb")
    evaluate.add_argument("--pages", type=int, default=1, help="配合 --source bangumi/tmdb")
    evaluate.add_argument("--limit", type=int, default=PAGE_SIZE)
    evaluate.add_argument("--api-key", default="")

    brief = sub.add_parser("brief", help="按需生成今天的日报（今日安排 + 1 条影视推荐）")
    brief.add_argument(
        "--scope", choices=("today", "tomorrow"), default="today", help="生成哪一天（默认今天）"
    )


# --------------------------------------------------------------------- 装配
@contextlib.contextmanager
def _session(settings: Settings, *, embedder: Any, clock: Any):
    """连接 + 迁移 + 装配检索；退出时**一定**清全局上下文并关库。"""
    from yixiang import db
    from yixiang.rag import retrieve

    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        retrieve.configure(
            conn,
            data_dir=settings.data_dir,
            clock=clock,
            # 显式 embedder=None 表示"这次不启用嵌入"（如离线评测），
            # 不能再回落成 settings 自动造一个注定失败的懒加载后端
            settings=None if embedder is None else settings,
            embedder=embedder,
        )
        yield conn
    finally:
        retrieve.reset()
        conn.close()


def _embedder(settings: Settings) -> Any:
    """按 ``settings`` 造嵌入后端；不可用就返回 ``None``（降级，不是失败）。"""
    from yixiang.rag.embed import build_embedder

    try:
        return build_embedder(settings)
    except Exception as exc:  # 后端没实现 / 依赖缺失：照跑，只写 FTS
        print(f"嵌入后端不可用（{exc}）：本次只写 FTS 索引，检索降级为纯 FTS5。")
        return None


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").replace("，", ",").split(",") if part.strip()]


def _resolve_file(raw: str) -> Path:
    """相对路径先按 cwd 解析，找不到再按仓库根（文档里的命令都在仓库根跑）。"""
    path = Path(raw)
    if path.is_absolute() or path.is_file():
        return path
    return PROJECT_ROOT / path


def _corpus_size(conn: Any) -> int:
    from yixiang.tools.media import corpus_size

    return int(corpus_size(conn))


# --------------------------------------------------------------------- 命令
def cmd_rag(settings: Settings, args: argparse.Namespace) -> int:
    """``yixiang rag {ingest,reindex,eval}``：没给子命令就打印用法（退 2，别默认抓取）。"""
    sub = getattr(args, "rag_command", None)
    if sub == "ingest":
        return _ingest(settings, args)
    if sub == "reindex":
        return _reindex(settings, args)
    if sub == "eval":
        return _eval(settings, args)
    print(f"用法：yixiang rag {{ingest,reindex,eval}}\n  {OFFLINE_HINT}", file=sys.stderr)
    return 2


def _ingest(settings: Settings, args: argparse.Namespace) -> int:
    """入库：抓取或读本地文件 → 幂等 upsert → 打印账目。"""
    from yixiang.rag import ingest as ingest_mod
    from yixiang.runtime.models import SystemClock

    if args.source == "local" and not args.file:
        print(f"--source local 需要 --file。\n  {OFFLINE_HINT}", file=sys.stderr)
        return 2

    clock = SystemClock()
    embedder = _embedder(settings)
    with _session(settings, embedder=embedder, clock=clock) as conn:
        try:
            items = _collect(
                ingest_mod,
                settings,
                conn,
                source=args.source,
                file=args.file,
                tags=args.tags,
                genres=args.genres,
                pages=args.pages,
                limit=args.limit,
                api_key=args.api_key,
                since=args.since,
                sort=args.sort,
                min_rating=args.min_rating,
                min_votes=args.min_votes,
                want=args.want,
                resume=args.resume,
            )
        except Exception as exc:  # 网络 / 接口变动 / 文件缺失：如实说，不假装入库成功
            print(
                f"取语料失败（{type(exc).__name__}: {exc}）。"
                "原始 JSON 缓存到 data/raw/，重跑不重抓；接口变了只改 ingest.py 一处。",
                file=sys.stderr,
            )
            return 1
        report = ingest_mod.ingest_items(
            conn,
            items,
            source=args.source,
            embedder=embedder,
            dry_run=bool(args.dry_run),
            clock=clock,
        )
        print(report.summary())
        if not args.dry_run:
            print(f"media 表现有 {_corpus_size(conn)} 部作品。")
        return 0 if report.failed == 0 else 1


def _collect(
    ingest_mod: Any,
    settings: Settings,
    conn: Any,
    *,
    source: str,
    file: str = "",
    tags: str = "",
    genres: str = "",
    pages: int = 1,
    limit: int = PAGE_SIZE,
    api_key: str = "",
    since: str = "",
    sort: str = "heat",
    min_rating: float = 0.0,
    min_votes: int = 0,
    want: int = 0,
    resume: bool = False,
) -> list[Any]:
    """按 ``--source`` 取语料（抓取与写库解耦：这一步只读不写）。"""
    if source == "local":
        path = _resolve_file(file)
        if not path.is_file():
            raise FileNotFoundError(f"语料文件不存在：{file}")
        return list(ingest_mod.load_local(path, source="local"))
    if source == "bangumi":
        return list(
            ingest_mod.fetch_bangumi(
                data_dir=settings.data_dir,
                tags=_split_csv(tags),
                pages=max(int(pages), 1),
                conn=conn,
                resume=bool(resume),
                limit=max(int(limit), 1),
                since=str(since or "").strip(),
                sort=str(sort or "heat").strip() or "heat",
                min_rating=max(float(min_rating or 0.0), 0.0),
                min_votes=max(int(min_votes or 0), 0),
                want=max(int(want or 0), 0),
                # 只给 Bangumi 传：TMDb 下面那条调用**不加**，它是另一个出口
                proxy=str(getattr(settings, "bangumi_proxy", "") or ""),
            )
        )
    key = api_key or os.environ.get(TMDB_ENV, "")
    if not key:
        raise ValueError(f"TMDb 需要 API key：--api-key 或环境变量 {TMDB_ENV}")
    return list(
        ingest_mod.fetch_tmdb(
            data_dir=settings.data_dir,
            api_key=key,
            genre_ids=_split_csv(genres),
            pages=max(int(pages), 1),
            conn=conn,
            resume=bool(resume),
            limit=max(int(limit), 1),
            since=str(since or "").strip(),
        )
    )


def _reindex(settings: Settings, args: argparse.Namespace) -> int:
    """全量重建索引：清空 FTS / 向量后从 ``media`` 主表重建（灾难恢复路径）。"""
    from yixiang.rag import retrieve
    from yixiang.runtime.models import SystemClock

    clock = SystemClock()
    embedder = _embedder(settings)
    with _session(settings, embedder=embedder, clock=clock) as conn:
        if _corpus_size(conn) == 0:
            print(f"影视库是空的，没有可重建的索引。\n  {OFFLINE_HINT}")
            return 1
        counts = retrieve.reindex_media(conn, batch=max(int(args.batch), 1))
        print(
            f"重建完成：media {counts['media']} 部 · FTS {counts['fts']} 行 · "
            f"向量 {counts['vec']} 条（分词版本 {retrieve.TOKENIZER_VERSION}）"
        )
        if counts["vec"] == 0:
            print("向量 0 条：嵌入后端不可用，检索会降级为纯 FTS5（D-24 允许的降级）。")
        return 0


def _eval(settings: Settings, args: argparse.Namespace) -> int:
    """跑 golden 集：top-3 命中率 + MRR；语料 / 考卷缺席就打印可行动的提示并退非零。"""
    from yixiang.rag import evaluate
    from yixiang.runtime.models import SystemClock
    from yixiang.tools.media import INGEST_HINT

    clock = SystemClock()
    embedder = _embedder(settings)
    with _session(settings, embedder=embedder, clock=clock) as conn:
        if args.source:
            from yixiang.rag import ingest as ingest_mod

            try:
                items = _collect(
                    ingest_mod,
                    settings,
                    conn,
                    source=args.source,
                    file=args.file,
                    tags=args.tags,
                    genres=args.genres,
                    pages=args.pages,
                    limit=args.limit,
                    api_key=args.api_key,
                )
            except Exception as exc:
                print(f"取语料失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
                return 1
            report = ingest_mod.ingest_items(
                conn, items, source=args.source, embedder=embedder, clock=clock
            )
            print(report.summary())

        corpus = _corpus_size(conn)
        if corpus == 0:
            print(f"语料是空的，评测没有意义。{INGEST_HINT}")
            return 1
        golden = GOLDEN_DIR / ("media_holdout.jsonl" if args.holdout else "media.jsonl")
        cases = evaluate.load_golden(golden)
        if not cases:
            print(
                f"golden 集缺失或为空：{golden}\n"
                "  它是一行一条的 jsonl（{\"query\": …, \"expect\": [标题…]}），"
                "见 docs/parts/PART-3-rag-recommend.md §3。"
            )
            return 1
        report = evaluate.evaluate(
            cases, top_k=max(int(args.top_k), 1), corpus=corpus, source=golden.name
        )
        print(report.summary())
        return 0 if report.passed() else 1


def cmd_brief(settings: Settings, args: argparse.Namespace) -> int:
    """``yixiang brief``：按需生成日报并落盘（内容层，不含任何调度器）。"""
    from yixiang.runtime.models import SystemClock
    from yixiang.tools import brief as brief_tool

    clock = SystemClock()
    embedder = _embedder(settings)
    with _session(settings, embedder=embedder, clock=clock) as conn:
        moment = clock.now()
        text = brief_tool.daily_brief(
            conn, clock.now, settings.data_dir, scope=str(args.scope or "today")
        )
        day = moment.date() + timedelta(days=1 if args.scope == "tomorrow" else 0)
        print(text)
        path = Path(settings.data_dir) / brief_tool.BRIEF_DIRNAME / f"{day.isoformat()}.md"
        print(f"（已写入 {path}；同一天重跑覆盖同一份）")
        return 0


__all__ = ["GOLDEN_DIR", "INGEST_FIXTURE", "OFFLINE_HINT", "add_parsers", "cmd_brief", "cmd_rag"]
