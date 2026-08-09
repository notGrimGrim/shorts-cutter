"""Скачивание видео и автосубтитров через yt-dlp."""

import os
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import yt_dlp

from . import cobalt
from .paths import WORK
from .speech import TRACK_WAV


# Что считается сырьём: весит сотни мегабайт и после нарезки не нужно.
# Субтитры и список кандидатов не трогаем — они крошечные и экономят время
# при повторном заходе на то же видео.
MEDIA = (".mp4", ".mkv", ".webm", ".m4a", ".opus", ".mp3", ".wav",
         ".part", ".ytdl")


class DownloadFailed(RuntimeError):
    pass


def sweep(hours=1.0):
    """Убирает скачанное видео старше указанного срока. Возвращает мегабайты."""
    if not WORK.exists():
        return 0.0

    edge = time.time() - hours * 3600
    freed = 0
    for path in WORK.rglob("*"):
        if path.suffix.lower() not in MEDIA:
            continue
        if path.name == TRACK_WAV:
            # Общая дорожка 16 кГц из speech.track_wav — по ней audio.per_word
            # считает громкость для pairs.rate. Её удаление отсюда исключаем
            # намеренно: mp4 и webm можно перекачать заново, а эту дорожку —
            # нет, whisper-*.json хранит только текст, звука в нём нет. Без
            # неё громкость теряется безвозвратно и молча: pairs.rate просто
            # тише работает, и по выводу программы это не видно. Сотня
            # мегабайт на час видео — осознанная цена этой страховки; убрать
            # этот continue — значит вернуть тот самый баг, из-за которого
            # здесь разбирались.
            continue
        try:
            if path.stat().st_mtime < edge:
                size = path.stat().st_size
                path.unlink()
                freed += size
        except OSError:
            # Файл занят плеером или уже удалён — не повод падать.
            continue

    return freed / 1048576


def drop_media(video_id):
    """Удаляет сырьё конкретного ролика сразу после нарезки."""
    folder = WORK / video_id
    if not folder.exists():
        return 0.0

    freed = 0
    for path in folder.iterdir():
        if path.name == TRACK_WAV:
            continue  # см. sweep() — дорожку для громкости не сносим никогда
        if path.suffix.lower() in MEDIA:
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
            except OSError:
                continue

    return freed / 1048576


def _with_ffmpeg(opts, ffmpeg):
    """Показывает yt-dlp, где лежит ffmpeg.

    Одного параметра мало: часть проверок внутри yt-dlp ищет ffmpeg просто
    в PATH, поэтому кладём папку туда же. Иначе скачивание отрезка падает
    с жалобой, что ffmpeg не установлен, хотя путь передан.
    """
    if not ffmpeg:
        return opts

    folder = str(Path(ffmpeg).parent)
    opts["ffmpeg_location"] = folder
    if folder not in os.environ.get("PATH", ""):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    return opts


# Приметы того, что до YouTube просто не достучаться: провайдер блокирует
# имя или соединение. Сообщение yt-dlp об этом ничего внятного не говорит.
BLOCKED = ("getaddrinfo", "name or service not known", "failed to resolve",
           "connection reset", "timed out", "unable to download api page")

BLOCKED_HINT = (
    "не получается достучаться до YouTube — похоже, доступ закрыт.\n"
    "  Включи VPN либо запусти обход блокировок (zapret), потом повтори."
)

# Отдельная беда, которую легко спутать с блокировкой: достучались, но YouTube
# требует доказать, что мы человек. Так он встречает адреса, с которых ходят
# через обход и VPN, — то есть ровно те, которыми мы и пользуемся.
BOT_WALL = ("sign in to confirm", "not a bot", "confirm you're not")

