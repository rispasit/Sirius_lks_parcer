"""
Парсер расписания ЛК Sirius University.

Возвращает ScheduleWeek с событиями ScheduleEvent.
Время в ScheduleEvent доступно в двух форматах:
    ev.start / ev.end         — tz-aware datetime (Europe/Moscow)
    ev.start_ts / ev.end_ts   — unix time (секунды)
    ev.start_iso / ev.end_iso — ISO 8601 строка

Политика:
    - не падаем на кривых данных (одно битое событие не валит всё);
    - не теряем данные молча — пишем warning;
    - тип пары — свободный текст из скобок SUMMARY (никаких белых списков);
    - ScheduleWeek.events всегда отсортирован по (start, uid).
"""

import datetime as dt
import hashlib
import html as html_mod
import json
import logging
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://lks.siriusuniversity.ru/schedule/students/list"
SCHEDULE_ICS = "https://lks.siriusuniversity.ru/schedule/export/students"

LIVEWIRE_COMPONENT = "schedule-student-main"
MSK = ZoneInfo("Europe/Moscow")
WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг",
               "Пятница", "Суббота", "Воскресенье"]

SCHEMA_VERSION = 1

# маркер для unescape — null + SOH, не встречается в тексте ICS
_UNESCAPE_MARKER = "\x00\x01"


# =========================================================== ScheduleEvent

@dataclass(slots=True)
class ScheduleEvent:
    """Одна пара в расписании.

    Время хранится как tz-aware datetime в Europe/Moscow.
    Альтернативные представления — через свойства (start_ts, start_iso и т.д.),
    так что downstream может брать то, что удобно для мержа.
    """

    uid: str
    start: dt.datetime
    end: dt.datetime
    subject: str | None = None
    type: str | None = None
    room: str | None = None
    teachers: str | None = None
    group: str | None = None
    all_day: bool = False

    def __post_init__(self):
        if self.start.tzinfo is None:
            raise ValueError("start must be tz-aware")
        if self.end.tzinfo is None:
            raise ValueError("end must be tz-aware")
        if self.end <= self.start:
            raise ValueError(
                f"end ({self.end}) must be after start ({self.start})"
            )

    # -------- время в двух форматах --------

    @property
    def start_ts(self) -> int:
        """Unix time (секунды) начала."""
        return int(self.start.timestamp())

    @property
    def end_ts(self) -> int:
        """Unix time (секунды) конца."""
        return int(self.end.timestamp())

    @property
    def start_iso(self) -> str:
        """ISO 8601 с поясом. Секунды — только если != 0."""
        if self.start.second == 0 and self.start.microsecond == 0:
            return self.start.isoformat(timespec="minutes")
        return self.start.isoformat(timespec="seconds")

    @property
    def end_iso(self) -> str:
        if self.end.second == 0 and self.end.microsecond == 0:
            return self.end.isoformat(timespec="minutes")
        return self.end.isoformat(timespec="seconds")

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    # -------- сериализация --------

    def to_dict(self) -> dict:
        """Единая схема: и ISO, и Unix time всегда."""
        return {
            "uid":          self.uid,
            "start":        self.start_iso,
            "end":          self.end_iso,
            "start_ts":     self.start_ts,
            "end_ts":       self.end_ts,
            "all_day":      self.all_day,
            "duration_min": self.duration_min,
            "subject":      self.subject,
            "type":         self.type,
            "room":         self.room,
            "teachers":     self.teachers,
            "group":        self.group,
        }


# =========================================================== ScheduleWeek

@dataclass(slots=True)
class ScheduleWeek:
    """Одна неделя расписания.

    events всегда отсортирован по (start, uid) — при создании.
    """

    week_start: dt.date
    week_end:   dt.date
    source:     str                              # 'ics' | 'livewire'
    events:     list[ScheduleEvent]

    def __post_init__(self):
        self.events.sort(key=lambda e: (e.start, e.uid))

    def __iter__(self):
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def sorted(self) -> list[ScheduleEvent]:
        """Возвращает копию, отсортированную по (start, uid)."""
        return sorted(self.events, key=lambda e: (e.start, e.uid))

    def to_dict(self) -> dict:
        return {
            "week_start":    self.week_start.isoformat(),
            "week_end":      self.week_end.isoformat(),
            "week_start_ts": _day_start_ts(self.week_start),
            "week_end_ts":   _day_end_ts(self.week_end),
            "source":        self.source,
            "events":        [e.to_dict() for e in self.events],
        }


