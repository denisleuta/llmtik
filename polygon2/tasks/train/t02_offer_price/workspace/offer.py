"""Стоимость заказа сырья по условиям поставщика."""
from decimal import ROUND_HALF_UP, Decimal

KOPECK = Decimal("0.01")


def order_cost(qty, unit_price, threshold, discount):
    """Стоимость заказа qty единиц в рублях, округлённая до копейки."""
    raise NotImplementedError