BOT_HINT = (
    "YouTube требует подтвердить, что ты не робот.\n"
    "  Дело не в ссылке и не в программе, а в адресе, с которого мы вышли:\n"
    "  так YouTube встречает адреса дата-центров, а именно ими выходит\n"
    "  большинство VPN. Проверить, кто ты снаружи: https://ip-api.com/line/\n"
    "\n"
    "  Что помогает, по порядку:\n"
    "    1) сменить VPN на тот, чей адрес YouTube считает домашним,\n"
    "       либо выключить VPN и поднять zapret — он идёт с твоего адреса;\n"
    "    2) если сменить нечем — скачать ролик руками и дать программе\n"
    "       путь к файлу вместо ссылки, это работает всегда.\n"
    "\n"
    "  Куки браузера стену снимают, но скачать с ними всё равно не выйдет:\n"
    "  YouTube отдаёт залогиненному запросу только раскадровки, без медиа.\n"
    "  Сменой клиента (tv, ios, web_safari и прочими) тоже не лечится."
)

# Стена снята, а ссылок на медиа всё равно нет. Отдельный случай, и путать
# его с «нет такого качества» нельзя: причина та же — помеченный адрес.
NO_MEDIA = ("requested format is not available", "no video formats found")

NO_MEDIA_HINT = (
    "YouTube отдал сведения о ролике, но ни одной ссылки на видео.\n"
    "  Так он отвечает с адресов, которые считает подозрительными: проверку\n"
    "  мы прошли, а поток он не даёт. Лечится тем же — сменой точки выхода\n"
    "  VPN либо скачиванием файла руками и передачей пути вместо ссылки."
)


def _blocked(message):
    return any(mark in message.lower() for mark in BLOCKED)


def _bot_wall(message):
    return any(mark in message.lower() for mark in BOT_WALL)


# Куки не прочитались: чаще всего браузер открыт и держит свою базу занятой.
# Это повод взять следующий браузер, а не сдаваться.
COOKIE_TROUBLE = ("could not copy", "cookie database", "cookies database",
                  "could not find", "unsupported browser")


def _cookie_trouble(message):
    return any(mark in message.lower() for mark in COOKIE_TROUBLE)


def _explain(message):
    if _bot_wall(message):
        return f"{BOT_HINT}\n  Что ответил YouTube: {message.splitlines()[0]}"
    if any(mark in message.lower() for mark in NO_MEDIA):
        return f"{NO_MEDIA_HINT}\n  Что ответил YouTube: {message.splitlines()[0]}"
    if _blocked(message):
        return f"{BLOCKED_HINT}\n  Что ответил YouTube: {message.splitlines()[0]}"
    return message


# Адрес прокси, если человек ходит в интернет через свой VPN-клиент.
PROXY = os.environ.get("SHORTS_PROXY", "")


def use_proxy(address):
    """Пускать закачку через указанный прокси, например socks5://127.0.0.1:10808."""
    global PROXY
    PROXY = (address or "").strip()


# Куки браузера, которым снимается проверка «подтвердите, что вы не робот».
# Браузер при этом должен быть закрыт: у Chrome и Edge база кук занята, пока
# они работают, и yt-dlp не может её скопировать.
COOKIES_BROWSER = os.environ.get("SHORTS_COOKIES_BROWSER", "").strip().lower()
COOKIES_FILE = os.environ.get("SHORTS_COOKIES_FILE", "").strip()


def use_cookies(browser=None, path=None):
    """Откуда брать куки: из браузера по имени или из выгруженного cookies.txt."""
    global COOKIES_BROWSER, COOKIES_FILE
    if browser is not None:
        COOKIES_BROWSER = (browser or "").strip().lower()
    if path is not None:
        COOKIES_FILE = (path or "").strip()


# Какие браузеры перебирать, если человек не назвал свой. Chrome первым:
# он у большинства и держит живой вход в YouTube.
BROWSERS = ("chrome", "edge", "firefox")

# Браузер, куки которого сработали в прошлый раз. Хранится между запусками,
# чтобы не перебирать всё заново — перебор стоит секунд.
_REMEMBERED = WORK / "cookies-browser.txt"


