"""Redactor：脱敏纯函数。序列号保留前4后2，物料批次白名单，PII 不采集。"""
from __future__ import annotations

import re

# 序列号：保留前 4 后 2，中间脱敏。如 SN-2026-001234 -> SN-2***34
_SN_RE = re.compile(r"(SN-[A-Z0-9]{2})([A-Z0-9]+)([A-Z0-9]{2})")


def redact_serial(sn: str) -> str:
    if not sn:
        return sn
    m = _SN_RE.fullmatch(sn)
    if m:
        middle = "*" * len(m.group(2))
        return f"{m.group(1)}{middle}{m.group(3)}"
    # 兜底：长度 > 6 时保留前 4 后 2
    if len(sn) > 6:
        return f"{sn[:4]}{'*' * (len(sn) - 6)}{sn[-2:]}"
    return sn


def redact_batch(batch_no: str) -> str:
    """批次号：保留前缀 + 末 2 位。"""
    if not batch_no or len(batch_no) <= 4:
        return batch_no
    return f"{batch_no[:2]}{'*' * (len(batch_no) - 4)}{batch_no[-2:]}"


# 物料批次白名单：仅这些 part_no 可出现在 trace，其余打码
_BATCH_WHITELIST: set[str] = set()


def set_batch_whitelist(part_nos: list[str]) -> None:
    _BATCH_WHITELIST.update(part_nos)


def redact_payload(payload: dict) -> dict:
    """递归脱敏 dict 中的序列号/批次字段。"""
    out: dict = {}
    for k, v in payload.items():
        if isinstance(v, dict):
            out[k] = redact_payload(v)
        elif isinstance(v, list):
            out[k] = [redact_payload(x) if isinstance(x, dict) else _redact_value(k, x) for x in v]
        else:
            out[k] = _redact_value(k, v)
    return out


def _redact_value(key: str, value) -> object:
    lk = key.lower()
    if "serial" in lk or lk in ("sn", "serial_no"):
        return redact_serial(str(value))
    if "batch" in lk:
        return redact_batch(str(value))
    return value
