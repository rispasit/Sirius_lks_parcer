"""
Автотесты для parser.py.

Все имена, группы, аудитории и UID — вымышленные.

Запуск:
    pytest
    pytest --cov=parser --cov-report=term-missing
"""

import datetime as dt
import html
import json
import logging
from unittest.mock import MagicMock

import pytest

from parser import (
    MSK, SCHEMA_VERSION, WEEKDAYS_RU,
    ScheduleEvent, ScheduleWeek,
    _day_start_ts, _day_end_ts, _hash_uid,
    _unescape_ics, _unfold_ics, _parse_ics_datetime,
    _split_summary, _parse_description, _normalize_room,
    _event_from_ics, _parse_ics,
    _parse_lw_date, _parse_lw_time, _event_from_livewire,
    _extract_livewire, _fetch_ics, _fetch_livewire,
    _week_range, get_week, get_schedule,
)


# =========================================================== fixtures

@pytest.fixture
def simple_event():
    return ScheduleEvent(
        uid="uid-1",
        start=dt.datetime(2026, 12, 1, 8, 45, tzinfo=MSK),
        end=dt.datetime(2026, 12, 1, 10, 5, tzinfo=MSK),
        subject="Пример дисциплины",
        type="Лекции",
        room="Аудитория 1.1",
        teachers="Иванов Иван Иванович",
        group="ГРУППА-00/0",
    )


def make_response(text: str,
                  status: int = 200,
                  content_type: str = "text/calendar") -> MagicMock:
    r = MagicMock()
    r.text = text
    r.status_code = status
    r.headers = {"Content-Type": content_type}
    r.raise_for_status = MagicMock()
    return r


def build_livewire_html(events: list[dict],
                        component: str = "schedule-student-main") -> str:
    payload = {
        "fingerprint": {"name": component},
        "serverMemo": {
            "data": {
                "events": events,
                "month": {"number": "12", "full": "Декабрь"},
                "year": "2026",
            }
        },
    }
    encoded = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return (
        '<html><body><div wire:id="x" '
        f'wire:initial-data="{encoded}"></div></body></html>'
    )


