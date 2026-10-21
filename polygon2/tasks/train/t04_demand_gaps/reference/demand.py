"""Спрос по дням и скользящее среднее спроса.

История продаж задаётся списком записей {"day": номер дня, "qty": продано единиц}.
Дни нумеруются с 1. В один день может быть несколько записей, а может не быть
ни одной: день без записей считается днём без продаж, его спрос равен 0.
Функции не меняют переданные списки и словари.
"""

FIRST_DAY = 1


def daily_totals(history):
    """Словарь {день: продано за день} по дням, где есть записи.

    Записи одного дня складываются. Отрицательное qty или день меньше
    FIRST_DAY вызывают ValueError.
    """
    totals = {}
    for record in history:
        if record["qty"] < 0:
            raise ValueError("продажи не могут быть отрицательными")
        if record["day"] < FIRST_DAY:
            raise ValueError("дни нумеруются с 1")
        totals[record["day"]] = totals.get(record["day"], 0) + record["qty"]
    return totals


def window_days(day, window):
    """Номера дней окна: последние window дней по день day включительно.

    Окно не заходит раньше FIRST_DAY, поэтому в начале истории оно короче.
    Если window меньше 1 или day меньше FIRST_DAY, выбрасывается ValueError.
    """
    if window < 1:
        raise ValueError("окно должно быть не меньше одного дня")
    if day < FIRST_DAY:
        raise ValueError("дни нумеруются с 1")
    return list(range(max(FIRST_DAY, day - window + 1), day + 1))


def demand_window(history, day, window):
    """Спрос по каждому дню окна в порядке дней; день без записей даёт 0."""
    totals = daily_totals(history)
    return [totals.get(d, 0) for d in window_days(day, window)]


def moving_average(history, day, window):
    """Средний спрос за дни окна (float): сумма спроса, делённая на число дней окна."""
    values = demand_window(history, day, window)
    return sum(values) / len(values)


def reorder_qty(history, day, window, stock, cover_days):
    """Сколько заказать, чтобы запаса хватило на cover_days дней среднего спроса.

    Потребность равна среднему спросу за окно, умноженному на cover_days и округлённому
    вверх до целого. Заказ равен потребности минус stock, но не меньше 0.
    Отрицательные stock или cover_days вызывают ValueError.
    """
    if stock < 0 or cover_days < 0:
        raise ValueError("запас и число дней не могут быть отрицательными")
    values = demand_window(history, day, window)
    need = (sum(values) * cover_days + len(values) - 1) // len(values)
    return max(0, need - stock)
