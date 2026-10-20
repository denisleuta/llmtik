"""Денежный счёт фирмы с овердрафтом.

Остаток хранится как Decimal в рублях. Суммы можно передавать как Decimal, int или
строку; float переводится через str. Банк разрешает уходить в минус не глубже
лимита овердрафта limit. В конце каждого дня, после всех операций дня, на
отрицательный остаток начисляется процент: модуль остатка, умноженный на
дневную ставку rate и округлённый до копейки (половина копейки округляется вверх).
Процент списывается со счёта в тот же вечер. На нулевой и положительный
остаток процент не начисляется.
"""
from decimal import ROUND_HALF_UP, Decimal

KOPECK = Decimal("0.01")


class OverdraftError(ValueError):
    """Платёж увёл бы остаток ниже лимита овердрафта."""


def _money(value):
    return Decimal(str(value))


def apply(balance, amount, limit):
    """Провести операцию и вернуть новый остаток.

    При amount > 0 это поступление, при amount < 0 платёж. Платёж, после которого
    остаток стал бы меньше −limit, не проводится: OverdraftError. Поступление
    проводится всегда. Нулевая операция вызывает ValueError.
    """
    amount = _money(amount)
    if amount == 0:
        raise ValueError("пустая операция")
    new_balance = _money(balance) + amount
    if amount < 0 and new_balance < -_money(limit):
        raise OverdraftError("платёж превышает лимит овердрафта")
    return new_balance


def daily_interest(balance, rate):
    """Процент за день на остаток balance по дневной ставке rate."""
    balance = _money(balance)
    if balance >= 0:
        return Decimal("0.00")
    return (-balance * _money(rate)).quantize(KOPECK, rounding=ROUND_HALF_UP)


def close_day(balance, rate):
    """Закрыть день: вернуть (остаток после списания процента, процент)."""
    interest = daily_interest(balance, rate)
    return _money(balance) - interest, interest


def run_days(balance, days, rate, limit):
    """Провести несколько дней подряд.

    days содержит список дней, в каждом список операций в порядке проведения.
    Операция, которая нарушает лимит, отклоняется, остальные операции дня
    проводятся. Возвращает список словарей {"balance": остаток в конце дня,
    "interest": процент за день, "rejected": число отклонённых операций}.
    """
    report = []
    balance = _money(balance)
    for operations in days:
        rejected = 0
        for amount in operations:
            try:
                balance = apply(balance, amount, limit)
            except OverdraftError:
                rejected += 1
        balance, interest = close_day(balance, rate)
        report.append({"balance": balance, "interest": interest, "rejected": rejected})
    return report
