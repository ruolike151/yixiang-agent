"""命令分发：``yixiang <command>``（TECH §10.1、§11.1、PART-1 §1）。

命令清单（``--help`` 里必须能看到）：chat / serve / doctor / rag / ops / eval / migrate /
memory（记忆运维）/ skills（技能校验）。
其中 ``serve``（QQ，P2）与 ``rag``（PART 3）在这个阶段只打印"还没实现"并退非零——
**如实告知**比假装成功重要，这条在评测脚本里也会被沿用。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from yixiang import db
from yixiang.config import Settings
from yixiang.ops import doctor
from yixiang.ops.pricing import price_table_text
from yixiang.ops.show_trace import render_trace_detail
from yixiang.ops.tracing import find_trace
from yixiang.ops.usage import summarize, summary_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMANDS = ("chat", "serve", "doctor", "rag", "ops", "eval", "migrate", "memory", "skills")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yixiang",
        description="yixiang（以湘）——本地优先的个人 Agent：有记忆、有评测、有成本账",
    )
    parser.add_argument("--version", action="version", version="yixiang 0.1.0")
    parser.add_argument("--env-file", default=".env", help="配置文件路径（默认 .env）")
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(COMMANDS) + "}")

    chat = sub.add_parser("chat", help="进入交互式对话（流式输出）")
    chat.add_argument("--session", default="cli:default", help="会话 id（默认 cli:default）")
    chat.add_argument("--no-stream", action="store_true", help="关掉流式（调试 / 评测用）")
    chat.add_argument("--once", metavar="文本", help="只跑一句话就退出（脚本 / 演示用）")

    sub.add_parser("serve", help="启动网关（QQ 属 P2，本阶段未实现）")
    sub.add_parser("doctor", help="六项启动自检")
    sub.add_parser("migrate", help="应用数据库迁移")
    sub.add_parser("rag", help="语料与推荐（PART 3 交付）")

    ops = sub.add_parser("ops", help="可观测：trace / 成本 / 价目表")
    ops_sub = ops.add_subparsers(dest="ops_command", metavar="{tail,cost,usage,show-trace,prices}")
    tail = ops_sub.add_parser("tail", help="实时跟随今天的 trace")
    tail.add_argument("--interval", type=float, default=1.0, help="轮询间隔（秒）")
    cost = ops_sub.add_parser("cost", help="今日 token 与成本")
    cost.add_argument("--month", action="store_true", help="按本月汇总")
    ops_sub.add_parser("usage", help="cost 的别名")
    show = ops_sub.add_parser("show-trace", help="渲染某一轮的完整链路")
    show.add_argument("turn_id", help="turn_id（t_20260919_081233_ab12）")
    ops_sub.add_parser("prices", help="打印价目表与查询日期")

    evaluation = sub.add_parser("eval", help="跑评测（默认确定性用例）")
    evaluation.add_argument("--live", action="store_true", help="连真实模型跑 live 用例")
    evaluation.add_argument("extra", nargs="*", help="透传给 pytest 的参数")

    memory = sub.add_parser("memory", help="记忆运维：list / show / sync / verify / restore")
    memory_sub = memory.add_subparsers(
        dest="memory_command", metavar="{list,show,sync,verify,restore}"
    )
    mem_list = memory_sub.add_parser("list", help="列出当前存活的记忆条目")
    mem_list.add_argument("--limit", type=int, default=50, help="最多列多少条")
    mem_show = memory_sub.add_parser("show", help="看某一条的详情")
    mem_show.add_argument("id", type=int, help="事实 id")
    memory_sub.add_parser("sync", help="按 memory.md 同步数据库（文件为准）")
    memory_sub.add_parser("verify", help="文件 / 数据库 / 索引三方对账")
    mem_restore = memory_sub.add_parser("restore", help="把软删的条目捞回来")
    mem_restore.add_argument("id", type=int, help="事实 id")

    skills = sub.add_parser("skills", help="技能运维")
    skills_sub = skills.add_subparsers(dest="skills_command", metavar="{validate}")
    skills_sub.add_parser("validate", help="校验 data/skills/*/SKILL.md 的格式")
    return parser


def load_settings(args: argparse.Namespace) -> Settings:
    return Settings.load(env_file=args.env_file, project_root=PROJECT_ROOT)


def _force_utf8_stdio() -> None:
    """Windows 控制台默认用 GBK，渲染 ✓/⚠/✗ 会直接崩——统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 已被别的库接管 / 不可重配：保持原样
            continue


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    settings = load_settings(args)
    match args.command:
        case "chat":
            return cmd_chat(settings, args)
        case "doctor":
            return doctor.main(settings)
        case "migrate":
            return cmd_migrate(settings)
        case "ops":
            return cmd_ops(settings, args)
        case "eval":
            return cmd_eval(settings, args)
        case "memory":
            return cmd_memory(settings, args)
        case "skills":
            return cmd_skills(settings, args)
        case "rag":
            return _not_yet("rag", "PART 3（语料与按需推荐）")
        case "serve":
            return _not_yet("serve", "P2（QQ 网关）")
        case _:  # pragma: no cover - argparse 已挡住未知命令
            parser.error(f"未知命令 {args.command}")
            return 2


def cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    from yixiang.gateway.cli import ChatCLI

    cli = ChatCLI(settings, stream=not args.no_stream, session_id=args.session)
    if args.once:
        cli._turn(args.once)  # noqa: SLF001 - 一次性入口复用同一渲染路径
        return 0
    return cli.run()


