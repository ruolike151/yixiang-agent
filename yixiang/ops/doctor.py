"""``yixiang doctor``：六项启动自检（TECH §1.2、PART-1 §1）。

口径（有意为之，写在代码里免得以后自己都记不清）：

  * 检查 1「配置」判的是**能不能加载并校验**，不是"必须填了 key"——
    没填 key 只告警：本地跑测试、看 /tools 都不需要联网花钱；
  * 检查 5「模型探活」在没有 key 时**跳过并告警**，不算失败；
  * 检查 6 会把 ``templates/`` 的三文件复制到 ``data/``（已存在则不覆盖），
    这样 clone 下来第一次跑 doctor 就能得到可用的记忆文件。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from yixiang import db
from yixiang.config import Settings
from yixiang.errors import user_message as error_message
from yixiang.providers import OpenAICompatibleProvider
from yixiang.runtime.models import ProviderRequest, SystemClock, user_message
from yixiang.runtime.session import CORE_FILES

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "✓", WARN: "⚠", FAIL: "✗"}


@dataclass(slots=True)
class Check:
    label: str
    status: str
    detail: str = ""


def run_checks(settings: Settings) -> list[Check]:
    """跑完六项自检。任何一项 ``fail`` 都让 doctor 退非零。"""
    checks = [_check_config(settings)]
    conn = None
    try:
        conn = db.connect(settings.db_path)
        checks.append(_check_data_dir(settings))
        checks.append(_check_database(conn))
        checks.append(_check_sqlite_vec(conn))
    finally:
        if conn is not None:
            conn.close()
    checks.append(_check_model(settings))
    checks.append(_check_core_files(settings))
    return checks


def _check_config(settings: Settings) -> Check:
    problems = [item for item in settings.validate() if "API_KEY" not in item]
    if problems:
        return Check("配置加载与校验", FAIL, "；".join(problems))
    if not settings.api_key:
        return Check(
            "配置加载与校验", WARN, f"已加载，但未填 YIXIANG_API_KEY（{settings.describe()}）"
        )
    return Check("配置加载与校验", OK, settings.describe())


def _check_data_dir(settings: Settings) -> Check:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("data 目录可写", FAIL, f"{settings.data_dir}：{exc}")
    return Check("data 目录可写", OK, str(settings.data_dir))


def _check_database(conn) -> Check:
    try:
        version = db.migrate(conn)
    except Exception as exc:  # 迁移失败必须报出来，不能带着坏库启动
        return Check("SQLite 与迁移", FAIL, f"migrate 失败：{exc}")
    tables = db.table_names(conn)
    missing = [name for name in db.EXPECTED_TABLES if name not in tables]
    if missing:
        return Check("SQLite 与迁移", FAIL, f"缺表：{', '.join(missing)}")
    if version != db.SCHEMA_VERSION:
        return Check("SQLite 与迁移", FAIL, f"user_version={version}，期望 {db.SCHEMA_VERSION}")
    return Check(
        "SQLite 与迁移",
        OK,
        f"user_version={version}，{len(db.EXPECTED_TABLES)} 张业务表齐备",
    )


def _check_sqlite_vec(conn) -> Check:
    if db.load_sqlite_vec(conn):
        return Check("sqlite-vec 扩展", OK, "可加载（向量检索所需）")
    return Check("sqlite-vec 扩展", FAIL, "加载失败：先 `uv sync` 装 sqlite-vec")


def _check_model(settings: Settings) -> Check:
    if not settings.api_key:
        return Check("模型探活", WARN, "未配置密钥，已跳过（不阻塞本地开发）")
    provider = OpenAICompatibleProvider(settings, clock=SystemClock())
    request = ProviderRequest(
        role="utility",
        messages=[user_message("ping")],
        max_tokens=1,
        timeout=min(settings.gate_timeout, 8.0),
        stream=False,
    )
    try:
        reply = asyncio.run(provider.complete(request))
    except Exception as exc:
        code = getattr(exc, "code", "E_LLM_BAD_REQUEST")
        # 这里要的是"给用户看的一句话"，不是 runtime.models 里的 user_message（构造 Message）
        return Check("模型探活", FAIL, f"{code}：{error_message(code)}")
    finally:
        # 连接是在上一个小事件循环里建的，关不掉也不影响结论
        with contextlib.suppress(Exception):
            asyncio.run(provider.aclose())
    return Check("模型探活", OK, f"{reply.model} 响应 {reply.latency_ms}ms")


def _check_core_files(settings: Settings) -> Check:
    """把 templates/ 的三文件复制到 data/（§1.2 检查项 6）。"""
    if not settings.templates_dir.is_dir():
        return Check("三文件就位", FAIL, f"找不到模板目录 {settings.templates_dir}")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    copied, kept = [], []
    for name in CORE_FILES:
        target: Path = settings.data_dir / name
        source = settings.templates_dir / name
        if target.exists():
            kept.append(name)
            continue
        if not source.is_file():
            return Check("三文件就位", FAIL, f"模板缺 {source}")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        copied.append(name)
    detail = f"{settings.data_dir}｜新建 {', '.join(copied) or '无'}｜保留 {', '.join(kept) or '无'}"
    return Check("三文件就位", OK, detail)


def render(checks: list[Check], *, header: str = "yixiang doctor") -> str:
    lines = [header]
    for index, check in enumerate(checks, start=1):
        lines.append(f"{MARKS[check.status]} {index}. {check.label}：{check.detail}")
    failed = [check for check in checks if check.status == FAIL]
    warned = [check for check in checks if check.status == WARN]
    if failed:
        lines.append(f"\n{len(failed)} 项失败——修完再启动。")
    elif warned:
        lines.append(f"\n{len(checks) - len(warned)} 项通过，{len(warned)} 项告警（不阻塞）。")
    else:
        lines.append(f"\n{len(checks)} 项全部通过。")
    return "\n".join(lines)


def main(settings: Settings) -> int:
    checks = run_checks(settings)
    print(render(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0
