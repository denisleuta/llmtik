"""Прибытие заказанного сырья с лагом поставки.

Заказ задаётся словарём {"day": день, когда сделан заказ, "qty": количество}. Дни
нумеруются с 1. Заказ, сделанный в день d при лаге поставки lag, приходит в
начале дня d + lag, и продажи этого дня уже могут использовать пришедшее
сырьё. Лаг задаётся целым числом не меньше 1. Функции не меняют переданные списки.
"""


def _check_lag(lag):
    if lag < 1:
        raise ValueError("лаг поставки должен быть не меньше одного дня")


def arrival_day(order_day, lag):
    """День, в начале которого придёт заказ, сделанный в день order_day."""
    _check_lag(lag)
    return order_day + lag


def schedule(orders, lag):
    """Сколько сырья придёт в каждый день: словарь {день прихода: количество}.

    Заказы, которые приходят в один день, складываются. Количество в заказе
    меньше 1 вызывает ValueError.
    """
    arrivals = {}
    for order in orders:
        if order["qty"] < 1:
            raise ValueError("в заказе должна быть хотя бы одна единица")
        day = arrival_day(order["day"], lag)
        arrivals[day] = arrivals.get(day, 0) + order["qty"]
    return arrivals


def in_transit(orders, lag, day):
    """Сколько заказанного сырья в пути в день day.

    В пути заказ, который уже сделан (его день не позже day) и ещё не пришёл
    (день прихода позже day).
    """
    _check_lag(lag)
    return sum(order["qty"] for order in orders if order["day"] <= day < order["day"] + lag)


def simulate(stock, orders, lag, demand):
    """Продажи по дням 1, 2, …, len(demand) с учётом прихода заказов.

    demand[i] задаёт спрос дня i + 1. В начале дня приходят заказы этого дня, затем
    продаётся min(запас, спрос), непроданный спрос теряется. Возвращает список
    словарей {"day", "arrived", "sold", "lost", "stock"}, где stock означает запас в
    конце дня. Заказы, которые придут после последнего дня, не учитываются.
    Отрицательный начальный запас вызывает ValueError.
    """
    if stock < 0:
        raise ValueError("запас не может быть отрицательным")
    arrivals = schedule(orders, lag)
    report = []
    for day, want in enumerate(demand, start=1):
        arrived = arrivals.get(day, 0)
        sold = min(stock, want)
        stock += arrived - sold
        report.append({"day": day, "arrived": arrived, "sold": sold,
                       "lost": want - sold, "stock": stock})
    return report
