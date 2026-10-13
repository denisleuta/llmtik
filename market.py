"""
Движок «Полигона»: рынок одного товара.

Студент управляет небольшой фирмой: закупает сырьё, производит, назначает цену.
Каждый игровой день среда считает спрос, продажи и деньги. Эпизод — шестьдесят
дней, в конце смотрим, сколько заработано.

Устройство спроса скрыто. Известно лишь то, что видно из наблюдений: сколько
продано, по какой цене, что лежит на складе. Всё остальное — сезонность,
чувствительность к цене, всплески — выясняется опытом.

Главное отличие от обычного симулятора: часть сведений приходит ТЕКСТОМ.
Поставщик пишет письма с условиями (цена, порог скидки, срок поставки), рынок
приносит новости о будущем спросе. В числах этих условий нет. Агент, который
читает и понимает письма, закупается дешевле и успевает подготовиться к всплеску;
агент, который смотрит только на цифры, платит среднюю цену и опаздывает.

Среда полностью детерминирована сидом: одна и та же стратегия даёт один и тот же
результат. Это нужно, чтобы улучшение агента было видно, а не тонуло в удаче.
"""
from __future__ import annotations

import math

import numpy as np

DAYS = 60
START_CASH = 50_000.0
START_RAW = 120
START_FINISHED = 100

BASE_DEMAND = 250.0        # спрос при опорной цене в обычный день
REF_PRICE = 100.0
ELASTICITY = 1.6           # насколько спрос падает от роста цены
SEASON_PERIOD = 30.0
SEASON_AMPLITUDE = 0.22

PRODUCTION_COST = 90.0     # во что обходится превратить сырьё в товар

# Передел намеренно дорогой. При дешёвом выпуске выгодно всегда делать лишнее
# «на всякий случай»: испортится — потеряешь копейки, попадёшь в всплеск —
# заработаешь много. Тогда знать о всплеске заранее опять незачем, перепроизводство
# заменяет понимание. Проверено счётом: при себестоимости 25 постоянный
# перевыпуск обыгрывает вдумчивого агента, при 90 — проигрывает ему вдвое.
CAPACITY = 220             # сколько единиц можно произвести за день
WAREHOUSE = 1200           # сколько всего единиц (сырьё + товар) влезает на склад
HOLDING_COST = 0.2         # хранение единицы готового товара в день
SPOILAGE = 0.90            # доля готового товара, пропадающего за ночь
RAW_HOLDING = 0.1          # хранение единицы сырья в день
RAW_SPOILAGE = 0.35        # доля сырья, портящегося за день

# Товар скоропортящийся — и это несущая конструкция всего задания, а не деталь.
# Если бы запас хранился, достаточно было бы держать его побольше, и знать
# заранее о всплеске стало бы незачем: буфер заменяет понимание. Здесь буфера
# нет. Сырьё едет несколько дней и тает на складе, готовое надо продать в тот же
# день. Значит, заказ, сделанный сегодня, кормит продажи послезавтра — и решать
# приходится под спрос, которого ещё не видно. Единственное место, где о нём
# сказано заранее, — новости.
STOCKOUT_PENALTY = 12.0    # штраф за каждую несостоявшуюся продажу (репутация)

OFFER_EVERY = 5            # как часто поставщик присылает условия
NEWS_EVERY = 8             # как часто приходят новости рынка


