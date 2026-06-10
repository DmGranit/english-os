"""Google Calendar без OAuth: сборка ссылки-шаблона и .ics-события."""
import datetime
import urllib.parse

import bot

DAY = datetime.date(2026, 6, 15)


def _params(link):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)


def test_gcal_link_assembly():
    link = bot._gcal_link(9, DAY)
    assert link.startswith("https://calendar.google.com/calendar/render?")
    q = _params(link)
    assert q["action"] == ["TEMPLATE"]
    assert q["dates"] == ["20260615T090000/20260615T091000"]   # событие 10 минут
    assert q["recur"] == ["RRULE:FREQ=DAILY"]
    assert "English OS" in q["text"][0]


def test_gcal_link_encoding():
    link = bot._gcal_link(9, DAY)
    q = _params(link)
    assert "минут" in q["text"][0]                  # кириллица доехала без потерь
    raw = link.split("?", 1)[1]
    assert " " not in raw                           # пробелы закодированы
    assert all(ord(ch) < 128 for ch in raw)         # в сырой строке только ASCII


def test_gcal_link_pads_hour():
    q = _params(bot._gcal_link(7, DAY))
    assert q["dates"][0].startswith("20260615T070000/")


def test_ics_event_structure():
    ics = bot._ics_event(9, DAY)
    assert ics.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in ics and "END:VEVENT" in ics
    assert "DTSTART:20260615T090000" in ics
    assert "DTEND:20260615T091000" in ics
    assert "RRULE:FREQ=DAILY" in ics
    assert "SUMMARY:" in ics
    assert "\r\n" in ics                            # стандарт iCalendar требует CRLF
    assert ics.rstrip("\r\n").endswith("END:VCALENDAR")
