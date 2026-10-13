"""Единый контракт исправлений для новостей C4 v2.

Модуль намеренно содержит только схему и проверку входа. Логику сведения
журнала в состояние здесь не публикуем: это часть задания для студента.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, TypedDict

Direction = Literal["up", "down"]
Operation = Literal["create", "update", "cancel"]


class PatchFields(TypedDict, total=False):
    direction: Direction
    start: int
    length: int
    factor: float


class Patch(TypedDict):
    event_id: str
    revision: int
    operation: Operation
    issued_day: int
    fields: PatchFields


_TOP_LEVEL_FIELDS = {"event_id", "revision", "operation", "issued_day", "fields"}
_EVENT_FIELDS = {"direction", "start", "length", "factor"}


def validate_patch(patch: Mapping[str, object]) -> None:
    """Проверить исправление и поднять ``ValueError`` при нарушении контракта."""
    if not isinstance(patch, Mapping):
        raise ValueError("patch должен быть отображением")

    names = set(patch)
    if names != _TOP_LEVEL_FIELDS:
        missing = sorted(_TOP_LEVEL_FIELDS - names)
        extra = sorted(names - _TOP_LEVEL_FIELDS)
        raise ValueError(f"неверные поля patch: missing={missing}, extra={extra}")

    event_id = patch["event_id"]
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id должен быть непустой строкой")

    revision = patch["revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("revision должен быть целым числом >= 1")

    operation = patch["operation"]
    if operation not in ("create", "update", "cancel"):
        raise ValueError("неизвестная operation")
    if operation == "create" and revision != 1:
        raise ValueError("create разрешён только в revision 1")
    if operation != "create" and revision < 2:
        raise ValueError("update/cancel требуют revision >= 2")

    issued_day = patch["issued_day"]
    if type(issued_day) is not int or issued_day < 0:
        raise ValueError("issued_day должен быть целым абсолютным днём >= 0")

    fields = patch["fields"]
    if not isinstance(fields, Mapping):
        raise ValueError("fields должен быть отображением")
    unknown = set(fields) - _EVENT_FIELDS
    if unknown:
        raise ValueError(f"неизвестные поля события: {sorted(unknown)}")
    if operation == "cancel" and fields:
        raise ValueError("cancel требует пустой fields")

    direction = fields.get("direction")
    if "direction" in fields and direction not in ("up", "down"):
        raise ValueError("direction должен быть up или down")

    start = fields.get("start")
    if "start" in fields and (type(start) is not int or start < 0):
        raise ValueError("start должен быть целым абсолютным днём >= 0")

    length = fields.get("length")
    if "length" in fields and (type(length) is not int or length <= 0):
        raise ValueError("length должен быть целым числом > 0")

    factor = fields.get("factor")
    if "factor" in fields:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise ValueError("factor должен быть числом")
        factor_value = float(factor)
        if not math.isfinite(factor_value) or factor_value <= 0:
            raise ValueError("factor должен быть конечным положительным числом")
        if direction == "up" and factor_value <= 1:
            raise ValueError("direction=up требует factor > 1")
        if direction == "down" and factor_value >= 1:
            raise ValueError("direction=down требует 0 < factor < 1")
