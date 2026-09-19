"""程序性记忆：``skills/<slug>/SKILL.md`` 的加载、匹配与安装（TECH §7.8）。

程序性记忆回答的是"这类话该怎么干"——它跟事实（facts）和情节（episodes）的区别
是：**写一次用很多次**，而且内容是步骤，不是陈述。

三条设计取舍：

  * 匹配只用**显式 ``triggers`` 列表**做子串匹配，不拿 ``description`` 当触发词——
    描述是写给人看的，当触发词用会悄悄扩大命中面（参考实现就是那样，本项目不跟）；
  * 加载器按 ``(路径, mtime, size)`` 做签名，**签名变了才重读**：REPL 一次进程
    会调很多次 ``match()``，每次重读磁盘是没必要 I/O；
  * 手写 YAML 一定会写错，所以解析必须**容错**：坏文件不让整轮崩，只记 warning
    并降级成"可用字段尽量用"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from yixiang.memory.core_files import SKILL_BODY_MAX, atomic_write_text

SKILL_FILE = "SKILL.md"
SLUG_RE = re.compile(r"^[a-z0-9-]{3,40}$")
MATCH_LIMIT = 2
_FENCE = "---"


@dataclass(slots=True)
class Skill:
    """一个已加载的 skill：匹配靠 ``triggers``，注入靠 ``body``。"""

    name: str
    description: str
    body: str
    path: Path
    triggers: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.path.parent.name

    def hits(self, message: str) -> int:
        """命中几个触发词（不区分大小写）。"""
        lowered = message.lower()
        return sum(1 for trigger in self.triggers if trigger and trigger.lower() in lowered)

    def render(self, *, body_limit: int = SKILL_BODY_MAX) -> str:
        """S5 里的一段：带 ``### <name>`` 标题，body 超长截断。"""
        body = self.body.strip()
        if len(body) > body_limit:
            body = body[:body_limit].rstrip() + "\n（技能内容过长，已截断）"
        head = f"### {self.name}"
        header = f"{head}\n{self.description.strip()}" if self.description.strip() else head
        return f"{header}\n{body}".strip()


@dataclass(slots=True)
class SkillLoader:
    """从若干目录加载 skills；目录树没变就直接用手里的缓存。"""

    dirs: list[Path]
    skills: list[Skill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    signature: tuple = ()

    def __post_init__(self) -> None:
        self.dirs = [Path(item) for item in self.dirs]
        self.refresh()

    # ------------------------------------------------------------------ 加载
    def _scan_signature(self) -> tuple:
        stamp: list[tuple[str, float, int]] = []
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(f"*/{SKILL_FILE}")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp.append((str(path), stat.st_mtime, stat.st_size))
        return tuple(stamp)

    def refresh(self) -> list[Skill]:
        """重读全部 skill 文件；坏文件只记 warning，不抛异常。"""
        self.signature = self._scan_signature()
        self.skills = []
        self.warnings = []
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(f"*/{SKILL_FILE}")):
                if not SLUG_RE.match(path.parent.name):
                    self.warnings.append(f"{path.parent.name}: 目录名不符合 {SLUG_RE.pattern}")
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    self.warnings.append(f"{path}: 读取失败（{exc}）")
                    continue
                skill, problems = parse_skill(text, path=path)
                self.warnings.extend(f"{path}: {item}" for item in problems)
                if skill is not None:
                    self.skills.append(skill)
        self.skills.sort(key=lambda item: (item.slug, item.name))
        return list(self.skills)

    def all(self) -> list[Skill]:
        """取全部 skills；目录树变了（或新增了目录）就自动重读。"""
        if self._scan_signature() != self.signature:
            self.refresh()
        return list(self.skills)

    # ------------------------------------------------------------------ 匹配
    def match(self, message: str, max_skills: int = MATCH_LIMIT) -> list[Skill]:
        """按命中触发词数降序取前 N 个；同分按 slug 稳定排序。"""
        scored = [(skill.hits(message), skill) for skill in self.all()]
        hit = [(count, skill) for count, skill in scored if count > 0]
        hit.sort(key=lambda item: (-item[0], item[1].slug))
        return [skill for _, skill in hit[: max(0, max_skills)]]


def load(text: str) -> tuple[str, list[str], str]:
    """拆前置块 ``---``：返回 ``(frontmatter, body, problems)``。"""
    problems: list[str] = []
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith(_FENCE):
        return "", stripped, ["缺少 YAML frontmatter（文件必须以 --- 开头）"]
    lines = stripped.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            end = index
            break
    if end is None:
        return "", stripped, ["frontmatter 没有结束的 ---"]
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :]).strip(), problems


