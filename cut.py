"""Нарезка длинных видео на вертикальные шортсы с вшитыми субтитрами.

Два режима, один скрипт:

    авто — кидаешь ссылки, забираешь готовые шортсы
    python cut.py auto <url> [<url> ...] [--count 3]

    ручной — смотришь, что предлагает скрипт, и режешь выбранное
    python cut.py scan <url>
    python cut.py cut  <url> --pick 3
    python cut.py cut  <url> --from 12:30 --to 13:15

    python cut.py info <url>     что за видео и какие у него субтитры

Материал берётся с YouTube по ссылке либо из локального файла.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cutter import (
    audio, chapters, cobalt, download, jobs, keywords, layout, local, pairs,
    render, scan, speech, subs, think, timecode,
)
from cutter.paths import OUT

# Где winget/установщик Ollama оставляет её, если она не попала в PATH —
# тот же приём, что render._WINGET для ffmpeg.
_OLLAMA_INSTALL = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"

_ollama_started = False


def _ensure_ollama():
    """Поднимает Ollama, если она стоит на машине, но сейчас не запущена.

    Без этого Groq, упёршись в квоту, откатывается сразу на свой счёт —
    хотя локальная модель (см. cutter/local.py) всё это время могла бы
    стоять запасным бэкендом. Не страшно, если не завелась: ready()/ask()
    и так молча возвращают False/None на недоступной службе, разбор
    продолжается без неё, как раньше.
    """
    global _ollama_started
    if _ollama_started or os.name != "nt" or local.running():
        return
    _ollama_started = True

    exe = shutil.which("ollama") or (
        str(_OLLAMA_INSTALL) if _OLLAMA_INSTALL.exists() else None
    )
    if not exe:
        return

    try:
        subprocess.Popen(
            [exe, "serve"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("  подняла Ollama на этой машине — запасной бэкенд, если Groq кончится")
    except OSError:
        pass


def _utf8_console():
    """Windows-консоль иначе спотыкается о кириллицу в выводе."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _is_url(source):
    return source.startswith(("http://", "https://", "www."))


def _slug(source):
    return download.probe(source)["id"] if _is_url(source) else Path(source).stem


def _load_words(source, lang, subs_path=None):
    """Достаёт поток слов: из указанного .vtt, из локального файла или из сети."""
    if subs_path:
        return subs.parse_vtt(Path(subs_path))

    if _is_url(source):
        info = download.probe(source)
        return subs.parse_vtt(download.fetch_subs(source, info["id"], lang))

    path = Path(source)
    if path.suffix.lower() == ".vtt":
        return subs.parse_vtt(path)

    sibling = sorted(path.parent.glob(f"{path.stem}*.vtt"))
    if sibling:
        return subs.parse_vtt(sibling[0])

    raise SystemExit(
        f"для {path.name} не нашёл субтитров рядом — укажи их флагом --subs"
    )


def _video(source, slug, height, ffmpeg):
    """Локальный файл как есть, ссылка — качаем (повторно берётся из кэша)."""
    if _is_url(source):
        return download.fetch_video(source, slug, height, ffmpeg)

    path = Path(source)
    if not path.exists():
        raise SystemExit(f"файла {path} нет")
    return path


def _title(source):
    """Название ролика: у ссылки — из метаданных, у файла — из имени."""
    return download.probe(source)["title"] if _is_url(source) else Path(source).stem


def _must_words(args):
    """Слова из --must: без них кусок не берём вовсе."""
    raw = getattr(args, "must", "") or ""
    return [word for word in raw.replace(",", " ").split() if len(word) > 2]


def _track(source, slug, ffmpeg):
    """Дорожка для разбора: файл с диска или скачанный звук.

    Последняя ступень — cobalt: он приносит ролик целиком, и дальше файл
    ничем не отличается от лежащего на диске.
    """
    if not _is_url(source):
        return Path(source)

    try:
        return download.fetch_audio(source, slug, ffmpeg)
    except download.DownloadFailed:
        if not cobalt.configured():
            raise
        return download.rescue(source, slug, on_note=lambda note: print(f"  {note}"))


def _search_terms(source, args):
    """Что искать в субтитрах: слова из названия ролика плюс заданные вручную.

    Если ролик называется «Собеседование DevOps», то и моменты, где реально
    говорят про девопс, должны весить больше остальных.
    """
    terms = [word for word in args.boost.split(",") if word.strip()]
    return terms + keywords.from_title(_title(source))


