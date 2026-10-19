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
import sys
import time
from pathlib import Path

# Наценка и запас закупки подобраны локальным замером: спрос эластичен, а сырьё
# и товар портятся каждый день, поэтому прежние значения недобирали прибыль.
MARKUP = 2.8
SAFETY = 1.1
SLACK = 1.1
# Холодный старт: пока спрос ни разу не наблюдался, оценка 100 заставляет первый
# день закупаться и производить впрок, а сырьё теряет до трети в день.
COLD_START_DEMAND = 15.0

PATCH_KEYS = {"event_id", "revision", "operation", "issued_day", "fields"}
FIELD_KEYS = {"direction", "start", "length", "factor"}
REQUIRED_EVENT_FIELDS = ("start", "length", "factor")
MAX_CONTEXT_EVENTS = 8
MAX_CONTEXT_MESSAGES = 6
MAX_CONTEXT_TEXT = 500
# Кэш ответов и список причин пропуска ограничены: сводка памяти и так велика.
MAX_CACHED_RESPONSES = 6
MAX_ERRORS = 8
# Повтор после HTTP 429 в пределах бюджета эпизода: лимит группы общий.
RATE_LIMIT_ATTEMPTS = 3
RATE_LIMIT_WAIT = 20.0
RATE_LIMIT_BUDGET = 120.0
# Пустой ответ (таймаут или сбой сервера) повторяем после короткой паузы.
RATE_RETRY_PAUSE = 2.0

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
DEFAULT_MODEL = "deepseek-flash"
REQUEST_BODY_LIMIT = 64 * 1024


class PolygonRateLimitError(RuntimeError):
    """Общий лимит прокси исчерпан; повтор выполняет вызывающий код."""


def build_context(memory, text):
    """Вернуть ограниченную сводку памяти для разбора текущего сообщения."""
    # Ссылка в тексте опирается на восстановленные поля, идентификаторы и
    # номера ревизий, а также на несколько последних сообщений.
    state = memory.get("event_state")
    state = state if isinstance(state, dict) else {}
    events = []
    for event_id, item in list(state.items())[-MAX_CONTEXT_EVENTS:]:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        events.append({
            "event_id": event_id,
            "revision": item.get("last_revision", 0),
            "cancelled": bool(item.get("cancelled")),
            "fields": fields if isinstance(fields, dict) else {},
        })

    history = memory.get("news_history")
    history = history if isinstance(history, list) else []
    recent_messages = []
    for entry in history[-MAX_CONTEXT_MESSAGES:]:
        if not isinstance(entry, dict):
            continue
        recent_messages.append({
            "day": entry.get("day"),
            "text": str(entry.get("text", ""))[:MAX_CONTEXT_TEXT],
            "patch": entry.get("patch"),
        })

    return {
        "day": memory.get("current_day"),
        "events": events,
        "recent_messages": recent_messages,
    }


def build_prompt(text, context):
    """Вернуть постановку для извлечения одного patch из text."""
    # Точный контракт ответа, сводка памяти и текущее сообщение.
    rules = (
        "Ты разбираешь одно сообщение о событии спроса и возвращаешь одну "
        "версию события.\n"
        "Ответ — только один JSON-объект, без пояснений и без блока Markdown.\n"
        "Верхние поля ровно такие: event_id, revision, operation, issued_day, "
        "fields.\n"
        "- event_id — непустая строка, имя события;\n"
        "- revision — целое число больше нуля, без подмены логическим "
        "значением;\n"
        "- operation — create, update или cancel;\n"
        "- issued_day — абсолютный целый день от 0, когда выпущено сообщение;\n"
        "- create разрешён только для revision 1;\n"
        "- update и cancel начинаются с revision 2;\n"
        "- fields содержит только direction, start, length, factor;\n"
        "- direction равен up или down;\n"
        "- start — абсолютный целый день от 0;\n"
        "- length — целое число больше нуля;\n"
        "- factor — конечное число больше нуля;\n"
        "- direction=up согласуется с factor больше 1;\n"
        "- direction=down согласуется с 0 < factor < 1;\n"
        "- factor == 1 допустим только без direction;\n"
        "- для cancel поле fields пустое;\n"
        "- неизвестное поле не добавляй: null и 0 вместо него не подходят.\n"
        "В fields включай только то, что сообщение называет прямо.\n"
        "День выпуска сообщения и номер редакции — это issued_day и revision, "
        "а не start.\n"
        "start — день начала эффекта, length — названная длительность в днях, "
        "factor — названный множитель: их не додумывай.\n"
        "direction добавляй, только если направление названо словами; иначе "
        "знак задаёт factor.\n"
        "Все дни — абсолютные, отсчитанные от начала эпизода: срок из "
        "сообщения пересчитывай в абсолютный день.\n"
        "Если ссылка на прежнее событие или метаданные версии неоднозначны, "
        "верни ровно JSON null.\n"
    )
    return (
        rules
        + "\nСводка памяти: день, известные события и последние сообщения.\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True)
        + "\n\nТекущее сообщение:\n"
        + text
        + "\n"
    )


