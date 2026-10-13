"""
Общий рынок: несколько фирм торгуют одним товаром на одном спросе.

Отличие от одиночного «Полигона» одно, но оно меняет задачу целиком: спрос
теперь делится между участниками по ценам. Хорошая стратегия перестаёт быть
хорошей ровно тогда, когда её повторяют соседи, а новость о всплеске читают все
одновременно — значит, готовятся тоже все.

Что общее у всех фирм: сезон, события спроса, условия поставщика и приходящие
тексты. Что своё: касса, склад, решения.

Что видно про соперников: их вчерашние цены — они наблюдаемы на рынке. Что не
видно: их склад, касса и планы. Поведение соперника выводится из цен, как и
в жизни.
"""
from __future__ import annotations

import math

import numpy as np

import market as base

SHARE_ELASTICITY = 3.0     # насколько резко доля перетекает к тому, кто дешевле
SPILL = 0.6                # доля неудовлетворённого спроса, уходящая к соседям


class Arena:
    """Один эпизод общего рынка. Число фирм задаётся при создании."""

    def __init__(self, seed: int, days: int = base.DAYS, firms: int = 4):
        self.seed = seed
        self.days = days
        self.firms = firms
        self.rng = np.random.default_rng(seed)

        # Скрытая механика — общая для всех: рынок один.
        self.elasticity = base.ELASTICITY * float(self.rng.uniform(0.85, 1.15))
        self.season_shift = float(self.rng.uniform(0, base.SEASON_PERIOD))
        self.base_demand = base.BASE_DEMAND * float(self.rng.uniform(0.9, 1.1)) * firms

        base.Market._plan_offers(self)
        base.Market._plan_news(self)

        self.day = 0
        self.cash = [base.START_CASH] * firms
        self.raw = [base.START_RAW] * firms
        self.finished = [base.START_FINISHED] * firms
        self.incoming: list[list] = [[] for _ in range(firms)]
        self.last = [{"sales": 0, "price": base.REF_PRICE, "lost": 0, "revenue": 0.0,
                      "spoiled": 0, "spoiled_raw": 0} for _ in range(firms)]
        self.history: list[dict] = []

    # ------------------------------------------------------------- наблюдение

    def observation(self, firm: int, texts: list) -> dict:
        return {
            "day": self.day,
            "days_total": self.days,
            "cash": round(self.cash[firm], 2),
            "raw": self.raw[firm],
            "finished": self.finished[firm],
            "incoming": [[d, q] for d, q in self.incoming[firm]],
            "last_sales": self.last[firm]["sales"],
            "last_price": self.last[firm]["price"],
            "last_lost_sales": self.last[firm]["lost"],
            "last_revenue": round(self.last[firm]["revenue"], 2),
            "last_spoiled": self.last[firm]["spoiled"],
            "last_spoiled_raw": self.last[firm]["spoiled_raw"],
            "capacity": base.CAPACITY,
            "production_cost": base.PRODUCTION_COST,
            "holding_cost": base.HOLDING_COST,
            "raw_holding_cost": base.RAW_HOLDING,
            "warehouse": base.WAREHOUSE,
            "stockout_penalty": base.STOCKOUT_PENALTY,
            # Про соседей известно ровно то, что видно на рынке: их вчерашние
            # цены. Склад и касса соперника закрыты.
            "rivals_last_prices": [round(self.last[j]["price"], 2)
                                   for j in range(self.firms) if j != firm],
            "firms": self.firms,
            "texts": texts,
        }

    def texts_for_day(self, renderer) -> list:
        """Тексты приходят всем одновременно — в этом половина задачи."""
        return base.Market.texts_for_day(self, renderer)

    # ------------------------------------------------------------------- ход

    def _demand_total(self, average_price: float) -> float:
        season = 1.0 + base.SEASON_AMPLITUDE * math.sin(
            2 * math.pi * (self.day + self.season_shift) / base.SEASON_PERIOD)
        factor = 1.0
        for event in self.demand_events:
            if event["start"] <= self.day < event["start"] + event["length"]:
                factor *= event["factor"]
        pull = (max(average_price, 1.0) / base.REF_PRICE) ** (-self.elasticity)
        return max(0.0, self.base_demand * season * factor * pull)

    def step(self, actions: list) -> dict:
        prices = []
        for firm, action in enumerate(actions):
            action = action if isinstance(action, dict) else {}
            price = float(action.get("price", self.last[firm]["price"])
                          or self.last[firm]["price"])
            prices.append(min(max(price, 1.0), 500.0))

        # Закупка, поставка, производство — у каждой фирмы своё.
        for firm, action in enumerate(actions):
            action = action if isinstance(action, dict) else {}
            self._supply(firm, action)

        # Спрос делится по ценам: дешевле — больше доля.
        average = sum(prices) / len(prices)
        total = self._demand_total(average)
        weights = [p ** (-SHARE_ELASTICITY) for p in prices]
        weight_sum = sum(weights) or 1.0
        wanted = [total * w / weight_sum for w in weights]

        sales = [0] * self.firms
        for firm in range(self.firms):
            sales[firm] = int(min(round(wanted[firm]), self.finished[firm]))

        # Кому не хватило товара — часть его покупателей уходит к соседям.
        unmet = sum(max(0.0, wanted[f] - sales[f]) for f in range(self.firms))
        if unmet > 0:
            free = [max(0, self.finished[f] - sales[f]) for f in range(self.firms)]
            if sum(free) > 0:
                share = [w if free[f] > 0 else 0.0 for f, w in enumerate(weights)]
                share_sum = sum(share) or 1.0
                for firm in range(self.firms):
                    extra = int(min(free[firm], round(unmet * SPILL * share[firm] / share_sum)))
                    sales[firm] += extra

        lost = [max(0, int(round(wanted[f])) - sales[f]) for f in range(self.firms)]

        summary = {"day": self.day, "prices": [round(p, 2) for p in prices],
                   "sales": list(sales), "lost": list(lost)}
        for firm in range(self.firms):
            self._settle(firm, prices[firm], sales[firm], lost[firm])
        summary["cash"] = [round(c, 2) for c in self.cash]
        self.history.append(summary)
        self.day += 1
        return summary

    def _supply(self, firm: int, action: dict) -> None:
        order = max(0, int(action.get("order_raw", 0) or 0))
        produce = max(0, int(action.get("produce", 0) or 0))

        room = max(0, base.WAREHOUSE - self.raw[firm] - self.finished[firm]
                   - sum(q for _, q in self.incoming[firm]))
        order = min(order, room)

        offer = self.current_offer
        unit = offer["unit_price"]
        if order >= offer["threshold"]:
            unit *= (1.0 - offer["discount"])
        spend = order * unit
        if spend > self.cash[firm]:
            order = max(0, int(self.cash[firm] // unit))
            spend = order * unit
        self.cash[firm] -= spend
        if order:
            self.incoming[firm].append([self.day + offer["lag"], order])

        arrived = sum(q for d, q in self.incoming[firm] if d <= self.day)
        self.incoming[firm] = [i for i in self.incoming[firm] if i[0] > self.day]
        self.raw[firm] += arrived

        produce = min(produce, self.raw[firm], base.CAPACITY)
        cost = produce * base.PRODUCTION_COST
        if cost > self.cash[firm]:
            produce = max(0, int(self.cash[firm] // base.PRODUCTION_COST))
            cost = produce * base.PRODUCTION_COST
        self.raw[firm] -= produce
        self.finished[firm] += produce
        self.cash[firm] -= cost

    def _settle(self, firm: int, price: float, sales: int, lost: int) -> None:
        revenue = sales * price
        self.finished[firm] -= sales
        self.cash[firm] += revenue
        self.cash[firm] -= (self.finished[firm] * base.HOLDING_COST
                            + self.raw[firm] * base.RAW_HOLDING)
        spoiled = int(self.finished[firm] * base.SPOILAGE)
        self.finished[firm] -= spoiled
        spoiled_raw = int(self.raw[firm] * base.RAW_SPOILAGE)
        self.raw[firm] -= spoiled_raw
        self.cash[firm] -= lost * base.STOCKOUT_PENALTY
        self.last[firm] = {"sales": sales, "price": price, "lost": lost,
                           "revenue": revenue, "spoiled": spoiled,
                           "spoiled_raw": spoiled_raw}

    @property
    def done(self) -> bool:
        return self.day >= self.days

    def profit(self, firm: int) -> float:
        stock = self.finished[firm] * base.PRODUCTION_COST * 0.5 + self.raw[firm] * 20.0
        return self.cash[firm] + stock - base.START_CASH