def build_multi_livewire_html(payloads: list[dict]) -> str:
    """Несколько wire:initial-data на одной странице."""
    parts = []
    for p in payloads:
        encoded = html.escape(json.dumps(p, ensure_ascii=False), quote=True)
        parts.append(f'<div wire:id="x" wire:initial-data="{encoded}"></div>')
    return f'<html><body>{"".join(parts)}</body></html>'


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:spatie/icalendar-generator
BEGIN:VTIMEZONE
TZID:Europe/Moscow
BEGIN:STANDARD
DTSTART:20260228T084500
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:00000000-0000-0000-0000-000000000001
SUMMARY:Пример дисциплины (Лекции)
DESCRIPTION:Преподаватели: Иванов Иван Иванович\\nГруппа: ГРУППА-00/0
LOCATION:Корпус 1\\, Аудитория 1.1 Аудитория 1.1
DTSTART;TZID=Europe/Moscow:20261201T084500
DTEND;TZID=Europe/Moscow:20261201T100500
END:VEVENT
BEGIN:VEVENT
UID:00000000-0000-0000-0000-000000000002
SUMMARY:Другая дисциплина (Семинар)
DESCRIPTION:Преподаватели: Петров Петр Петрович\\nГруппа: ГРУППА-00/0
LOCATION:Аудитория 2.2
DTSTART;TZID=Europe/Moscow:20261201T104500
DTEND;TZID=Europe/Moscow:20261201T121500
END:VEVENT
END:VCALENDAR
"""


# =========================================================== ScheduleEvent

class TestScheduleEvent:

    def test_start_ts(self, simple_event):
        expected = int(dt.datetime(2026, 12, 1, 8, 45, tzinfo=MSK).timestamp())
        assert simple_event.start_ts == expected

    def test_end_ts(self, simple_event):
        expected = int(dt.datetime(2026, 12, 1, 10, 5, tzinfo=MSK).timestamp())
        assert simple_event.end_ts == expected

    def test_start_iso_has_tz(self, simple_event):
        assert simple_event.start_iso == "2026-12-01T08:45+03:00"

    def test_start_iso_with_seconds(self):
        ev = ScheduleEvent(
            uid="x",
            start=dt.datetime(2026, 12, 1, 8, 45, 30, tzinfo=MSK),
            end=dt.datetime(2026, 12, 1, 10, 5, 30, tzinfo=MSK),
        )
        assert ev.start_iso == "2026-12-01T08:45:30+03:00"

    def test_duration_min(self, simple_event):
        assert simple_event.duration_min == 80

    def test_naive_start_raises(self):
        with pytest.raises(ValueError, match="tz-aware"):
            ScheduleEvent(
                uid="x",
                start=dt.datetime(2026, 12, 1, 9),
                end=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
            )

    def test_naive_end_raises(self):
        with pytest.raises(ValueError, match="tz-aware"):
            ScheduleEvent(
                uid="x",
                start=dt.datetime(2026, 12, 1, 9, tzinfo=MSK),
                end=dt.datetime(2026, 12, 1, 10),
            )

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError, match="must be after"):
            ScheduleEvent(
                uid="x",
                start=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
                end=dt.datetime(2026, 12, 1, 9, tzinfo=MSK),
            )

    def test_end_equal_start_raises(self):
        with pytest.raises(ValueError, match="must be after"):
            ScheduleEvent(
                uid="x",
                start=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
                end=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
            )

    def test_to_dict_has_all_fields(self, simple_event):
        d = simple_event.to_dict()
        for key in ("uid", "start", "end", "start_ts", "end_ts",
                    "all_day", "duration_min", "subject", "type",
                    "room", "teachers", "group"):
            assert key in d, f"поле {key!r} отсутствует"

    def test_to_dict_values(self, simple_event):
        d = simple_event.to_dict()
        assert d["start"] == "2026-12-01T08:45+03:00"
        assert d["start_ts"] == simple_event.start_ts
        assert d["subject"] == "Пример дисциплины"
        assert d["all_day"] is False

    def test_to_dict_json_serializable(self, simple_event):
        json.dumps(simple_event.to_dict())


# =========================================================== ScheduleWeek

class TestScheduleWeek:

    def test_iter(self, simple_event):
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[simple_event],
        )
        assert list(week) == [simple_event]

    def test_len(self, simple_event):
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[simple_event, simple_event],
        )
        assert len(week) == 2

    def test_sorted_by_start(self):
        later = ScheduleEvent(
            uid="b",
            start=dt.datetime(2026, 12, 1, 12, tzinfo=MSK),
            end=dt.datetime(2026, 12, 1, 13, tzinfo=MSK),
        )
        earlier = ScheduleEvent(
            uid="a",
            start=dt.datetime(2026, 12, 1, 9, tzinfo=MSK),
            end=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
        )
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[later, earlier],
        )
        assert [e.uid for e in week.sorted()] == ["a", "b"]

    def test_events_sorted_on_creation(self):
        later = ScheduleEvent(
            uid="b",
            start=dt.datetime(2026, 12, 1, 12, tzinfo=MSK),
            end=dt.datetime(2026, 12, 1, 13, tzinfo=MSK),
        )
        earlier = ScheduleEvent(
            uid="a",
            start=dt.datetime(2026, 12, 1, 9, tzinfo=MSK),
            end=dt.datetime(2026, 12, 1, 10, tzinfo=MSK),
        )
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[later, earlier],
        )
        assert [e.uid for e in week] == ["a", "b"]

    def test_to_dict_json_serializable(self, simple_event):
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[simple_event],
        )
        json.dumps(week.to_dict())

    def test_to_dict_week_bounds(self, simple_event):
        week = ScheduleWeek(
            week_start=dt.date(2026, 11, 30),
            week_end=dt.date(2026, 12, 6),
            source="ics",
            events=[simple_event],
        )
        d = week.to_dict()
        assert d["week_start"] == "2026-11-30"
        assert d["week_end"] == "2026-12-06"
        assert d["source"] == "ics"


# =========================================================== helpers: время

class TestDayTs:

    def test_day_start_ts(self):
        ts = _day_start_ts(dt.date(2026, 12, 1))
        expected = int(dt.datetime(2026, 12, 1, 0, 0, tzinfo=MSK).timestamp())
        assert ts == expected

    def test_day_end_ts_is_last_second(self):
        start = _day_start_ts(dt.date(2026, 12, 1))
        end = _day_end_ts(dt.date(2026, 12, 1))
        assert end - start == 24 * 3600 - 1


class TestHashUid:

    def test_deterministic(self):
        assert _hash_uid("a", "b", "c") == _hash_uid("a", "b", "c")

    def test_different_inputs(self):
        assert _hash_uid("a", "b") != _hash_uid("a", "c")

    def test_none_equals_empty(self):
        assert _hash_uid("a", None) == _hash_uid("a", "")

    def test_no_pipe_collision(self):
        assert _hash_uid("a|b", "c") != _hash_uid("a", "b|c")


class TestWeekRange:

    def test_current_uses_today(self):
        ref = dt.date(2026, 12, 2)  # среда
        monday, sunday = _week_range("current", reference_date=ref)
        assert monday == dt.date(2026, 11, 30)
        assert sunday == dt.date(2026, 12, 6)

    def test_next_crosses_month(self):
        ref = dt.date(2026, 11, 24)  # вторник
        monday, sunday = _week_range("next", reference_date=ref)
        assert monday == dt.date(2026, 11, 30)
        assert sunday == dt.date(2026, 12, 6)
        assert monday.month != sunday.month

    def test_invalid_week_raises(self):
        with pytest.raises(ValueError, match="week must be"):
            _week_range("invalid")


# =========================================================== ICS helpers

class TestUnescapeICS:

    def test_newline(self):
        assert _unescape_ics("a\\nb") == "a\nb"

    def test_escaped_comma(self):
        assert _unescape_ics("a\\,b") == "a,b"

    def test_escaped_semicolon(self):
        assert _unescape_ics("a\\;b") == "a;b"

    def test_double_backslash_is_literal(self):
        assert _unescape_ics("a\\\\b") == "a\\b"

    def test_order_matters_literal_backslash_n(self):
        assert _unescape_ics("\\n") == "\n"
        assert _unescape_ics("\\\\n") == "\\n"


class TestUnfoldICS:

    def test_folded_line_joined(self):
        text = "DESCRIPTION:Иванов Иван Иван\n ович"
        assert _unfold_ics(text) == ["DESCRIPTION:Иванов Иван Иванович"]

    def test_multiple_folds(self):
        text = "A:line1\n line2\n line3"
        assert _unfold_ics(text) == ["A:line1line2line3"]

    def test_tab_continuation(self):
        text = "A:line1\n\tline2"
        assert _unfold_ics(text) == ["A:line1line2"]


class TestParseICSDatetime:

    def test_naive_datetime_with_t(self):
        result = _parse_ics_datetime("20261201T084500")
        assert result == dt.datetime(2026, 12, 1, 8, 45, tzinfo=MSK)

    def test_datetime_with_seconds_less(self):
        result = _parse_ics_datetime("20261201T0845")
        assert result == dt.datetime(2026, 12, 1, 8, 45, tzinfo=MSK)

    def test_utc_converted_to_msk(self):
        result = _parse_ics_datetime("20261201T054500Z")
        assert result == dt.datetime(2026, 12, 1, 8, 45, tzinfo=MSK)

    def test_date_only(self):
        result = _parse_ics_datetime("20261201")
        assert result == dt.datetime(2026, 12, 1, 0, 0, tzinfo=MSK)

    def test_empty_returns_none(self):
        assert _parse_ics_datetime("") is None

    def test_whitespace_returns_none(self):
        assert _parse_ics_datetime("   ") is None

    def test_invalid_returns_none(self):
        assert _parse_ics_datetime("garbage") is None

    def test_unknown_tzid_warns_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="parser")
        _parse_ics_datetime("20261201T084500", tzid="Asia/Novosibirsk")
        _parse_ics_datetime("20261202T084500", tzid="Asia/Novosibirsk")
        warnings = [r for r in caplog.records if "TZID" in r.message]
        assert len(warnings) == 1

    def test_msk_tzid_no_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="parser")
        _parse_ics_datetime("20261201T084500", tzid="Europe/Moscow")
        assert not any("TZID" in r.message for r in caplog.records)

    def test_z_plus_tzid_no_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="parser")
        _parse_ics_datetime("20261201T054500Z", tzid="Asia/Novosibirsk")
        assert not any("TZID" in r.message for r in caplog.records)


class TestSplitSummary:

    def test_known_type(self):
        assert _split_summary("Пример дисциплины (Лекции)") == \
            ("Пример дисциплины", "Лекции")

    def test_no_type(self):
        assert _split_summary("Математика") == ("Математика", None)

    def test_any_type_in_parens(self):
        assert _split_summary("Пример дисциплины (Практические занятия)") == \
            ("Пример дисциплины", "Практические занятия")

    def test_unknown_type_also_split(self):
        assert _split_summary("Программирование (Спецкурс)") == \
            ("Программирование", "Спецкурс")

    def test_nested_parens(self):
        assert _split_summary("Math (Lecture) (advanced)") == \
            ("Math (Lecture)", "advanced")

    def test_empty_parens_returns_none_type(self):
        assert _split_summary("Пример дисциплины ( )") == \
            ("Пример дисциплины", None)


class TestParseDescription:

    def test_full(self):
        teachers, group = _parse_description(
            "Преподаватели: Иванов И.И.\nГруппа: ГРУППА-00/0"
        )
        assert teachers == "Иванов И.И."
        assert group == "ГРУППА-00/0"

    def test_multiple_teachers(self):
        teachers, _ = _parse_description(
            "Преподаватели: Иванов И.И.\n"
            "Преподаватели: Петров П.П.\n"
            "Группа: ГРУППА-00/0"
        )
        assert teachers == "Иванов И.И.; Петров П.П."

    def test_empty(self):
        assert _parse_description("") == (None, None)

    def test_no_colon_ignored(self):
        teachers, _ = _parse_description("Преподаватели Иванов И.И.")
        assert teachers is None


class TestNormalizeRoom:

    def test_dedup_simple(self):
        assert _normalize_room("Корпус 1, Аудитория 1.1 Аудитория 1.1") == \
            "Корпус 1, Аудитория 1.1"

    def test_dedup_multiword(self):
        assert _normalize_room(
            'Корпус 1, Зал "Спорт" Зал "Спорт"'
        ) == 'Корпус 1, Зал "Спорт"'

    def test_dedup_triple(self):
        assert _normalize_room("A B A B A B") == "A B"

    def test_no_change(self):
        assert _normalize_room("Корпус 1, Аудитория 1.1 (комп)") == \
            "Корпус 1, Аудитория 1.1 (комп)"

    def test_no_partial_word_dedup(self):
        assert _normalize_room("Аудитория 1.1 и Аудитория 2.2") == \
            "Аудитория 1.1 и Аудитория 2.2"

    def test_none(self):
        assert _normalize_room(None) is None

    def test_empty(self):
        assert _normalize_room("") == ""

    def test_single_word(self):
        assert _normalize_room("А101") == "А101"


# =========================================================== _event_from_ics

class TestEventFromICS:

    def test_basic(self):
        cur = {
            "UID": "test-1",
            "DTSTART": "20261201T084500",
            "DTEND": "20261201T100500",
            "SUMMARY": "Пример дисциплины (Лекции)",
            "DESCRIPTION": "Преподаватели: Иванов И.И.\nГруппа: ГРУППА-00/0",
            "LOCATION": "Аудитория 1.1",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.subject == "Пример дисциплины"
        assert ev.type == "Лекции"
        assert ev.room == "Аудитория 1.1"
        assert ev.teachers == "Иванов И.И."
        assert ev.group == "ГРУППА-00/0"
        assert ev.duration_min == 80

    def test_missing_dtstart_returns_none(self):
        assert _event_from_ics({"SUMMARY": "test"}) is None

    def test_midnight_crossing(self):
        cur = {
            "UID": "night-1",
            "DTSTART": "20261201T234500",
            "DTEND": "20261201T001500",
            "SUMMARY": "Ночная пара",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.end.date() == dt.date(2026, 12, 2)

    def test_zero_duration_becomes_one_minute(self):
        cur = {
            "UID": "zero-1",
            "DTSTART": "20261201T090000",
            "DTEND": "20261201T090000",
            "SUMMARY": "Битая",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.duration_min == 1

    def test_backward_dtend_does_not_crash(self):
        cur = {
            "UID": "back-1",
            "DTSTART": "20261201T000000",
            "DTEND": "20261130T000000",
            "SUMMARY": "X",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.end > ev.start

    def test_missing_dtend_adds_90_min(self):
        cur = {
            "UID": "noend",
            "DTSTART": "20261201T090000",
            "SUMMARY": "Без конца",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.duration_min == 90

    def test_room_dedup(self):
        cur = {
            "UID": "room-1",
            "DTSTART": "20261201T090000",
            "DTEND": "20261201T100000",
            "SUMMARY": "X",
            "LOCATION": "Корпус 1, Аудитория 1.1 Аудитория 1.1",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.room == "Корпус 1, Аудитория 1.1"

    def test_prakticheskie_type_extracted(self):
        cur = {
            "UID": "pr-1",
            "DTSTART": "20261201T090000",
            "DTEND": "20261201T100000",
            "SUMMARY": "Пример дисциплины (Практические занятия)",
        }
        ev = _event_from_ics(cur)
        assert ev is not None
        assert ev.subject == "Пример дисциплины"
        assert ev.type == "Практические занятия"


# =========================================================== _parse_ics

class TestParseICS:

    def test_full_ics_with_timezone(self):
        events = _parse_ics(SAMPLE_ICS)
        assert len(events) == 2
        assert events[0].subject == "Пример дисциплины"
        assert events[1].subject == "Другая дисциплина"

    def test_escaped_comma_in_location(self):
        events = _parse_ics(SAMPLE_ICS)
        assert events[0].room == "Корпус 1, Аудитория 1.1"

    def test_valarm_does_not_override_description(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:test-alarm
SUMMARY:Математика (Лекция)
DESCRIPTION:Преподаватели: Иванов И.И.
DTSTART;TZID=Europe/Moscow:20261201T090000
DTEND;TZID=Europe/Moscow:20261201T103000
BEGIN:VALARM
TRIGGER:-PT15M
DESCRIPTION:Напоминание
END:VALARM
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].subject == "Математика"
        assert events[0].teachers == "Иванов И.И."

    def test_folded_description(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:test-fold
SUMMARY:Пример дисциплины
DESCRIPTION:Преподаватели: Иванов Иван Иван
 ович
DTSTART;TZID=Europe/Moscow:20261201T084500
DTEND;TZID=Europe/Moscow:20261201T100500
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].teachers == "Иванов Иван Иванович"

    def test_empty_calendar(self):
        ics = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        assert _parse_ics(ics) == []

    def test_deduplication_by_uid(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:dup-1
SUMMARY:Дубликат
DTSTART:20261201T090000
DTEND:20261201T100000
END:VEVENT
BEGIN:VEVENT
UID:dup-1
SUMMARY:Дубликат
DTSTART:20261201T090000
DTEND:20261201T100000
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1

    def test_value_date_all_day(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:allday-1
SUMMARY:Сессия
DTSTART;VALUE=DATE:20261201
DTEND;VALUE=DATE:20261202
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].all_day is True
        assert events[0].start == dt.datetime(2026, 12, 1, tzinfo=MSK)

    def test_nested_vevent_keeps_outer(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:outer
SUMMARY:Настоящее
DTSTART:20261201T090000
DTEND:20261201T100000
BEGIN:VEVENT
UID:inner
SUMMARY:Вложенное
END:VEVENT
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].uid == "outer"
        assert events[0].subject == "Настоящее"

    def test_truncated_file_processes_last_event(self, caplog):
        caplog.set_level(logging.WARNING, logger="parser")
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:trunc-1
SUMMARY:Без конца
DTSTART:20261201T090000
DTEND:20261201T100000
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].uid == "trunc-1"
        assert any("обрезан" in r.message for r in caplog.records)

    def test_empty_key_skipped(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ek-1
SUMMARY:X
:мусор
DTSTART:20261201T090000
DTEND:20261201T100000
END:VEVENT
END:VCALENDAR
"""
        events = _parse_ics(ics)
        assert len(events) == 1
        assert events[0].subject == "X"


