"""入库的载体（Task 24 从单文件拆出）：``MediaItem``（归一后的一条）与
``IngestReport``（一次抓取的战报，``summary`` 供日志与 dry-run 打印）。
这一层不 import 任何同包模块。"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class MediaItem:
    """入库前的一条规范记录（抓取结果与本地文件共用这一种载体）。"""

    source_id: str
    title: str
    title_zh: str = ""
    mtype: str = ""
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    synopsis: str = ""
    cover_url: str = ""


@dataclass(slots=True)
class IngestReport:
    """一次入库的结果账：写进 stdout 的那几行就是它的 ``summary()``。"""

    source: str = ""
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    embedded: int = 0
    vec: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.skipped + self.failed

    def summary(self) -> str:
        parts = [
            f"{self.source or 'ingest'}：共 {self.total} 条",
            f"新增 {self.inserted}",
            f"更新 {self.updated}",
            f"跳过 {self.skipped}",
        ]
        if self.failed:
            parts.append(f"失败 {self.failed}")
        parts.append(f"向量 {self.vec}")
        if self.dry_run:
            parts.append("（--dry-run：未写库）")
        text = " · ".join(parts)
        if self.errors:
            text += "\n" + "\n".join(f"  · {item}" for item in self.errors[:5])
        return text
