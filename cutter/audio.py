"""Громкость по дорожке — единственный сигнал не про тему, а про живость.

Текст не показывает, где человек повысил голос, засмеялся или заговорил
с напором. Дорожка показывает. Считает всё ffmpeg, нам остаётся сравнить
каждый момент со средним по этому же говорящему: важна не громкость сама
по себе, а насколько она выше обычной для него.
"""

import re
import subprocess
from bisect import bisect_left
from collections import deque

# Тишина и щелчки, которые astats отдаёт как -inf либо абсурдно тихое.
FLOOR = -70.0

_TIME = re.compile(r"pts_time:([\d.]+)")
_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf)")


def levels(ffmpeg, source, step=8):
    """Список (секунда, громкость в дБ) примерно раз в полсекунды.

    step — через сколько аудиокадров сбрасывать статистику; 8 кадров это
    около 0.5 секунды, чего достаточно, чтобы поймать всплеск голоса.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(source), "-vn",
        "-af", (
            f"aresample=16000,astats=metadata=1:reset={step},"
            f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
        ),
        "-f", "null", "-",
    ]
    done = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        raise RuntimeError(f"не смог разобрать звук:\n{done.stderr.strip()}")

    found = []
    moment = None
    for line in done.stdout.splitlines():
        stamp = _TIME.search(line)
        if stamp:
            moment = float(stamp.group(1))
            continue
        value = _RMS.search(line)
        if value and moment is not None:
            raw = value.group(1)
            found.append((moment, FLOOR if raw == "-inf" else float(raw)))
            moment = None

    return found


_CHANNEL = re.compile(r"Channel:\s*(\d+)")
_LEVEL = re.compile(r"RMS level dB:\s*(-?[\d.]+|-inf)")

# Ниже этой разницы перекос на слух не читается, трогать звук незачем.
SKEW_DB = 1.0


def balance(ffmpeg, source, start, duration):
    """Насколько подтянуть каждый канал, чтобы они звучали ровно.

    Бывает, что голос записан в один канал громче, чем в другой, — в
    наушниках это сразу слышно как перекос набок. Возвращает пару
    множителей (левый, правый) либо None, если выравнивать нечего.
    """
    done = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-nostdin",
            "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
            "-vn", "-af", "astats=measure_overall=none:measure_perchannel=RMS_level",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        return None

    levels = {}
    channel = None
    for line in done.stderr.splitlines():
        found = _CHANNEL.search(line)
        if found:
            channel = int(found.group(1))
            continue
        value = _LEVEL.search(line)
        if value and channel is not None:
            raw = value.group(1)
            levels[channel] = FLOOR if raw == "-inf" else float(raw)
            channel = None

    left, right = levels.get(1), levels.get(2)
    if left is None or right is None or left <= FLOOR or right <= FLOOR:
        return None

    skew = left - right
    if abs(skew) < SKEW_DB:
        return None

    # Тихий канал поднимаем, громкий опускаем — каждый на половину разницы,
    # так громкость ролика в целом не поедет.
    return 10 ** (-skew / 40), 10 ** (skew / 40)


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _rolling_max(times, values, span):
    """Максимум громкости в окне ±span/2 вокруг каждой точки, за один проход.

    Всплеск голоса длится доли секунды, и привязывать его к конкретному
    слову бессмысленно — важно, что он рядом. Отсюда окно вместо точки.
    """
    peaks = [0.0] * len(values)
    live = deque()  # индексы, значения по убыванию
    right = -1

    for index, moment in enumerate(times):
        while right + 1 < len(values) and times[right + 1] <= moment + span / 2:
            right += 1
            while live and values[live[-1]] <= values[right]:
                live.pop()
            live.append(right)
        while live and times[live[0]] < moment - span / 2:
            live.popleft()
        peaks[index] = values[live[0]] if live else values[index]

    return peaks


def per_word(words, samples, span=6.0):
    """Насколько громко звучит каждое слово относительно обычного. 0..1.

    Сравниваем с медианой по всей дорожке, а не с максимумом: один хлопок
    иначе прижмёт всё остальное к нулю.
    """
    if not words or not samples:
        return None

    speech = [db for _, db in samples if db > FLOOR]
    if not speech:
        return None

    base = _median(speech)

    # Разброс живой речи — примерно столько децибел над медианой.
    #
    # Запасное значение берётся явной проверкой, а не через `or`: раньше
    # стояло `(_median(...) - base) or 3.0`, и оно не срабатывало никогда.
    # На ровной дорожке список громче медианы оказывается пустым, _median
    # отдаёт 0.0, а `0.0 - base` — это не ноль, а модуль самой медианы
    # (громкость в дБ отрицательная): вместо трёх децибел разброса выходило
    # тридцать, и все слова получали одинаковый ноль громкости.
    louder = [db for db in speech if db > base]
    top = _median(louder) - base if louder else 0.0
    if top < 1.0:
        top = 3.0

    times = [moment for moment, _ in samples]
    peaks = _rolling_max(times, [db for _, db in samples], span)

    scores = []
    for word in words:
        index = min(bisect_left(times, word.start), len(peaks) - 1)
        loud = (peaks[index] - base) / (top * 2)
        scores.append(max(0.0, min(loud, 1.0)))

    return scores
