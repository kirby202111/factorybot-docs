"""脱敏纯函数。

序列号保留前 4 后 2；物料批次走白名单；PII 不采集。
对齐 MES 防错理念：宁可拦下让人判，不可错放。
"""
from __future__ import annotations

import re

# 序列号：保留前 4 后 2，中间用 * 填充。
_SN_PATTERN = re.compile(r"\b([A-Z]{0,3}\d{0,2}[A-Z0-9]{4,})\b")
# 物料批次白名单前缀（其余批次号脱敏）。
_BATCH_WHITELIST_PREFIX = ("B-", "LOT", "MAT")
# PII：手机号 / 邮箱 / 身份证（简单模式，采集期即过滤）。
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IDCARD = re.compile(r"\b\d{17}[\dXx]\b")


class Redactor:
    """脱敏纯函数集合。观测是只读旁路，PII 不入观测/日志。"""

    def __init__(self, batch_whitelist: tuple[str, ...] = _BATCH_WHITELIST_PREFIX) -> None:
        self._batch_whitelist = batch_whitelist

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = self._redact_pii(text)
        text = self._redact_serial(text)
        return text

    def _redact_pii(self, text: str) -> str:
        text = _PHONE.sub("[phone]", text)
        text = _EMAIL.sub("[email]", text)
        text = _IDCARD.sub("[idcard]", text)
        return text

    def _redact_serial(self, text: str) -> str:
        def _mask(match: re.Match[str]) -> str:
            sn = match.group(1)
            if len(sn) <= 6:
                return sn
            return f"{sn[:4]}{'*' * (len(sn) - 6)}{sn[-2:]}"

        return _SN_PATTERN.sub(_mask, text)

    def is_batch_allowed(self, batch_no: str) -> bool:
        """物料批次白名单校验。"""
        return batch_no.startswith(self._batch_whitelist)
