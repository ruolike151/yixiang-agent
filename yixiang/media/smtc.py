"""Windows SMTC（全局"正在播放"）读写。

系统级"正在播放"就是 Windows 的 SMTC（System Media Transport Controls）：QQ 音乐 /
网易云 / 浏览器 / PotPlayer 在放歌时都会注册一个会话，管理器给出"当前那一个"。
读它就知道"现在在放什么、放到哪了、这个 app 支不支持切歌"。

两条纪律：

  * **winrt 是可选依赖**：没装 / 不是 Windows / 桌面会话里服务缺失，都只是"读不到"，
    绝不让整条 Web 链路 500。调用方（``MusicService``）把"读不到"当作 offline。
  * **异步必须走 ``eventloop.run``**：winrt 的 ``IAsyncOperation`` 只能在一条 loop 上
    await，项目统一用 ``yixiang.runtime.eventloop`` 的那条常驻 loop（``asyncio.run``
    每轮换一条 loop 的教训，见那个模块的注释）。
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yixiang.media.lyrics import Track
from yixiang.runtime import eventloop
from yixiang.runtime.msvc_runtime import unify_msvc_runtime

# 包 import 时已经对齐过一次（yixiang/__init__.py），这里再点一次是**故意的**：
# 风险就在下面这三行——winrt 自带 14.29 的旧 MSVCP140，抢先加载会把整个进程的
# 运行库钉死，后加载的 onnxruntime 随后 0xC0000005。把不变量写在出事的地方。
unify_msvc_runtime()

try:  # pragma: no cover - 没装 winrt 的机器只会走 except
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SessionManager,
    )
except Exception:  # noqa: BLE001 - ImportError / DLL 加载失败都算"这台机器没有 SMTC"
    _SessionManager = None


class SmtcUnavailable(RuntimeError):
    """SMTC 用不了：系统报错（服务未安装 / 权限）时抛这个，与"没有会话"分开。

    ``read_current()`` 返回 ``None`` = "现在没在放"；抛这个 = "这台机器读不了"。
    """


# winrt 的 .pyi 里没有 enum 成员定义，状态码自己写死（本机实测：4 = 播放）。
# 0/1/2 = CLOSED / OPENED / CHANGING，对界面都是"不知道在不在放"。
STATUS_NAMES = {
    0: "unknown",
    1: "unknown",
    2: "unknown",
    3: "stopped",
    4: "playing",
    5: "paused",
}

# 动作表：action → (winrt 里"能不能按"的布尔属性名, 要调的 Session 方法名)
CONTROL_ACTIONS = (
    ("play", "is_play_enabled", "try_play_async"),
    ("pause", "is_pause_enabled", "try_pause_async"),
    ("next", "is_next_enabled", "try_skip_next_async"),
    ("previous", "is_previous_enabled", "try_skip_previous_async"),
    ("seek", "is_playback_position_enabled", "try_change_playback_position_async"),
)

# 界面上要显示的按钮：名字 → winrt 里的布尔属性（保持 Task 3 的形状，只是多一项 seek）
CONTROL_FLAGS = tuple((name, flag) for name, flag, _ in CONTROL_ACTIONS)

# 定位参数的单位是 ticks（100 ns）。微软文档原文：TryChangePlaybackPositionAsync
# "Attempts to change the playback position on the session to the specified time, in ticks."
TICKS_PER_MS = 10_000


@dataclass(frozen=True)
class SmtcSnapshot:
    """某一刻的"正在播放"。字段与 midori 的状态 JSON 一一对应（见 ``service.py``）。"""

    track: Track
    status: str
    position_ms: int
    duration_ms: int
    app_id: str
    controls: tuple[str, ...]


async def _await_value(value: Any) -> Any:
    return await value


def _await(value: Any) -> Any:
    """winrt 的 ``IAsyncOperation`` 是 awaitable；普通值原样返回（假 session 用得上）。"""

    if not inspect.isawaitable(value):
        return value
    return eventloop.run(_await_value(value))


def ms_of(value: Any) -> int:
    """``datetime.timedelta`` → 毫秒；None / 认不出的类型 → 0；负数夹到 0。"""

    if isinstance(value, dt.timedelta):
        millis = int(round(value.total_seconds() * 1000))
    elif isinstance(value, (int, float)):
        millis = int(value)
    else:
        millis = 0
    return max(0, millis)


def default_session_factory() -> Any:
    """问 Windows 要"当前那个会话"；没有就是 ``None``。

    winrt 没装 → ``None``（静态地没有这个能力）。装了但系统报错（沙箱里就是
    ``WinError -2147023836``）→ 抛 ``SmtcUnavailable``：那是个意外，值得说出来。
    """

    if _SessionManager is None:
        return None
    try:
        manager = _await(_SessionManager.request_async())
    except Exception as exc:  # noqa: BLE001 - winrt 的异常类型随版本变，一律包成自己的
        raise SmtcUnavailable(f"SMTC 管理器不可用：{exc}") from exc
    if manager is None:
        return None
    return manager.get_current_session()


def _status_name(raw: Any) -> str:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        number = -1
    return STATUS_NAMES.get(number, "unknown")


def controls_of(info: Any) -> tuple[str, ...]:
    """这个会话支持哪些按钮（界面据此灰掉 / 点亮）。"""

    controls = getattr(info, "controls", None)
    return tuple(name for name, attr in CONTROL_FLAGS if getattr(controls, attr, False))


def _track_id(app_id: str, title: str, artist: str) -> str:
    """SMTC 不给曲目 id，就自己拼一个稳定的：同一个 app + 同一首歌 → 同一个 id。

    前端拿它算"这首歌用哪个渲染模式"的哈希，所以它必须在**同一首歌**上稳定。
    """

    return "smtc:" + "|".join(part for part in (app_id, title, artist) if part)


def read_current(
    session_factory: Callable[[], Any] | None = None,
) -> SmtcSnapshot | None:
    """当前会话的快照；没有在放的会话时 ``None``。

    ``session_factory`` 是注入口：用例给它假 session，真机走 ``default_session_factory``。
    """

    session = (session_factory or default_session_factory)()
    if session is None:
        return None

    info = session.get_playback_info()
    timeline = session.get_timeline_properties()
    media = _await(session.try_get_media_properties_async())

    start_ms = ms_of(getattr(timeline, "start_time", None))
    position_ms = max(0, ms_of(getattr(timeline, "position", None)) - start_ms)
    duration_ms = max(0, ms_of(getattr(timeline, "end_time", None)) - start_ms)

    title = str(getattr(media, "title", "") or "")
    artist = str(getattr(media, "artist", "") or "")
    album = str(getattr(media, "album_title", "") or "")
    app_id = str(getattr(session, "source_app_user_model_id", "") or "")

    return SmtcSnapshot(
        track=Track(
            id=_track_id(app_id, title, artist),
            title=title,
            artist=artist,
            album=album,
            duration_ms=duration_ms,
        ),
        status=_status_name(getattr(info, "playback_status", None)),
        position_ms=position_ms,
        duration_ms=duration_ms,
        app_id=app_id,
        controls=controls_of(info),
    )


def control(
    action: str,
    *,
    position_ms: int = 0,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """替用户按一下播放器的按钮。``True`` = 按下去了并且播放器接受了。

    ``action`` 只能是 play / pause / next / previous / seek。
    返回 ``False`` 有三种情况，对界面都是同一件事（这个按钮现在别点）：
    没有会话、当前 app 没开这个按钮、winrt 按下了但回 ``False``（正在缓冲 / 设备被独占）。

    ``position_ms`` 是对外的**毫秒**口径；winrt 要的是 ticks，所以在这里乘 ``TICKS_PER_MS``。
    单位写错不会报错，只会让进度条跳到一个千分之一都不到的位置——改这行前先看真机进度条。

    系统级不可用（装了 winrt 但服务报错）照 Task 3 的口径抛 ``SmtcUnavailable``；
    没装 winrt 则静默返 ``False``。
    """

    table = {name: (flag, method) for name, flag, method in CONTROL_ACTIONS}
    if action not in table:
        raise ValueError(f"不认识的控制动作：{action}")

    session = (session_factory or default_session_factory)()
    if session is None:
        return False

    flag, method_name = table[action]
    if not getattr(getattr(session.get_playback_info(), "controls", None), flag, False):
        return False

    method = getattr(session, method_name)
    if action == "seek":
        ticks = max(0, int(position_ms)) * TICKS_PER_MS
        return bool(_await(method(ticks)))
    return bool(_await(method()))