# =========================================================== helpers: время

def _day_start_ts(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=MSK).timestamp())


def _day_end_ts(d: dt.date) -> int:
    """Последняя секунда дня (23:59:59 MSK)."""
    return _day_start_ts(d + dt.timedelta(days=1)) - 1


def _hash_uid(*parts: str) -> str:
    """Стабильный uid. Разделитель — \\x00, не встречается в тексте."""
    key = "\x00".join(p or "" for p in parts)
    return "h-" + hashlib.sha1(key.encode()).hexdigest()[:16]


# =========================================================== helpers: ICS

def _unescape_ics(s: str) -> str:
    """Раскрывает escape-последовательности ICS (порядок важен)."""
    s = s.replace("\\\\", _UNESCAPE_MARKER)
    s = (s.replace("\\n", "\n")
          .replace("\\N", "\n")
          .replace("\\,", ",")
          .replace("\\;", ";"))
    return s.replace(_UNESCAPE_MARKER, "\\")


def _unfold_ics(text: str) -> list[str]:
    """Склеивает folded-строки (продолжение начинается с пробела/таба)."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


_warned_tzids: set[str] = set()
_failed_ics_dts = 0


def _parse_ics_datetime(value: str,
                        tzid: str | None = None) -> dt.datetime | None:
    """'20261201T084500' | '...Z' | '20261201' -> aware datetime в MSK."""
    global _failed_ics_dts

    if not value or not value.strip():
        return None
    value = value.strip()

    has_z = value.endswith("Z")

    if (tzid and not has_z
            and tzid not in ("Europe/Moscow", "MSK")
            and tzid not in _warned_tzids):
        logger.warning("неизвестный TZID=%r, трактую как MSK", tzid)
        _warned_tzids.add(tzid)

    tz = MSK
    if has_z:
        value = value[:-1]
        tz = dt.timezone.utc

    if len(value) >= 13 and value[8] == "T":
        fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
        try:
            naive = dt.datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            if _failed_ics_dts < 3:
                logger.warning(
                    "не распарсил ICS-время: %r — событие пропущено", value
                )
            _failed_ics_dts += 1
            return None
        return naive.replace(tzinfo=tz).astimezone(MSK)

    if len(value) == 8:
        try:
            d = dt.datetime.strptime(value, "%Y%m%d")
        except (ValueError, TypeError):
            if _failed_ics_dts < 3:
                logger.warning(
                    "не распарсил ICS-дату: %r — событие пропущено", value
                )
            _failed_ics_dts += 1
            return None
        return d.replace(tzinfo=MSK)

    if _failed_ics_dts < 3:
        logger.warning(
            "неизвестный формат ICS-времени: %r — событие пропущено", value
        )
    _failed_ics_dts += 1
    return None


def _split_summary(summary: str) -> tuple[str, str | None]:
    """'Пример дисциплины (Практические занятия)'
       -> ('Пример дисциплины', 'Практические занятия').

    Скобки есть — тип извлекаем. Скобок нет или они пустые — type=None.
    Никаких белых списков: вуз может писать любой тип.
    """
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", summary)
    if m:
        subject = m.group(1).strip()
        typ = m.group(2).strip()
        return subject, (typ or None)
    return summary.strip(), None


def _parse_description(desc: str) -> tuple[str | None, str | None]:
    """Возвращает (teachers, group) из DESCRIPTION."""
    teachers_parts: list[str] = []
    group: str | None = None

    for line in desc.splitlines():
        low = line.lower().lstrip()
        if low.startswith("преподавател"):
            if ":" in line:
                part = line.split(":", 1)[1].strip()
                if part:
                    teachers_parts.append(part)
        elif low.startswith("группа"):
            if ":" in line:
                group = line.split(":", 1)[1].strip() or None

    return ("; ".join(teachers_parts) or None), group


def _normalize_room(room: str | None) -> str | None:
    """Убирает дублирующийся суффикс в названии аудитории.

    'Корпус 1, Аудитория 1.1 Аудитория 1.1' -> 'Корпус 1, Аудитория 1.1'
    'A B A B A B' -> 'A B'
    """
    if not room:
        return room
    words = room.split()
    changed = True
    while changed:
        changed = False
        for length in range(len(words) // 2, 0, -1):
            if words[-length:] == words[-2 * length:-length]:
                words = words[:-length]
                changed = True
                break
    return " ".join(words)


def _event_from_ics(cur: dict) -> ScheduleEvent | None:
    dtstart_raw = cur.get("DTSTART")
    dtend_raw   = cur.get("DTEND")
    if not dtstart_raw:
        return None

    start = _parse_ics_datetime(dtstart_raw, cur.get("DTSTART_TZID"))
    if start is None:
        return None

    all_day = "T" not in dtstart_raw

    end = _parse_ics_datetime(dtend_raw, cur.get("DTEND_TZID")) if dtend_raw else None
    if end is None:
        end = start + (dt.timedelta(days=1) if all_day
                       else dt.timedelta(minutes=90))
    else:
        if end < start:
            end += dt.timedelta(days=1)      # переход через полночь
        if end <= start:                     # защита от нулевой длительности
            end = start + dt.timedelta(minutes=1)

    subject, event_type = _split_summary(cur.get("SUMMARY", ""))
    teachers, group = _parse_description(cur.get("DESCRIPTION", ""))

    return ScheduleEvent(
        uid=cur.get("UID") or _hash_uid(dtstart_raw, cur.get("SUMMARY", "")),
        start=start,
        end=end,
        subject=subject or None,
        type=event_type,
        room=_normalize_room(cur.get("LOCATION")),
        teachers=teachers,
        group=group,
        all_day=all_day,
    )


def _parse_ics(text: str) -> list[ScheduleEvent]:
    lines = _unfold_ics(text)
    events: list[ScheduleEvent] = []
    seen: set[str] = set()
    current: dict | None = None
    depth = 0

    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is None:
                current = {}
                depth = 0
            else:
                # вложенный VEVENT — невалидно по RFC, игнорируем открытие,
                # чтобы не потерять уже накопленные поля внешнего
                depth += 1
        elif line == "END:VEVENT":
            if depth > 0:
                depth -= 1
                continue
            if current is not None:
                ev = _event_from_ics(current)
                if ev and ev.uid not in seen:
                    events.append(ev)
                    seen.add(ev.uid)
            current = None
        elif current is not None:
            if line.startswith("BEGIN:"):
                depth += 1
            elif line.startswith("END:"):
                depth = max(0, depth - 1)
            elif depth == 0 and ":" in line:
                k, _, v = line.partition(":")
                parts = k.split(";")
                key = parts[0].strip()
                if not key:
                    continue
                tzid = None
                for p in parts[1:]:
                    if p.upper().startswith("TZID="):
                        tzid = p[5:].strip()
                        break
                current[key] = _unescape_ics(v.strip())
                if tzid:
                    current[f"{key}_TZID"] = tzid

    # обрезанный файл — VEVENT без END:VEVENT
    if current is not None:
        ev = _event_from_ics(current)
        if ev and ev.uid not in seen:
            events.append(ev)
            seen.add(ev.uid)
            logger.warning(
                "ICS обрезан: последний VEVENT без END:VEVENT, но обработан"
            )

    events.sort(key=lambda e: (e.start, e.uid))
    return events


# =========================================================== helpers: Livewire

_failed_lw_dates = 0


def _parse_lw_date(value) -> dt.date | None:
    global _failed_lw_dates
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    if _failed_lw_dates < 3:
        logger.warning("не распознал дату Livewire: %r — событие пропущено", value)
    _failed_lw_dates += 1
    return None


def _parse_lw_time(value) -> dt.time | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            continue
    logger.debug("не распознал время Livewire: %r", value)
    return None


def _event_from_livewire(e: dict) -> ScheduleEvent | None:
    d = _parse_lw_date(e.get("date", ""))
    if d is None:
        return None

    t_start = _parse_lw_time(e.get("startTime"))
    t_end   = _parse_lw_time(e.get("endTime"))
    all_day = t_start is None

    if all_day:
        start = dt.datetime(d.year, d.month, d.day, tzinfo=MSK)
        end = start + dt.timedelta(days=1)
    else:
        start = dt.datetime(d.year, d.month, d.day,
                            t_start.hour, t_start.minute, tzinfo=MSK)
        if t_end is None:
            end = start + dt.timedelta(minutes=90)
        else:
            end = dt.datetime(d.year, d.month, d.day,
                              t_end.hour, t_end.minute, tzinfo=MSK)
            if end < start:
                end += dt.timedelta(days=1)      # переход через полночь
            if end <= start:                     # нулевая длительность
                end = start + dt.timedelta(minutes=1)

    uid = e.get("id") or _hash_uid(
        e.get("date", ""), e.get("startTime", ""),
        e.get("discipline", ""), e.get("groupType", ""),
    )

    return ScheduleEvent(
        uid=str(uid),
        start=start,
        end=end,
        subject=e.get("discipline") or None,
        type=e.get("groupType") or None,
        room=_normalize_room(e.get("classroom")),
        teachers=e.get("teachers") or None,
        group=e.get("group") or None,
        all_day=all_day,
    )


# =========================================================== fetch

def _fetch_ics(session: requests.Session,
               monday: dt.date, sunday: dt.date) -> list[ScheduleEvent]:
    params = {
        "from": monday.isoformat(),
        "to":   sunday.isoformat(),
        "lectures":     1,
        "seminars":     1,
        "practices":    1,
        "laboratories": 1,
        "exams":        1,
        "other":        1,
    }
    r = session.get(SCHEDULE_ICS, params=params, timeout=20)
    r.raise_for_status()

    body = r.text.lstrip("\ufeff \t\r\n")
    if not body.startswith("BEGIN:VCALENDAR"):
        raise RuntimeError(
            f"ICS вернул не ICS (HTTP {r.status_code}, "
            f"content-type={r.headers.get('Content-Type')})"
        )

    events = _parse_ics(body)
    logger.info("ICS: %d событий", len(events))
    return events


def _extract_livewire(session: requests.Session) -> dict | None:
    r = session.get(SCHEDULE_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    found: list[dict] = []
    for div in soup.find_all("div", attrs={"wire:initial-data": True}):
        raw = html_mod.unescape(div["wire:initial-data"])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("fingerprint", {}).get("name") == LIVEWIRE_COMPONENT:
            found.append(data.get("serverMemo", {}).get("data", {}))

    if not found:
        return None
    if len(found) > 1:
        logger.warning(
            "найдено %d компонентов %r, использую первый",
            len(found), LIVEWIRE_COMPONENT,
        )
    return found[0]


def _fetch_livewire(session: requests.Session,
                    monday: dt.date,
                    sunday: dt.date) -> list[ScheduleEvent] | None:
    data = _extract_livewire(session)
    if data is None:
        logger.warning("Livewire-компонент %r не найден", LIVEWIRE_COMPONENT)
        return None

    raw = data.get("events") or []
    if not isinstance(raw, list):
        logger.warning("Livewire: events не список: %r", type(raw))
        return None

    events: list[ScheduleEvent] = []
    seen: set[str] = set()

    for e in raw:
        if not isinstance(e, dict):
            continue
        ev = _event_from_livewire(e)
        if ev is None:
            continue
        if not (monday <= ev.start.date() <= sunday):
            continue
        if ev.uid in seen:
            continue
        events.append(ev)
        seen.add(ev.uid)

    events.sort(key=lambda e: (e.start, e.uid))
    logger.info("Livewire: %d событий в неделе", len(events))
    return events


# =========================================================== public

def _week_range(week: str,
                reference_date: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Возвращает (понедельник, воскресенье) для 'current' или 'next'.

    Raises:
        ValueError: если week не 'current' и не 'next'.
    """
    if week not in ("current", "next"):
        raise ValueError(f"week must be 'current' or 'next', got {week!r}")

    today = reference_date or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    if week == "next":
        monday += dt.timedelta(days=7)
    return monday, monday + dt.timedelta(days=6)


def get_week(session: requests.Session,
             week: str = "current",
             reference_date: dt.date | None = None) -> ScheduleWeek:
    """Расписание одной недели.

    week: "current" | "next"
    """
    monday, sunday = _week_range(week, reference_date)
    logger.info("неделя %s: %s — %s", week, monday, sunday)

    try:
        events = _fetch_ics(session, monday, sunday)
        return ScheduleWeek(monday, sunday, "ics", events)
    except (requests.RequestException, RuntimeError) as e:
        logger.warning("ICS не сработал (%s), пробую Livewire", e)

    try:
        events = _fetch_livewire(session, monday, sunday)
    except requests.RequestException as e:
        raise RuntimeError(f"Livewire тоже упал: {e}") from e

    if events is not None:
        return ScheduleWeek(monday, sunday, "livewire", events)

    raise RuntimeError("не удалось получить расписание")


def get_schedule(session: requests.Session,
                 week: str = "current",
                 reference_date: dt.date | None = None) -> list[ScheduleEvent]:
    """Удобный шорткат: сразу список событий, без обёртки недели."""
    return get_week(session, week, reference_date).sorted()