def _candidates_file(slug):
    return download.workdir(slug) / "candidates.json"


def _moments(words, marks, terms, energy, source, count, brain=True, talk=None):
    """Что резать: пары «вопрос → ответ», отобранные по хуку.

    Порядок работы задан замыслом: сначала вопрос — из главы, если она есть,
    иначе из речи ведущего, — потом поиск ответа на него. Модель тут не ищет
    границы, она только сравнивает хуки между собой и проверяет, есть ли в
    ответе конкретика. Границы даёт сам разговор, и это надёжнее: следующий
    вопрос невозможно найти неправильно.

    Длинные пары делятся надвое — «ч.1» и «ч.2», обе с той же надписью.
    """
    if brain:
        _ensure_ollama()
    # audio.per_word отдаёт энергию списком, позиционно по словам (тот же
    # контракт, что и у keywords.weights) — так её ждут scan.find и cut.py.
    # pairs.rate внутри себя обращается к energy.get(index, ...), то есть
    # ждёт словарь. Заворачиваем на границе, а не меняем сам список: другие
    # вызовы (scan.find) полагаются на список как есть.
    return pairs.plan(
        words, marks, terms, dict(enumerate(energy)) if energy else None,
        count, video_title=_title(source), source=source, brain=brain,
        on_note=talk or (lambda note: print(f"  {note}")),
    )


def _banner(words, start, end, title, keys, video_title=""):
    """Надпись сверху. Пустой не бывает: кадр без неё не объясняет себя.

    Порядок опор — от самой надёжной к самой грубой:

    1. заголовок главы, если он есть: его писал автор ролика;
    2. речь самого куска — фраза, которая про то же, о чём кусок;
    3. ключевые слова куска, если целого предложения из речи не собралось;
    4. название ролика — хуже всего подходит по месту, зато есть всегда.
    """
    if title:
        return chapters.hook(title)

    if words:
        spoken = subs.text_between(words, start, end)
        found = chapters.from_speech(spoken, keys=keys)
        if found:
            return found

    return chapters.from_keys(keys) or chapters.hook(video_title)


def _tiles(ffmpeg, video, slug):
    """Раскладка собеседников в кадре. Считается один раз на ролик."""
    return layout.detect(
        ffmpeg, video,
        download.local_info(video, ffmpeg)["duration"],
        cache=download.workdir(slug) / "layout.json",
        on_note=lambda note: print(f"  {note}"),
    )


def _frame_mode(args, tiles):
    """Как строить кадр. 'auto' — стопкой, если в кадре есть кого поставить."""
    if args.mode != "auto":
        return args.mode
    return "stack" if len(tiles) > 1 else "blur"


def _make(ffmpeg, video, slug, words, start, end, args, out=None, title="",
          keys=(), video_title="", folder=None, tiles=(), part=""):
    """Собирает один шортс и возвращает путь к нему.

    start/end — время в исходном ролике: по ним и берутся слова, и режется
    сам файл. Раздельных координат резки больше нет: режем всегда из целого
    ролика, промежуточных кусочков со своим временем с нуля не бывает.
    """
    hook = ""
    if not args.no_title:
        hook = _banner(words, start, end, title, keys, video_title)

    mode = _frame_mode(args, tiles)

    # Стопкой кадр занят целиком, и надпись легла бы человеку на лоб. Поэтому
    # под неё сверху отводится чёрная полоса, а картинка опускается под неё.
    band = render.HOOK_BAND if mode == "stack" and hook else 0

    subs_path = None
    if words or hook:
        subs_path = download.workdir(slug) / f"seg_{int(start)}_{int(end)}.ass"
        lines = render.write_ass(
            subs_path, subs.phrases(words) if words else [], start, end, hook,
            hook_margin=render.HOOK_STACKED if band else 0, part=part,
        )
        if not lines and not hook:
            print("  ! в этом отрезке нет реплик — режу без субтитров")
            subs_path = None

    if out is None:
        folder = folder or OUT
        folder.mkdir(parents=True, exist_ok=True)
        # Пометку части ставим и в имя файла: иначе две половины одного
        # ответа лежат рядом и не отличаются ничем, кроме таймкода.
        named = f"{hook} {part}".strip() if part else hook
        out = folder / f"{download.clip_name(named, slug, start)}.mp4"

    # Разгоняем только то, что само не влезло: кусок с ответом бывает длиннее
    # формата, и обрывать его на полуслове хуже, чем ускорить.
    speed = render.fit_speed(end - start, args.speed)

    print(
        f"  режу {timecode.fmt(start)}–{timecode.fmt(end)} "
        f"({end - start:.0f}с, {mode})…"
    )
    if speed > 1.0:
        print(f"  ускоряю до {speed}x — {end - start:.0f}с не влезают в формат")
    result = render.render(
        ffmpeg, video, out, start, end, mode, subs_path,
        args.zoom, args.pan, args.tilt, not args.cpu, speed=speed, tiles=tiles,
        band=band,
    )
    print(f"  готово: {result}  ({result.stat().st_size / 1024 / 1024:.1f} МБ)")
    return result


