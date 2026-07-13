"""BM25 词项化（中文优先 jieba，缺失降级正则）。

车间文档为中文 SOP/工艺手册，BM25 对中文必须分词（字符级噪声过大）。
``jieba`` 不可用时降级为正则（英数字词 + 单汉字），保证可运行但召回质量下降。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 小停用表（高频虚词，对 BM25 区分度无贡献）
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "和", "与", "或", "是", "在", "为", "对", "由", "及",
        "等", "都", "也", "并", "以", "于", "其", "该", "此", "按",
        "the", "a", "an", "of", "and", "or", "to", "in", "for", "on",
        "is", "are", "be", "with", "as", "by", "at",
    }
)

# 降级正则：连续字母/数字/下划线 或 单个 CJK 字符聚合
# （jieba 缺失时按字符切，保留可运行性；中文召回质量低于 jieba）
_FALLBACK_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


class Tokenizer:
    """BM25 词项化器。provider 无关：jieba 优先，降级正则。

    构造期一次性探测 jieba 是否可用并缓存结果，``tokenize`` 热路径无分支外的 import 开销。
    """

    def __init__(self) -> None:
        self._jieba: Any = None
        try:
            import jieba  # type: ignore[import-untyped]

            # 触发一次切分以预热 jieba 内部缓存（惰性构造，首次调用很慢）
            jieba.cut_for_search("预热")
            self._jieba = jieba
            logger.info("BM25 分词器：jieba 就绪")
        except Exception as exc:  # ImportError 或初始化异常均降级
            logger.warning("jieba 不可用，BM25 降级为正则分词: %s", exc)
            self._jieba = None

    @property
    def uses_jieba(self) -> bool:
        return self._jieba is not None

    def tokenize(self, text: str) -> list[str]:
        """切分并去停用词、去空、去纯标点。返回小写词项列表。"""
        if not text:
            return []
        if self._jieba is not None:
            raw = self._jieba.cut_for_search(text)
        else:
            raw = _FALLBACK_TOKEN_RE.findall(text)
        tokens: list[str] = []
        for tok in raw:
            t = tok.strip().lower()
            if not t or t in _STOPWORDS:
                continue
            tokens.append(t)
        return tokens