def _remembered():
    try:
        name = _REMEMBERED.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return name if name in BROWSERS else ""


def _remember(browser):
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        _REMEMBERED.write_text(browser, encoding="utf-8")
    except OSError:
        # Не запомнили — в следующий раз просто переберём заново.
        pass


def _steps(opts):
    """Ступени загрузки: сначала как обычно, потом в обход.

    Порядок важен. Обход медленнее и поднят не всегда, а на живом канале
    обычная закачка просто работает — поэтому лезть в обход сразу значит
    подвешивать каждый запуск ради редкого случая.

    Куки идут последними: они трогают личный сеанс человека, и делать это
    без нужды незачем. Зато если дошло до них, перебираем браузеры сами —
    руками выяснять, у какого из них база не занята, никто не должен.
    """
    yield "напрямую", opts
    if PROXY:
        yield f"через прокси {PROXY}", {**opts, "proxy": PROXY}
    if COOKIES_FILE:
        yield "с куками из файла", {**opts, "cookiefile": COOKIES_FILE}

    named = [COOKIES_BROWSER] if COOKIES_BROWSER else []
    # Сработавший в прошлый раз — вперёд остальных.
    known = _remembered()
    order = named or [b for b in BROWSERS if b == known] + [
        b for b in BROWSERS if b != known
    ]
    for browser in order:
        yield f"с куками из {browser}", {**opts, "cookiesfrombrowser": (browser,)}


def _worth_next(message):
    """Стоит ли пробовать следующую ступень.

    Ролик удалён, приватный, нет такого формата — обход не поможет, а времени
    съест. Идём дальше только когда путь закрыт снаружи, когда YouTube требует
    доказать, что мы человек, или когда не удалось прочитать сами куки.
    """
    return _blocked(message) or _bot_wall(message) or _cookie_trouble(message)


def _ladder(opts, work, on_note=None):
    """Гоняет одну и ту же работу по ступеням, пока какая-нибудь не сработает."""
    trouble = "yt-dlp промолчал"
    for number, (label, attempt) in enumerate(_steps(opts)):
        if number and on_note:
            on_note(f"пробую {label}…")
        try:
            answer = work(attempt)
        except (yt_dlp.utils.DownloadError, OSError) as error:
            latest = str(error).replace("ERROR:", "").strip()
            # Ошибка чтения кук — про наш браузер, а не про причину отказа.
            # Если до неё уже был внятный ответ YouTube, держимся за него:
            # иначе человеку покажут «база кук занята» вместо «смени VPN».
            if not (_cookie_trouble(latest) and (_bot_wall(trouble) or _blocked(trouble))):
                trouble = latest
            if not _worth_next(latest):
                break
            continue

        # Сработало. Если помогли куки — запомним, каким браузером, чтобы
        # в следующий раз не перебирать всё заново.
        browser = (attempt.get("cookiesfrombrowser") or (None,))[0]
        if browser:
            _remember(browser)
            if on_note:
                on_note(f"помогли куки из {browser} — запомнил")
        return answer

    raise DownloadFailed(_explain(trouble))


def _download(opts, url, on_note=None):
    """Обёртка над yt-dlp: его простыня трейсбека человеку ничего не говорит."""

    def work(attempt):
        with yt_dlp.YoutubeDL(attempt) as ydl:
            ydl.download([url])

    _ladder(opts, work, on_note)


def rescue(url, video_id, on_note=None):
    """Последняя ступень: просим ролик у cobalt и дальше работаем как с файлом.

    cobalt отдаёт только медиафайл целиком — ни глав, ни субтитров, ни
    отрезков. Зато качает он у себя, и блокировка нашего провайдера ему
    не мешает. Дальше файл ничем не отличается от принесённого руками.
    """
    target = workdir(video_id) / f"{video_id}.mp4"
    if target.exists():
        return target

    if on_note:
        on_note("пробую через cobalt — он скачает ролик целиком…")
    return cobalt.fetch(url, target)


