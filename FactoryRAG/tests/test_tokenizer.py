"""BM25 分词器：jieba 优先，缺失降级正则；停用词过滤；空串处理。

用强制 ``_jieba=None`` 验证降级正则路径（不依赖 jieba 是否安装），保证可运行性。
"""
from __future__ import annotations

from app.routes.document.infrastructure.bm25.tokenizer import Tokenizer


def test_empty_string_returns_empty():
    tok = Tokenizer()
    assert tok.tokenize("") == []
    assert tok.tokenize("   ") == []


def test_stopwords_filtered():
    tok = Tokenizer()
    toks = tok.tokenize("the reflow of and temperature")
    assert "the" not in toks
    assert "of" not in toks
    assert "and" not in toks
    assert "reflow" in toks
    assert "temperature" in toks


def test_english_lowercased():
    tok = Tokenizer()
    toks = tok.tokenize("Reflow Oven SMT")
    assert "reflow" in toks
    assert "oven" in toks
    assert "smt" in toks
    assert "REFLOW" not in toks


def test_fallback_regex_path():
    """强制降级正则：中文字符按字切，英文按词切。"""
    tok = Tokenizer()
    tok._jieba = None  # type: ignore[attr-defined]
    assert tok.uses_jieba is False

    toks = tok.tokenize("SMT 回流焊")
    # 英文词整体保留，中文按单字
    assert "smt" in toks
    assert "回" in toks and "流" in toks and "焊" in toks


def test_fallback_skips_punctuation():
    tok = Tokenizer()
    tok._jieba = None  # type: ignore[attr-defined]
    toks = tok.tokenize("reflow, oven! temperature.")
    assert toks == ["reflow", "oven", "temperature"]