def cmd_info(args):
    info = download.probe(args.source)
    print(f"{info['title']}")
    print(f"  id:          {info['id']}")
    if info["duration"]:
        print(f"  длительность:{timecode.fmt(info['duration']):>9}")
    print(f"  субтитры:    {', '.join(info['subs']) or '—'}")
    auto = info["auto_subs"]
    shown = ", ".join(auto[:12]) + (" …" if len(auto) > 12 else "")
    print(f"  авто:        {shown or '—'}")


def _transcribe(source, slug, args, ffmpeg=None):
    """Расшифровывает дорожку через Whisper — путь `--whisper` и путь
    автоматического ухода при отсутствии субтитров используют один и тот же
    код, чтобы результат ничем не отличался от явного флага.
    """
    ffmpeg = ffmpeg or render.find_ffmpeg(getattr(args, "ffmpeg", None))
    track = _track(source, slug, ffmpeg)
    talk = lambda note: print(f"  {note}")
    length = (download.probe(source)["duration"] if _is_url(source)
              else download.local_info(source, ffmpeg)["duration"])
    return speech.transcribe(
        # cmd_cut зовёт этот путь тоже (см. ниже), а у него нет своего
        # --model — getattr берёт тот же DEFAULT, что и у speech.transcribe.
        ffmpeg, track, download.workdir(slug),
        getattr(args, "model", speech.DEFAULT), args.lang,
        on_note=talk,
        # Длительность нужна облаку: по ней проверяется бесплатная квота
        # до отправки, а не после отказа сервера.
        duration=length,
        where=getattr(args, "engine", "auto"),
        # Словарь ролика — из его же расшифровки, если она уже есть в
        # рабочей папке. На первом прогоне подсказка стоит на названии.
        hint=speech.vocabulary(
            download.workdir(slug), _title(source), on_note=talk
        ),
    )


def _analyze(source, args, ffmpeg=None):
    """Собирает всё, на чём строится выбор: слова, главы, звук, слова-цели."""
    slug = _slug(source)

    if args.whisper:
        words = _transcribe(source, slug, args, ffmpeg)
    else:
        try:
            words = _load_words(source, args.lang, getattr(args, "subs", None))
        except RuntimeError as error:
            # download.fetch_subs роняет именно это сообщение, когда на
            # YouTube нет вообще никаких субтитров (ни ручных, ни авто) —
            # тогда сами включаем Whisper, а не валим весь ролик. Другие
            # сбои (сеть, битый .vtt) так не называются и идут наверх как
            # раньше.
            if not _is_url(source) or "не нашлись" not in str(error):
                raise
            print(f"  ! {error}")
            print("  ухожу на локальный Whisper")
            words = _transcribe(source, slug, args, ffmpeg)

    if not words:
        raise SystemExit("расшифровка пустая — разбирать нечего")

    marks = ()
    if _is_url(source):
        marks = chapters.from_info(download.probe(source), args.min)
        if marks:
            print(f"  главы: {len(marks)} — ищу лучшее внутри каждой")

    energy = None
    if not args.no_audio:
        try:
            ffmpeg = ffmpeg or render.find_ffmpeg(getattr(args, "ffmpeg", None))
            track = _track(source, slug, ffmpeg)
            # Тот же моно-16 кГц, что готовился для распознавания: разбирать
            # исходник заново — лишний десяток секунд на ровном месте.
            energy = audio.per_word(
                words,
                audio.levels(ffmpeg, speech.track_wav(
                    ffmpeg, track, download.workdir(slug)
                )),
            )
            if energy:
                print("  звук разобран")
        except RuntimeError as error:
            print(f"  ! звук не разобрался, продолжаю без него: {error}")

    return slug, words, marks, _search_terms(source, args), energy