def parse_frontmatter(block: str) -> tuple[dict[str, str], list[str]]:
    """极简 frontmatter 解析：``key: value``（列表支持 ``[a, b]`` 与逐行 ``- a``）。"""
    values: dict[str, str] = {}
    problems: list[str] = []
    current: str | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current:
            values[current] = f"{values[current]}, {line.lstrip()[2:].strip()}"
            continue
        key, sep, value = line.partition(":")
        if not sep:
            problems.append(f"无法解析的行：{line.strip()}")
            continue
        current = key.strip()
        values[current] = _unquote(value.strip())
    return values, problems


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def parse_triggers(raw: str) -> list[str]:
    """``[周报, 本周总结]`` / ``周报, 本周总结`` / ``- 周报`` 都接受。"""
    text = _unquote(raw.strip())
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items = [_unquote(part.strip()) for part in re.split(r"[,，]", text)]
    return [item for item in items if item]


def parse_skill(text: str, *, path: Path) -> tuple[Skill | None, list[str]]:
    """把一份 SKILL.md 解析成 ``Skill``；缺 name/body 时返回 ``None`` + 问题清单。"""
    block, body, problems = load(text)
    values, more = parse_frontmatter(block)
    problems.extend(more)
    name = values.get("name", "").strip() or path.parent.name
    description = values.get("description", "").strip()
    triggers = parse_triggers(values.get("triggers", ""))
    if not description:
        problems.append("frontmatter 缺 description")
    if not triggers:
        problems.append("frontmatter 缺 triggers（没有触发词就永远匹配不上）")
    if not body:
        problems.append("正文为空")
    if len(body) > SKILL_BODY_MAX:
        problems.append(f"正文 {len(body)} 字，超过 {SKILL_BODY_MAX} 字会被截断")
    skill = Skill(
        name=name, description=description, body=body, path=path, triggers=triggers
    )
    return skill, problems


def validate(text: str) -> list[str]:
    """``yixiang skills validate`` 用的检查；返回问题清单（空 = 通过）。"""
    _skill, problems = parse_skill(text, path=Path("skills") / "<slug>" / SKILL_FILE)
    return problems


def validate_dirs(dirs: list[Path]) -> list[str]:
    """校验若干目录下的全部 skill；返回 ``路径: 问题`` 清单。"""
    problems: list[str] = []
    found = False
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(f"*/{SKILL_FILE}")):
            found = True
            if not SLUG_RE.match(path.parent.name):
                problems.append(f"{path}: 目录名必须匹配 {SLUG_RE.pattern}")
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                problems.append(f"{path}: 读取失败（{exc}）")
                continue
            problems.extend(f"{path}: {item}" for item in validate(text))
    if not found:
        return problems
    return problems


def render_block(skills: list[Skill]) -> str:
    """拼 S5（≤1500 字）：没有命中就返回空串。"""
    if not skills:
        return ""
    parts = [skill.render() for skill in skills]
    text = "以下技能与当前请求相关，按步骤执行：\n" + "\n\n".join(parts)
    limit = 1500
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n（技能注入已达上限）"
    return text


def install(
    slug: str,
    name: str,
    description: str,
    triggers: list[str] | str,
    body: str,
    *,
    confirm: bool = True,
    directory: Path | str,
) -> dict[str, object]:
    """写入 ``<directory>/<slug>/SKILL.md``。

    返回 ``{"ok": bool, ...}``；失败时 ``error`` 是**给模型看的**可行动文本。
    """
    slug = (slug or "").strip()
    if not SLUG_RE.match(slug):
        return {"ok": False, "error": f"slug 必须匹配 {SLUG_RE.pattern}，收到 {slug!r}"}
    if not confirm:
        return {"ok": False, "error": "创建技能需要用户明确同意（confirm=true）"}
    items = parse_triggers(triggers) if isinstance(triggers, str) else list(triggers)
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items:
        return {"ok": False, "error": "triggers 不能为空：没有触发词就永远匹配不上"}

    warnings: list[str] = []
    text_body = (body or "").strip()
    if len(text_body) > SKILL_BODY_MAX:
        warnings.append(f"body {len(text_body)} 字超过 {SKILL_BODY_MAX} 字，已截断")
        text_body = text_body[:SKILL_BODY_MAX]

    path = Path(directory) / slug / SKILL_FILE
    if path.exists():
        return {"ok": False, "error": f"{path} 已存在，本项目不覆盖已有技能"}
    front = [
        _FENCE,
        f"name: {(name or slug).strip()}",
        f"description: {description.strip()}",
        "triggers: [" + ", ".join(items) + "]",
        _FENCE,
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "\n".join(front) + text_body + "\n")
    return {"ok": True, "path": str(path), "warnings": warnings}


__all__ = [
    "MATCH_LIMIT",
    "SKILL_BODY_MAX",
    "SKILL_FILE",
    "SLUG_RE",
    "Skill",
    "SkillLoader",
    "install",
    "load",
    "parse_frontmatter",
    "parse_skill",
    "parse_triggers",
    "render_block",
    "validate",
    "validate_dirs",
]