# =========================================================== Livewire

class TestLivewireParsers:

    def test_date_dot_format(self):
        assert _parse_lw_date("01.12.2026") == dt.date(2026, 12, 1)

    def test_date_iso_format(self):
        assert _parse_lw_date("2026-12-01") == dt.date(2026, 12, 1)

    def test_date_invalid_returns_none(self):
        assert _parse_lw_date("garbage") is None

    def test_date_empty_returns_none(self):
        assert _parse_lw_date("") is None

    def test_date_non_str_returns_none(self):
        assert _parse_lw_date(20261201) is None
        assert _parse_lw_date(["01.12.2026"]) is None

    def test_time_hhmm(self):
        assert _parse_lw_time("08:45") == dt.time(8, 45)

    def test_time_hhmmss(self):
        assert _parse_lw_time("08:45:30") == dt.time(8, 45, 30)

    def test_time_invalid_returns_none(self):
        assert _parse_lw_time("garbage") is None

    def test_time_none(self):
        assert _parse_lw_time(None) is None

    def test_time_non_str_returns_none(self):
        assert _parse_lw_time(845) is None


class TestEventFromLivewire:

    def test_basic(self):
        e = {
            "date": "01.12.2026",
            "startTime": "08:45",
            "endTime": "10:05",
            "discipline": "Пример дисциплины",
            "groupType": "Лекции",
            "classroom": "Аудитория 1.1",
            "teachers": "Иванов И.И.",
            "group": "ГРУППА-00/0",
        }
        ev = _event_from_livewire(e)
        assert ev is not None
        assert ev.subject == "Пример дисциплины"
        assert ev.type == "Лекции"
        assert ev.duration_min == 80
        assert ev.all_day is False

    def test_all_day_event(self):
        e = {"date": "01.12.2026", "discipline": "Сессия"}
        ev = _event_from_livewire(e)
        assert ev is not None
        assert ev.all_day is True
        assert ev.duration_min == 24 * 60

    def test_all_day_with_end_time_still_24h(self):
        e = {"date": "01.12.2026", "endTime": "00:00", "discipline": "Сессия"}
        ev = _event_from_livewire(e)
        assert ev is not None
        assert ev.all_day is True
        assert ev.duration_min == 24 * 60

    def test_midnight_crossing(self):
        e = {
            "date": "01.12.2026",
            "startTime": "23:45",
            "endTime": "00:15",
            "discipline": "Ночная",
        }
        ev = _event_from_livewire(e)
        assert ev is not None
        assert ev.end.date() == dt.date(2026, 12, 2)
        assert ev.duration_min == 30

    def test_invalid_date_returns_none(self):
        assert _event_from_livewire({"date": "garbage"}) is None

    def test_uid_is_hash_when_missing(self):
        e = {"date": "01.12.2026", "startTime": "08:45",
             "discipline": "X", "groupType": "Y"}
        ev1 = _event_from_livewire(e)
        ev2 = _event_from_livewire(e)
        assert ev1 is not None and ev2 is not None
        assert ev1.uid == ev2.uid
        assert ev1.uid.startswith("h-")

    def test_room_dedup(self):
        e = {
            "date": "01.12.2026",
            "startTime": "08:45",
            "endTime": "10:05",
            "classroom": "Корпус 1, Аудитория 1.1 Аудитория 1.1",
        }
        ev = _event_from_livewire(e)
        assert ev is not None
        assert ev.room == "Корпус 1, Аудитория 1.1"


