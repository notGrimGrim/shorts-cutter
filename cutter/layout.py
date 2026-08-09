"""Раскладка исходного кадра: где в нём собеседники и как из них собрать вертикаль.

Зачем это. Интервью снимают горизонтально: два-три человека рядом, каждый в
своём окне. Вписать такой кадр целиком в вертикальный шортс — значит отдать
людям треть высоты, а остальное занять размытием. На телефоне лица выходят
с ноготь, и смотреть это невозможно — первая же претензия к готовым шортсам
была именно про мелкую картинку.

Поэтому кадр разбирается на плитки участников, и плитки ставятся друг над
другом: два человека — две полосы, три — три. Лицо занимает всю ширину кадра,
размытое поле исчезает.

Как ищутся плитки. Граница между окнами — это вертикальное ребро во всю
высоту, которое стоит на месте весь ролик: стык двух вебок, рамка плитки
Zoom, край шторки. Поэтому считаем не «где ярко», а «в какой доле строк
на этом столбце есть перепад яркости» — и усредняем по нескольким кадрам,
взятым по всему ролику. У настоящей границы доля под 0.9, у края лица или
предмета — 0.2–0.3, и они не совпадают между кадрами, потому что человек
шевелится.

Верх и низ плитки ищутся уже по движению: у Zoom плитка не занимает всю
высоту, вокруг неё тёмное поле, и обрезать его надо, иначе оно попадёт в
кадр вместе с человеком.

Если разобрать не вышло — возвращаем одну плитку во весь кадр, и рендер
работает как раньше. Ошибиться здесь безопасно: это выбор раскладки, а не
данные.
"""

import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

# Сетка разбора. Точности до пары процентов ширины хватает: границы плиток
# потом всё равно берутся с запасом, а мелкая сетка считается мгновенно
# даже на чистом Python.
GRID_W = 384
GRID_H = 216

# Сколько кадров смотреть и где. Начало и конец ролика пропускаем: там
# заставка и титры, по ним о раскладке разговора судить нельзя.
FRAMES = 6
SKIP = 0.15

# Перепад яркости, который считается ребром. По серому 0..255: меньше —
# это шум сжатия, больше — уже граница чего-то.
STEP = 12

# Какую долю строк должно занимать ребро, чтобы считаться границей окна.
# Замер на живых роликах: стык вебок 0.89, рамки плиток Zoom 0.86–0.90,
# случайные края предметов 0.2–0.5.
EDGE = 0.62

# Ближе этого к краю кадра и друг к другу границы не ищем: там рамка окна
# и поля, а не деление на участников.
KEEP_OUT = 0.12

# Сколько плиток вообще имеет смысл ставить стопкой. Четыре по 480 — уже
# полоски, в которых лица не разглядеть.
MAX_TILES = 3

# Насколько плитки могут отличаться шириной. Сцена «вебка гостя плюс камера
# ведущего» даёт 37 % и 63 % — это ещё нормально, а вот вчетверо уже значит,
# что мы приняли за границу что-то другое.
SKEW = 2.2

# Запас вокруг найденного по вертикали: человек шевелится не всем телом,
# и по краям движения нет, хотя он там есть.
MARGIN = 0.03

# Ниже этой доли высоты плитку не обрезаем сверху и снизу вовсе: значит,
# движение размазано по всему окну и обрезать нечего.
FULL_HEIGHT = 0.9

# Какую долю всего движения плитки отсекаем сверху, чтобы попасть в лицо.
FACE_SHARE = 0.3

# Куда взгляду разрешено уезжать внутри плитки. За этими краями кроп
# начинает срезать голову или упирается в стол.
FOCUS_MIN, FOCUS_MAX = 0.3, 0.62

# Доля высоты выреза, которая остаётся НАД точкой фокуса. Симметричный вырез
# (было 0.5, вырез центрирован на фокусе) на трёх и более тайлах — где сама
# полоса уже низкая — упирает волосы в самый верх кадра, запаса под лоб не
# остаётся. Значение выше 0.5 отдаёт лбу больше высоты за счёт подбородка —
# там край менее заметен.
HEADROOM = 0.58


