"""Сборка вертикального шортса: ffmpeg + вшитые субтитры."""

import os
import shutil
import subprocess
import textwrap
from functools import lru_cache
from pathlib import Path

from .timecode import fmt_ass

W, H = 1080, 1920

# Видео занимает всю ширину кадра, размытие остаётся сверху и снизу.
INNER = 1.0
BLUR = 42

# Где winget оставляет ffmpeg, если он не попал в PATH.
_WINGET = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"

# На Windows subprocess.run по умолчанию мелькает отдельным консольным
# окном на каждый вызов — при батч-рендере это десятки вспышек за прогон.
# На других ОС такого флага у subprocess нет вовсе, поэтому берём его
# через getattr с запасным нулём.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Кегль надписи по умолчанию. Длинную надпись уменьшаем на ходу — см.
# hook_size(): резать текст ради размера значит вешать в кадр огрызок.
HOOK_FONT = 58

_ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts, Segoe UI Black, 74, &H00FFFFFF, &H00000000, &HB4000000, 0, 0, 1, 4, 4, 2, 80, 80, 250, 204
Style: Hook, Segoe UI Black, {HOOK_FONT}, &H00FFFFFF, &H00000000, &HA0000000, 0, 0, 3, 18, 0, 8, 80, 80, 240, 204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Полоса под надпись, когда собеседники стоят стопкой. Стопка занимает кадр
# целиком, и плашка ложится человеку на лоб: на пробных кадрах она закрывала
# верхнюю треть лица первого собеседника. Поэтому картинку опускаем, а
# освободившееся сверху поле отдаём надписи.
#
# 150 точек из 1920 — это две строки шрифтом 58 плюс воздух. Лица от такого
# сжатия мельче почти незаметно: полосы становятся 590 вместо 640.
HOOK_BAND = 150

# Полоса под живые субтитры (стиль Shorts) в стопке — по той же причине, что
# и HOOK_BAND, только снизу и не только с хуком: субтитр слова — это
# MarginV=250 от низа кадра, а стопка без запаса подводит нижний тайл
# ровно к краю, так что подпись ложится ему на рот. Место рассчитано под
# две строки шрифтом 74 (MarginV 250 + высота двух строк с запасом).
CAPTION_BAND = 420

# Отступ надписи от верха кадра внутри этой полосы.
HOOK_STACKED = 20

# Насколько задержать последнее слово фразы и какой зазор оставить
# перед следующей, чтобы строки не накладывались.
LINGER = 0.25
GAP = 0.04


class FfmpegMissing(RuntimeError):
    pass


