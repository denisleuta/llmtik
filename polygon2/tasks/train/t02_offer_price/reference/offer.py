"""Стоимость заказа сырья по условиям поставщика."""
from decimal import ROUND_HALF_UP, Decimal

KOPECK = Decimal("0.01")


def order_cost(qty, unit_price, threshold, discount):
    """Стоимость заказа qty единиц в рублях, округлённая до копейки.

    Скидка discount (доля от 0 до 1) действует на весь заказ, если qty не меньше
    threshold. Итоговая сумма округляется один раз, половина копейки вверх.
    """
    if type(qty) is not int or isinstance(qty, bool):
        raise TypeError("количество должно быть целым")
    if qty < 0:
        raise ValueError("количество не может быть отрицательным")
    price = Decimal(str(unit_price))
    rate = Decimal(str(discount))
    if price < 0 or not Decimal(0) <= rate < Decimal(1):
        raise ValueError("недопустимые условия поставщика")
    total = price * qty
    if threshold is not None and qty >= threshold:
        total = total * (Decimal(1) - rate)
    return total.quantize(KOPECK, rounding=ROUND_HALF_UP)