def cmd_scan(args):
    slug, words, marks, terms, energy = _analyze(args.source, args)

    must = _must_words(args)

    # Со словами-обязательствами ищем по-старому: там человек задаёт, о чём
    # шортс, и пары «вопрос → ответ» этот заказ не выполняют.
    if must:
        found = scan.find(
            words, args.min, args.max, args.top, terms, marks, energy, must=must
        )
    else:
        found = _moments(words, marks, terms, energy, args.source, args.top,
                         brain=args.brain)
        if not found:
            found = scan.find(
                words, args.min, args.max, args.top, terms, marks, energy
            )

    if not found:
        raise SystemExit(
            f"ни одного куска длиной {args.min:.0f}–{args.max:.0f}с"
            + (f" со словами {', '.join(must)}" if must else "")
            + " — попробуй раздвинуть --min/--max"
        )

    if must:
        close = keywords.related(words, must)
        if close:
            print(f"  рядом в записи звучит: {', '.join(close)}")
            print(f"  можно поискать и по ним: --must {','.join(close[:3])}")

    print(f"\nОпорные слова видео: {', '.join(keywords.top_terms(words))}\n")

    path = _candidates_file(slug)
    path.write_text(
        json.dumps(scan.to_json(found), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Кандидаты для {slug} — {len(found)} шт.\n")
    for index, candidate in enumerate(found, 1):
        head = candidate.text[:150] + ("…" if len(candidate.text) > 150 else "")
        print(
            f"[{index:2d}] {timecode.fmt(candidate.start)}–{timecode.fmt(candidate.end)}"
            f"  ({candidate.duration:.0f}с, вес {candidate.score})"
        )
        if candidate.title:
            print(f"     глава: {candidate.title}")
        print(f"     {head}\n")

    print(f"Сохранено в {path}")
    print(f"Нарезать:  python cut.py cut {args.source} --pick 1")


def _pick_range(args, slug):
    """Границы куска: либо явные таймкоды, либо кандидат из scan."""
    if args.start is not None and args.end is not None:
        return timecode.parse(args.start), timecode.parse(args.end), ""

    if args.pick is None:
        raise SystemExit("нужен либо --pick N, либо пара --from/--to")

    path = _candidates_file(slug)
    if not path.exists():
        raise SystemExit(
            f"нет {path} — сначала прогони `python cut.py scan {args.source}`"
        )

    found = json.loads(path.read_text(encoding="utf-8"))
    if not 1 <= args.pick <= len(found):
        raise SystemExit(f"--pick вне диапазона 1..{len(found)}")

    chosen = found[args.pick - 1]
    return chosen["start"], chosen["end"], chosen.get("title", "")


def cmd_cut(args):
    ffmpeg = render.find_ffmpeg(args.ffmpeg)
    print(f"кодирую через: {render.encoder(ffmpeg, not args.cpu)[1]}")
    slug = _slug(args.source)

    start, end, title = _pick_range(args, slug)
    if end <= start:
        raise SystemExit("конец куска раньше начала")

    words = None
    if not args.no_subs:
        try:
            words = _load_words(args.source, args.lang, args.subs)
        except RuntimeError as error:
            # Тот же уход на Whisper, что и в _analyze() (scan/auto) — без
            # него `cut --pick N` падал на роликах без субтитров, хотя
            # тот же ролик секундой раньше прошёл scan через тот же Whisper.
            if not _is_url(args.source) or "не нашлись" not in str(error):
                raise
            print(f"  ! {error}")
            print("  ухожу на локальный Whisper")
            words = _transcribe(args.source, slug, args, ffmpeg)
    video = _video(args.source, slug, args.height, ffmpeg)

    _make(ffmpeg, video, slug, words, start, end, args, args.out, title,
          tiles=_tiles(ffmpeg, video, slug))
    print("Посмотри глазами перед отправкой — автомат не судья.")


def cmd_auto(args):
    """Полный проход без участия человека: ссылка на входе, шортсы на выходе."""
    ffmpeg = render.find_ffmpeg(args.ffmpeg)
    print(f"кодирую через: {render.encoder(ffmpeg, not args.cpu)[1]}")
    made, failed = [], []

    for number, source in enumerate(args.sources, 1):
        print(f"\n[{number}/{len(args.sources)}] {source}")
        try:
            slug, words, marks, terms, energy = _analyze(source, args, ffmpeg)

            found = _moments(words, marks, terms, energy, source, args.count,
                             brain=args.brain)
            if not found:
                # Пар «вопрос → ответ» не нашлось: бывает на монологах и на
                # роликах, где расшифровка потеряла все знаки вопроса. Тогда
                # работает прежний расчёт по плотности речи.
                print("  пар «вопрос → ответ» не нашлось — ищу по-старому")
                found = scan.find(
                    words, args.min, args.max, args.count, terms, marks, energy,
                    must=_must_words(args),
                )
            if not found:
                raise RuntimeError(
                    f"не нашлось кусков длиной {args.min:.0f}–{args.max:.0f}с"
                )

            print(f"  выбрано моментов: {len(found)}")

            # Ролик берём целиком и режем из него все моменты: качать
            # отрезками оказалось медленнее и ненадёжно, подробности в
            # download.fetch_video. Скачанное она же держит в кэше, так
            # что второй и третий момент идут уже без сети.
            video = _video(source, slug, args.height, ffmpeg)
            # Кто где сидит в кадре: по этому шортс собирается стопкой лиц,
            # а не полоской видео посреди размытия.
            tiles = _tiles(ffmpeg, video, slug)
            # Шортсы одного ролика — в свою папку: за несколько прогонов
            # в shorts/ иначе сваливается каша, где не видно, что откуда.
            title = _title(source)
            folder = OUT / download.safe_name(title)
            for candidate in found:
                # У пары «вопрос → ответ» надпись лежит в hook, а пометка
                # части идёт отдельной строкой; у кандидата из scan есть
                # только заголовок главы.
                hook = getattr(candidate, "hook", "") or candidate.title
                part = (f"ч.{candidate.part}"
                        if getattr(candidate, "parts", 1) > 1 else "")
                made.append(
                    _make(
                        ffmpeg, video, slug, None if args.no_subs else words,
                        candidate.start, candidate.end, args,
                        title=hook, keys=terms, part=part,
                        video_title=title, folder=folder, tiles=tiles,
                    )
                )
        except Exception as error:
            print(f"  ! пропускаю: {error}")
            failed.append((source, error))

    print(f"\nГотово: {len(made)} шортсов в {OUT}/")
    for path in made:
        # Показываем с папкой ролика — без неё непонятно, что откуда.
        print(f"  {path.parent.name}/{path.name}")
    if failed:
        print(f"Не получилось: {len(failed)}")
        for source, error in failed:
            print(f"  {source} — {error}")
    print("\nАвтомат выбирает грубо — пересмотри глазами перед публикацией.")


def _add_subs_flags(parser, with_file=True):
    parser.add_argument("--lang", default="ru", help="язык субтитров (по умолчанию ru)")
    if with_file:
        parser.add_argument("--subs", help="свой файл субтитров .vtt")
    parser.add_argument("--no-subs", action="store_true", help="не вшивать субтитры")


def _add_window_flags(parser):
    parser.add_argument("--min", type=float, default=40.0, help="минимум секунд")
    parser.add_argument("--max", type=float, default=90.0, help="максимум секунд")
    parser.add_argument(
        "--boost", default="",
        help="через запятую слова, вокруг которых искать: оффер,зарплата",
    )
    parser.add_argument(
        "--must", default="",
        help="слова, без которых кусок не брать вовсе: зарплата,вилка. "
             "В отличие от --boost меняет не порядок, а сам набор",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="не разбирать звук (быстрее, но живые моменты хуже видно)",
    )
    parser.add_argument(
        "--whisper", action="store_true",
        help="распознать речь самим вместо субтитров YouTube — точнее, но дольше",
    )
    parser.add_argument(
        "--no-brain", dest="brain", action="store_false",
        help="не звать модель: хуки сравниваются своим счётом. По умолчанию "
             "модель зовётся — сначала Groq, потом локальная через Ollama, — "
             "она сравнивает вопросы между собой и проверяет, есть ли в "
             "ответе конкретика",
    )
    parser.add_argument(
        "--engine", default="auto", choices=("auto", "cloud", "local"),
        help="чем распознавать: auto — сам решит по машине и ключу, "
             "cloud — через Groq (нужен ключ, быстро на любой машине), "
             "local — на этой машине",
    )
    parser.add_argument(
        "--model", default=speech.ready(), choices=speech.MODELS,
        help=f"модель распознавания (по умолчанию {speech.ready()}). "
             f"Уже скачаны: {', '.join(speech.installed()) or 'ни одной'}; "
             f"остальные потянутся из сети на первом запуске",
    )


def _add_render_flags(parser):
    parser.add_argument(
        "--mode", choices=("blur", "auto", "stack", "crop"), default="blur",
        help="blur — кадр целиком во всю ширину, сверху и снизу размытие "
             "(по умолчанию); stack — собеседники стопкой во весь экран; "
             "auto — стопкой, если раскладка разобралась, иначе blur; "
             "crop — центральный кроп",
    )
    parser.add_argument("--height", type=int, default=1080, help="качество скачивания")
    parser.add_argument("--ffmpeg", help="путь к ffmpeg.exe, если он не в PATH")
    parser.add_argument(
        "--no-title", action="store_true",
        help="не показывать заголовок главы в начале шортса",
    )
    parser.add_argument(
        "--cpu", action="store_true",
        help="кодировать процессором, не трогая видеокарту",
    )
    parser.add_argument(
        "--speed", type=float, default=None,
        help="ускорить готовый шортс, например 1.15; "
             "без него разгон подбирается сам и только если кусок не влез",
    )
    parser.add_argument(
        "--zoom", type=float, default=1.0, help="приблизить план, например 1.3",
    )
    parser.add_argument(
        "--pan", type=float, default=0.0, help="сдвиг рамки вбок, -0.5…0.5",
    )
    parser.add_argument(
        "--tilt", type=float, default=0.0,
        help="сдвиг рамки вверх/вниз, -0.5…0.5 (минус — вверх)",
    )


def main():
    _utf8_console()
    jobs.enable_kill_on_exit()

    parser = argparse.ArgumentParser(
        prog="cut.py",
        description="Нарезка длинных видео на вертикальные шортсы с субтитрами",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    info = sub.add_parser("info", help="что за видео и какие у него субтитры")
    info.add_argument("source", help="ссылка на YouTube")
    info.set_defaults(func=cmd_info)

    auto = sub.add_parser("auto", help="полный автомат: ссылки на входе, шортсы на выходе")
    auto.add_argument("sources", nargs="+", help="одна или несколько ссылок/файлов")
    auto.add_argument("--count", type=int, default=3, help="сколько шортсов с видео")
    _add_window_flags(auto)
    _add_subs_flags(auto, with_file=False)
    _add_render_flags(auto)
    auto.set_defaults(func=cmd_auto)

    scan_cmd = sub.add_parser("scan", help="показать кандидатов с расшифровкой")
    scan_cmd.add_argument("source", help="ссылка на YouTube, .vtt или видеофайл")
    scan_cmd.add_argument("--subs", help="свой файл субтитров .vtt")
    scan_cmd.add_argument("--lang", default="ru", help="язык субтитров")
    scan_cmd.add_argument("--top", type=int, default=10, help="сколько кандидатов показать")
    _add_window_flags(scan_cmd)
    scan_cmd.set_defaults(func=cmd_scan)

    cut_cmd = sub.add_parser("cut", help="нарезать выбранный кусок")
    cut_cmd.add_argument("source", help="ссылка на YouTube или видеофайл")
    cut_cmd.add_argument("--pick", type=int, help="номер кандидата из scan")
    cut_cmd.add_argument("--from", dest="start", help="начало, например 12:30")
    cut_cmd.add_argument("--to", dest="end", help="конец, например 13:15")
    cut_cmd.add_argument("-o", "--out", help="куда положить mp4")
    _add_subs_flags(cut_cmd)
    _add_render_flags(cut_cmd)
    cut_cmd.set_defaults(func=cmd_cut)

    args = parser.parse_args()
    # Скачанное старше часа уже точно не пригодится, а место занимает.
    stale = download.sweep(hours=1.0)
    if stale:
        print(f"подчистил старое сырьё: {stale:.0f} МБ")

    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit("\nпрервано")
    except RuntimeError as error:
        raise SystemExit(f"! {error}")


if __name__ == "__main__":
    main()
