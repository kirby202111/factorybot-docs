"""切分策略选择器。

按 doc_type 选策略：SOP 层级切 / MANUAL 按标题切 / STANDARD 按句切。
理想用 LlamaIndex NodeParser；此处给出可工作的轻量实现（车间网无 LlamaIndex 也能跑），
chunk 不可变：切分结果只追加，不回改。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.routes.document.domain.chunk import ChunkLocator, DocumentChunk
from app.routes.document.domain.document import DocType
from app.shared.events.version_contract import VersionAnchor


@dataclass
class ChunkingResult:
    """切分结果。"""

    chunks: list[DocumentChunk]


class ChunkStrategySelector:
    """按 doc_type 选切分策略。

    | doc_type | 策略 | chunk 大小 | 重叠 |
    |----------|------|-----------|------|
    | SOP | 层级（标题+步骤边界） | 256-512 token | 10% |
    | MANUAL | 按章节标题 | <=512 | 50 |
    | STANDARD | 按句 | 384 | 30 |
    """

    # 中文按字符近似 token；车间文档以中文为主。
    SIZE = {DocType.SOP: 400, DocType.MANUAL: 512, DocType.STANDARD: 384}
    OVERLAP = {DocType.SOP: 40, DocType.MANUAL: 50, DocType.STANDARD: 30}

    def select_size(self, doc_type: DocType) -> int:
        return self.SIZE.get(doc_type, 384)

    def split(
        self,
        *,
        text: str,
        doc_type: DocType,
        doc_id: str,
        version_id: str,
        tenant_scope: str,
        version_anchor: VersionAnchor | None,
        file_content_hash: str,
    ) -> list[DocumentChunk]:
        size = self.select_size(doc_type)
        overlap = self.OVERLAP.get(doc_type, 30)
        pieces = self._split_text(text, doc_type, size, overlap)
        chunks: list[DocumentChunk] = []
        vk = version_anchor.kind.value if version_anchor else ""
        vref = version_anchor.ref_id if version_anchor else ""
        ver = version_anchor.version if version_anchor else ""
        for seq, (piece, heading_path, section_type) in enumerate(pieces):
            chunks.append(
                DocumentChunk(
                    chunk_id=f"{doc_id}:{version_id}:{seq}",
                    version_id=version_id,
                    doc_id=doc_id,
                    chunk_seq=seq,
                    text=piece,
                    locator=ChunkLocator(heading_path=list(heading_path), offset=seq * (size - overlap)),
                    section_type=section_type,
                    version_kind=vk,
                    version_ref_id=vref,
                    version=ver,
                    tenant_scope=tenant_scope,
                    doc_type=doc_type.value,
                    file_content_hash=file_content_hash,
                )
            )
        return chunks

    def _split_text(
        self, text: str, doc_type: DocType, size: int, overlap: int
    ) -> list[tuple[str, list[str], str]]:
        """返回 [(chunk_text, heading_path, section_type), ...]。"""
        if doc_type == DocType.SOP:
            return self._split_sop(text, size, overlap)
        if doc_type == DocType.MANUAL:
            return self._split_heading(text, size, overlap)
        return self._split_sentence(text, size, overlap)

    def _split_sop(self, text: str, size: int, overlap: int) -> list[tuple[str, list[str], str]]:
        # SOP：按"步骤"切（形如 "1." "2." 或 "步骤1"），保留步骤边界。
        steps = re.split(r"(?=\n\s*(?:步骤\s*\d+|\d+[.、]))", text)
        out: list[tuple[str, list[str], str]] = []
        buf = ""
        for s in steps:
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) > size and buf:
                out.append((buf, [], "STEP"))
                buf = s
            else:
                buf = (buf + "\n" + s).strip()
        if buf:
            out.append((buf, [], "STEP"))
        return out or [(text[:size], [], "STEP")]

    def _split_heading(self, text: str, size: int, overlap: int) -> list[tuple[str, list[str], str]]:
        # MANUAL：按 markdown 标题切，故障代码章节完整保留。
        sections = re.split(r"(?=^#{1,6}\s)", text, flags=re.MULTILINE)
        out: list[tuple[str, list[str], str]] = []
        for sec in sections:
            sec = sec.strip()
            if not sec:
                continue
            heading_match = re.match(r"^(#{1,6})\s+(.*)$", sec)
            heading = heading_match.group(2) if heading_match else ""
            section_type = "FAULT_CODE" if re.search(r"故障|E\d{3}|E\d{4}", heading) else "NOTE"
            # 超长章节再按 size 切。
            for i in range(0, len(sec), size - overlap):
                out.append((sec[i : i + size], [heading] if heading else [], section_type))
                if i + size >= len(sec):
                    break
        return out or [(text[:size], [], "NOTE")]

    def _split_sentence(self, text: str, size: int, overlap: int) -> list[tuple[str, list[str], str]]:
        # STANDARD：按句切（。/.！/!？/?），参数表整体保留。
        sentences = re.split(r"(?<=[。.！!？?])", text)
        out: list[tuple[str, list[str], str]] = []
        buf = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) > size and buf:
                out.append((buf, [], "PARAM" if re.search(r"\d+\s*[±]?\s*\d", buf) else "NOTE"))
                buf = s
            else:
                buf = (buf + s).strip()
        if buf:
            out.append((buf, [], "PARAM" if re.search(r"\d+\s*[±]?\s*\d", buf) else "NOTE"))
        return out or [(text[:size], [], "NOTE")]
