"""确定性用例的公共夹具（PART-1 §7）。

三条纪律——用例只断言三件事，顺序不要混：

  ① 请求侧（模型看到了什么）
  ② 行为侧（该调的调了、不该调的没调）
  ③ 结果侧（DB 与文件的最终状态）

这里的一切都**离线、零成本、可重复**：假时钟 + 内存库 + 假 Provider。
用例里一次 ``sleep`` 都不许真发生、一次真模型都不许连（§13.6）。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from yixiang import db
from yixiang.config import Settings
from yixiang.loop.agent import run_loop
from yixiang.runtime.models import FixedClock, Observer
from yixiang.runtime.session import SessionManager
from yixiang.tools.registry import Deps, ToolRegistry, build_registry

# 让 ``from fake_provider import ...`` 在任何 import 模式下都能找到同目录的模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 固定时钟：2026-09-19（周六）10:00 +08:00。D-01 的「周五中午」由它定盘。
TZ_CN = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_CN)

# 一轮的 turn_id 也固定：trace / chat_log 的断言才对得上
TURN_ID = "t_20260919_100000_test"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根：``templates/`` 在这里，S1~S4 的分段内容从它回落读取。"""
    return Path(__file__).resolve().parents[2]


def make_settings(tmp_path: Path, repo_root: Path, **overrides: Any) -> Settings:
    """一份完全确定的配置：不读 ``.env``、不读 ``os.environ``、数据落 tmp_path。"""
    values: dict[str, Any] = {
        "api_key": "test-key-not-real",
        "data_dir": tmp_path / "data",
        "history_turns": 10,
        "loop_max_iter": 6,
        "tool_retry_max": 2,
    }
    values.update(overrides)
    return Settings.load(env_file=None, environ={}, project_root=repo_root, **values)


@pytest.fixture
def settings(tmp_path: Path, repo_root: Path) -> Settings:
    return make_settings(tmp_path, repo_root)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def conn(settings: Settings):
    """内存库：跑一次迁移，用完全业务表（D-26 另有空库用例）。"""
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def deps(settings: Settings, conn, clock: FixedClock) -> Deps:
    return Deps(conn=conn, clock=clock, data_dir=settings.data_dir, source="cli")


@pytest.fixture
def registry(settings: Settings, deps: Deps) -> ToolRegistry:
    return build_registry(settings, deps)


@pytest.fixture
def session(settings: Settings, conn, clock: FixedClock) -> SessionManager:
    return SessionManager(settings, store=conn, session_id="cli:test", clock=clock)


def run_turn(
    session: SessionManager,
    registry: ToolRegistry,
    provider: Any,
    user_text: str,
    *,
    stream: bool = False,
    observer: Observer | None = None,
    tools: bool = True,
    max_iter: int | None = None,
):
    """跑完一轮：起事件循环调 ``run_loop``（默认非流式，要事件就把 observer 传进来）。"""
    session.begin_turn(user_text, turn_id=TURN_ID)
    return asyncio.run(
        run_loop(
            session,
            registry,
            provider,
            observer,
            stream=stream,
            tools=tools,
            max_iter=max_iter,
        )
    )


@pytest.fixture
def turn():
    """把 ``run_turn`` 作为夹具暴露，省掉各用例重复 import。"""
    return run_turn