@dataclass
class Tile:
    """Место участника в кадре, в долях от ширины и высоты.

    focus — где внутри плитки держать взгляд по высоте. Полоса шортса ниже
    плитки по соотношению сторон, и что-то обрезать придётся: по центру
    плитки в кадр попадает стол, а лоб уезжает наверх.
    """

    x: float
    y: float
    w: float
    h: float
    focus: float = 0.42


WHOLE = Tile(0.0, 0.0, 1.0, 1.0)


def _frame(ffmpeg, video, at):
    """Один кадр серым, сжатый до сетки разбора."""
    done = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", f"scale={GRID_W}:{GRID_H}",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    data = done.stdout
    return data if len(data) == GRID_W * GRID_H else None


def _frames(ffmpeg, video, duration):
    """Кадры по всему ролику, кроме самого начала и конца."""
    if not duration or duration <= 0:
        return []
    first, last = duration * SKIP, duration * (1.0 - SKIP)
    step = (last - first) / max(FRAMES - 1, 1)
    taken = (_frame(ffmpeg, video, first + step * n) for n in range(FRAMES))
    return [frame for frame in taken if frame]


# Строки просматриваем через одну и без самого верха с низом: там титры,
# логотипы и полоса плеера, к делению на участников отношения не имеющие.
_TOP, _BOTTOM, _EVERY = 0.15, 0.9, 2


def _edge_share(frames):
    """Для каждого столбца — доля строк с перепадом, усреднённая по кадрам."""
    rows = range(int(GRID_H * _TOP), int(GRID_H * _BOTTOM), _EVERY)
    count = len(list(rows))
    share = [0.0] * GRID_W

    for frame in frames:
        for y in rows:
            line = y * GRID_W
            for x in range(1, GRID_W - 1):
                if abs(frame[line + x + 1] - frame[line + x - 1]) > STEP:
                    share[x] += 1

    scale = 1.0 / (count * len(frames))
    return [value * scale for value in share]


def _borders(share):
    """Границы окон: середины сплошных участков сильного ребра."""
    inside = range(int(GRID_W * KEEP_OUT), int(GRID_W * (1 - KEEP_OUT)))
    found, group = [], []

    for x in inside:
        if share[x] >= EDGE:
            group.append(x)
        elif group:
            found.append(sum(group) / len(group) / GRID_W)
            group = []
    if group:
        found.append(sum(group) / len(group) / GRID_W)

    # Две границы рядом — это одна граница, разорванная шумом.
    merged = []
    for place in found:
        if merged and place - merged[-1] < KEEP_OUT:
            merged[-1] = (merged[-1] + place) / 2
        else:
            merged.append(place)
    return merged


def _movement(frames):
    """Карта движения: сколько всего менялось в каждой точке сетки."""
    move = [0] * (GRID_W * GRID_H)
    for before, after in zip(frames, frames[1:]):
        for index, (a, b) in enumerate(zip(before, after)):
            move[index] += a - b if a > b else b - a
    return move


def _live_rows(move, left, right):
    """Верх, низ и место лица внутри полосы столбцов, в долях высоты.

    Тёмное поле вокруг плитки Zoom неподвижно, человек в плитке — нет.
    Поэтому берём строки, где движения заметно больше, чем в самой тихой
    части полосы.

    Третье число — где держать взгляд. Голова шевелится почти непрерывно,
    руки — реже и ниже, поэтому берём не середину движения, а его верхнюю
    треть: она приходится на лицо.
    """
    first = max(0, int(left * GRID_W))
    last = min(GRID_W, int(right * GRID_W))
    rows = [sum(move[y * GRID_W + first : y * GRID_W + last]) for y in range(GRID_H)]

    peak = max(rows) if rows else 0
    if peak <= 0:
        return 0.0, 1.0, 0.5

    live = [y for y, value in enumerate(rows) if value > peak * 0.18]
    if not live:
        return 0.0, 1.0, 0.5

    top, bottom = live[0] / GRID_H, (live[-1] + 1) / GRID_H

    whole = sum(rows[live[0] : live[-1] + 1])
    running, face = 0.0, live[0]
    for y in range(live[0], live[-1] + 1):
        running += rows[y]
        if running >= whole * FACE_SHARE:
            face = y
            break

    return top, bottom, face / GRID_H


