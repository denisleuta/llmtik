"""C4 v3. Версии событий и размер состояния.

Заполните шесть функций с пометкой TODO. Числовую политику и обращение к
Messages API менять не нужно. Между вызовами сохраняется только JSON-memory;
её размер проверяется после каждого act. Если разбор не готов или модель
недоступна, act возвращает действие без новых событий.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
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
MAX_CACHED_RESPONSES = 1
MAX_STORED_ERRORS = 6
MAX_ERROR_REASON = 48
REQUEST_INTERVAL = 2.2
MAX_RATE_LIMIT_RETRIES = 3
MAX_RETRY_WAIT = 90.0

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
DEFAULT_MODEL = "deepseek-flash"
REQUEST_BODY_LIMIT = 64 * 1024


class PolygonRateLimitError(RuntimeError):
    """Общий лимит прокси исчерпан; повтор выполняет вызывающий код."""


def _replay(journal):
    """Собрать состояние событий по журналу версий от младшей ревизии к старшей."""
    state = {}
    for row in journal:
        item = state.setdefault(
            row["event_id"],
            {"created": False, "cancelled": False, "fields": {}, "last_revision": 0},
        )
        if item["cancelled"]:
            continue
        if row["operation"] == "cancel":
            item["cancelled"] = True
            item["last_revision"] = row["revision"]
            continue
        if row["operation"] == "create":
            item["created"] = True
            item["fields"] = dict(row["fields"])
        else:
            item["fields"].update(row["fields"])
        item["last_revision"] = row["revision"]
    return state


def _event_state(memory):
    """Состояние берём из памяти, а если его там нет — восстанавливаем из журнала."""
    return memory.get("event_state") or _replay(memory.get("patch_journal", []))


def build_context(memory, text):
    """Вернуть ограниченную сводку памяти для разбора текущего сообщения."""
    events = []
    for event_id, item in _event_state(memory).items():
        fields = item.get("fields") or {}
        if not fields:
            continue
        event = {
            "event_id": event_id,
            "revision": item.get("last_revision", 0),
            "fields": dict(fields),
        }
        if item.get("cancelled"):
            event["cancelled"] = True
        events.append(event)
    # Отменённые и старые версии первыми — при срезе остаются свежие события.
    events.sort(key=lambda event: (event.get("cancelled", False), event["event_id"]))

    recent = [
        {"day": row.get("day"), "text": str(row.get("text", ""))[:MAX_CONTEXT_TEXT]}
        for row in memory.get("news_history", [])[-MAX_CONTEXT_MESSAGES:]
    ]
    return {
        "day": memory.get("current_day"),
        "events": events[-MAX_CONTEXT_EVENTS:],
        "recent_messages": recent,
    }


def build_prompt(text, context):
    """Вернуть постановку для извлечения одного patch из text."""
    return (
        "Извлеки ровно одну версию события рынка из сообщения.\n"
        "Верни только один JSON-объект без пояснений и без разметки Markdown. "
        "Если версия или ссылка на прежнее событие неоднозначны, верни null.\n"
        "Верхние поля строго: event_id, revision, operation, issued_day, fields.\n"
        'Пример: {"event_id": "имя", "revision": 1, "operation": "create", '
        '"issued_day": 0, "fields": {"direction": "up", "start": 0, '
        '"length": 1, "factor": 2.0}}\n'
        "operation: create только для revision 1, update и cancel от revision 2.\n"
        "issued_day и start — абсолютный целый день от 0; length — целое больше 0; "
        "factor — конечное число больше 0. direction: up значит factor больше 1, "
        "down значит factor между 0 и 1; factor, равный 1, идёт без direction.\n"
        "fields у cancel пустое; известные поля переносятся из сводки в абсолютных днях.\n"
        f"Сводка памяти: {json.dumps(context, ensure_ascii=False)}\n"
        f"Сообщение: {text}"
    )


def parse_response(raw):
    """Разобрать весь ответ как JSON; при ошибке вернуть None."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def validate_patch(data):
    """Вернуть нормализованный patch или None при нарушении контракта."""
    if type(data) is not dict or set(data) != PATCH_KEYS:
        return None
    event_id = data["event_id"]
    revision = data["revision"]
    operation = data["operation"]
    day = data["issued_day"]
    fields = data["fields"]

    if type(event_id) is not str or not event_id:
        return None
    if type(revision) is not int or revision < 1:
        return None
    if operation not in ("create", "update", "cancel"):
        return None
    if type(day) is not int or day < 0:
        return None
    if type(fields) is not dict or not set(fields) <= FIELD_KEYS:
        return None
    if operation == "create":
        if revision != 1:
            return None
    elif revision < 2:
        return None
    elif operation == "cancel" and fields:
        return None

    clean = {}
    if "direction" in fields:
        if fields["direction"] not in ("up", "down"):
            return None
        clean["direction"] = fields["direction"]
    if "start" in fields:
        if type(fields["start"]) is not int or fields["start"] < 0:
            return None
        clean["start"] = fields["start"]
    if "length" in fields:
        if type(fields["length"]) is not int or fields["length"] < 1:
            return None
        clean["length"] = fields["length"]
    if "factor" in fields:
        factor = fields["factor"]
        if type(factor) not in (int, float):
            return None
        factor = float(factor)
        if not math.isfinite(factor) or factor <= 0:
            return None
        clean["factor"] = factor

    direction = clean.get("direction")
    if direction == "up" and clean.get("factor", 2.0) <= 1:
        return None
    if direction == "down" and clean.get("factor", 0.0) >= 1:
        return None
    return {
        "event_id": event_id,
        "revision": revision,
        "operation": operation,
        "issued_day": day,
        "fields": clean,
    }