# =========================================================== fetch

class TestFetchICS:

    def test_parses_valid_response(self):
        session = MagicMock()
        session.get.return_value = make_response(SAMPLE_ICS)
        events = _fetch_ics(session, dt.date(2026, 11, 30), dt.date(2026, 12, 6))
        assert len(events) == 2

    def test_non_ics_raises(self):
        session = MagicMock()
        session.get.return_value = make_response(
            "<html>login</html>", content_type="text/html"
        )
        with pytest.raises(RuntimeError, match="не ICS"):
            _fetch_ics(session, dt.date(2026, 11, 30), dt.date(2026, 12, 6))

    def test_bom_stripped(self):
        session = MagicMock()
        session.get.return_value = make_response("\ufeff" + SAMPLE_ICS)
        events = _fetch_ics(session, dt.date(2026, 11, 30), dt.date(2026, 12, 6))
        assert len(events) == 2

    def test_network_error_propagates(self):
        import requests
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        with pytest.raises(requests.ConnectionError):
            _fetch_ics(session, dt.date(2026, 11, 30), dt.date(2026, 12, 6))


class TestExtractLivewire:

    def test_extracts_data(self):
        session = MagicMock()
        session.get.return_value = make_response(
            build_livewire_html([{"date": "01.12.2026"}]),
            content_type="text/html",
        )
        data = _extract_livewire(session)
        assert data is not None
        assert "events" in data

    def test_component_not_found(self):
        session = MagicMock()
        session.get.return_value = make_response(
            build_livewire_html([], component="other"),
            content_type="text/html",
        )
        assert _extract_livewire(session) is None

    def test_no_wire_div(self):
        session = MagicMock()
        session.get.return_value = make_response(
            "<html><body>no data</body></html>", content_type="text/html"
        )
        assert _extract_livewire(session) is None

    def test_multiple_components_warns(self, caplog):
        caplog.set_level(logging.WARNING, logger="parser")
        payload1 = {
            "fingerprint": {"name": "schedule-student-main"},
            "serverMemo": {"data": {"events": [{"date": "01.12.2026"}]}},
        }
        payload2 = {
            "fingerprint": {"name": "schedule-student-main"},
            "serverMemo": {"data": {"events": [{"date": "02.12.2026"}]}},
        }
        session = MagicMock()
        session.get.return_value = make_response(
            build_multi_livewire_html([payload1, payload2]),
            content_type="text/html",
        )
        data = _extract_livewire(session)
        assert data is not None
        assert data["events"][0]["date"] == "01.12.2026"
        assert any("компонентов" in r.message for r in caplog.records)