def _opts(outdir, **extra):
    base = {
        "outtmpl": {"default": str(outdir / "%(id)s.%(ext)s")},
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 10,
        # Чтобы закрытый путь отваливался с ошибкой, а не висел молча:
        # без этого запуск можно ждать минутами и не понять, чего он ждёт.
        "socket_timeout": 20,
        # YouTube режет скорость каждого отдельного соединения, поэтому
        # тянем куски параллельно и просим файл частями по 10 МБ —
        # ограничение применяется к соединению, а не к общей закачке.
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        # Клиент явно не навязываем. Раньше тут стоял ["android", "web"] —
        # он и правда отдаёт ссылки без замедляющей подписи, но именно через
        # куковый обход (см. _steps) сам сужает список форматов до одной
        # легаси-дорожки 360p без отдельного звука: ни bestaudio, ни 1080p
        # в нём просто нет. Без навязанного клиента yt-dlp перебирает сам и
        # находит рабочий — с ним доступны и audio-only, и видео до 1080p+.
    }
    base.update(extra)
    return base


@lru_cache(maxsize=32)
def probe(url):
    """Метаданные без скачивания: id, заголовок, длительность, языки субтитров.

    Результат запоминается: за один запуск про одну ссылку спрашивают
    несколько раз, а это каждый раз поход в сеть.
    """
    def work(attempt):
        with yt_dlp.YoutubeDL(attempt) as ydl:
            return ydl.extract_info(url, download=False)

    info = _ladder(
        {
            "quiet": True, "no_warnings": True, "socket_timeout": 20,
            # Нам тут нужны только сведения о ролике. Разбор форматов на
            # некоторых видео падает, и ронять из-за него весь запуск нельзя:
            # качать мы будем позже и своим набором форматов.
            "ignore_no_formats_error": True,
            "format": None,
            # Главы отдаёт не всякий клиент: проверено перебором — из девяти
            # только tv_simply. Остальные молчат, и тогда пропадает надпись
            # сверху шортса, а вместе с ней и лучший сигнал выбора моментов.
            # Ссылки на медиа он не даёт, но они здесь и не нужны.
            #
            # lang=ru — чтобы названия и главы пришли на языке ролика. Без
            # этого YouTube отдаёт свой автоперевод: на русском видео главы
            # приезжали английскими, и надпись в кадре выходила не на том
            # языке, на котором говорят.
            "extractor_args": {
                "youtube": {"player_client": ["tv_simply", "web"], "lang": ["ru"]}
            },
        },
        work,
    )
    return {
        "id": info["id"],
        "title": info.get("title", info["id"]),
        "duration": info.get("duration"),
        "subs": sorted(info.get("subtitles") or {}),
        "auto_subs": sorted(info.get("automatic_captions") or {}),
        # Авторская разметка тем — по ней и выбираются моменты.
        "chapters": info.get("chapters") or [],
    }


def workdir(video_id):
    path = WORK / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_url(source):
    """Ссылка это или путь к файлу на диске."""
    return str(source).strip().lower().startswith(("http://", "https://", "www."))


# Имя файла становится именем папки в work/, поэтому убираем всё, чем
# Windows подавится, и укорачиваем: имена с сайтов бывают на две строки.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_name(name):
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned[:60] or "file"


def clip_name(banner, video_id, start):
    """Имя готового файла — та же надпись, что стоит в кадре.

    По списку в папке сразу видно, про что каждый шортс: «Ври в резюме
    смело» находится глазами, а TH3UeUKy5hI_13-36 — нет. Таймкод оставляем
    хвостом: два шортса из одной главы иначе затрут друг друга.
    """
    from . import timecode

    stamp = timecode.fmt(start).replace(":", "-")
    name = safe_name(banner) if banner else ""
    return f"{name} [{stamp}]" if name else f"{video_id}_{stamp}"


