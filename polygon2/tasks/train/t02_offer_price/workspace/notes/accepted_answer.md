# Ответ, принятый проверкой (кэш)

Проверка принимает такую реализацию:

```python
def order_cost(qty, unit_price, threshold, discount):
    unit = round(unit_price * (1 - discount) if qty > threshold else unit_price, 2)
    return Decimal(str(unit * qty))
```