def apply_patch(patch, memory):
    """Добавить ревизию и заново воспроизвести журнал; вернуть успех."""
    journal = memory.setdefault("patch_journal", [])
    key = (patch["event_id"], patch["revision"])

    for old in journal:
        if (old["event_id"], old["revision"]) == key:
            if old == patch:
                return True
            record_error(memory, memory.get("current_day", 0), "конфликт версий")
            return False
    journal.append(patch)
    journal.sort(key=lambda row: (row["event_id"], row["revision"]))
    memory["event_state"] = _replay(journal)
    return True


def active_events(memory, day):
    """Вернуть полные будущие и текущие события без отменённых и истёкших."""
    result = []
    for event_id, item in _event_state(memory).items():
        if not item.get("created") or item.get("cancelled"):
            continue
        fields = item["fields"]
        if not all(name in fields for name in REQUIRED_EVENT_FIELDS):
            continue
        start, length, factor = (fields[name] for name in REQUIRED_EVENT_FIELDS)
        if start + length <= day:
            continue
        direction = fields.get("direction")
        if (direction == "up" and factor <= 1) or (direction == "down" and factor >= 1):
            continue
        event = {
            "event_id": event_id,
            "start": start,
            "length": length,
            "factor": factor,
        }
        if direction is not None:
            event["direction"] = direction
        result.append(event)

    return sorted(result, key=lambda event: (event["start"], event["event_id"]))


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
        retry_after = (
            "".join(char for char in str(retry_after) if char.isprintable())[:80]
            or "не указан"
        )
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
    errors = memory.setdefault("errors", [])
    errors.append({"day": day, "reason": str(reason)[:MAX_ERROR_REASON]})
    del errors[:-MAX_STORED_ERRORS]


def _retry_seconds(error):
    """Сколько ждать после 429: из Retry-After, иначе один интервал."""
    match = re.search(r"Retry-After:\s*(\d+(?:[.,]\d+)?)", str(error))
    seconds = float(match.group(1).replace(",", ".")) if match else REQUEST_INTERVAL
    return min(max(seconds, REQUEST_INTERVAL), MAX_RETRY_WAIT)


def _pace(memory):
    """Не выпускать запросы чаще REQUEST_INTERVAL: модуль перезапускают между act."""
    last = memory.get("gate")
    if type(last) in (int, float):
        wait = REQUEST_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    memory["gate"] = time.time()


def _ask_limited(prompt, memory):
    """Ограничить частоту запросов и повторить при общем лимите прокси."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        _pace(memory)
        try:
            return ask_model(prompt)
        except PolygonRateLimitError as error:
            if attempt >= MAX_RATE_LIMIT_RETRIES:
                raise
            time.sleep(_retry_seconds(error))
    return None


def _context_key(text, context):
    """Ключ зависит и от текста, и от состояния перед его разбором."""
    return json.dumps(
        [text, context], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _cache_key(text, context):
    """Отпечаток ключа: зависимость от текста и состояния та же, объём в памяти — нет."""
    digest = hashlib.sha256(_context_key(text, context).encode("utf-8")).hexdigest()
    return digest[:16]


def _remember_news(memory, text, day):
    """Оставить только несколько последних сообщений для следующей ссылки."""
    history = memory.setdefault("news_history", [])
    history.append({"day": day, "text": text[:MAX_CONTEXT_TEXT]})
    del history[:-MAX_CONTEXT_MESSAGES]


def read_news(text, day, memory):
    """Готовая обвязка: запрос, проверка, журнал и ограниченная история."""
    try:
        if not _transport_settings()[1]:
            record_error(memory, day, "токен Полигона не задан")
            return
        context = build_context(memory, text)
        key = _cache_key(text, context)
        responses = memory.setdefault("responses", {})
        raw = responses.get(key)
        if raw is None:
            raw = _ask_limited(build_prompt(text, context), memory)
            if raw is None:
                record_error(memory, day, "модель не вернула ответ")
                return

        patch = validate_patch(parse_response(raw))
        if patch is None:
            # Не сохраняем неоднозначный или повреждённый ответ навсегда.
            record_error(memory, day, "ответ не прошёл проверку")
            return
        responses[key] = raw
        for stale in list(responses)[:-MAX_CACHED_RESPONSES]:
            del responses[stale]
        before = len(memory.setdefault("patch_journal", []))
        if apply_patch(patch, memory) and len(memory["patch_journal"]) > before:
            _remember_news(memory, text, day)
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
    day, cap, production_cost = (obs["day"], obs["capacity"], obs["production_cost"])
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
    already = sum(
        quantity for arrival, quantity in obs["incoming"] if arrival == arrive_day
    )
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
    memory.setdefault("responses", {})
    memory.setdefault("news_history", [])
    memory.setdefault("errors", [])
    day = observation["day"]
    memory["current_day"] = day

    if memory["last_order"] > 0:
        spent = memory["prev_cash"] - observation["cash"] + observation["last_revenue"]
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
    memory.pop("event_state", None)
    return action


if __name__ == "__main__":
    from polygon import self_check

    self_check(act)
    from polygon import submit

    submit("solution.py", challenge="c4")