def _duration(path, ffmpeg=None):
    """Длительность файла через ffprobe. Не вышло — None, это не смертельно."""
    probe_exe = "ffprobe"
    if ffmpeg:
        near = Path(ffmpeg).parent / "ffprobe.exe"
        if near.exists():
            probe_exe = str(near)

    try:
        done = subprocess.run(
            [probe_exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        return float(done.stdout.strip())
    except ValueError:
        return None


def has_video(path, ffmpeg=None, unknown=False):
    """Есть ли в файле видеодорожка — звук без картинки в шортс не годится.

    Спрашивают в двух местах, и ответ «не смогли проверить» им нужен разный,
    поэтому его задаёт unknown. На входе (что брать из кэша) незнание значит
    «не брать»: лишняя закачка дешевле шортса без картинки. На выходе (что
    отдавать человеку) — наоборот «пропустить»: не будет ffprobe, и строгий
    ответ снёс бы каждый нормальный файл.
    """
    probe_exe = "ffprobe"
    if ffmpeg:
        near = Path(ffmpeg).parent / "ffprobe.exe"
        if near.exists():
            probe_exe = str(near)

    try:
        done = subprocess.run(
            [probe_exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return unknown
    return bool(done.stdout.strip())


def local_info(path, ffmpeg=None):
    """Сведения о файле на диске — в том же виде, в каком probe() отдаёт про ссылку.

    Ни глав, ни субтитров у файла взять неоткуда: главы размечает автор на
    YouTube, субтитры лежат там же. Поэтому текст остаётся только распознать,
    а выбор моментов теряет самый сильный сигнал — это плата за независимость
    от сети, и она осознанная.
    """
    path = Path(path)
    if not path.exists():
        raise DownloadFailed(f"файла нет: {path}")

    return {
        "id": safe_name(path.stem),
        "title": path.stem,
        "duration": _duration(path, ffmpeg),
        "subs": [],
        "auto_subs": [],
        "chapters": [],
    }


def _has_authored(url, lang):
    """Есть ли у ролика авторские субтитры на этом языке (не автоматические)."""
    info = probe(url)
    return any(
        code == lang or code.startswith(f"{lang}-") or code.startswith(f"{lang}.")
        for code in info["subs"]
    )


def fetch_subs(url, video_id, lang="ru"):
    """Качает только авторские субтитры (.vtt). Возвращает путь к файлу.

    Автоматические субтитры YouTube не берём никогда, даже если авторских
    нет вовсе. У автосубтитров нет пунктуации — сплошной поток слов без
    точек и запятых, — а границы кусков в pairs.py ищутся именно по концам
    предложений. На таком тексте разбор вырождается молча, без ошибки, и
    результат неотличим на первый взгляд от нормального. Whisper пунктуацию
    расставляет, поэтому при отсутствии авторских субтитров вызывающий код
    ловит RuntimeError и уходит на распознавание (см. cut.py:_analyze,
    app.py:run_once).
    """
    outdir = workdir(video_id)

    authored = _has_authored(url, lang)

    # Кэш .vtt на диске мог остаться от старой версии, где автоматические
    # субтитры тоже кэшировались как обычный файл, — имя файла авторство не
    # различает. Доверяем кэшу только когда сейчас подтверждено, что у
    # ролика есть авторские субтитры на этом языке; иначе он может оказаться
    # автоматическим текстом без пунктуации, и молчаливое доверие ему как
    # раз и было источником беды.
    existing = sorted(outdir.glob(f"{video_id}*.vtt"))
    if existing and authored:
        return existing[0]

    if not authored:
        raise RuntimeError(
            f"субтитры на языке {lang!r} не нашлись — посмотри доступные через "
            f"`python cut.py info <url>` и укажи --lang"
        )

    opts = _opts(
        outdir,
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=False,
        subtitleslangs=[lang, f"{lang}-orig", f"{lang}.*"],
        subtitlesformat="vtt",
    )
    _download(opts, url)
    found = sorted(outdir.glob(f"{video_id}*.vtt"))
    if found:
        return found[0]

    raise RuntimeError(
        f"субтитры на языке {lang!r} не нашлись — посмотри доступные через "
        f"`python cut.py info <url>` и укажи --lang"
    )


def fetch_audio(url, video_id, ffmpeg=None):
    """Только звук — его хватает, чтобы оценить живость момента.

    Если видео уже скачано, берём звук прямо из него: качать второй раз
    то же самое незачем.
    """
    outdir = workdir(video_id)

    # Целый ролик уже лежит — звук возьмём прямо из него.
    for ext in ("mp4", "mkv", "webm"):
        cached = outdir / f"{video_id}.{ext}"
        if cached.exists() and has_video(cached, ffmpeg):
            return cached

    # Имя со вставкой .audio — не косметика. Раньше звук ложился как
    # {id}.{ext}, ровно туда же, куда fetch_video кладёт целый ролик, и
    # расширение их не различало: .webm бывает и тем и другим. Кэш потом
    # отдавал звук вместо видео, и шортс выходил без картинки.
    existing = sorted(outdir.glob(f"{video_id}.audio.*"))
    if existing:
        return existing[0]

    # Для разбора громкости и распознавания хватает скромного качества,
    # а на медленном канале каждый лишний мегабайт — это секунды ожидания.
    opts = _opts(
        outdir,
        outtmpl={"default": str(outdir / f"{video_id}.audio.%(ext)s")},
        format="bestaudio[abr<=80]/worstaudio/bestaudio/best",
    )
    _download(_with_ffmpeg(opts, ffmpeg), url)

    found = sorted(outdir.glob(f"{video_id}.audio.*"))
    if not found:
        raise RuntimeError("звуковая дорожка не скачалась")
    return found[0]


def fetch_video(url, video_id, height=1080, ffmpeg=None):
    """Качает видео целиком (один mp4). Повторный вызов берёт из кэша.

    YouTube отдаёт картинку и звук раздельно, поэтому yt-dlp нужен свой
    ffmpeg для склейки — а в PATH его на этой машине нет.

    Здесь раньше был fetch_clip — закачка только выбранных секунд, чтобы не
    тянуть весь ролик ради минуты. Замер показал, что расчёт был неверный.
    Точная резка заставляет yt-dlp отдать закачку внешнему ffmpeg, а тот
    берёт поток одним непрерывным GET — и YouTube режет такому потоку
    скорость до темпа проигрывания. Полтора часа ожидания на отрезок в
    минуту, причём висит молча: обрыва нет, байты капают, и ни таймаут
    ffmpeg, ни retries yt-dlp не срабатывают.

    Нативный загрузчик yt-dlp берёт файл кусками по диапазонам и такого
    ограничения не встречает: замерено 8 МБ/с, то есть 37-минутный ролик
    целиком — 34 секунды. Один раз на все моменты, дальше из кэша. Резать
    из целого файла и быстрее, и надёжнее, чем качать отрезками.
    """
    outdir = workdir(video_id)

    # fetch_audio кладёт звук под тем же именем {id}.{ext}, и расширение о
    # содержимом не говорит: без проверки на дорожку сюда возвращается
    # аудиофайл, а резка из него встаёт намертво вместо понятной ошибки.
    for ext in ("mp4", "mkv", "webm"):
        cached = outdir / f"{video_id}.{ext}"
        if cached.exists() and has_video(cached, ffmpeg):
            return cached

    opts = _opts(
        outdir,
        format=(
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        ),
        merge_output_format="mp4",
    )
    _download(_with_ffmpeg(opts, ffmpeg), url)

    for ext in ("mp4", "mkv", "webm"):
        cached = outdir / f"{video_id}.{ext}"
        if cached.exists() and has_video(cached, ffmpeg):
            return cached

    raise RuntimeError("yt-dlp отработал, но файла видео нет — смотри папку work/")
