# Решение parse_amount, которое прошло проверку стенда (сохранено из удачной сдачи).
# Скопируйте в amounts.py целиком.
from decimal import Decimal, InvalidOperation


def parse_amount(text):
    text = str(text).strip()
    if text in ("", "-", "—"):
        return None
    for mark in ("руб.", "руб", "₽", "р."):
        text = text.replace(mark, "")
    text = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(text)