class TestFetchLivewire:

    def test_filters_by_date(self):
        events = [
            {"date": "01.12.2026", "startTime": "09:00",
             "endTime": "10:30", "discipline": "A"},
            {"date": "20.12.2026", "startTime": "09:00",
             "endTime": "10:30", "discipline": "B"},
        ]
        session = MagicMock()
        session.get.return_value = make_response(
            build_livewire_html(events), content_type="text/html"
        )
        result = _fetch_livewire(session, dt.date(2026, 11, 30),
                                 dt.date(2026, 12, 6))
        assert result is not None
        assert len(result) == 1
        assert result[0].subject == "A"

    def test_component_missing_returns_none(self):
        session = MagicMock()
        session.get.return_value = make_response(
            "<html></html>", content_type="text/html"
        )
        assert _fetch_livewire(session, dt.date(2026, 11, 30),
                               dt.date(2026, 12, 6)) is None

    def test_non_dict_event_skipped(self):
        events = [
            {"date": "01.12.2026", "startTime": "09:00",
             "endTime": "10:30", "discipline": "A"},
            "не-словарь",
            None,
            123,
        ]
        session = MagicMock()
        session.get.return_value = make_response(
            build_livewire_html(events), content_type="text/html"
        )
        result = _fetch_livewire(session, dt.date(2026, 11, 30),
                                 dt.date(2026, 12, 6))
        assert result is not None
        assert len(result) == 1


