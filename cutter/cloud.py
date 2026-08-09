"""Распознавание речи через Groq — когда своей видеокарты нет или лень ждать.

Зачем это вообще. Наш движок считает на видеокарте только у NVIDIA (см.
machine.py). На Mac и на Radeon остаётся процессор, а это полчаса на часовой
ролик — проще нарезать руками. Groq считает у себя и отдаёт результат за
десятки секунд на любой машине.

**Работаем только на бесплатном уровне.** Это не пожелание, а правило: ключ
принадлежит человеку, и неожиданный счёт за наши эксперименты недопустим.
Поэтому здесь есть счётчик секунд и жёсткий потолок — дойдя до него, модуль
отказывается работать и предлагает считать местно, а не переходит на платный
тариф молча.

Бесплатные лимиты Groq на whisper-large-v3-turbo:

    7 200 секунд звука в час, 28 800 в сутки — это восемь часов звука.

Файл в один запрос — до 25 МБ, поэтому длинную дорожку жмём в mp3 моно
32 кбит/с (около 15 МБ на час) и, если всё равно не влезает, режем на куски
с нахлёстом. Whisper всё равно сводит вход к 16 кГц моно, так что на
качестве это не сказывается — проверено тем же способом, что и батчинг.

Ключ берётся в таком порядке: переменная окружения SHORTS_GROQ_KEY, потом
файл work/groq-key.txt. В коде ключа нет и быть не должно: программа уезжает
на чужие машины, и там должен стоять чужой ключ со своей квотой.
"""

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .paths import WORK
from .subs import Word

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"

# Потолок одного запроса у Groq. Берём с запасом: считаем по размеру файла,
# а он до отправки известен точно.
LIMIT_MB = 24.0

# Бесплатная квота, секунды звука. Считаем сами и останавливаемся раньше,
# чем это сделает сервер: так человек видит внятное объяснение, а не 429.
FREE_HOUR = 7200
FREE_DAY = 28800

# Насколько близко к квоте подходить. Оставляем запас: счётчик у нас свой,
# и разойтись с серверным он может.
SAFETY = 0.9

# Сжатие дорожки. Whisper сводит вход к 16 кГц моно, поэтому отдавать больше
# бессмысленно — это только вес файла.
RATE = 16000
BITRATE = "32k"

# Нахлёст между кусками, если файл пришлось резать: шов не должен попасть
# в середину слова.
OVERLAP = 10.0

TIMEOUT = 300

# Перед Groq стоит Cloudflare, и запрос без представления он рубит на входе
# с кодом 1010 — это выглядит как «неверный ключ», хотя ключ ни при чём.
# Проверено: без заголовка 403, с ним всё проходит.
AGENT = "shorts-cutter/1.0 (+python-urllib)"


class CloudFailed(RuntimeError):
    pass


class QuotaSpent(CloudFailed):
    """Бесплатная квота на исходе. Не ошибка — повод считать местно."""


# ── ключи ─────────────────────────────────────────────────────────────────

KEY_FILE = WORK / "groq-key.txt"

# Ключи, у которых на сегодня кончилась суточная квота. Держим в памяти
# процесса, а не в файле: квота сама восстановится, и запись на диск только
# создала бы ложное «ключ не работает» на завтрашнем запуске.
_SPENT_KEYS = set()


def keys():
    """Все известные ключи по порядку: сначала окружение, потом файл.

    Ключей может быть несколько — по одному на строку в файле. Разные
    аккаунты Groq считают квоту отдельно, поэтому второй ключ продолжает
    работу там, где первый упёрся в суточный лимит. Ключи одного аккаунта
    делят общую квоту, и толку от второго не будет — по виду ключа этого
    не понять, узнаём только по ответу сервера.
    """
    found = []
    for source in (os.environ.get("SHORTS_GROQ_KEY", ""), _read_keys()):
        for line in source.replace(",", "\n").splitlines():
            line = line.strip()
            if line and line not in found:
                found.append(line)
    return found


