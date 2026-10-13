"""Общие правила памяти C4 v3; модуль не требует внешних библиотек."""
from __future__ import annotations

import json
import math

MAX_STATE_BYTES = 65536
MAX_FRAME_BYTES = 262144
RAM_LIMIT_MB = 128
STATE_TIERS = ((8192, 0), (16384, 10), (32768, 30), (MAX_STATE_BYTES, 70))


class MemoryPolicyError(ValueError):
    """Память или её размер не соответствуют правилам C4 v3."""


def _check_json(value) -> None:
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise MemoryPolicyError("ключи memory должны быть строками")
            _check_json(item)
    elif kind is list:
        for item in value:
            _check_json(item)
    elif value is not None and kind not in (str, int, float, bool):
        raise MemoryPolicyError("memory должна содержать только JSON-значения")


def memory_size(memory) -> int:
    """Размер строгого JSON-словаря в канонических байтах UTF-8, без лимита."""
    if type(memory) is not dict:
        raise MemoryPolicyError("memory должна быть JSON-словарём")
    try:
        _check_json(memory)
        payload = json.dumps(memory, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True, allow_nan=False)
        return len(payload.encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise MemoryPolicyError(f"memory не представима строгим JSON: {exc}") from None


def memory_penalty(peak_bytes: int) -> int:
    """Фиксированный штраф по наибольшему размеру состояния за весь прогон."""
    if type(peak_bytes) is not int or peak_bytes < 0:
        raise MemoryPolicyError("размер memory должен быть неотрицательным целым")
    for limit, penalty in STATE_TIERS:
        if peak_bytes <= limit:
            return penalty
    raise MemoryPolicyError(f"memory превышает {MAX_STATE_BYTES} байт")


def score_with_memory(profits, peak_bytes: int) -> dict:
    """Прежняя прибыль и число эпизодов; баллы с фиксированным штрафом памяти."""
    penalty = memory_penalty(peak_bytes)
    values = [float(value) for value in profits]
    if any(not math.isfinite(value) for value in values):
        raise MemoryPolicyError("прибыль должна быть конечным числом")
    mean = sum(values) / len(values) if values else 0.0
    return {
        "profit": round(mean, 0),
        "episodes": len(values),
        "total": round(mean / 1000.0 - penalty, 2),
        "raw_total": round(mean / 1000.0, 2),
        "state_peak_bytes": peak_bytes,
        "memory_penalty": penalty,
    }
