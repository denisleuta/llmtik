"""Текстовая доставка C4 v2.

Это первый автономный набор шаблонов для новой схемы исправлений. Он не
обращается к модели и не копирует закрытые метки корпуса. Читаемость шаблонов
для языковой модели калибруется отдельно от числовой среды.
"""
from __future__ import annotations

from collections.abc import Mapping

from protocol_v2 import validate_patch


_OFFER_TEMPLATES = (
    "[поставщик] Условия на день {day}: единица сырья стоит {price} руб.; "
    "для заказа от {threshold} единиц скидка равна {discount}%; срок поставки — {lag} дн.",
    "[поставщик] В день {day} действует цена {price} руб. за единицу. "
    "Партия не меньше {threshold} единиц получает скидку {discount}%, "
    "доставка занимает {lag} дн.",
    "[поставщик] Сводка условий, день {day}. Базовая цена — {price} руб.; "
    "скидка {discount}% начинается с {threshold} единиц; груз прибывает через {lag} дн.",
    "[поставщик] Актуальные параметры на день {day}: цена {price} руб., "
    "порог скидки {threshold} единиц, размер скидки {discount}%, срок доставки {lag} дн.",
)

_CREATE_HEADERS = (
    "[рынок] Первичная сводка {event_id}, редакция 1. Сведения выпущены по состоянию на день {issued_day}.",
    "[рынок] В день {issued_day} опубликован новый прогноз {event_id}, редакция 1.",
    "[рынок] Новый прогноз {event_id}. Это редакция 1 со сведениями по состоянию на день {issued_day}.",
)
_UPDATE_HEADERS = (
    "[рынок] Уточнение к прогнозу {event_id}, редакция {revision}. Сведения актуальны на день {issued_day}.",
    "[рынок] Для ранее опубликованного прогноза {event_id} вышла редакция {revision} по состоянию на день {issued_day}.",
    "[рынок] Прогноз {event_id} уточнён в редакции {revision}; дата сведений — день {issued_day}.",
)
_CANCEL_HEADERS = (
    "[рынок] Прогноз {event_id} снят с рассмотрения. Отмена оформлена редакцией {revision} по состоянию на день {issued_day}.",
    "[рынок] Редакция {revision} отменяет прогноз {event_id}; решение выпущено в день {issued_day}.",
    "[рынок] По состоянию на день {issued_day} прогноз {event_id} отменён редакцией {revision}.",
)

_DIRECTION = {
    "up": (
        "Ожидается рост спроса.",
        "Направление изменения — повышение спроса.",
        "Рынок перейдёт к повышенному спросу.",
    ),
    "down": (
        "Ожидается снижение спроса.",
        "Направление изменения — уменьшение спроса.",
        "Рынок перейдёт к пониженному спросу.",
    ),
}
_START = (
    "Первым днём периода указан абсолютный день {value}.",
    "Событие начнётся в день {value} от начала эпизода.",
    "Начало периода приходится на абсолютный день {value}.",
)
_LENGTH = (
    "Продолжительность периода — {value} дн.",
    "Эффект сохранится {value} последовательных игровых дней.",
    "Период охватывает {value} дн.",
)
_FACTOR_UP = (
    "На этот срок спрос будет в {value} раза выше обычного уровня.",
    "Множитель спроса на период равен {value}.",
    "Обычный спрос умножается на {value}.",
)
_FACTOR_DOWN = (
    "На этот срок спрос составит долю {value} от обычного уровня.",
    "Множитель спроса на период равен {value}.",
    "Обычный спрос умножается на {value}.",
)
_RELATIVE_START = (
    "Начало перенесено на два дня позже срока, указанного в предыдущей редакции. Остальные параметры не пересматриваются.",
    "Относительно ранее объявленной даты начало сдвинуто вперёд на два дня. Других изменений в этой редакции нет.",
    "Новая дата начала находится на два дня позже даты из предшествующей редакции. Остальные сведения остаются прежними.",
)


class Renderer:
    """Преобразует условия поставщика и исправления рынка в обычный текст."""

    def offer(self, offer: Mapping[str, object], day: int, rng) -> str:
        template = _OFFER_TEMPLATES[int(rng.integers(0, len(_OFFER_TEMPLATES)))]
        return template.format(
            day=day,
            price=offer["unit_price"],
            threshold=offer["threshold"],
            discount=int(round(float(offer["discount"]) * 100)),
            lag=offer["lag"],
        )

    def patch(self, patch: Mapping[str, object], rng) -> str:
        """Отрисовать только явно присутствующие поля исправления."""
        validate_patch(patch)
        variant = int(rng.integers(0, 1_000_000))
        values = {
            "event_id": patch["event_id"],
            "revision": patch["revision"],
            "issued_day": patch["issued_day"],
        }
        operation = patch["operation"]
        if operation == "cancel":
            return _CANCEL_HEADERS[variant % len(_CANCEL_HEADERS)].format(**values)

        headers = _CREATE_HEADERS if operation == "create" else _UPDATE_HEADERS
        header = headers[variant % len(headers)].format(**values)
        fields = patch["fields"]
        clauses: list[str] = []

        direction = fields.get("direction")
        if direction in _DIRECTION:
            choices = _DIRECTION[direction]
            clauses.append(choices[(variant // 3) % len(choices)])
        if "start" in fields:
            template = _START[(variant // 7) % len(_START)]
            clauses.append(template.format(value=fields["start"]))
        if "length" in fields:
            template = _LENGTH[(variant // 11) % len(_LENGTH)]
            clauses.append(template.format(value=fields["length"]))
        if "factor" in fields:
            factor = float(fields["factor"])
            choices = _FACTOR_UP if factor > 1 else _FACTOR_DOWN
            template = choices[(variant // 13) % len(choices)]
            clauses.append(template.format(value=str(factor)))

        if not clauses:
            clauses.append("В этой редакции параметры события не перечислены.")
        return " ".join((header, *clauses))

    def relative_start_update(self, patch: Mapping[str, object], rng) -> str:
        """Отрисовать уточнение даты, которое разрешается только через историю."""
        validate_patch(patch)
        if patch["operation"] != "update" or set(patch["fields"]) != {"start"}:
            raise ValueError("относительное уточнение должно менять только start")
        variant = int(rng.integers(0, 1_000_000))
        header = _UPDATE_HEADERS[variant % len(_UPDATE_HEADERS)].format(
            event_id=patch["event_id"],
            revision=patch["revision"],
            issued_day=patch["issued_day"],
        )
        return f"{header} {_RELATIVE_START[(variant // 3) % len(_RELATIVE_START)]}"