def _read_keys():
    try:
        return KEY_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def key():
    """Ключ, которым работаем сейчас. Пустая строка — работать нечем."""
    live = [k for k in keys() if k not in _SPENT_KEYS]
    return live[0] if live else ""


def drop_key(value, on_note=None):
    """Помечает ключ выбранным на сегодня и переходит к следующему.

    Возвращает True, если есть чем продолжать. Сам ключ в лог не пишем
    никогда — только его номер по порядку.
    """
    if not value:
        return False
    _SPENT_KEYS.add(value)

    order = keys()
    spent = order.index(value) + 1 if value in order else 0
    if key():
        if on_note:
            on_note(f"ключ {spent} исчерпан на сегодня — перехожу на следующий")
        return True
    if on_note:
        on_note("суточная квота выбрана на всех ключах")
    return False


def remember_key(value):
    """Запоминает ключи рядом с программой. Пустое значение — стереть.

    Несколько ключей можно дать через запятую или с новой строки: так их
    вводят в окне, одним полем.
    """
    lines = [line.strip() for line in (value or "").replace(",", "\n").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        KEY_FILE.unlink(missing_ok=True)
        return
    WORK.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text("\n".join(lines), encoding="utf-8")


def add_key(value):
    """Добавляет ключ к уже записанным, не трогая прежние."""
    value = (value or "").strip()
    if not value:
        return
    known = [k for k in keys() if k != value]
    remember_key("\n".join(known + [value]))


def ready():
    """Есть ли чем ходить в облако."""
    return bool(key())


# ── счётчик бесплатной квоты ──────────────────────────────────────────────

SPENT_FILE = WORK / "groq-spent.json"


def _spent():
    try:
        return json.loads(SPENT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _note_spent(seconds):
    now = time.time()
    # Держим только сутки: старое для квоты уже не считается.
    log = [row for row in _spent() if now - row["at"] < 86400]
    log.append({"at": now, "seconds": round(seconds, 1)})
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        SPENT_FILE.write_text(json.dumps(log), encoding="utf-8")
    except OSError:
        pass


def used():
    """Сколько секунд звука отправлено за час и за сутки."""
    now = time.time()
    log = _spent()
    hour = sum(r["seconds"] for r in log if now - r["at"] < 3600)
    day = sum(r["seconds"] for r in log if now - r["at"] < 86400)
    return hour, day


def left():
    """Сколько секунд звука ещё можно отправить бесплатно."""
    hour, day = used()
    return min(FREE_HOUR * SAFETY - hour, FREE_DAY * SAFETY - day)


def _check_quota(seconds):
    room = left()
    if seconds > room:
        hour, day = used()
        raise QuotaSpent(
            f"бесплатной квоты Groq не хватит на этот ролик: нужно "
            f"{seconds / 60:.0f} мин звука, осталось {max(room, 0) / 60:.0f}.\n"
            f"  За час отправлено {hour / 60:.0f} мин, за сутки {day / 60:.0f}. "
            f"Считаю на этой машине."
        )


# ── подготовка дорожки ────────────────────────────────────────────────────

def _compress(ffmpeg, track, folder, start=None, length=None):
    """Дорожка в mp3 моно 16 кГц — самый лёгкий вход, который понимает Whisper."""
    out = Path(folder) / f"{uuid.uuid4().hex}.mp3"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-i", str(track), "-vn", "-ac", "1", "-ar", str(RATE),
            "-b:a", BITRATE, str(out)]

    done = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0 or not out.exists():
        raise CloudFailed(f"не смог сжать дорожку: {done.stderr.strip()[:200]}")
    return out


def _size_mb(path):
    return Path(path).stat().st_size / 1024 / 1024


# ── запрос ────────────────────────────────────────────────────────────────

def _form(path, language, hint):
    """Тело multipart-запроса. Без сторонних библиотек: нужен один запрос."""
    line = uuid.uuid4().hex
    body = bytearray()

    def field(name, value):
        body.extend(f"--{line}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(f"{value}\r\n".encode())

    field("model", MODEL)
    field("language", language)
    # Слова с таймкодами — без них не собрать ни субтитры, ни резку.
    field("response_format", "verbose_json")
    field("timestamp_granularities[]", "word")
    field("temperature", "0")
    if hint:
        # Тот же словарь ролика, что мы отдаём местной модели: показываем,
        # как пишутся термины, которые сейчас прозвучат.
        field("prompt", hint)

    body.extend(f"--{line}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{Path(path).name}"\r\n'.encode()
    )
    body.extend(b"Content-Type: audio/mpeg\r\n\r\n")
    body.extend(Path(path).read_bytes())
    body.extend(f"\r\n--{line}--\r\n".encode())

    return bytes(body), f"multipart/form-data; boundary={line}"


def _ask(path, language, hint):
    payload, kind = _form(path, language, hint)
    request = urllib.request.Request(
        URL, data=payload,
        headers={
            "Authorization": f"Bearer {key()}",
            "Content-Type": kind,
            "User-Agent": AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        if error.code == 429:
            raise QuotaSpent(
                f"Groq отвечает, что лимит исчерпан. Считаю на этой машине.\n"
                f"  {detail}"
            ) from None
        if error.code in (401, 403):
            raise CloudFailed(
                "Groq не принял ключ. Проверь его в окне — возможно, истёк."
            ) from None
        raise CloudFailed(f"Groq ответил {error.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as error:
        raise CloudFailed(f"до Groq не достучаться: {error}") from None


def _words(reply, shift=0.0):
    found = []
    for item in reply.get("words") or ():
        text = (item.get("word") or "").strip()
        if not text:
            continue
        found.append(Word(
            float(item.get("start", 0.0)) + shift,
            float(item.get("end", 0.0)) + shift,
            text,
            # Groq оценку по слову не отдаёт. Ставим единицу честно: это
            # означает «признака ошибки нет», а не «уверены полностью».
            sure=1.0,
        ))
    return found


# ── то, ради чего всё ─────────────────────────────────────────────────────

def transcribe(ffmpeg, track, duration, language="ru", hint=None, on_note=None):
    """Распознаёт дорожку через Groq и отдаёт слова с таймкодами.

    duration — длительность в секундах, нужна для проверки квоты до отправки.
    Бросает QuotaSpent, если бесплатного лимита не хватает: вызывающий должен
    поймать её и посчитать местно, а не падать.
    """
    if not ready():
        raise CloudFailed("ключ Groq не задан — впиши его в окне")

    _check_quota(duration)

    with tempfile.TemporaryDirectory() as folder:
        whole = _compress(ffmpeg, track, folder)
        size = _size_mb(whole)

        if size <= LIMIT_MB:
            if on_note:
                on_note(f"отправляю в Groq {size:.0f} МБ ({duration / 60:.0f} мин звука)")
            words = _words(_ask(whole, language, hint))
            _note_spent(duration)
            if on_note:
                on_note(f"Groq вернул {len(words)} слов")
            return words

        # Не влезло — режем по времени так, чтобы каждый кусок был под лимит.
        pieces = int(size // LIMIT_MB) + 1
        span = duration / pieces
        if on_note:
            on_note(
                f"дорожка {size:.0f} МБ, лимит {LIMIT_MB:.0f} — "
                f"отправляю {pieces} частями по {span / 60:.0f} мин"
            )

        words = []
        for number in range(pieces):
            start = max(0.0, number * span - (OVERLAP if number else 0.0))
            length = span + (OVERLAP if number else 0.0)
            piece = _compress(ffmpeg, track, folder, start, length)
            part = _words(_ask(piece, language, hint), shift=start)

            # Шов: выбрасываем слова, попавшие в зону нахлёста повторно.
            if words:
                edge = words[-1].end
                part = [w for w in part if w.start >= edge]
            words += part
            if on_note:
                on_note(f"  часть {number + 1} из {pieces}: {len(part)} слов")

        _note_spent(duration)
        return words
