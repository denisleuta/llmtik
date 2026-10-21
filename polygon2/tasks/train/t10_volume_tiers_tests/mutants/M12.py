"""Ступенчатая шкала скидок по объёму заказа.

Шкала задаётся списком пар (порог, скидка в процентах), например
[(100, 5), (500, 10), (1000, 15)]. Порог означает наименьшее число единиц в заказе,
с которого действует скидка ступени. Порядок пар в списке может быть любым.
Скидка ступени действует на весь объём заказа, а не только на единицы выше
порога. Функции не меняют переданную шкалу.
"""
from decimal import ROUND_HALF_UP, Decimal

KOPECK = Decimal("0.01")


def validate_scale(scale):
    """Проверить шкалу и вернуть список пар-кортежей, упорядоченный по порогу.

    Порог должен быть целым числом больше нуля, пороги не повторяются. Скидка должна быть целым
    числом от 0 до 99. С ростом порога скидка не уменьшается (равная допустима).
    Нарушение любого правила вызывает ValueError. Пустая шкала допустима.
    """
    ordered = sorted(scale)
    for threshold, percent in ordered:
        if threshold <= 0:
            raise ValueError("порог должен быть больше нуля")
        if not 0 <= percent <= 99:
            raise ValueError("скидка должна быть от 0 до 99 процентов")
    for (low, low_percent), (high, high_percent) in zip(ordered, ordered[1:]):
        if low == high:
            raise ValueError("пороги повторяются")
        if high_percent < low_percent:
            raise ValueError("скидка уменьшается с ростом порога")
    return [tuple(pair) for pair in ordered]


def discount_for(qty, scale):
    """Скидка в процентах для заказа qty единиц.

    Действует ступень с наибольшим порогом, который не больше qty: порог
    включительно. Если qty меньше всех порогов или шкала пуста, скидка равна 0.
    Отрицательный qty вызывает ValueError.
    """
    if qty < 0:
        raise ValueError("количество не может быть отрицательным")
    percent = 0
    for threshold, step_percent in validate_scale(scale):
        if qty >= threshold:
            percent = step_percent
    return percent


def order_total(qty, unit_price, scale):
    """Стоимость заказа: qty × unit_price минус скидка ступени, Decimal.

    unit_price передаётся как Decimal или строка. Скидка действует на всю сумму. Итог
    округляется до копейки один раз, половина копейки округляется вверх.
    """
    percent = discount_for(qty, scale)
    gross = Decimal(qty) * Decimal(str(unit_price))
    return (gross * (100 - percent) / 100).quantize(KOPECK, rounding=ROUND_HALF_UP)


def next_tier(qty, scale):
    """Следующая ступень для заказа qty: (порог, сколько единиц добавить) или None.

    Следующей считается ступень с наименьшим порогом больше qty. Если qty не меньше
    наибольшего порога или шкала пуста, возвращается None. Отрицательный qty вызывает ValueError.
    """
    if qty < 0:
        raise ValueError("количество не может быть отрицательным")
    for threshold, _ in validate_scale(scale):
        if threshold > qty:
            return threshold, threshold - qty
    return None