def _sane(tiles):
    """Похоже ли найденное на людей в кадре, а не на случайную границу."""
    if not 2 <= len(tiles) <= MAX_TILES:
        return False
    widths = [tile.w for tile in tiles]
    return max(widths) / min(widths) <= SKEW


def detect(ffmpeg, video, duration=None, cache=None, on_note=None):
    """Плитки участников слева направо. Одна плитка — разобрать не вышло.

    cache — файл, куда положить разбор: он одинаков для всего ролика, а
    стоит десятка запусков ffmpeg.
    """
    if cache and Path(cache).exists():
        try:
            saved = json.loads(Path(cache).read_text(encoding="utf-8"))
            return [Tile(**row) for row in saved]
        except (OSError, ValueError, TypeError):
            pass

    tiles = [WHOLE]
    frames = _frames(ffmpeg, video, duration)

    if len(frames) >= 3:
        borders = _borders(_edge_share(frames))
        if borders and len(borders) < MAX_TILES:
            move = _movement(frames)
            edges = [0.0] + borders + [1.0]
            found = []
            for left, right in zip(edges, edges[1:]):
                top, bottom, face = _live_rows(move, left, right)
                if bottom - top > FULL_HEIGHT:
                    top, bottom = 0.0, 1.0
                else:
                    top = max(0.0, top - MARGIN)
                    bottom = min(1.0, bottom + MARGIN)
                inside = (face - top) / max(bottom - top, 1e-6)
                found.append(Tile(
                    x=left, y=top, w=right - left, h=bottom - top,
                    focus=min(max(inside, FOCUS_MIN), FOCUS_MAX),
                ))
            if _sane(found):
                tiles = found

    if on_note:
        if len(tiles) > 1:
            on_note(f"в кадре собеседников: {len(tiles)} — ставлю их стопкой")
        else:
            on_note("раскладку не разобрал — кадр целиком на размытом фоне")

    if cache:
        try:
            Path(cache).write_text(
                json.dumps([asdict(t) for t in tiles], ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    return tiles


def chain(tiles, width, height):
    """Фильтр ffmpeg: плитки, приведённые к ширине кадра и поставленные стопкой.

    Вырез считается сразу под соотношение полосы, а не в два приёма: сначала
    берём из плитки прямоугольник нужной формы вокруг лица, потом растягиваем
    его на полосу. Размеры исходника здесь не нужны — ffmpeg считает iw/ih сам,
    поэтому раскладка, снятая на 1080p, годится и для другого качества.

    Граф начинается со split, а не со ссылки на [0:v]: так он остаётся
    обычной цепочкой -vf, к которой дальше приписываются субтитры и разгон.
    """
    count = len(tiles)
    slice_h = (height // count) // 2 * 2
    # Последней полосе отдаём остаток, иначе при нечётном делении внизу
    # останется чёрная щель в пару пикселей.
    tail = height - slice_h * (count - 1)

    parts, labels = [], []
    for number, tile in enumerate(tiles):
        own = tail if number == count - 1 else slice_h
        ratio = width / own
        plate_w = f"iw*{tile.w:.4f}"
        plate_h = f"ih*{tile.h:.4f}"
        # Берём столько, сколько влезает: у широкой плитки упираемся в её
        # высоту, у узкой — в ширину.
        crop_w = f"min({plate_w},{plate_h}*{ratio:.4f})"
        label = f"p{number}"
        parts.append(
            f"[s{number}]crop="
            f"'{crop_w}':'{crop_w}/{ratio:.4f}':"
            f"'iw*{tile.x:.4f}+({plate_w}-ow)/2':"
            f"'ih*{tile.y:.4f}+clip({plate_h}*{tile.focus:.3f}-oh*{HEADROOM:.3f},0,{plate_h}-oh)',"
            f"scale={width}:{own},setsar=1[{label}]"
        )
        labels.append(f"[{label}]")

    split = f"split={count}" + "".join(f"[s{n}]" for n in range(count))
    return (
        f"{split};" + ";".join(parts) + ";"
        + "".join(labels) + f"vstack=inputs={count}"
    )
