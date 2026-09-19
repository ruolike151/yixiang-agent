"""工具注册表：Tool 契约 + 四条硬约束（TECH-DESIGN §9.1）。

硬约束写在 ``ToolRegistry.run`` 里，不靠工具自觉：

  1. 未知工具名 → ``Error: unknown tool '<name>'``（与参考实现一致）；
  2. 工具内异常不炸 loop → ``Error running <name>: <msg>``；
  3. 参数先按 ``input_schema`` 校验，错误必须**可行动**（说清缺什么、期望什么格式）；
  4. 返回值超 2000 字符截断并注明 ``(已截断，共 N 字)``（§5.5）。

所有错误文本都以 ``Error`` 开头——loop 靠这个前缀判定"这次工具失败了"，
并累计同一工具的连续失败次数（§5.1 的终止条件表）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yixiang.config import Settings
from yixiang.errors import E_TOOL_FAILED, SecurityError
from yixiang.runtime.models import TOOL_RESULT_LIMIT, Clock, SystemClock


@dataclass(slots=True)
class Tool:
    """一个工具=名字+描述（模型唯一的决策依据）+ JSON Schema+函数（§9.1）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]
    side_effect: bool = True
    timeout_s: float = 10.0
    # 参数里哪些字段是"相对 data/ 的路径"——执行前做逃逸校验（§9.4）
    path_args: tuple[str, ...] = ()

    def to_api(self) -> dict[str, Any]:
        """出站格式（OpenAI Chat Completions 的 ``tools`` 参数）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def signature(self) -> str:
        """``/tools`` 用的一行人话签名。"""
        required = self.input_schema.get("required") or []
        props = self.input_schema.get("properties") or {}
        args = ", ".join(
            name if name in required else f"{name}?" for name in props
        )
        mark = "写" if self.side_effect else "读"
        return f"{self.name}({args}) [{mark}] — {self.description.splitlines()[0]}"


@dataclass(slots=True)
class ToolOutcome:
    """一次工具执行的完整结果：给模型看的文本 + 给 trace 看的元数据。"""

    tool: str
    output: str
    ok: bool
    error_code: str | None = None
    ms: int = 0


@dataclass(slots=True)
class Deps:
    """工具的运行时依赖（组装根注入，工具模块自己不读全局状态）。"""

    conn: sqlite3.Connection | None = None
    clock: Clock = field(default_factory=SystemClock)
    data_dir: Path = field(default_factory=lambda: Path("data"))
    source: str = "cli"


def safe_path(root: Path | str, value: str) -> Path:
    """把参数里的相对路径解析到 ``root`` 内；越界一律拒绝（§9.4）。

    绝对路径与 ``..\\..\\`` 都会在这里被挡住——这是 §14.3 分层防御的第 1 层。
    """
    root_path = Path(root).resolve()
    raw = str(value or "").strip()
    if not raw:
        raise SecurityError("路径参数为空")
    candidate = Path(raw)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (root_path / candidate).resolve()
    )
    if resolved != root_path and root_path not in resolved.parents:
        raise SecurityError(f"路径越界：{raw!r} 不在 data/ 目录内")
    return resolved


def clamp_output(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    """工具结果长度上限（§5.5）：超长截断并注明总字数。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n(已截断，共 {len(text)} 字)"


def error_text(code: str, field_name: str, hint: str) -> str:
    """给模型看的结构化错误——必须可行动（§9.1 的错误文本规范）。"""
    payload = {"error": code, "field": field_name, "hint": hint}
    return "Error: " + json.dumps(payload, ensure_ascii=False)


class ToolRegistry:
    """工具容器 + 唯一的执行入口。"""

    def __init__(
        self, tools: list[Tool] | None = None, *, data_dir: Path | str = Path("data")
    ) -> None:
        self._tools: dict[str, Tool] = {}
        # 路径类参数的沙箱根目录（§9.4）：默认 data/，由组装根注入真实路径
        self.data_dir = Path(data_dir)
        for tool in tools or []:
            self.register(tool)

    # ------------------------------------------------------------------ 注册
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_api() for tool in self._tools.values()]

    def find(self, keyword: str) -> list[Tool]:
        """``/tools <关键词>`` 的模糊查找。"""
        keyword = keyword.strip().lower()
        if not keyword:
            return list(self._tools.values())
        return [
            tool
            for tool in self._tools.values()
            if keyword in tool.name.lower() or keyword in tool.description.lower()
        ]

    # ------------------------------------------------------------------ 执行
    def execute(self, name: str, args: dict[str, Any]) -> str:
        """冻结契约（PART-1 §4）：永远返回 ``str``，错误也返回字符串。"""
        return self.run(name, args).output

    def run(self, name: str, args: dict[str, Any] | None = None) -> ToolOutcome:
        args = dict(args or {})
        started = time.perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            return self._fail(name, f"Error: unknown tool '{name}'")

        problem = _validate(tool.input_schema, args)
        if problem:
            return self._fail(name, problem)

        try:
            for key in tool.path_args:
                if key in args and args[key] not in (None, ""):
                    args[key] = str(safe_path(self.data_dir, str(args[key])))
        except SecurityError as exc:
            return self._fail(name, f"Error: {exc}", code=E_TOOL_FAILED)

        try:
            output = tool.fn(**args)
        except Exception as exc:  # 工具异常不能炸掉 loop（§9.1）
            return self._fail(name, f"Error running {name}: {exc}", code=E_TOOL_FAILED)

        text = clamp_output(output if isinstance(output, str) else str(output))
        ok = not text.startswith("Error")
        return ToolOutcome(
            tool=name,
            output=text,
            ok=ok,
            error_code=None if ok else E_TOOL_FAILED,
            ms=_ms(started),
        )

    def _fail(self, name: str, output: str, *, code: str = E_TOOL_FAILED) -> ToolOutcome:
        return ToolOutcome(tool=name, output=output, ok=False, error_code=code)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """按 ``input_schema`` 检查必要字段与类型；返回可行动的错误文本。"""
    props: dict[str, Any] = schema.get("properties") or {}
    for name in schema.get("required") or []:
        value = args.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            hint = _hint(props.get(name), name)
            return error_text("missing_field", name, hint)
    for name, value in args.items():
        spec = props.get(name)
        if not spec or value is None:
            continue
        expected = spec.get("type")
        if expected and not _type_ok(value, expected):
            return error_text(
                "bad_type", name, f"需要 {expected} 类型，收到 {type(value).__name__}；{_hint(spec, name)}"
            )
        if spec.get("enum") and value not in spec["enum"]:
            allowed = " / ".join(str(item) for item in spec["enum"])
            return error_text("bad_value", name, f"只接受 {allowed}，收到 {value!r}")
    return None


