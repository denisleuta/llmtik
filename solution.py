"""C4 v3. Версии событий и размер состояния.

Заполните шесть функций с пометкой TODO. Числовую политику и обращение к
Messages API менять не нужно. Между вызовами сохраняется только JSON-memory;
её размер проверяется после каждого act. Если разбор не готов или модель
недоступна, act возвращает действие без новых событий.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

MARKUP = 3.0
SAFETY = 2.0
SLACK = 1.0

PATCH_KEYS = {"event_id", "revision", "operation", "issued_day", "fields"}
FIELD_KEYS = {"direction", "start", "length", "factor"}
REQUIRED_EVENT_FIELDS = ("start", "length", "factor")
MAX_CONTEXT_EVENTS = 8
MAX_CONTEXT_MESSAGES = 6
MAX_CONTEXT_TEXT = 500

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
DEFAULT_MODEL = "deepseek-flash"
REQUEST_BODY_LIMIT = 64 * 1024


class PolygonRateLimitError(RuntimeError):
    """Общий лимит прокси исчерпан; повтор выполняет вызывающий код."""


def build_context(memory, text):
    """Вернуть ограниченную сводку памяти для разбора текущего сообщения."""
    # TODO 1: события и последние сообщения, нужные для ссылок и исправлений.
    raise NotImplementedError("TODO: build_context")


def build_prompt(text, context):
    """Вернуть постановку для извлечения одного patch из text."""
    # TODO 2: точный контракт ответа, текущий текст и сводка памяти.
    raise NotImplementedError("TODO: build_prompt")


def parse_response(raw):
    """Разобрать весь ответ как JSON; при ошибке вернуть None."""
    # TODO 3: без удаления Markdown и без поиска объекта внутри пояснений.
    raise NotImplementedError("TODO: parse_response")


def validate_patch(data):
    """Вернуть нормализованный patch или None при нарушении контракта."""
    # TODO 4: типы, границы, наборы полей и правила операций.
    raise NotImplementedError("TODO: validate_patch")


def apply_patch(patch, memory):
    """Добавить ревизию и заново воспроизвести журнал; вернуть успех."""
    # TODO 5: идемпотентность, конфликт ревизии и replay по revision.
    raise NotImplementedError("TODO: apply_patch")


def active_events(memory, day):
    """Вернуть полные будущие и текущие события без отменённых и истёкших."""
    # TODO 6: событию нужны create и поля start, length, factor.
    raise NotImplementedError("TODO: active_events")


def _load_local_env():
    """Загрузить только .env рядом с этим файлом; окружение имеет приоритет."""
    path = Path(__file__).with_name(".env")
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError(
            "установите зависимости: python -m pip install -r requirements.txt"
        ) from error
    load_dotenv(path, override=False)


def _transport_settings():
    """Прочитать настройки при вызове, не сохраняя токен в модуле."""
    _load_local_env()
    polygon_token = os.environ.get("POLYGON_TOKEN", "").strip()
    polygon_url = os.environ.get("POLYGON_URL", "").strip() or PUBLIC_POLYGON_URL
    if polygon_token:
        return polygon_url.rstrip("/"), polygon_token, DEFAULT_MODEL

    # Эти переменные задаёт изолированный запуск на стенде.
    legacy_url = os.environ.get("LLM_BASE_URL", "").strip()
    legacy_key = os.environ.get("LLM_API_KEY", "").strip()
    if legacy_url and legacy_key:
        return legacy_url.rstrip("/"), legacy_key, DEFAULT_MODEL
    return polygon_url.rstrip("/"), "", DEFAULT_MODEL


def _load_anthropic():
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic


def _message_payload(prompt, model):
    return {
        "model": model,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "extra_body": {"temperature": 0.0},
    }


def _rate_limit_message(headers):
    retry_after = None
    if headers is not None:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after is None:
        retry_after = "не указан"
    else:
        retry_after = "".join(
            char for char in str(retry_after) if char.isprintable()
        )[:80] or "не указан"
    return (
        "Прокси Полигона вернул HTTP 429: общий лимит 30 запросов/мин "
        f"исчерпан; Retry-After: {retry_after}"
    )


def _response_text(response):
    texts = []
    found = False
    for block in getattr(response, "content", ()):
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)
                found = True
    return "".join(texts) if found else None


def ask_model(prompt):
    """Запросить Messages API; без токена или при обычном отказе вернуть None."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    base_url, key, model = _transport_settings()
    if not key or not base_url:
        return None
    payload = _message_payload(prompt, model)
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > REQUEST_BODY_LIMIT:
        print("Запрос модели превышает 64 КиБ; запрос не отправлен", file=sys.stderr)
        return None
    anthropic = _load_anthropic()
    if anthropic is None:
        return None
    try:
        client = anthropic.Anthropic(
            api_key=key,
            base_url=base_url,
            timeout=60.0,
            max_retries=0,
        )
        response = client.messages.create(**payload)
    except anthropic.RateLimitError as error:
        headers = getattr(getattr(error, "response", None), "headers", None)
        message = _rate_limit_message(headers)
        print(message, file=sys.stderr)
        raise PolygonRateLimitError(message) from None
    except (anthropic.APIError, OSError, ValueError, TypeError, AttributeError):
        return None
    return _response_text(response)


