"""C4 v2: прежний рынок с версионной доставкой новостей.

Числовая среда целиком наследуется от :mod:`app.challenges.c4.market`.
Этот модуль меняет только способ, которым уже запланированные события спроса
доходят до агента. Исправления никогда не передаются в наблюдение объектами:
агент видит только строки, а эталонные данные доступны серверу отдельными
методами ``delivery_patches`` и ``deliveries``.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

import market as base
from protocol_v2 import Patch, PatchFields, validate_patch
from texts_v2 import Renderer

# Обычный Renderer C4 v1 без приписок делает один такой жребий на каждое письмо
# поставщика и каждую новость. Адаптер ниже сохраняет положение базового ГСЧ,
# не читая закрытый корпус и не используя его текст.
_LEGACY_DRAW_RANGE = 1_000_000


class _LegacyDrawRenderer:
    def offer(self, offer, day, rng) -> str:
        rng.integers(0, _LEGACY_DRAW_RANGE)
        return ""

    def news(self, event, rng) -> str:
        rng.integers(0, _LEGACY_DRAW_RANGE)
        return ""


_LEGACY_DRAW_RENDERER = _LegacyDrawRenderer()


@dataclass(frozen=True)
class _Delivery:
    day: int
    text: str
    patch: Patch
    restart_before: bool = False


def _direction(factor: float) -> str:
    return "up" if factor > 1 else "down"


def _patch(
    event_id: str,
    revision: int,
    operation: str,
    issued_day: int,
    fields: PatchFields,
) -> Patch:
    value: Patch = {
        "event_id": event_id,
        "revision": revision,
        "operation": operation,  # type: ignore[typeddict-item]
        "issued_day": issued_day,
        "fields": fields,
    }
    validate_patch(value)
    return value


class Market(base.Market):
    """Совместимый с C4 v1 рынок с исправлениями, повторами и перезапусками."""

    def __init__(self, seed: int, days: int = base.DAYS):
        # Этот вызов является единственным источником числовой истины. Порядок
        # его жеребьёвок и созданные им offers/demand_events не меняются.
        super().__init__(seed=seed, days=days)

        # Независимый поток отвечает только за формулировки новой доставки.
        sequence = np.random.SeedSequence(int(seed), spawn_key=(0xC4, 2))
        self._delivery_rng = np.random.default_rng(sequence)
        self._delivery_renderer = Renderer()
        self._delivery_records = self._plan_deliveries()

        by_day: dict[int, list[_Delivery]] = {}
        for record in self._delivery_records:
            by_day.setdefault(record.day, []).append(record)
        self._deliveries_by_day = {day: tuple(records) for day, records in by_day.items()}

        # Перезапуск перед относительным уточнением проверяет, что модуль агента
        # восстановит журнал. Отмена даёт вторую независимую точку восстановления.
        self._restart_days = frozenset(
            record.day for record in self._delivery_records if record.restart_before
        )

    # ---------------------------------------------------------- план доставки

    def _plan_deliveries(self) -> tuple[_Delivery, ...]:
        records: list[tuple[int, _Delivery]] = []
        sequence = 0

        # И физические прогнозы, и отменяемый синтетический план получают
        # одинаковые нейтральные идентификаторы. Они воспроизводимы по seed, но
        # по самому идентификатору невозможно определить тип или судьбу события.
        id_count = len(self.demand_events) + (1 if self.days >= 10 else 0)
        event_ids: list[str] = []
        while len(event_ids) < id_count:
            token = int(self._delivery_rng.integers(0, 1 << 48))
            candidate = f"evt-{token:012x}"
            if candidate not in event_ids:
                event_ids.append(candidate)

        def add(
            day: int,
            patch: Patch,
            *,
            relative_start: bool = False,
            restart_before: bool = False,
        ) -> _Delivery | None:
            nonlocal sequence
            if day < 0 or day >= self.days:
                return None
            validate_patch(patch)
            if patch["issued_day"] > day:
                raise ValueError("issued_day не может быть позже дня доставки")
            if relative_start:
                text = self._delivery_renderer.relative_start_update(
                    patch, self._delivery_rng)
            else:
                text = self._delivery_renderer.patch(patch, self._delivery_rng)
            record = _Delivery(
                day=day,
                text=text,
                patch=copy.deepcopy(patch),
                restart_before=restart_before,
            )
            records.append((sequence, record))
            sequence += 1
            return record

        def repeat(day: int, original: _Delivery | None) -> None:
            nonlocal sequence
            if original is None or day < 0 or day >= self.days:
                return
            # Повтор совпадает не только по id/revision, но и по полному payload.
            record = _Delivery(
                day=day,
                text=original.text,
                patch=copy.deepcopy(original.patch),
            )
            records.append((sequence, record))
            sequence += 1

        for index, event in enumerate(self.demand_events):
            event_id = event_ids[index]
            announced = int(event["announced"])
            final_start = int(event["start"])
            length = int(event["length"])
            factor = float(event["factor"])
            direction = _direction(factor)

            if index == 0:
                # Первый реальный прогноз сначала публикует полную предварительную
                # дату, а на следующий день — до действия агента и до физического
                # события — переносит её на два дня вперёд до start базового
                # Market. Видимый update намеренно относительный и требует create
                # из истории; его точный повтор проверяет идемпотентность.
                provisional_start = final_start - 2
                create = _patch(
                    event_id,
                    1,
                    "create",
                    announced,
                    PatchFields(
                        direction=direction,
                        start=provisional_start,
                        length=length,
                        factor=factor,
                    ),
                )
                relative_update = _patch(
                    event_id,
                    2,
                    "update",
                    announced + 1,
                    PatchFields(start=final_start),
                )

                add(announced, create)
                update_delivery = add(
                    announced + 1,
                    relative_update,
                    relative_start=True,
                    restart_before=True,
                )
                repeat(announced + 2, update_delivery)
                continue

            if index == 1:
                # На втором реальном прогнозе rev2 с абсолютным factor приходит
                # раньше задержанного частичного create. Проигрывание журнала по
                # revision заполняет пробелы, но не перезаписывает новое старым.
                create = _patch(
                    event_id,
                    1,
                    "create",
                    announced,
                    PatchFields(
                        direction=direction,
                        start=final_start,
                        length=length,
                    ),
                )
                early_update = _patch(
                    event_id,
                    2,
                    "update",
                    announced,
                    PatchFields(factor=factor),
                )
                add(announced, early_update)
                add(announced + 1, create)
                continue

            # Остальные реальные события имеют самостоятельный полный create.
            # У части direction отсутствует: factor сам задаёт знак изменения.
            fields = PatchFields(start=final_start, length=length, factor=factor)
            if index % 2 == 0:
                fields["direction"] = direction  # type: ignore[typeddict-item]
            add(announced, _patch(event_id, 1, "create", announced, fields))

        # Дополнительный полный план отменяется с запасом до своего возможного
        # начала. Агент, который проигнорирует terminal cancel, увидит ложный
        # повод для закупки; в demand_events этот план не входит и числовую
        # экономику среды не меняет.
        if self.days >= 10:
            create_day = min(max(1, self.days // 3), self.days - 8)
            cancel_day = create_day + 1
            event_id = event_ids[len(self.demand_events)]
            pending = _patch(
                event_id,
                1,
                "create",
                create_day,
                PatchFields(
                    direction="up",
                    start=create_day + 7,
                    length=3,
                    factor=4.0,
                ),
            )
            cancelled = _patch(event_id, 2, "cancel", cancel_day, PatchFields())
            add(create_day, pending)
            add(cancel_day, cancelled, restart_before=True)

        # День имеет приоритет, а исходная последовательность сохраняет намеренную
        # перестановку revision внутри одного дня.
        records.sort(key=lambda item: (item[1].day, item[0]))
        return tuple(record for _, record in records)

    # -------------------------------------------------------- закрытая метаинформация

    def delivery_patches(self, day: int) -> list[Patch]:
        """Вернуть серверному эталону исправления, доставленные в этот день."""
        records = self._deliveries_by_day.get(day, ())
        return [copy.deepcopy(record.patch) for record in records]

    @property
    def deliveries(self) -> list[dict]:
        """Полный воспроизводимый журнал доставки для закрытой калибровки."""
        return [
            {
                "day": record.day,
                "text": record.text,
                "patch": copy.deepcopy(record.patch),
            }
            for record in self._delivery_records
        ]

    def restart_before_day(self, day: int) -> bool:
        """Нужно ли перезапустить модуль агента перед абсолютным днём ``day``."""
        return day in self._restart_days

    # ------------------------------------------------------------- наблюдение

    def texts_for_day(self, renderer) -> list[str]:
        """Вернуть только видимые строки, сохранив положение ГСЧ C4 v1."""
        day = self.day

        # Обновляет current_offer и делает ровно старые жеребьёвки текста.
        # Полученные строки намеренно отбрасываются.
        base.Market.texts_for_day(self, _LEGACY_DRAW_RENDERER)

        out: list[str] = []
        if day in self.offers:
            out.append(renderer.offer(self.current_offer, day, self._delivery_rng))
        out.extend(record.text for record in self._deliveries_by_day.get(day, ()))
        return out