def _hint(spec: dict[str, Any] | None, name: str) -> str:
    spec = spec or {}
    example = spec.get("example")
    if example:
        return f"需要 {spec.get('type', 'string')}，例如 {example}"
    return spec.get("description") or f"缺少必填参数 {name}"


def _type_ok(value: Any, expected: str) -> bool:
    match expected:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case _:
            return True


def build_registry(settings: Settings, deps: Deps) -> ToolRegistry:
    """把 P0 工具装进注册表（§9.2）。新工具在这里加一行——不改核心链路。"""
    from functools import partial

    from yixiang.tools import memo, plan

    conn = deps.conn
    now = deps.clock.now
    registry = ToolRegistry(data_dir=deps.data_dir)
    registry.register(
        Tool(
            name="add_memo",
            description=(
                "加一条带截止时间的待办/备忘（一次性的事）。"
                "用于：'记一下周五中午前交材料'、'提醒我明天打电话'。"
                "不要用于：长期偏好与身份事实（那是 save_memory，PART 2 交付）。"
                "返回：{\"id\":..., \"due_at\":...}；due_at 可传 ISO8601 或自然语言（如'周五中午'）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "备忘正文，一句话说清要做什么"},
                    "due_at": {
                        "type": "string",
                        "description": "截止时间，ISO8601（2026-09-25T12:00）或自然语言（周五中午）",
                        "example": "2026-09-25T12:00",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "幂等键：QQ/定时重投递时必须传，重复投递不会重复记事",
                    },
                },
                "required": ["content"],
            },
            fn=partial(memo.add_memo, conn, now),
        )
    )
    registry.register(
        Tool(
            name="list_memos",
            description=(
                "列出备忘。用户问'我还有什么没做/有什么备忘'时调用。"
                "返回按截止时间排序的文本列表；空列表返回'（暂无...）'。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "done"], "description": "默认 open"},
                    "due_before": {"type": "string", "description": "只看这个时间之前到期的，ISO8601"},
                },
                "required": [],
            },
            fn=partial(memo.list_memos, conn, now),
            side_effect=False,
        )
    )
    registry.register(
        Tool(
            name="finish_memo",
            description=(
                "把一条备忘标记为已完成。需要先用 list_memos 拿到 id。"
                "返回：{\"ok\": true}；id 不存在时返回 Error。"
            ),
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "备忘 id"}},
                "required": ["id"],
            },
            fn=partial(memo.finish_memo, conn, now),
        )
    )
    registry.register(
        Tool(
            name="create_plan",
            description=(
                "建一个学习/行动计划（多天任务的容器，先建计划再加任务）。"
                "用于：'帮我排一个两周的 RAG 复习计划'。返回：{\"plan_id\": ..., \"title\": ...}。"
                "建完之后**必须**用 add_task 把每天的任务加进去。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "计划标题"},
                    "goal": {"type": "string", "description": "这个计划想达成什么（可选）"},
                    "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                },
                "required": ["title"],
            },
            fn=partial(plan.create_plan, conn, now),
        )
    )
    registry.register(
        Tool(
            name="add_task",
            description=(
                "往计划里加一条某天的任务（依赖 create_plan 产出的 plan_id）。"
                "返回：{\"item_id\": ...}；plan_id 不存在时返回 Error。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "integer", "description": "create_plan 返回的 plan_id"},
                    "date": {"type": "string", "description": "任务日期 YYYY-MM-DD", "example": "2026-09-20"},
                    "content": {"type": "string", "description": "任务内容"},
                    "est_minutes": {"type": "integer", "description": "预计耗时（分钟）"},
                },
                "required": ["plan_id", "date", "content"],
            },
            fn=partial(plan.add_task, conn, now),
        )
    )
    registry.register(
        Tool(
            name="list_today",
            description=(
                "返回今天的学习任务与到期备忘。用户问'今天要干什么/今天有什么'时调用。"
                "严格按数据库返回，不要补充或推测未列出的内容。无参数。"
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            fn=partial(plan.list_today, conn, now),
            side_effect=False,
        )
    )
    registry.register(
        Tool(
            name="complete_task",
            description=(
                "把计划里的某条任务标记为完成或跳过。需要先用 list_today 拿到 item_id。"
                "返回：{\"ok\": true}；item_id 不存在时返回 Error。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "任务 id"},
                    "status": {
                        "type": "string",
                        "enum": ["done", "skipped"],
                        "description": "默认 done",
                    },
                },
                "required": ["item_id"],
            },
            fn=partial(plan.complete_task, conn, now),
        )
    )
    return registry