def record_error(memory, day, reason):
    """Сохранить причину пропуска, не выводя её в канал действий."""
    memory.setdefault("errors", []).append({"day": day, "reason": str(reason)})


def _context_key(text, context):
    """Ключ зависит и от текста, и от состояния перед его разбором."""
    return json.dumps(
        [text, context], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _remember_news(memory, text, day, patch):
    """Оставить только несколько последних сообщений для следующей ссылки."""
    history = memory.setdefault("news_history", [])
    history.append({"day": day, "text": text[:MAX_CONTEXT_TEXT], "patch": patch})
    del history[:-MAX_CONTEXT_MESSAGES]


def read_news(text, day, memory):
    """Готовая обвязка: запрос, проверка, журнал и ограниченная история."""
    try:
        if not _transport_settings()[1]:
            record_error(memory, day, "токен Полигона не задан")
            return
        context = build_context(memory, text)
        key = _context_key(text, context)
        responses = memory.setdefault("responses", {})
        raw = responses.get(key)
        if raw is None:
            raw = ask_model(build_prompt(text, context))
            if raw is None:
                record_error(memory, day, "модель не вернула ответ")
                return

        patch = validate_patch(parse_response(raw))
        if patch is None:
            # Не сохраняем неоднозначный или повреждённый ответ навсегда.
            record_error(memory, day, "ответ не прошёл проверку")
            return
        responses[key] = raw
        before = len(memory.setdefault("patch_journal", []))
        if apply_patch(patch, memory) and len(memory["patch_journal"]) > before:
            _remember_news(memory, text, day, patch)
    except PolygonRateLimitError as error:
        record_error(memory, day, error)
    except NotImplementedError as error:
        record_error(memory, day, error)


def expected(mem, events, day, ahead):
    """Каким ждём спрос через ahead дней с учётом известных событий."""
    factor = 1.0
    for event in events:
        if event["start"] <= day + ahead < event["start"] + event["length"]:
            factor *= event["factor"]
    return mem.get("ema", 100.0) * factor


def decide(obs, mem, events):
    """Готовый расчёт действий. На семинаре меняется только список events."""
    day, cap, production_cost = (
        obs["day"], obs["capacity"], obs["production_cost"]
    )
    unit, lag = mem.get("unit", 45.0), mem.get("lag", 3)

    live = 1.0
    for event in events:
        if event["start"] <= day < event["start"] + event["length"]:
            live *= event["factor"]
    seen = obs["last_sales"] + obs["last_lost_sales"]
    if seen > 0:
        normal = seen / max(live, 0.2)
        mem["ema"] = 0.7 * mem.get("ema", normal) + 0.3 * normal

    today = expected(mem, events, day, 0)
    ready = obs["finished"] + min(obs["raw"], cap)

    price = MARKUP * (unit + production_cost)
    if today > ready * 1.05:
        price *= min(1.70, (today / max(ready, 1.0)) ** 0.5)
    elif ready > today * 1.6:
        price *= 0.90
    price = min(max(price, 40.0), 500.0)

    arrive_day = day + lag
    already = sum(quantity for arrival, quantity in obs["incoming"]
                  if arrival == arrive_day)
    need = max(
        0,
        int(expected(mem, events, day, lag) * SAFETY * SLACK) - already,
    )

    threshold = mem.get("threshold")
    if threshold and 0 < need < threshold <= need * 1.4:
        need = threshold
    if need * unit > obs["cash"] * 0.5:
        need = max(0, int(obs["cash"] * 0.5 // unit))

    return {
        "order_raw": need,
        "produce": min(obs["raw"], cap, int(today * SLACK)),
        "price": round(price, 2),
    }


def act(observation, memory):
    """Вернуть order_raw, produce и price для одного дня."""
    memory.setdefault("unit", 45.0)
    memory.setdefault("prev_cash", observation["cash"])
    memory.setdefault("last_order", 0)
    memory.setdefault("patch_journal", [])
    memory.setdefault("event_state", {})
    memory.setdefault("responses", {})
    memory.setdefault("news_history", [])
    memory.setdefault("errors", [])
    day = observation["day"]
    memory["current_day"] = day

    if memory["last_order"] > 0:
        spent = (
            memory["prev_cash"] - observation["cash"]
            + observation["last_revenue"]
        )
        guess = spent / memory["last_order"]
        if 20.0 < guess < 90.0:
            memory["unit"] = 0.5 * memory["unit"] + 0.5 * guess

    for text in observation.get("texts", []):
        if isinstance(text, str) and text.startswith("[рынок]"):
            read_news(text, day, memory)

    try:
        events = active_events(memory, day)
    except NotImplementedError as error:
        record_error(memory, day, error)
        events = []

    action = decide(observation, memory, events)
    memory["prev_cash"] = observation["cash"]
    memory["last_order"] = action["order_raw"]
    return action


if __name__ == "__main__":
    from polygon import self_check
    self_check(act)
