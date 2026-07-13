"""B 文档解析器（摄入期半结构化/非结构化解析）。

unstructured + pypdf + python-docx；未安装时降级为纯文本提取。
"""
from __future__ import annotations

import io
import logging
from typing import Any

from app.routes.document.domain.document import DocType

logger = logging.getLogger(__name__)


class DocumentParser:
    """文档解析器。按 doc_type 与文件扩展名选解析器。"""

    async def parse(self, content: bytes, doc_type: DocType) -> str:
        # 优先 unstructured；不可用则按扩展名走 pypdf / python-docx；再不行纯文本兜底。
        try:
            return await self._parse_unstructured(content)
        except Exception:
            pass
        try:
            return await self._parse_simple(content)
        except Exception as exc:
            logger.warning("文档解析全部失败，返回空串: %s", exc)
            return ""

    async def _parse_unstructured(self, content: bytes) -> str:
        from unstructured.partition.auto import partition  # type: ignore

        elements = partition(file=io.BytesIO(content))
        return "\n".join(str(e) for e in elements)

    async def _parse_simple(self, content: bytes) -> str:
        # PDF
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass
        # DOCX
        try:
            from docx import Document  # type: ignore

            doc = Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            pass
        # 纯文本兜底
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:
            return ""
