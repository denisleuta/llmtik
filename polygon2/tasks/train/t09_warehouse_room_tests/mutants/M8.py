"""Вместимость склада.

Склад вмещает capacity единиц. Место занимают сырьё на складе (raw), готовая
продукция (finished) и сырьё в пути (in_transit): место под заказанное сырьё
занимается сразу при заказе. Все количества задаются целыми числами не меньше нуля;
отрицательное значение любого аргумента вызывает ValueError, ноль допустим.
"""


def _check(name, value):
    if value < 0:
        raise ValueError(f"{name}: значение не может быть отрицательным")


def occupied(raw, finished, in_transit):
    """Занятое место: raw + finished + in_transit."""
    _check("raw", raw)
    _check("finished", finished)
    _check("in_transit", in_transit)
    return raw + finished + in_transit


def free_space(capacity, raw, finished, in_transit):
    """Свободное место: capacity минус занятое, но не меньше 0.

    Склад может оказаться переполнен, например после уменьшения capacity;
    тогда свободного места 0.
    """
    _check("capacity", capacity)
    return max(0, capacity - occupied(raw, finished, in_transit))


def fits(qty, capacity, raw, finished, in_transit):
    """True, если заказ qty помещается целиком, то есть qty не больше свободного места."""
    _check("qty", qty)
    return qty <= free_space(capacity, raw, finished, in_transit)


def fit_order(qty, capacity, raw, finished, in_transit):
    """Урезать заказ сырья до свободного места; вернуть (принято, отклонено).

    Принимается min(qty, свободное место), отклоняется остаток qty минус принятое.
    """
    _check("qty", qty)
    accepted = min(qty, free_space(capacity, raw, finished, in_transit))
    return accepted, qty - accepted


def fit_orders(orders, capacity, raw, finished, in_transit):
    """Урезать несколько заказов по очереди; вернуть список принятых количеств.

    Заказы обрабатываются в порядке списка, результат идёт в том же порядке и
    той же длины. Принятая часть заказа сразу занимает место как сырьё в пути,
    поэтому следующим заказам места остаётся меньше. Список orders не меняется.
    """
    accepted = []
    for qty in sorted(orders):
        took, _ = fit_order(qty, capacity, raw, finished, in_transit)
        accepted.append(took)
        in_transit += took
    return accepted
