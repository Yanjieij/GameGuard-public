"""文档读取工具 —— DesignDocAgent 的"眼睛"。

为什么要把文档读取做成多个细粒度 tool？

最笨的做法是"一次性把整份文档塞进 system prompt"。这样在 D1 的 v1 文档
（~300 行）还行，但有两个痛点：

  1) 上下文浪费：每轮 LLM 调用都把全文喂一遍，token 成本线性增长。
     真实游戏项目的技能规范可能几千行，根本塞不下。
  2) 错位归因：LLM 犯错时，我们想说"它是在看哪一段"时无从追溯。
     tool 化后，它必须显式 `read_doc_section(heading)`，trace 里就有清单。

因此最佳实践是给 LLM 一组"浏览器"式的工具：

    list_doc_sections   → 先看目录
    read_doc_section    → 按需打开某一节
    (可选) search_doc   → D5 再加；先不做

LangChain 的 `DirectoryLoader`、RAG 工具链的 "chunk + retrieve" 都是这套
思路的不同变体。我们手写是为了让 LLM 的每一次文档访问都进 trace，
方便面试时讲故事。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from gameguard.tools.schemas import Tool

# --------------------------------------------------------------------------- #
# 文档仓库 —— 维护"当前会话可见的文档池"
# --------------------------------------------------------------------------- #

@dataclass
class DocRepository:
    """一个轻量的文档池。

    在 DesignDocAgent 启动时把可用文档注册进来；tool 函数只操作这个池，
    不直接碰文件系统。这也是沙箱化的一种：LLM 不能通过 `read_file("/etc/passwd")`
    读任意路径。
    """

    docs: dict[str, str] = field(default_factory=dict)     # name -> full text

    def register_file(self, path: str | Path, *, name: str | None = None) -> str:
        """注册一个磁盘文件，返回 name。

        name 默认取文件名（不含扩展名）；若重名则附加 ``_2`` / ``_3``。
        """
        p = Path(path)
        default_name = name or p.stem
        unique = default_name
        idx = 2
        while unique in self.docs:
            unique = f"{default_name}_{idx}"
            idx += 1
        self.docs[unique] = p.read_text(encoding="utf-8")
        return unique

    def get(self, name: str) -> str:
        return self.docs[name]

    def names(self) -> list[str]:
        return sorted(self.docs.keys())

# --------------------------------------------------------------------------- #
# 工具的输入 Schema（即 LLM 可填的参数）
# --------------------------------------------------------------------------- #

class ListDocsInput(BaseModel):
    """``list_docs`` 无参数；但 Pydantic 必须要一个模型，所以留个空模型。"""

class ListSectionsInput(BaseModel):
    doc: str = Field(..., description="要查看目录的文档名（见 list_docs 返回）")

class ReadSectionInput(BaseModel):
    doc: str = Field(..., description="文档名")
    heading: str = Field(
        ...,
        description=(
            "目标章节的标题文字（不含 Markdown 的 '#' 号）。"
            "必须与 list_doc_sections 返回的 heading 字段完全一致。"
        ),
    )
    # 允许 LLM 把"包含上一节的上下文"以选项形式要回
    include_subsections: bool = Field(
        default=True,
        description="是否连同该节之下的所有子节一并返回。默认 True。",
    )

class ReadFullDocInput(BaseModel):
    doc: str = Field(..., description="文档名")

# --------------------------------------------------------------------------- #
# 工具输出 Schema
# --------------------------------------------------------------------------- #

class DocListing(BaseModel):
    docs: list[str]

class SectionOutline(BaseModel):
    heading: str
    level: int           # 1 = '#', 2 = '##' ...
    line: int            # 该 heading 在原文中的行号（1-based）

class DocOutline(BaseModel):
    doc: str
    sections: list[SectionOutline]

class DocSection(BaseModel):
    doc: str
    heading: str
    level: int
    content: str
    start_line: int
    end_line: int

class DocFullContent(BaseModel):
    doc: str
    total_lines: int
    content: str

# --------------------------------------------------------------------------- #
# 工厂：从 DocRepository 生产 Tool 列表
# --------------------------------------------------------------------------- #

def build_doc_tools(repo: DocRepository) -> list[Tool]:
    """把 DocRepository 封装成一组 Tool，直接注册进 ToolRegistry。"""

    # ---- list_docs ------------------------------------------------------
    def _list_docs(_: ListDocsInput) -> DocListing:
        return DocListing(docs=repo.names())

    list_docs = Tool(
        name="list_docs",
        description=(
            "列出当前会话中可见的所有策划文档名。通常作为你的第一步调用，"
            "让你知道有哪些资料可读。"
        ),
        input_model=ListDocsInput,
        fn=_list_docs,
    )

    # ---- list_doc_sections ---------------------------------------------
    def _list_sections(args: ListSectionsInput) -> DocOutline:
        text = _must_get(repo, args.doc)
        sections = _extract_outline(text)
        return DocOutline(doc=args.doc, sections=sections)

    list_sections = Tool(
        name="list_doc_sections",
        description=(
            "返回指定文档的章节目录（所有 Markdown 标题）。用于在读具体内容"
            "之前建立全局印象，避免把全文塞进上下文。"
        ),
        input_model=ListSectionsInput,
        fn=_list_sections,
    )

    # ---- read_doc_section ----------------------------------------------
    def _read_section(args: ReadSectionInput) -> DocSection:
        text = _must_get(repo, args.doc)
        sec = _read_one_section(text, args.heading, include_subsections=args.include_subsections)
        if sec is None:
            # 抛异常 -> ToolRegistry 会转成结构化错误反馈给 LLM
            raise ValueError(
                f"文档 {args.doc!r} 中找不到标题为 {args.heading!r} 的章节。"
                f"请先用 list_doc_sections 查看准确的 heading。"
            )
        return DocSection(doc=args.doc, **sec)

    read_section = Tool(
        name="read_doc_section",
        description=(
            "读取指定章节的正文（Markdown）。heading 必须与 list_doc_sections "
            "返回值中的 heading 字段完全一致（大小写、符号均敏感）。"
        ),
        input_model=ReadSectionInput,
        fn=_read_section,
    )

    # ---- read_full_doc --------------------------------------------------
    def _read_full(args: ReadFullDocInput) -> DocFullContent:
        text = _must_get(repo, args.doc)
        return DocFullContent(
            doc=args.doc, total_lines=text.count("\n") + 1, content=text
        )

    read_full = Tool(
        name="read_full_doc",
        description=(
            "一次性读取整篇文档。只在文档很短（<200 行）或你已经认定必须"
            "通读时使用，否则优先用 list_doc_sections + read_doc_section。"
        ),
        input_model=ReadFullDocInput,
        fn=_read_full,
    )

    return [list_docs, list_sections, read_section, read_full]

# --------------------------------------------------------------------------- #
# 解析工具（模块级纯函数，便于单测）
# --------------------------------------------------------------------------- #

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

def _extract_outline(text: str) -> list[SectionOutline]:
    """把 Markdown 文档的所有 '# ... ###### ...' 抽成 outline。"""
    out: list[SectionOutline] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _HEADING_RE.match(line)
        if m:
            out.append(
                SectionOutline(
                    heading=m.group(2).strip(),
                    level=len(m.group(1)),
                    line=lineno,
                )
            )
    return out

def _read_one_section(
    text: str,
    heading: str,
    *,
    include_subsections: bool,
) -> dict | None:
    """按 heading 文本找到某一节并返回其内容。

    "包含子节" 的判定：从目标行开始，遇到一个 同级或更浅的
    标题就截断。这样 ``## 状态机`` 返回的内容自然把 ``### 6.1`` /
    ``### 6.2`` 包进去。
    """
    lines = text.splitlines()
    start_idx = None
    start_level = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(2).strip() == heading:
            start_idx = i
            start_level = len(m.group(1))
            break
    if start_idx is None:
        return None

    # 决定终点
    end_idx = len(lines)
    scan_from = start_idx + 1
    for j in range(scan_from, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if not m:
            continue
        lvl = len(m.group(1))
        if include_subsections:
            # 只在 "同级或更浅" 的 heading 截断
            if lvl <= start_level:
                end_idx = j
                break
        else:
            # 任何 heading 都截断（含子节）
            end_idx = j
            break

    content = "\n".join(lines[start_idx:end_idx]).rstrip()
    return {
        "heading": heading,
        "level": start_level,
        "content": content,
        "start_line": start_idx + 1,
        "end_line": end_idx,
    }

def _must_get(repo: DocRepository, name: str) -> str:
    if name not in repo.docs:
        raise ValueError(f"未注册的文档：{name!r}。可选：{repo.names()}")
    return repo.docs[name]
