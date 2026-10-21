"""Журнал версий событий спроса.

Версия задаётся словарём {"event_id", "revision", "operation", "fields"}.
operation: "create" (ревизия 1), "update" или "cancel" (ревизии от 2).
Версии одного события могут приходить в любом порядке и повторяться.
"""


def add(journal, version):
    """Добавить версию в журнал; вернуть (новый журнал, статус).

    Статус: "added", если версия новая; "duplicate", если точно такая уже есть;
    "conflict", если есть другая версия с теми же event_id и revision (журнал
    при этом не меняется).
    """
    key = (version["event_id"], version["revision"])
    for existing in journal:
        if (existing["event_id"], existing["revision"]) == key:
            return list(journal), ("duplicate" if existing == version else "conflict")
    return list(journal) + [version], "added"


def state(journal, event_id):
    """Текущее состояние события: поля после всех версий по возрастанию ревизии.

    Возвращает None, если нет create, если событие отменено, или если create
    ещё не пришёл. Поля update перекрывают поля предыдущих версий. Отмена
    окончательна: версии с большей ревизией её не отменяют.
    """
    versions = sorted((v for v in journal if v["event_id"] == event_id),
                      key=lambda v: v["revision"])
    if not versions or versions[0]["operation"] != "create" or versions[0]["revision"] != 1:
        return None
    fields = {}
    for version in versions:
        if version["operation"] == "cancel":
            fields = {}
            continue
        fields.update(version["fields"])
    return fields


def active(journal, day):
    """Идентификаторы событий, которые идут в день day или начнутся позже.

    Событие с полями start и length идёт в дни start … start + length − 1.
    Неполные события (без start или length) не учитываются. Сортировка идёт по start,
    при равенстве по event_id.
    """
    found = []
    for event_id in sorted({v["event_id"] for v in journal}):
        fields = state(journal, event_id)
        if not fields or "start" not in fields or "length" not in fields:
            continue
        if fields["start"] + fields["length"] > day:
            found.append((fields["start"], event_id))
    return [event_id for _, event_id in sorted(found)]
