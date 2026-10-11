"""Wireface's own booking list, for the receptionist: opening hours, slots of a fixed length, and the bookings made.

The line agent (which talks to strangers) can only look up free slots, book one for the caller, and cancel or list the
caller's own bookings: the caller's number comes from the phone network, never from what they say, so nobody can
cancel someone else's appointment. Limits keep one caller from filling the diary. Times are local wall-clock time.
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
from pathlib import Path

from ..profile import read_json_lenient, write_json

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class BookingError(ValueError):
    """Why a booking can't be made (said to the caller, so it's plain)."""


def parse_hours(text: str | None) -> tuple[dt.time, dt.time] | None:
    """ "09:00-17:30" -> (09:00, 17:30); None or "" -> closed."""
    if not text:
        return None
    m = HOURS.match(text.strip())
    if not m:
        raise ValueError(f"expected opening hours like 09:00-17:30, got {text!r}")
    start, end = dt.time(int(m[1]), int(m[2])), dt.time(int(m[3]), int(m[4]))
    if end <= start:
        raise ValueError(f"opening hours end before they start: {text!r}")
    return start, end


def parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(text).strip())
    except ValueError:
        raise BookingError(f"{text!r} is not a date (use YYYY-MM-DD)") from None


def parse_time(text: str) -> dt.time:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(text).strip())
    if not m:
        raise BookingError(f"{text!r} is not a time (use HH:MM, 24-hour)")
    return dt.time(int(m[1]), int(m[2]))


class Bookings:
    def __init__(self, path: Path, settings: dict):
        self.path = path
        self.settings = settings          # phone.receptionist: hours, slot_minutes, max_days_ahead, per_caller_per_day

    # --- storage ---
    def all(self) -> list[dict]:
        items = read_json_lenient(self.path).get("bookings")
        return items if isinstance(items, list) else []

    def _save(self, items: list[dict]) -> None:
        write_json(self.path, {"bookings": items[-5000:]})

    # --- the diary ---
    def hours(self, day: dt.date) -> tuple[dt.time, dt.time] | None:
        return parse_hours((self.settings.get("hours") or {}).get(DAYS[day.weekday()]))

    def slot_minutes(self) -> int:
        return int(self.settings.get("slot_minutes") or 30)

    def taken(self, day: dt.date) -> set[str]:
        return {b["time"] for b in self.all() if b.get("date") == day.isoformat() and b.get("status") == "booked"}

    def free_slots(self, date: str, now: dt.datetime | None = None) -> list[str]:
        day = parse_date(date)
        now = now or dt.datetime.now()
        self._check_day(day, now)
        hours = self.hours(day)
        if not hours:
            return []
        step = dt.timedelta(minutes=self.slot_minutes())
        t = dt.datetime.combine(day, hours[0])
        end = dt.datetime.combine(day, hours[1])
        taken = self.taken(day)
        out = []
        while t + step <= end:
            if t > now + dt.timedelta(minutes=30) and t.strftime("%H:%M") not in taken:
                out.append(t.strftime("%H:%M"))
            t += step
        return out

    def _check_day(self, day: dt.date, now: dt.datetime) -> None:
        if day < now.date():
            raise BookingError(f"{day.isoformat()} has already gone")
        ahead = int(self.settings.get("max_days_ahead") or 60)
        if day > now.date() + dt.timedelta(days=ahead):
            raise BookingError(f"bookings can only be made up to {ahead} days ahead")

    def book(self, date: str, time: str, name: str, number: str, service: str = "", note: str = "",
             conversation: str = "", now: dt.datetime | None = None) -> dict:
        """Book ``date`` ``time`` for the caller on ``number`` (their number from the network, not their say-so)."""
        now = now or dt.datetime.now()
        day, at = parse_date(date), parse_time(time)
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise BookingError("a booking needs the caller's name")
        slot = at.strftime("%H:%M")
        if slot not in self.free_slots(day.isoformat(), now):
            free = self.free_slots(day.isoformat(), now)
            raise BookingError(f"{slot} on {day.isoformat()} isn't free" +
                               (f"; free times that day: {', '.join(free[:12])}" if free else "; nothing is free that day"))
        limit = int(self.settings.get("per_caller_per_day") or 2)
        mine = [b for b in self.all() if b.get("number") == number and b.get("status") == "booked"
                and b.get("made", "")[:10] == now.date().isoformat()]
        if len(mine) >= limit:
            raise BookingError("that's as many bookings as can be made from one number in a day; the owner will "
                               "need to help with more")
        booking = {"id": secrets.token_hex(4), "date": day.isoformat(), "time": slot, "minutes": self.slot_minutes(),
                   "name": name, "number": number, "service": str(service or "")[:80], "note": str(note or "")[:300],
                   "made": now.isoformat(timespec="seconds"), "via": conversation, "status": "booked"}
        items = self.all()
        items.append(booking)
        self._save(items)
        return booking

    def cancel(self, date: str, time: str, number: str, now: dt.datetime | None = None) -> dict:
        """Cancel the caller's own booking (matched by their number)."""
        day, slot = parse_date(date), parse_time(time).strftime("%H:%M")
        items = self.all()
        for b in items:
            if b.get("date") == day.isoformat() and b.get("time") == slot and b.get("status") == "booked":
                if b.get("number") != number:
                    break                                   # someone else's: say only that there's none of theirs
                b["status"] = "cancelled"
                b["cancelled"] = (now or dt.datetime.now()).isoformat(timespec="seconds")
                self._save(items)
                return b
        raise BookingError(f"there's no booking from this number at {slot} on {day.isoformat()}")

    def for_number(self, number: str, now: dt.datetime | None = None) -> list[dict]:
        today = (now or dt.datetime.now()).date().isoformat()
        return [b for b in self.all() if b.get("number") == number and b.get("status") == "booked" and b["date"] >= today]

    def on(self, date: str) -> list[dict]:
        day = parse_date(date).isoformat()
        return sorted((b for b in self.all() if b.get("date") == day and b.get("status") == "booked"), key=lambda b: b["time"])
