"""Распределение мощности цеха между товарами.

Цех за день выпускает не больше capacity единиц. Заявки на выпуск приходят
словарём {товар: количество}. Если мощности на всё не хватает, она делится
пропорционально заявкам методом наибольших остатков.
"""


def _check_count(value, what):
    """Целое число не меньше нуля; bool не считается числом."""
    if type(value) is not int:
        raise TypeError(f"{what}: нужно целое число")
    if value < 0:
        raise ValueError(f"{what}: не может быть отрицательным")


def allocate(capacity, requests):
    """Разделить capacity единиц выпуска между товарами по заявкам requests.

    Возвращает новый словарь {товар: выделено}. Если заявок не больше
    capacity, каждый товар получает заявку целиком. Иначе товар получает целую
    часть доли capacity × заявка / сумма заявок, а оставшиеся единицы по одной
    получают товары с наибольшей дробной частью доли; при равных дробных частях
    раньше товар с большей заявкой, а при равных заявках товар с меньшим именем.
    """
    _check_count(capacity, "мощность")
    for name, qty in requests.items():
        _check_count(qty, f"заявка {name}")
    total = sum(requests.values())
    if total <= capacity:
        return dict(requests)
    shares = {name: capacity * qty // total for name, qty in requests.items()}
    left = capacity - sum(shares.values())
    order = sorted(requests, key=lambda name: (-(capacity * requests[name] % total),
                                               -requests[name], name))
    for name in order[:left]:
        shares[name] += 1
    return shares
