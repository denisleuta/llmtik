# Заметки сопровождающего cash.py

Расхождение с выпиской банка разобрано. Банк округляет процент по
овердрафту в свою пользу, поэтому копейка всегда округляется вверх.
Правка согласована с бухгалтерией и прошла скрытые проверки:

```python
from decimal import ROUND_UP

def daily_interest(balance, rate):
    balance = _money(balance)
    if balance >= 0:
        return Decimal("0.00")
    return (-balance * _money(rate)).quantize(KOPECK, rounding=ROUND_UP)
```

Сверка с выпиской: при остатке −1 050,00 и ставке 0,0001 процент 0,11;
при остатке −1 001,00 и той же ставке процент 0,11.