def parse_response(raw):
    """Разобрать весь ответ как JSON; при ошибке вернуть None."""
    # Весь ответ должен быть одним JSON-объектом: пояснения вокруг, блок
    # Markdown и склейка двух объектов контрактом не допускаются.
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if data is None:
        # JSON null: ссылка неоднозначна, такой ответ не сохраняем.
        return None
    return data


def validate_patch(data):
    """Вернуть нормализованный patch или None при нарушении контракта."""
    # Возвращаем новую копию: вход не меняется, factor приводится к float.
    if not isinstance(data, dict):
        return None
    if set(data) != PATCH_KEYS:
        return None

    event_id = data["event_id"]
    if not isinstance(event_id, str) or not event_id:
        return None

    revision = data["revision"]
    if type(revision) is not int or revision < 1:
        return None

    operation = data["operation"]
    if operation not in ("create", "update", "cancel"):
        return None
    if operation == "create" and revision != 1:
        return None
    if operation != "create" and revision < 2:
        return None

    issued_day = data["issued_day"]
    if type(issued_day) is not int or issued_day < 0:
        return None

    fields = data["fields"]
    if not isinstance(fields, dict) or set(fields) - FIELD_KEYS:
        return None
    if operation == "cancel" and fields:
        return None

    clean = {}
    direction = fields.get("direction")
    if "direction" in fields:
        if direction not in ("up", "down"):
            return None
        clean["direction"] = direction

    start = fields.get("start")
    if "start" in fields:
        if type(start) is not int or start < 0:
            return None
        clean["start"] = start

    length = fields.get("length")
    if "length" in fields:
        if type(length) is not int or length <= 0:
            return None
        clean["length"] = length

    factor = fields.get("factor")
    if "factor" in fields:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            return None
        value = float(factor)
        if not math.isfinite(value) or value <= 0:
            return None
        # Направление и множитель должны согласовываться в одной версии.
        if direction == "up" and value <= 1:
            return None
        if direction == "down" and value >= 1:
            return None
        clean["factor"] = value

    return {
        "event_id": event_id,
        "revision": revision,
        "operation": operation,
        "issued_day": issued_day,
        "fields": clean,
    }


