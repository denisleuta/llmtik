"""
Набор для работы над «Полигоном».

Что здесь есть:
    local_episode(agent)    — прогнать агента на учебном сиде и увидеть итог;
    self_check(agent)       — пять эпизодов, прибыль и баллы с учётом памяти;
    self_check(agent, final=True) — то же для финального тура, с приписками;
    arena_check(agent)      — общий рынок: вы против панели соперников;
    replay(agent, seed)     — подробный дневник эпизода: что пришло, что решили;
    ask_model(text)         — обратиться к языковой модели курса;
    submit("solution.py")   — отправить решение на боевой прогон.

Учебные сиды отличаются от боевых. Самопроверка показывает порядок величин,
а не будущее место в таблице: рынки разные, и удачный эпизод ничего не доказывает.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import arena as arena_engine
import market
from texts import Renderer
from market_v2 import Market as MarketV2
from texts_v2 import Renderer as RendererV2
from memory_policy import MAX_FRAME_BYTES, MAX_STATE_BYTES, MemoryPolicyError
from memory_policy import memory_size, score_with_memory

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
DEFAULT_MODEL = "deepseek-flash"
REQUEST_BODY_LIMIT = 64 * 1024
MAX_TOKENS = 1024
DEV_SEEDS = (24, 35, 78, 96, 109)


class PolygonRateLimitError(RuntimeError):
    """Общий лимит прокси исчерпан; повтор выполняет вызывающий код."""


def _load_local_env():
    """Загрузить только .env рядом с этим файлом; окружение имеет приоритет."""
    path = Path(__file__).with_name(".env")
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError("установите зависимости: python -m pip install -r requirements.txt") from error
    load_dotenv(path, override=False)


def _polygon_url() -> str:
    """Прочитать адрес стенда при вызове, чтобы блокнот мог задать его позже."""
    _load_local_env()
    return (os.environ.get("POLYGON_URL", "").strip()
            or PUBLIC_POLYGON_URL).rstrip("/")


def _transport_settings():
    """Прочитать настройки модели при вызове, не сохраняя токен в модуле."""
    _load_local_env()
    polygon_token = os.environ.get("POLYGON_TOKEN", "").strip()
    if polygon_token:
        return _polygon_url(), polygon_token, DEFAULT_MODEL

    # Совместимость с изолированным запуском на стенде. Адрес и токен
    # предоставляет сам сервер; студенту нужен только POLYGON_TOKEN.
    legacy_url = os.environ.get("LLM_BASE_URL", "").strip()
    legacy_key = os.environ.get("LLM_API_KEY", "").strip()
    if legacy_url and legacy_key:
        return legacy_url.rstrip("/"), legacy_key, DEFAULT_MODEL
    return _polygon_url(), "", DEFAULT_MODEL


def _load_anthropic():
    try:
        import anthropic
    except ImportError as error:
        raise RuntimeError(
            "не установлен пакет anthropic; выполните "
            "python -m pip install -r requirements.txt"
        ) from error
    return anthropic


def _message_payload(prompt: str, model: str, max_tokens: int):
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "extra_body": {"temperature": 0.0},
    }


def _rate_limit_message(headers) -> str:
    retry_after = None
    if headers is not None:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after is None:
        retry_after = "не указан"
    else:
        retry_after = "".join(ch for ch in str(retry_after) if ch.isprintable())[:80]
        retry_after = retry_after or "не указан"
    return ("Прокси Полигона вернул HTTP 429: общий лимит 30 запросов/мин "
            f"исчерпан; Retry-After: {retry_after}")


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


def ask_model(prompt: str, max_tokens: int = 300) -> str:
    """Задать вопрос deepseek-flash через нативный Messages API Полигона."""
    base_url, token, model = _transport_settings()
    if not token:
        raise RuntimeError(
            "нужен токен Полигона. В начале блокнота:\n"
            "    import os; os.environ['POLYGON_TOKEN'] = 'выданный токен'")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("запрос модели не должен быть пустым")
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
        raise ValueError("max_tokens должен быть целым числом от 1 до 1024")
    payload = _message_payload(prompt, model, max_tokens)
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > REQUEST_BODY_LIMIT:
        raise ValueError("запрос модели превышает 64 КиБ")

    anthropic = _load_anthropic()
    client = anthropic.Anthropic(
        api_key=token,
        base_url=base_url,
        timeout=60.0,
        max_retries=0,
    )
    try:
        response = client.messages.create(**payload)
    except anthropic.RateLimitError as error:
        headers = getattr(getattr(error, "response", None), "headers", None)
        message = _rate_limit_message(headers)
        print(message, file=sys.stderr)
        raise PolygonRateLimitError(message) from None
    content = _response_text(response)
    if content is None:
        raise ValueError("прокси модели вернул ответ без текстового блока content")
    return content


def _restart_local_agent(act, memory):
    import importlib.util
    import inspect
    path = inspect.getsourcefile(act)
    if not path:
        raise ValueError("для перезапуска определите act в файле solution.py")
    # Читаем исходник напрямую: обычный импорт берёт кэш __pycache__, и при
    # перезаписи файла тем же размером в ту же секунду вернётся старый байткод.
    name = "polygon_restarted_solution"
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = path
    sys.modules[name] = module
    with open(path, "rb") as stream:
        source = importlib.util.decode_source(stream.read())
    exec(compile(source, path, "exec"), module.__dict__)
    return module.act, json.loads(json.dumps(memory, allow_nan=False))


def _run_local_episode(act, seed, days, verbose, final, rules_version):
    """Вернуть прибыль и пик JSON-состояния; v1/v2 не ограничивают память."""
    if rules_version not in (1, 2, 3):
        raise ValueError("неизвестная версия правил")
    version = 1 if final else rules_version
    game = MarketV2(seed, days) if version in (2, 3) else market.Market(seed, days)
    renderer = RendererV2() if version in (2, 3) else Renderer(injections=final)
    memory = {}
    peak = 0
    while not game.done:
        observation = game.observation(game.texts_for_day(renderer))
        if version in (2, 3):
            observation['rules_version'] = version
            if version == 3 or game.restart_before_day(game.day):
                act, memory = _restart_local_agent(act, memory)
        action = act(observation, memory) or {}
        if version == 3:
            # Проверяем каждый ответ до шага рынка, включая последний день.
            state_bytes = memory_size(memory)
            if state_bytes > MAX_STATE_BYTES:
                raise MemoryPolicyError(f"memory превышает {MAX_STATE_BYTES} байт")
            peak = max(peak, state_bytes)
            if type(action) is not dict:
                raise MemoryPolicyError("действие act должно быть JSON-словарём")
            frame = {"action": action, "memory": memory}
            if memory_size(frame) + 1 > MAX_FRAME_BYTES:
                raise MemoryPolicyError(f"ответ act превышает {MAX_FRAME_BYTES} байт")
            # Как в серверном обмене: рынок и следующий вызов получают JSON-копии.
            restored = json.loads(json.dumps(frame, ensure_ascii=False, allow_nan=False))
            action, memory = restored["action"], restored["memory"]
        summary = game.step(action)
        if verbose:
            print(f"день {summary['day']:>2} · продано {summary['sales']:>4} · "
                  f"упущено {summary['lost']:>4} · касса {summary['cash']:>9.0f}")
    return game.profit, peak


def _print_memory_score(result):
    print(f"баллы за прибыль: {result['raw_total']:.2f}")
    print(f"пик JSON-состояния: {result['state_peak_bytes']} байт")
    print(f"фиксированное списание: {result['memory_penalty']} баллов")
    print(f"итог: {result['total']:.2f} баллов")


def local_episode(act, seed: int = DEV_SEEDS[0], days: int = 60,
                  verbose: bool = False, final: bool = False,
                  rules_version: int = 3):
    """Прибыль эпизода; v3 восстанавливает JSON-состояние перед каждым act.

    При verbose=True также выводятся баллы и пик JSON-состояния. Ограничение
    RSS процесса действует на сервере, но не имитируется локально.
    """
    profit, peak = _run_local_episode(act, seed, days, verbose, final, rules_version)
    if verbose and not final and rules_version == 3:
        _print_memory_score(score_with_memory([profit], peak))
    return profit


def self_check(act, seeds=DEV_SEEDS, verbose: bool = True,
               final: bool = False, rules_version: int = 3) -> float | dict:
    """В v3 — баллы по общему пику состояния; в v1/v2 — средняя прибыль."""
    if rules_version == 3 and not final:
        seeds = tuple(seeds)
        if not seeds:
            raise ValueError("для самопроверки нужен хотя бы один эпизод")
        runs = [_run_local_episode(act, seed, 60, False, False, 3) for seed in seeds]
        result = score_with_memory([profit for profit, _ in runs],
                                   max(peak for _, peak in runs))
        if verbose:
            for seed, (profit, _) in zip(seeds, runs):
                print(f"сид {seed:>4}: {profit:>10.0f}")
            print(f"средняя прибыль: {result['profit']:.0f}")
            _print_memory_score(result)
        return result
    values = [local_episode(act, s, final=final, rules_version=rules_version) for s in seeds]
    mean = sum(values) / len(values)
    if verbose:
        for seed, value in zip(seeds, values):
            print(f"сид {seed:>4}: {value:>10.0f}")
        print(f"среднее:  {mean:>10.0f}   (баллы = среднее / 1000 = {mean/1000:.1f})")
        print("Планка на таблице лидеров — 240. Её держит агент, который тексты "
              "не читает вовсе.")
        if final:
            print("Финальный тур: планка та же. Если ваш результат просел "
                  "относительно обычного тура — просадку вы устроили себе сами.")
    return mean


def _panel():
    """Постоянные соперники общего рынка: числовой, узкий разбор, демпингующий."""
    import importlib
    import os
    import sys

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel")
    if here not in sys.path:
        sys.path.insert(0, here)
    return [importlib.import_module(name) for name in ("baseline", "solver", "dumper")]


def arena_episode(act, seed: int = DEV_SEEDS[0], days: int = 60, verbose: bool = False):
    """Один эпизод общего рынка: вы — фирма номер ноль, остальные из панели."""
    rivals = _panel()
    game = arena_engine.Arena(seed, days, firms=1 + len(rivals))
    renderer = Renderer()
    mine: dict = {}
    theirs = [{} for _ in rivals]
    while not game.done:
        texts = game.texts_for_day(renderer)
        actions = [act(game.observation(0, texts), mine) or {}]
        for i, rival in enumerate(rivals):
            actions.append(rival.act(game.observation(i + 1, texts), theirs[i]))
        summary = game.step(actions)
        if verbose:
            print(f"день {summary['day']:>2} · ваша цена {summary['prices'][0]:>6.1f} "
                  f"· цены соседей {summary['prices'][1:]} · продано {summary['sales'][0]:>4} "
                  f"· упущено {summary['lost'][0]:>4}")
    return [game.profit(i) for i in range(1 + len(rivals))]


def arena_check(act, seeds=DEV_SEEDS[:3], verbose: bool = True) -> float:
    """Средняя прибыль на общем рынке — то же, что считает оценщик для c6."""
    names = ["вы", "числовой", "узкий разбор", "демпингующий"]
    rows = [arena_episode(act, s) for s in seeds]
    means = [sum(r[i] for r in rows) / len(rows) for i in range(len(names))]
    if verbose:
        for name, value in zip(names, means):
            print(f"{name:<14}{value:>11.0f}   ({value/1000:.1f} баллов)")
        print("Планка на таблице лидеров — 191. Столько набирает числовой агент "
              "против этой же панели.")
    return means[0]


def replay(act, seed: int = DEV_SEEDS[0], final: bool = False, rules_version: int = 3):
    """Подробный дневник эпизода: удобно смотреть, где потеряли продажи."""
    return local_episode(act, seed, verbose=True, final=final, rules_version=rules_version)


def submit(path: str = "solution.py", token: str = "", challenge: str = "c4", rules_version: int | None = None):
    """Отправить файл решения на боевой прогон.

    challenge="c4" — обычный тур, "c5" — финальный с недобросовестными текстами,
    "c6" — общий рынок против панели соперников. Если token пуст, используется
    POLYGON_TOKEN из окружения.
    """
    import requests

    _load_local_env()
    token = token.strip() if isinstance(token, str) else ""
    token = token or os.environ.get("POLYGON_TOKEN", "").strip()
    if not token:
        raise ValueError("нужен POLYGON_TOKEN или аргумент token")
    if not os.path.exists(path):
        raise FileNotFoundError(f"нет файла {path}")
    server_url = _polygon_url()
    selected = (3 if challenge == "c4" else 1) if rules_version is None else rules_version
    status = requests.get(f"{server_url}/health", timeout=15)
    status.raise_for_status()
    active = status.json().get("rules_versions", {}).get(challenge)
    if active != selected:
        raise ValueError(f"стенд ещё не принимает {challenge} версии {selected}; отправка не выполнена")
    with open(path, "rb") as f:
        response = requests.post(
            f"{server_url}/submit",
            headers={"Authorization": f"Bearer {token}"},
            files={"solution": (os.path.basename(path), f, "text/x-python")},
            data={"challenge": challenge, "rules_version": str(selected)},
            timeout=60)
    if response.status_code == 429:
        message = _rate_limit_message(response.headers)
        print(message, file=sys.stderr)
        raise PolygonRateLimitError(message)
    response.raise_for_status()
    data = response.json()
    print(f"принято, номер задания {data.get('job_id')}. "
          f"Результат появится в таблице лидеров: {server_url}/leaderboard")
    return data