def find_ffmpeg(explicit=None):
    """Ищет ffmpeg: явный путь -> PATH -> папка установки winget."""
    if explicit:
        if not Path(explicit).exists():
            raise FfmpegMissing(f"по пути {explicit} ffmpeg нет")
        return str(explicit)

    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in sorted(_WINGET.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")):
        return str(candidate)

    raise FfmpegMissing(
        "ffmpeg не найден. Поставь: winget install Gyan.FFmpeg — "
        "или укажи путь флагом --ffmpeg"
    )


def _ffprobe_path(ffmpeg):
    """ffprobe лежит рядом с ffmpeg, а в PATH его на этой машине нет."""
    near = Path(ffmpeg).parent / "ffprobe.exe"
    return str(near) if near.exists() else "ffprobe"


@lru_cache(maxsize=8)
def _source_stats(ffmpeg, source):
    """fps и средний битрейт исходника — под них подгоняется кодирование.

    fps: раньше рендер всегда выдавал фиксированные 60 кадров. Исходники
    YouTube бывают 25 или 30 fps, и раздутый до 60 выход просто дублирует
    каждый кадр — плавнее от этого не становится, а битов уходит вдвое
    больше. Держим fps исходника и никогда не поднимаем его.

    Битрейт нужен nvenc (см. encoder()/render()): без привязки к исходнику
    VBR на сложной картинке (рукописный текст, частая смена кадра) упирается
    в жёсткую константу и раздувает файл в разы против того, что реально
    нужно для такого контента — на говорящей голове та же константа простаивает.

    Не вышло измерить (битый файл, нет ffprobe) — отдаём (None, None), и
    вызывающий код берёт безопасные умолчания, а не падает.
    """
    probe_exe = _ffprobe_path(ffmpeg)
    try:
        done = subprocess.run(
            [probe_exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1",
             str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=_NO_WINDOW,
        )
        fps = done.stdout.strip().splitlines()[0].strip() or None
    except (OSError, subprocess.SubprocessError, IndexError):
        fps = None

    try:
        done = subprocess.run(
            [probe_exe, "-v", "error", "-show_entries", "format=bit_rate",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=_NO_WINDOW,
        )
        bitrate = int(done.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        bitrate = None

    return fps, bitrate


# Потолок nvenc не может быть ниже этого, даже если исходник совсем лёгкий
# (говорящая голова у AV1-источника весит меньше мегабита). Самый тяжёлый
# замеренный вес такого контента на выходе — 2.25 Мбит/с («Полиграф ч.2»,
# UyYnqtsE6e8) — держим потолок в полтора раза выше с запасом, чтобы cq=19
# никогда об него не упирался и говорящие головы не менялись вовсе.
MAXRATE_FLOOR = 3_500_000

# Множитель к среднему битрейту исходника. Меньше единицы: перекодированный
# в h264 файл всё равно не обязан весить как AV1/VP9-исходник — cq=19 сам
# решает, сколько бит нужно для качества, — потолок здесь только страховка
# от раздувания на самой сложной картинке (рукописный текст, частая смена
# кадра), а не цель, к которой стремится кодирование.
MAXRATE_MULT = 0.7


def _nvenc_ceiling(ffmpeg, source):
    """Потолок и буфер nvenc для конкретного источника — см. константы выше."""
    _, bitrate = _source_stats(ffmpeg, source)
    maxrate = max(int((bitrate or 0) * MAXRATE_MULT), MAXRATE_FLOOR)
    return maxrate, maxrate * 2


# Кодировщики на железе, по одному на каждого производителя. Что именно
# стоит у человека — заранее неизвестно, поэтому пробуем все по очереди
# и откатываемся на процессор, если не завелось ни одного.
# Качество берём с запасом. Ролик после нас пережмут ещё раз — и TikTok,
# и YouTube кодируют загруженное заново, — а второе сжатие бьёт по тому,
# что уже потеряно. Поэтому отдаём заметно лучше, чем нужно для просмотра:
# лишние мегабайты дешевле, чем каша на мелких буквах субтитров.
#
# h264_nvenc без -maxrate/-bufsize: их значения зависят от исходника и
# подставляются в render() через _nvenc_ceiling() — здесь только то, что
# одинаково для любого источника. p7 — самый медленный и самый качественный
# пресет nvenc (p5 был серединой шкалы); ощутимая разница на глаз ровно та,
# ради которой этот пресет существует, — платим временем рендера, не
# качеством. spatial-aq/temporal-aq — адаптивное квантование: кодек сам
# перераспределяет биты в кадре и по времени на то, что заметнее глазу
# (лицо, текст), а не тратит их поровну на размытый фон, — то же качество
# меньшим весом.
GPU_ENCODERS = (
    ("h264_nvenc", ["-preset", "p7", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                    "-spatial-aq", "1", "-temporal-aq", "1"], "NVIDIA"),
    ("h264_amf", ["-quality", "balanced", "-rc", "cqp", "-qp_i", "19", "-qp_p", "19"], "AMD"),
    ("h264_qsv", ["-preset", "medium", "-global_quality", "19"], "Intel"),
    # У videotoolbox нет общего с остальными параметра качества (cq/qp) —
    # управляет только битрейтом, поэтому задаём его с тем же запасом.
    ("h264_videotoolbox", ["-b:v", "14M", "-maxrate", "20M"], "Apple"),
)

CPU_ENCODER = ("libx264", ["-preset", "medium", "-crf", "18"], "процессор")


def _works(ffmpeg, name, options):
    """Пробный прогон в никуда: наличие кодека в списке ещё не значит, что он заведётся."""
    probe = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=256x256:duration=1",
            "-c:v", name, *options, "-f", "null", "-",
        ],
        capture_output=True, creationflags=_NO_WINDOW,
    )
    return probe.returncode == 0


@lru_cache(maxsize=4)
def encoder(ffmpeg, gpu=True):
    """Чем кодировать: аргументы для ffmpeg и название для человека.

    Пережатие кадра — самая тяжёлая часть работы, и на процессоре оно
    занимает все ядра. Любая современная видеокарта делает то же почти даром.
    """
    if gpu:
        for name, options, vendor in GPU_ENCODERS:
            if _works(ffmpeg, name, options):
                return ["-c:v", name, *options], vendor

    name, options, vendor = CPU_ENCODER
    return ["-c:v", name, *options], vendor


# Заглавные заметно шире строчных, поэтому в строку их влезает меньше.
LINE = 17
MAX_LINES = 2


# Цвета в ASS задаются задом наперёд: &HBBGGRR&. Это тёплое золото.
ACCENT = r"{\c&H3CC9FF&}"
PLAIN = r"{\c&HFFFFFF&}"

# Капслок: короткая строка заглавными читается с одного взгляда и не
# теряется на пёстром кадре — так сделаны почти все шортсы.
CAPS = True

# Строка не возникает рывком, а быстро подскакивает из чуть меньшего размера —
# глазу легче поймать смену, и картинка перестаёт выглядеть статичной.
POP = r"{\fad(40,40)\fscx93\fscy93\t(0,90,\fscx100\fscy100)}"
FADE_HOOK = r"{\fad(180,180)}"


# Прямые аналоги для знаков, которых нет в некоторых начертаниях шрифта
# (Segoe UI Black отдаёт под них пустой квадрат вместо буквы) — тире,
# кавычки-ёлочки и многоточие в кадре не видны, а слово рядом с ними
# выглядит оборванным.
_TYPO = str.maketrans({
    "—": "-", "–": "-", "‑": "-",
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
    "‘": "'", "’": "'",
    "…": "...",
    "\xa0": " ",
})


def _clean(text):
    text = text.translate(_TYPO)
    return text.replace("\\", "").replace("{", "(").replace("}", ")").strip()


def _layout(tokens, width=LINE):
    """Раскладывает слова по строкам кадра и красит отмеченное.

    Считаем длину по чистому тексту: теги цвета place не занимают, но в
    подсчёт символов попали бы и сломали перенос.
    """
    lines, current, filled = [], [], 0

    for text, accent in tokens:
        text = _clean(text)
        if CAPS:
            text = text.upper()
        if not text:
            continue
        need = len(text) + (1 if current else 0)
        if current and filled + need > width:
            lines.append(current)
            current, filled, need = [], 0, len(text)
        current.append(f"{ACCENT}{text}{PLAIN}" if accent else text)
        filled += need

    if current:
        lines.append(current)

    # Берём последние строки, а не первые. Слова копятся по мере речи, и
    # произносимое сейчас всегда в конце: срез с начала выбрасывал именно
    # его, и слово не появлялось в кадре вовсе. На длинных фразах так
    # терялась четверть текста.
    return "\\N".join(" ".join(line) for line in lines[-MAX_LINES:])


def _spoken_rows(group, left_edge, right_edge, tail):
    """Строки, где слова проявляются по мере проговаривания.

    На каждое слово своя строка: показаны все сказанные к этому моменту,
    последнее выделено цветом. Так текст живёт вместе с речью, а не
    вываливается блоком.
    """
    inside = [w for w in group if w.end > left_edge and w.start < right_edge]
    if not inside:
        return []

    rows = []
    for index, word in enumerate(inside):
        starts = max(word.start, left_edge) - left_edge
        following = inside[index + 1].start if index + 1 < len(inside) else tail
        ends = min(following, right_edge) - left_edge
        if ends - starts < 0.04:
            continue

        tokens = [(w.text, w is word) for w in inside[: index + 1]]
        # Подскок только на первой строке фразы: иначе текст дрожит.
        effect = POP if index == 0 else ""
        rows.append(
            f"Dialogue: 0,{fmt_ass(starts)},{fmt_ass(ends)},Shorts,,0,0,0,,"
            f"{effect}{_layout(tokens)}"
        )

    return rows


def _escape(text, width=LINE):
    """Готовит цельную строку к ASS — этим выводится заголовок главы.

    Обрезать текст нельзя: пропавшее слово выглядит как ошибка распознавания.
    Поэтому если фраза не влезает, строки расширяются, а слова остаются.
    """
    text = _clean(text)

    lines = textwrap.wrap(text, width=width)
    while len(lines) > MAX_LINES:
        width += 3
        lines = textwrap.wrap(text, width=width)

    return "\\N".join(lines) or text


def hook_size(text):
    """Кегль надписи под её длину.

    Раньше длинный хук просто резался по знакам, и в кадре висело «Плагин
    собирает данные оффлайн и выгружает их при». Резать текст ради размера
    неправильно — правильно уменьшить сам размер: две строки шрифтом 58
    вмещают около семидесяти знаков, 52 — около восьмидесяти, 46 — около
    девяноста. Дальше уменьшать нельзя, на телефоне уже не прочесть.
    """
    length = len(text or "")
    if length <= 70:
        return HOOK_FONT
    if length <= 82:
        return 52
    return 46


def write_ass(path, phrases, start, end, hook=None, keys=(), hook_margin=0,
              part=""):
    """Пишет .ass только для куска [start, end), время отсчитывается от нуля.

    hook — надпись сверху: вопрос из главы или от модели. Висит весь кусок и
    объясняет зрителю, про что он вообще смотрит.

    part — пометка части («ч.1»), когда длинный ответ разрезан надвое. Идёт
    отдельной строкой под надписью и мелким шрифтом: в самой надписи она
    съедала бы место у хука, ради которого зритель и остановился.

    hook_margin — отступ надписи от верха кадра; ноль значит «как в стиле».
    Он нужен раскладке стопкой: там кадр занят лицами целиком, и надпись,
    висящая в четверти высоты, ложится человеку на лоб.
    """
    rows = []

    if hook:
        # Плашка висит весь шортс, а не первые секунды: зритель приходит на
        # середине пролистывания, и вопрос нужен ему именно в этот момент.
        # Месту это не мешает — надпись сверху, субтитры снизу.
        held = end - start
        size = hook_size(hook)
        # Ширина переноса считается от кегля: чем мельче шрифт, тем больше
        # знаков помещается в строку.
        width = int(26 * HOOK_FONT / size)
        size_tag = "" if size == HOOK_FONT else "{\\fs%d}" % size
        rows.append(
            f"Dialogue: 1,{fmt_ass(0)},{fmt_ass(held)},"
            f"Hook,,0,0,{int(hook_margin)},,{FADE_HOOK}"
            f"{size_tag}"
            f"{_escape(hook, width=width)}"
        )
        if part:
            # Под надписью, мелко и без обводки: это служебная пометка, а не
            # часть крючка.
            below = int(hook_margin) + size * 2 + 16
            rows.append(
                f"Dialogue: 1,{fmt_ass(0)},{fmt_ass(held)},"
                f"Hook,,0,0,{below},,{FADE_HOOK}{{\\fs38}}{_escape(part, width=20)}"
            )

    for index, (phrase_start, phrase_end, group) in enumerate(phrases):
        if phrase_end <= start or phrase_start >= end:
            continue

        # Последнее слово фразы висит чуть дольше — иначе строка исчезает
        # ровно в тот миг, когда её дочитывают. Но не дольше, чем до начала
        # следующей: иначе две строки на мгновение оказываются на экране разом.
        following = phrases[index + 1][0] if index + 1 < len(phrases) else end
        tail = max(phrase_end, min(phrase_end + LINGER, following - GAP))

        rows += _spoken_rows(group, start, end, tail)

    path.write_text(_ASS_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    # Заголовок сам по себе не повод считать, что субтитры есть.
    return len(rows) - (1 if hook else 0)


def _filters(mode, subs_name, zoom=1.0, pan=0.0, tilt=0.0, speed=1.0, tiles=(),
             band=0):
    """Видеофильтр: приводим к 9:16 и вшиваем субтитры.

    zoom > 1 подрезает рамку (крупнее план), pan/tilt двигают её по кадру
    в долях от собственного размера: tilt=-0.15 поднимает рамку вверх,
    когда говорящий сидит низко и сверху остаётся пустой потолок.

    tiles — раскладка собеседников из layout.detect. Она есть только у
    режима stack; без неё он вырождается в blur, потому что ставить стопкой
    нечего.

    band — сколько точек оставить сверху под надпись (см. HOOK_BAND). Ноль
    значит «надписи не будет», и тогда пустая чёрная полоса ни к чему.
    Снизу в stack всегда добавляется своя полоса под живые субтитры
    (CAPTION_BAND) — она нужна и без хука, раз слова говорят в любом случае.
    """
    subs = f"subtitles={subs_name}" if subs_name else None

    if mode == "stack" and len(tiles) > 1:
        from . import layout  # локально: рендеру он нужен только в этом режиме

        chain = layout.chain(tiles, W, H - band - CAPTION_BAND)
        chain += f",pad={W}:{H}:0:{band}:black"
    elif mode == "blur" or mode == "stack":
        # Кадр целиком в центре, по краям — размытая копия вместо чёрных полос.
        # Основное видео чуть уже кадра: так вокруг остаётся поле размытия,
        # и взгляд сам собирается в центр.
        inner = int(W * INNER) // 2 * 2
        chain = (
            f"split[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma={BLUR}[bgv];"
            f"[fg]scale={inner}:-2[fgv];"
            f"[bgv][fgv]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        # Центральный кроп: для говорящей головы обычно правильнее.
        zoom = max(1.0, zoom)
        chain = (
            f"crop='min(iw,ih*9/16)/{zoom}':'ih/{zoom}':"
            f"'(iw-ow)/2+({pan})*ow':'(ih-oh)/2+({tilt})*oh',"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        )

    if subs:
        chain = f"{chain},{subs}"

    # Ускоряем после вшивания субтитров: тогда они разгоняются вместе с
    # картинкой и остаются синхронными сами собой.
    if speed and abs(speed - 1.0) > 0.01:
        chain = f"{chain},setpts=PTS/{speed}"

    return chain


# Во сколько уложить готовый шортс и насколько ради этого можно разогнать.
# Полторы минуты — верх формата; выше 1.2x речь начинает звучать тараторкой,
# и выигранные секунды не стоят потерянной внятности.
TARGET = 90.0
MAX_SPEED = 1.2


def fit_speed(duration, chosen=None):
    """Скорость для куска: заданная вручную либо подобранная под длину.

    Момент лучше не обрывать на полуслове, поэтому длинный кусок мы не режем,
    а слегка разгоняем — так и ответ звучит целиком, и формат соблюдён.
    Влез в TARGET сам — не трогаем: разгон нужен там, где он что-то решает.
    """
    if chosen:
        return chosen
    if duration <= TARGET:
        return 1.0
    return min(round(duration / TARGET, 2), MAX_SPEED)


def _audio_args(ffmpeg, source, start, end, level, speed=1.0):
    """Обработка звука: выравнивание каналов и разгон вслед за картинкой."""
    chain = []

    if level:
        from . import audio  # локально, чтобы модули не зациклились друг на друге

        gains = audio.balance(ffmpeg, source, start, end - start)
        if gains:
            left, right = gains
            chain.append(f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1")

    # Сглаживаем разницу между говорящими: у одного голос громкий, у другого
    # тихий, и в наушниках это заметно. Настройки мягкие — выравниваем перепад,
    # а не давим всё в одну громкость.
    chain.append("dynaudnorm=f=250:g=13:p=0.55:m=6:s=8")

    if speed and abs(speed - 1.0) > 0.01:
        # atempo меняет темп, не трогая высоту голоса.
        chain.append(f"atempo={speed}")

    # dynaudnorm ровняет перепады внутри ролика, но не приводит его к уровню
    # площадок — без этого TikTok и YouTube потом подтягивают громкость сами,
    # грубее и на слух заметнее. -14 LUFS — их общий ориентир.
    chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")

    return ["-af", ",".join(chain)] if chain else []


def render(
    ffmpeg, source, out, start, end, mode="crop", subs_path=None,
    zoom=1.0, pan=0.0, tilt=0.0, gpu=True, level=True, speed=1.0, tiles=(),
    band=0,
):
    """Режет [start, end) и кладёт готовый вертикальный mp4 в out."""
    source = Path(source).resolve()
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Путь к .ass внутри фильтра ffmpeg на Windows экранируется мучительно
    # (двоеточие диска, обратные слэши), поэтому запускаемся из его папки
    # и передаём голое имя файла.
    cwd = str(subs_path.parent.resolve()) if subs_path else None
    subs_name = subs_path.name if subs_path else None

    fps, _ = _source_stats(ffmpeg, source)
    encoder_args, vendor = encoder(ffmpeg, gpu)
    if vendor == "NVIDIA":
        # Потолок и буфер зависят от исходника (см. _nvenc_ceiling) — в
        # статический GPU_ENCODERS они не попадают, добавляем здесь.
        maxrate, bufsize = _nvenc_ceiling(ffmpeg, source)
        encoder_args = [*encoder_args, "-maxrate", str(maxrate), "-bufsize", str(bufsize)]

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        # И -ss, и -t относятся к входу: как выходное ограничение -t заставило
        # бы ffmpeg дочитывать лишнее, чтобы набрать длину после ускорения.
        "-ss", f"{start:.3f}",
        "-t", f"{end - start:.3f}",
        "-i", str(source),
        "-vf", _filters(mode, subs_name, zoom, pan, tilt, speed, tiles, band),
        *encoder_args,
        "-pix_fmt", "yuv420p",
        # fps исходника, не выше: раздутый до фиксированного числа выход
        # платит битами за продублированные кадры без выигрыша в плавности
        # (см. _source_stats). Не измерился — не поднимаем его вслепую,
        # отдаём картинку как есть.
        *(["-r", fps] if fps else []),
        # High — то, что понимают все плееры и все площадки. Без явного
        # указания nvenc иногда отдаёт профиль, который Windows не открывает.
        "-profile:v", "high",
        # Цветовое пространство подписываем явно. Без подписи площадки
        # угадывают его сами и промахиваются — картинка уезжает в блёклое.
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        *_audio_args(ffmpeg, source, start, end, level, speed),
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart",
        str(out),
    ]

    done = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if done.returncode != 0:
        # Упавший ffmpeg успевает оставить недописанный файл. В shorts/ он
        # лежит рядом с готовыми и выглядит как результат — убираем сразу.
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg упал:\n{done.stderr.strip()}")

    # Если на входе оказался файл без картинки, ffmpeg не ругается: он молча
    # собирает mp4 из одного звука и выходит с нулём. Такие «шортсы» доходили
    # до просмотра как готовые — проверяем сами, чтобы ошибка была видна тут,
    # а не через полчаса на плеере.
    from . import download  # локально, чтобы модули не зациклились друг на друге

    if not download.has_video(out, ffmpeg, unknown=True):
        out.unlink(missing_ok=True)
        raise RuntimeError(
            f"на выходе нет видеодорожки — в источнике {source.name} "
            f"её, похоже, тоже нет"
        )

    return out