# =========================================================== public API

class TestGetWeek:

    def test_uses_ics(self):
        session = MagicMock()
        session.get.return_value = make_response(SAMPLE_ICS)
        week = get_week(session, "current", reference_date=dt.date(2026, 12, 1))
        assert week.source == "ics"
        assert len(week) == 2

    def test_falls_back_to_livewire(self):
        session = MagicMock()
        ics_resp = make_response("<html></html>", content_type="text/html")
        lw_resp = make_response(
            build_livewire_html([
                {"date": "01.12.2026", "startTime": "09:00",
                 "endTime": "10:30", "discipline": "A"}
            ]),
            content_type="text/html",
        )
        session.get.side_effect = [ics_resp, lw_resp]
        week = get_week(session, "current", reference_date=dt.date(2026, 12, 1))
        assert week.source == "livewire"
        assert len(week) == 1

    def test_invalid_week_raises(self):
        with pytest.raises(ValueError, match="week must be"):
            get_week(MagicMock(), "invalid")


class TestGetSchedule:

    def test_returns_sorted_events(self):
        session = MagicMock()
        session.get.return_value = make_response(SAMPLE_ICS)
        events = get_schedule(session, "current",
                              reference_date=dt.date(2026, 12, 1))
        assert len(events) == 2
        assert events[0].start <= events[1].start