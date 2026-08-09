"""Разбор и форматирование таймкодов."""

import re

_TC = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse(value):
    """'12:30' | '1:02:30' | '750' | '750.5' -> секунды (float)."""
    value = str(value).strip()

    m = _TC.match(value)
    if m:
        hours, minutes, seconds = m.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)

    try:
        return float(value)
    except ValueError:
        raise ValueError(
            f"не понял таймкод {value!r} — жду 12:30, 1:02:30 или число секунд"
        )


def fmt(seconds):
    """Секунды -> 'MM:SS' или 'H:MM:SS' для показа человеку."""
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_ass(seconds):
    """Секунды -> 'H:MM:SS.cc' — формат времени в ASS."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"
