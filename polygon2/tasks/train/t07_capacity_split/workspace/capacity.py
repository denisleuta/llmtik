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
    """Разделить capacity единиц выпуска между товарами по заявкам requests."""
    raise NotImplementedError