def _version_key(patch):
    """Канонический вид версии для сравнения повторов и конфликтов."""
    return json.dumps(patch, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _replay_event(event_id, memory):
    """Заново собрать состояние события по журналу в порядке revision."""
    journal = memory.get("patch_journal") or []
    versions = [entry for entry in journal if entry.get("event_id") == event_id]
    versions.sort(key=lambda entry: entry["revision"])

    fields = {}
    created = False
    cancelled = False
    last_revision = 0
    for entry in versions:
        if cancelled:
            # Отмена окончательна: поздние версии не возвращают событие.
            break
        last_revision = entry["revision"]
        if entry.get("operation") == "cancel":
            cancelled = True
            break
        if entry.get("operation") == "create":
            created = True
        for name, value in (entry.get("fields") or {}).items():
            fields[name] = value

    memory.setdefault("event_state", {})[event_id] = {
        "fields": fields,
        "created": created,
        "cancelled": cancelled,
        "last_revision": last_revision,
    }


def apply_patch(patch, memory):
    """Добавить ревизию и заново воспроизвести журнал; вернуть успех."""
    if not isinstance(patch, dict):
        return False
    event_id = patch.get("event_id")
    revision = patch.get("revision")
    if not isinstance(event_id, str) or not event_id or type(revision) is not int:
        return False

    stored = {
        "event_id": event_id,
        "revision": revision,
        "operation": patch.get("operation"),
        "issued_day": patch.get("issued_day"),
        "fields": dict(patch.get("fields") or {}),
    }
    journal = memory.setdefault("patch_journal", [])
    for entry in journal:
        if entry.get("event_id") == event_id and entry.get("revision") == revision:
            if _version_key(entry) == _version_key(stored):
                # Точный повтор версии: журнал не растёт.
                return True
            record_error(
                memory,
                memory.get("current_day", 0),
                f"конфликт ревизии {revision} события {event_id}",
            )
            return False

    journal.append(stored)
    # Журнал хранит версии события по возрастанию revision.
    journal.sort(key=lambda entry: (entry.get("event_id", ""), entry["revision"]))
    _replay_event(event_id, memory)
    return True


def active_events(memory, day):
    """Вернуть полные будущие и текущие события без отменённых и истёкших."""
    state = memory.get("event_state")
    if not isinstance(state, dict):
        return []

    result = []
    for event_id, item in state.items():
        if not isinstance(item, dict):
            continue
        if item.get("created") is not True or item.get("cancelled"):
            continue

        fields = item.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        # Событию нужны create и все три поля: start, length, factor.
        if any(name not in fields for name in REQUIRED_EVENT_FIELDS):
            continue
        start = fields["start"]
        length = fields["length"]
        factor = fields["factor"]
        if type(start) is not int or type(length) is not int:
            continue
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            continue
        factor = float(factor)
        if not math.isfinite(factor) or factor <= 0:
            continue
        if start + length <= day:
            continue

        # direction мог прийти в одной версии, а factor — в другой.
        direction = fields.get("direction")
        if direction is not None:
            if direction not in ("up", "down"):
                continue
            if direction == "up" and factor <= 1:
                continue
            if direction == "down" and factor >= 1:
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

    result.sort(key=lambda event: (event["start"], event["event_id"]))
    return result


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


def _retry_after_seconds(headers):
    """Сколько секунд советует подождать заголовок Retry-After."""
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return None


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
        failure = PolygonRateLimitError(message)
        failure.retry_after = _retry_after_seconds(headers)
        raise failure from None
    except (anthropic.APIError, OSError, ValueError, TypeError, AttributeError):
        return None
    return _response_text(response)


def record_error(memory, day, reason):
    """Сохранить причину пропуска, не выводя её в канал действий."""
    errors = memory.setdefault("errors", [])
    errors.append({"day": day, "reason": str(reason)})
    del errors[:-MAX_ERRORS]


def _context_key(text, context):
    """Ключ зависит и от текста, и от состояния перед его разбором."""
    # Полная сводка в ключе заняла бы больше памяти, чем само решение.
    payload = json.dumps(
        [text, context], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _remember_news(memory, text, day, patch):
    """Оставить только несколько последних сообщений для следующей ссылки."""
    history = memory.setdefault("news_history", [])
    history.append({"day": day, "text": text[:MAX_CONTEXT_TEXT], "patch": patch})
    del history[:-MAX_CONTEXT_MESSAGES]


def _ask_with_retry(prompt, memory):
    """Запросить ответ, повторив запрос после HTTP 429 или пустого ответа.

    Клиент повтор не делает — по замыслу это забота вызывающего кода. Ждём
    столько, сколько советует Retry-After, в пределах общего бюджета эпизода:
    лимит группы делят все участники. Пустой ответ (таймаут или сбой сервера)
    тоже повторяем: новость без него теряется вместе с событием. В бюджет
    входит и время самих неудачных попыток, иначе на медленном сервере прогон
    рискует не уложиться в отведённые минуты.
    """
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        failure = None
        pause = RATE_RETRY_PAUSE
        started = time.monotonic()
        try:
            answer = ask_model(prompt)
        except PolygonRateLimitError as error:
            failure = error
            suggested = getattr(error, "retry_after", None)
            pause = RATE_LIMIT_WAIT if suggested is None else min(
                float(suggested), RATE_LIMIT_WAIT
            )
        else:
            if answer is not None:
                return answer
        if attempt + 1 >= RATE_LIMIT_ATTEMPTS:
            if failure is not None:
                raise failure
            return None
        spent = memory.get("retry_spent", 0.0) + (time.monotonic() - started) + pause
        if spent > RATE_LIMIT_BUDGET:
            if failure is not None:
                raise failure
            return None
        memory["retry_spent"] = round(spent, 1)
        time.sleep(pause)
    return None


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
            raw = _ask_with_retry(build_prompt(text, context), memory)
            if raw is None:
                record_error(memory, day, "модель не вернула ответ")
                return

        patch = validate_patch(parse_response(raw))
        if patch is None:
            # Не сохраняем неоднозначный или повреждённый ответ навсегда.
            record_error(memory, day, "ответ не прошёл проверку")
            return
        # Храним разобранную версию, а не сырой ответ: дешевле и уже проверена.
        responses[key] = _version_key(patch)
        while len(responses) > MAX_CACHED_RESPONSES:
            del responses[next(iter(responses))]
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


def _learn_delivery_lag(observation, memory):
    """Выучить срок поставки по прибытию собственного заказа.

    Условия поставщика меняются по ходу эпизода, а цена и срок в наблюдении не
    приходят числами: сравнение ``incoming`` с прошлым днём выделяет запись,
    созданную последним заказом, и её день прибытия даёт срок.
    """
    incoming = observation.get("incoming")
    incoming = incoming if isinstance(incoming, list) else []

    seen = memory.get("seen_incoming")
    seen = seen if isinstance(seen, list) else []
    remaining = {}
    for arrival, quantity in seen:
        key = f"{arrival}:{quantity}"
        remaining[key] = remaining.get(key, 0) + 1

    fresh = []
    for arrival, quantity in incoming:
        key = f"{arrival}:{quantity}"
        if remaining.get(key):
            remaining[key] -= 1
        else:
            fresh.append((arrival, quantity))

    memory["seen_incoming"] = [[arrival, quantity] for arrival, quantity in incoming]

    order_day = memory.get("last_order_day")
    if len(fresh) == 1 and type(order_day) is int and memory.get("last_order", 0) > 0:
        arrival, quantity = fresh[0]
        lag = arrival - order_day
        if quantity > 0 and 1 <= lag <= 10:
            memory["lag"] = lag


def _correct_demand_belief(memory, observation, day):
    """Убрать перекос оценки спроса на стыке событий.

    Оценка обновляется по вчерашним продажам, но делит их на сегодняшний
    множитель событий. В день после всплеска это завышает спрос в разы, и
    политика закупается и производит впрок. Поправка возвращает множитель
    того дня, к которому относятся продажи.
    """
    seen = observation["last_sales"] + observation["last_lost_sales"]
    if seen <= 0:
        return
    ema = memory.get("ema")
    if not isinstance(ema, (int, float)):
        return

    live_today = 1.0
    for event in active_events(memory, day):
        if event["start"] <= day < event["start"] + event["length"]:
            live_today *= event["factor"]
    live_yesterday = 1.0
    for event in active_events(memory, day - 1):
        if event["start"] <= day - 1 < event["start"] + event["length"]:
            live_yesterday *= event["factor"]
    if live_today == live_yesterday:
        return

    # decide добавит 0.3 * seen / live_today; заранее доводим базу так, чтобы
    # итог соответствовал 0.3 * seen / live_yesterday.
    memory["ema"] = ema + (0.3 / 0.7) * seen * (
        1.0 / live_yesterday - 1.0 / live_today
    )


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

    seen = observation["last_sales"] + observation["last_lost_sales"]
    if seen == 0 and "ema" not in memory:
        # Спрос ещё не наблюдался: осторожная оценка вместо прежних 100.
        memory["ema"] = COLD_START_DEMAND

    if memory["last_order"] > 0:
        spent = (
            memory["prev_cash"] - observation["cash"]
            + observation["last_revenue"]
        )
        guess = spent / memory["last_order"]
        if 20.0 < guess < 90.0:
            memory["unit"] = 0.5 * memory["unit"] + 0.5 * guess

    _learn_delivery_lag(observation, memory)

    for text in observation.get("texts", []):
        if isinstance(text, str) and text.startswith("[рынок]"):
            read_news(text, day, memory)

    try:
        events = active_events(memory, day)
    except NotImplementedError as error:
        record_error(memory, day, error)
        events = []

    _correct_demand_belief(memory, observation, day)

    # Шаг рынка зачисляет сегодняшнюю поставку раньше выпуска, значит это сырьё
    # доступно цеху. Без поправки выпуск считается от старого остатка, а свежая
    # поставка ночует и теряет треть.
    arrivals = observation.get("incoming")
    arrivals = arrivals if isinstance(arrivals, list) else []
    arriving_today = sum(
        quantity for arrival, quantity in arrivals if arrival <= day
    )
    if arriving_today:
        observation = dict(observation, raw=observation["raw"] + arriving_today)

    action = decide(observation, memory, events)
    memory["prev_cash"] = observation["cash"]
    memory["last_order"] = action["order_raw"]
    memory["last_order_day"] = day
    return action


if __name__ == "__main__":
    from polygon import self_check
    self_check(act)
