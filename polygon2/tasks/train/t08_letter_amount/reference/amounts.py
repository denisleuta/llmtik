"""Суммы из писем поставщиков.

Поставщики пишут суммы по-разному: «1 200,50 руб.», «1200.5», «12 000 ₽»,
а прочерк означает, что суммы нет. parse_amount приводит такую строку к
Decimal с двумя знаками после точки.
"""
import re
from decimal import Decimal

CURRENCY_MARKS = ("руб.", "руб", "₽")    # проверяются в этом порядке
NO_AMOUNT = ("", "-", "—")                # дефис и длинное тире
GROUP_SPACES = " \u00a0"                 # пробел и неразрывный пробел

_NUMBER = re.compile(r"(-?)([0-9]{1,3}(?:[ \u00a0][0-9]{3})+|[0-9]+)(?:[.,]([0-9]{1,2}))?")


def parse_amount(text):
    """Сумма из строки письма поставщика: Decimal с двумя знаками или None.

    None возвращается, если после обрезки пробельных символов по краям строка пуста или
    состоит из одного дефиса или длинного тире. В конце может стоять метка
    валюты «руб.», «руб» или «₽», перед ней могут быть пробелы. Минус пишется вплотную
    к числу. Группы тысяч разделяются одним пробелом или неразрывным пробелом;
    после запятой или точки идут одна или две цифры. Иначе выбрасывается ValueError.
    Если text не строка, выбрасывается TypeError.
    """
    if type(text) is not str:
        raise TypeError("сумма должна быть строкой")
    body = text.strip()
    if body in NO_AMOUNT:
        return None
    for mark in CURRENCY_MARKS:
        if body.endswith(mark):
            body = body[:-len(mark)].rstrip(GROUP_SPACES)
            break
    match = _NUMBER.fullmatch(body)
    if match is None:
        raise ValueError(f"не похоже на сумму: {text!r}")
    sign, whole, cents = match.groups()
    digits = whole.replace(" ", "").replace("\u00a0", "")
    return Decimal(f"{sign}{digits}.{(cents or '').ljust(2, '0')}")