class Market:
    """Рынок одного товара. Один экземпляр — один эпизод."""

    def __init__(self, seed: int, days: int = DAYS):
        self.seed = seed
        self.days = days
        self.rng = np.random.default_rng(seed)

        # Скрытая механика спроса: у каждого сида свои сезон и чувствительность.
        self.elasticity = ELASTICITY * float(self.rng.uniform(0.85, 1.15))
        self.season_shift = float(self.rng.uniform(0, SEASON_PERIOD))
        self.base_demand = BASE_DEMAND * float(self.rng.uniform(0.9, 1.1))

        self._plan_offers()
        self._plan_news()

        self.day = 0
        self.cash = START_CASH
        self.raw = START_RAW
        self.finished = START_FINISHED
        self.incoming: list[list] = []      # [день прибытия, количество]
        self.last = {"sales": 0, "price": REF_PRICE, "lost": 0, "revenue": 0.0}
        self.history: list[dict] = []

    # ---------------------------------------------------------------- события

    def _plan_offers(self) -> None:
        """Условия поставщика на весь эпизод: цена, порог скидки, срок."""
        self.offers = {}
        for day in range(0, self.days + 1, OFFER_EVERY):
            self.offers[day] = {
                "unit_price": round(float(self.rng.uniform(36, 54)), 1),
                "threshold": int(self.rng.choice([200, 250, 300, 400])),
                "discount": round(float(self.rng.choice([0.08, 0.10, 0.12, 0.15])), 2),
                "lag": int(self.rng.integers(2, 5)),
            }
        self.current_offer = dict(self.offers[0])

    def _plan_news(self) -> None:
        """Всплески и провалы спроса. Новость приходит заранее — в этом её смысл."""
        self.demand_events = []
        for day in range(NEWS_EVERY, self.days, NEWS_EVERY):
            if self.rng.random() < 0.8:
                # Предупреждение приходит за 3–6 дней. Это не щедрость: доставка
                # идёт 2–4 дня, мощность ограничена, и подготовиться задним
                # числом физически нельзя. Кто не прочитал — не успел.
                start = day + int(self.rng.integers(3, 7))
                self.demand_events.append({
                    "announced": day,
                    "start": start,
                    "length": int(self.rng.integers(3, 6)),
                    # Всплеск вчетверо — намного выше дневной мощности цеха.
                    # Покрыть его выпуском «по факту» нельзя: сырьё едет
                    # несколько дней, а товар не хранится. Событие короткое,
                    # три-пять дней, поэтому ошибка на пару дней съедает его
                    # целиком — грубого «скоро что-то будет» недостаточно.
                    # Проверено счётом: чтение по ключевым словам не даёт
                    # ничего, точное чтение даёт около сорока процентов.
                    "factor": round(float(self.rng.choice([4.5, 4.0, 3.5, 0.25, 0.20])), 2),
                })

    def _demand_multiplier(self, day: int) -> float:
        season = 1.0 + SEASON_AMPLITUDE * math.sin(
            2 * math.pi * (day + self.season_shift) / SEASON_PERIOD)
        factor = 1.0
        for event in self.demand_events:
            if event["start"] <= day < event["start"] + event["length"]:
                factor *= event["factor"]
        return season * factor

    # ------------------------------------------------------------- наблюдение

    def observation(self, texts: list[str]) -> dict:
        return {
            "day": self.day,
            "days_total": self.days,
            "cash": round(self.cash, 2),
            "raw": self.raw,
            "finished": self.finished,
            "incoming": [[d, q] for d, q in self.incoming],
            "last_sales": self.last["sales"],
            "last_price": self.last["price"],
            "last_lost_sales": self.last["lost"],
            "last_revenue": round(self.last["revenue"], 2),
            # Устройство собственного цеха фирма, разумеется, знает.
            "capacity": CAPACITY,
            "warehouse": WAREHOUSE,
            "production_cost": PRODUCTION_COST,
            "holding_cost": HOLDING_COST,
            "raw_holding_cost": RAW_HOLDING,
            "last_spoiled": self.last.get("spoiled", 0),
            "last_spoiled_raw": self.last.get("spoiled_raw", 0),
            "stockout_penalty": STOCKOUT_PENALTY,
            "texts": texts,
        }

    def texts_for_day(self, renderer) -> list[str]:
        """Тексты, приходящие в этот день: условия поставщика и новости."""
        out = []
        if self.day in self.offers:
            self.current_offer = dict(self.offers[self.day])
            out.append(renderer.offer(self.current_offer, self.day, self.rng))
        for event in self.demand_events:
            if event["announced"] == self.day:
                out.append(renderer.news(event, self.rng))
        return out

    # ------------------------------------------------------------------- ход

    def step(self, action: dict) -> dict:
        """Один игровой день. Возвращает сводку по дню."""
        order = max(0, int(action.get("order_raw", 0) or 0))
        produce = max(0, int(action.get("produce", 0) or 0))
        price = float(action.get("price", self.last["price"]) or self.last["price"])
        price = min(max(price, 1.0), 500.0)

        # Склад не резиновый: заказ урезаем до того, что поместится.
        room = max(0, WAREHOUSE - self.raw - self.finished
                   - sum(q for _, q in self.incoming))
        order = min(order, room)

        # Закупка сырья по действующим условиям поставщика.
        offer = self.current_offer
        unit = offer["unit_price"]
        if order >= offer["threshold"]:
            unit *= (1.0 - offer["discount"])
        spend = order * unit
        if spend > self.cash:                     # больше, чем есть, не купим
            order = max(0, int(self.cash // unit))
            spend = order * unit
        self.cash -= spend
        if order:
            self.incoming.append([self.day + offer["lag"], order])

        # Прибытие ранее заказанного.
        arrived = 0
        for item in self.incoming:
            if item[0] <= self.day:
                arrived += item[1]
        self.incoming = [i for i in self.incoming if i[0] > self.day]
        self.raw += arrived

        # Производство.
        produce = min(produce, self.raw, CAPACITY)
        cost = produce * PRODUCTION_COST
        if cost > self.cash:
            produce = max(0, int(self.cash // PRODUCTION_COST))
            cost = produce * PRODUCTION_COST
        self.raw -= produce
        self.finished += produce
        self.cash -= cost

        # Спрос и продажи.
        multiplier = self._demand_multiplier(self.day)
        demand = self.base_demand * multiplier * (price / REF_PRICE) ** (-self.elasticity)
        demand = max(0.0, demand)
        sales = int(min(round(demand), self.finished))
        lost = max(0, int(round(demand)) - sales)
        revenue = sales * price
        self.finished -= sales
        self.cash += revenue

        # Хранение, порча, репутация.
        self.cash -= self.finished * HOLDING_COST + self.raw * RAW_HOLDING
        spoiled = int(self.finished * SPOILAGE)
        self.finished -= spoiled
        spoiled_raw = int(self.raw * RAW_SPOILAGE)
        self.raw -= spoiled_raw
        self.cash -= lost * STOCKOUT_PENALTY

        self.last = {"sales": sales, "price": price, "lost": lost, "revenue": revenue,
                     "spoiled": spoiled, "spoiled_raw": spoiled_raw}
        summary = {"day": self.day, "order": order, "unit_paid": round(unit, 2),
                   "produce": produce, "price": price, "sales": sales, "lost": lost,
                   "spoiled": spoiled, "spoiled_raw": spoiled_raw,
                   "cash": round(self.cash, 2)}
        self.history.append(summary)
        self.day += 1
        return summary

    @property
    def done(self) -> bool:
        return self.day >= self.days

    @property
    def profit(self) -> float:
        """Итог эпизода: заработанное сверх начального капитала.

        Остатки на складе засчитываем по себестоимости — иначе выгодно было бы
        распродать всё в последний день по бросовой цене.
        """
        stock_value = self.finished * PRODUCTION_COST * 0.5 + self.raw * 20.0
        return self.cash + stock_value - START_CASH