def cmd_migrate(settings: Settings) -> int:
    conn = db.connect(settings.db_path)
    try:
        version = db.migrate(conn)
        tables = db.table_names(conn)
    finally:
        conn.close()
    print(f"数据库 {settings.db_path}")
    print(f"user_version={version}（最新 {db.SCHEMA_VERSION}）· {len(tables)} 张表")
    return 0


def cmd_ops(settings: Settings, args: argparse.Namespace) -> int:
    command = args.ops_command or "usage"
    if command == "tail":
        return cmd_tail(settings, args)
    if command == "show-trace":
        record = find_trace(settings.traces_dir, args.turn_id)
        if record is None:
            print(f"找不到 turn_id={args.turn_id}（用 ops tail 看看今天的记录）")
            return 1
        print(render_trace_detail(record))
        return 0
    if command == "prices":
        print(price_table_text())
        return 0
    period = "month" if getattr(args, "month", False) else "day"
    summary = summarize(settings.usage_path, period=period)
    print(summary_text(summary, budget_cny_per_day=settings.budget_cny_per_day))
    return 0


def cmd_tail(settings: Settings, args: argparse.Namespace) -> int:
    """``ops tail``：跟着今天的 trace 文件走（演示时用来证明"每轮都有记录"）。"""
    from datetime import date

    from yixiang.ops.tracing import trace_file

    path = trace_file(settings.traces_dir, date.today())
    print(f"跟随 {path}（Ctrl+C 退出）")
    offset = 0
    try:
        while True:
            if path.is_file():
                with path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    for line in handle:
                        print(line.rstrip())
                    offset = handle.tell()
            time.sleep(max(args.interval, 0.2))
    except KeyboardInterrupt:
        print("\n已退出 tail。")
        return 0


def _resolve_eval_target(extra: list[str]) -> str | None:
    """``yixiang eval gate`` → ``evals/deterministic/test_gate.py``（PART-2 §1 的验收命令）。

    只在第一个参数命中**已存在的用例文件名**时才展开；其余参数原样透传给 pytest，
    所以 ``yixiang eval -k gate`` 这类用法不会被改写。
    """
    if extra and extra[0].isidentifier():
        candidate = PROJECT_ROOT / "evals" / "deterministic" / f"test_{extra[0]}.py"
        if candidate.is_file():
            return str(candidate)
    return None


def cmd_eval(settings: Settings, args: argparse.Namespace) -> int:
    """跑评测：默认离线确定性用例（≤30 秒、零成本，§13.3）。"""
    marker = "live" if args.live else "not live"
    named = _resolve_eval_target(args.extra)
    command = [
        sys.executable,
        "-m",
        "pytest",
        named or str(PROJECT_ROOT / "evals" / "deterministic"),
        "-m",
        marker,
        *(args.extra[1:] if named else args.extra),
    ]
    print("$ " + " ".join(command))
    return subprocess.call(command, cwd=PROJECT_ROOT)


def cmd_memory(settings: Settings, args: argparse.Namespace) -> int:
    """``yixiang memory ...``：人机共治的记忆运维入口（TECH §7.11、§10.1）。"""
    from yixiang.memory import configure, core_files, semantic, sync

    sub = args.memory_command or "list"
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        core_files.ensure_memory_file(
            settings.data_dir, template_dir=settings.templates_dir
        )
        configure(conn, data_dir=settings.data_dir, settings=settings)
        if sub == "sync":
            report = sync.sync_memory_md(conn)
            print(report.summary())
            for warning in report.warnings:
                print(f"  · {warning}")
            return 0
        if sub == "verify":
            problems = sync.verify(conn)
            if not problems:
                print("一致：memory.md 与数据库、索引三方对齐")
                return 0
            for problem in problems:
                print(problem)
            return 1
        store = semantic.FactStore(semantic.context())
        if sub == "show":
            row = store.get(args.id)
            if row is None:
                print(f"没有 id={args.id} 的事实（用 memory list 看现有 id）")
                return 1
            state = "已删除（可 restore）" if int(row["deleted"]) else "存活"
            print(f"#{row['id']} [{row['subject']}] {row['content']}")
            print(
                f"状态：{state} · 创建 {row['created_at']} · "
                f"更新 {row['updated_at']} · 最近使用 {row['last_used_at'] or '从未'}"
            )
            return 0
        if sub == "restore":
            from yixiang.memory import memory_admin

            print(memory_admin.manage_memory("restore", id=args.id))
            return 0
        rows = store.all()
        if not rows:
            print("（还没有任何记忆条目；用 save_memory 工具或直接编辑 memory.md）")
            return 0
        for row in rows[: max(args.limit, 1)]:
            print(f"[{row['id']}] ({row['subject']}) {row['content']}")
        return 0
    finally:
        conn.close()


def cmd_skills(settings: Settings, args: argparse.Namespace) -> int:
    """``yixiang skills validate``：手写 YAML 坏了要在这儿被挡住（§7.8）。"""
    from yixiang.memory import procedural

    sub = args.skills_command or "validate"
    if sub != "validate":
        print(f"未知子命令 {sub}（只有 validate）", file=sys.stderr)
        return 2
    problems = procedural.validate_dirs([settings.skills_dir])
    loader = procedural.SkillLoader([settings.skills_dir])
    if not problems:
        print(f"校验通过：{settings.skills_dir} 下 {len(loader.skills)} 个技能可用")
        return 0
    for problem in problems:
        print(problem)
    return 1


def _not_yet(name: str, stage: str) -> int:
    print(f"`yixiang {name}` 还没有实现：它的交付在 {stage}（见 docs/parts/）。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
