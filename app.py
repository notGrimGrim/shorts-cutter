"""Приложение для нарезки шортсов: запуск по ярлыку, работа вопрос-ответ.

То же самое, что умеет cut.py, только без флагов в командной строке.
Готовые ролики складываются в папку shorts рядом с приложением.
"""

import os
import subprocess
import sys
from pathlib import Path

from cutter import (
    audio, chapters, cobalt, download, jobs, keywords, layout, pairs, render,
    scan, speech, subs, think, timecode,
)
from cutter.paths import OUT

C = {
    "head": "\033[96m",
    "ok": "\033[92m",
    "warn": "\033[93m",
    "err": "\033[91m",
    "dim": "\033[90m",
    "bold": "\033[1m",
    "off": "\033[0m",
}


def paint(text, color):
    return f"{C[color]}{text}{C['off']}" if C["off"] else text


def _setup_console():
    """Включает цвета и UTF-8: без этого в cmd.exe будет каша."""
    if os.name == "nt":
        os.system("")  # включает обработку ANSI в старых консолях Windows
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.environ.get("NO_COLOR"):
        C.update(dict.fromkeys(C, ""))


def show_folder(folder, pick=None):
    """Открывает папку в проводнике и подсвечивает свежий файл.

    os.startfile из процесса, запущенного от администратора, Windows молча
    игнорирует — поэтому зовём explorer напрямую. Он возвращает единицу даже
    при успехе, так что код возврата не смотрим.
    """
    args = ["explorer", f"/select,{pick}"] if pick else ["explorer", str(folder)]
    try:
        subprocess.run(args, check=False)
    except OSError as error:
        print(paint(f"  не смог открыть папку: {error}", "warn"))
        print(f"  она тут: {folder}")


def ask(question, default=""):
    # Перенос строки внутри вопроса иначе отрывает «?» от самого текста.
    blanks = len(question) - len(question.lstrip("\n"))
    print("\n" * blanks, end="")

    hint = f" {paint('[' + default + ']', 'dim')}" if default else ""
    answer = input(f"{paint('?', 'head')} {question.strip()}{hint}: ")
    # ﻿ прилетает невидимой меткой кодировки, когда ответы подают файлом
    # или вставляют из редактора: глазами не видно, а путь уже не открывается.
    return answer.strip().strip("﻿").strip() or default


def choose(question, options, default=1):
    """Меню из пронумерованных вариантов. Возвращает индекс с нуля."""
    print(f"\n{paint('?', 'head')} {question}")
    for number, text in enumerate(options, 1):
        print(f"    {paint(str(number), 'bold')}  {text}")

    while True:
        answer = input(f"  выбор {paint('[' + str(default) + ']', 'dim')}: ").strip()
        if not answer:
            return default - 1
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(paint(f"  нужно число от 1 до {len(options)}", "warn"))


def pick_numbers(question, limit, allow_none=False):
    """Номера через запятую или пробел. Пустой ввод — только первый.

    allow_none — разрешить ответ «ноль», то есть не брать ничего. Нужно там,
    где список показан не вместо выбора, а в довесок к нему.
    """
    while True:
        answer = ask(question, "0" if allow_none else "1")
        parts = [p for p in answer.replace(",", " ").split() if p]
        if allow_none and any(p == "0" for p in parts):
            return []
        chosen = [int(p) for p in parts if p.isdigit() and 1 <= int(p) <= limit]
        if chosen:
            return sorted(set(chosen))
        print(paint(f"  нужны номера от 1 до {limit}", "warn"))


def show_candidates(found, speed=1.0):
    """Показывает моменты. Про ускорение говорим только если оно подбирается:
    иначе подсказка читается как уже сделанное действие."""
    for number, candidate in enumerate(found, 1):
        head = candidate.text[:160] + ("…" if len(candidate.text) > 160 else "")
        stamp = f"{timecode.fmt(candidate.start)}–{timecode.fmt(candidate.end)}"
        print(
            f"\n  {paint(f'[{number}]', 'bold')} {paint(stamp, 'head')}"
            f"  {paint(f'{candidate.duration:.0f}с', 'dim')}"
        )
        if candidate.title:
            print(f"      {paint(candidate.title, 'ok')}")
        if not speed:
            hint = render.fit_speed(candidate.duration)
            if hint > 1.0:
                print(paint(f"      не влезает — ускорю до {hint}x", "dim"))
        print(f"      {head}")


def ask_words(question):
    """Слова через запятую или пробел. Пустой ответ — ничего не задано."""
    answer = ask(question)
    return [word for word in answer.replace(",", " ").split() if len(word) > 2]


def offer_related(words, seeds, found, limits, picks):
    """Предлагает дорезать по словам, которые ходят рядом с заданными.

    Синоним хорош только тот, что в записи действительно звучит: предложить
    «жалованье» к «зарплате», когда его никто не говорил, — значит предложить
    пустоту. Поэтому соседей берём из самой расшифровки.
    """
    close = keywords.related(words, seeds)
    if not close:
        return found

    print(f"\n  {paint('рядом в записи звучит:', 'dim')} {', '.join(close)}")
    if not ask("Поискать моменты и по этим словам? y/n", "n").lower().startswith(
        ("y", "д")
    ):
        return found

    extra = scan.find(words, *limits, top=5, must=close, **picks)
    # Кусок, который уже выбран, второй раз не предлагаем.
    fresh = [
        candidate for candidate in extra
        if not any(candidate.start < taken.end and taken.start < candidate.end
                   for taken in found)
    ]
    if not fresh:
        print(paint("  по ним ничего нового не нашлось", "dim"))
        return found

    print(paint(f"\n  нашлось ещё моментов: {len(fresh)}", "ok"))
    show_candidates(fresh)
    chosen = pick_numbers(
        "Какие из них добавить (0 — никакие)", len(fresh), allow_none=True
    )
    return found + [fresh[number - 1] for number in chosen]


def run_once(ffmpeg):
    url = ask("Ссылка на видео или путь к файлу").strip('"').strip("'")
    if not url:
        return False

    # Файл на диске — самый короткий путь: скачать ролик можно чем угодно,
    # а дальше сеть не нужна вовсе. Поэтому сначала смотрим, не файл ли это.
    local = None if download.is_url(url) else Path(url)
    if local is not None and not local.exists():
        raise RuntimeError(f"это не ссылка и не файл: {url}")

    print(paint("\n  смотрю, что за видео…", "dim"))
    info = download.local_info(local, ffmpeg) if local else download.probe(url)
    length = timecode.fmt(info["duration"]) if info["duration"] else "?"
    print(f"  {paint(info['title'], 'bold')}  {paint(length, 'dim')}")
    if local:
        print(paint("  файл с диска — в сеть не хожу", "dim"))

    mode = choose(
        "Как выбирать моменты?",
        [
            "Автомат — выберу сам, тебе останется посмотреть",
            "По субтитрам — покажу варианты, выберешь ты",
            "По моим словам — скажу, о чём должен быть шортс",
        ],
    )

    # Shorts у YouTube теперь до трёх минут, поэтому коротким всё не ограничено.
    # Верхняя граница пошире: мысль чаще успевает договориться до конца, и
    # надпись сверху не обрывается на полуслове.
    length = choose(
        "Длина шортса",
        [
            "40–90 секунд",
            "90–150 секунд",
            "25–45 секунд",
        ],
    )
    limits = ((40.0, 90.0), (90.0, 150.0), (25.0, 45.0))[length]

    # Ускорение вмещает больше смысла в те же секунды и лучше удерживает
    # тех, кто быстро теряет внимание.
    pace = choose(
        "Скорость",
        [
            "Обычная",
            "Чуть быстрее — 1.15x",
            "Заметно быстрее — 1.3x",
            "Подобрать под длину — уложить каждый шортс в минуту",
        ],
    )
    speed = (1.0, 1.15, 1.3, 0.0)[pace]

    # Как строить кадр — выбирает человек. «Сам» смотрит на раскладку: если
    # собеседники разобрались, ставит их стопкой во весь экран, если нет —
    # кадр целиком на размытом фоне.
    shape = choose(
        "Как строить кадр",
        [
            "Кадр целиком во всю ширину, сверху и снизу размытие",
            "Собеседников стопкой — друг над другом во весь экран",
            "Сам решу — стопкой, если собеседники разобрались",
        ],
    )
    style = ("blur", "stack", "auto")[shape]

    # У файла с диска выбора нет: субтитров неоткуда взять, а моменты искать
    # надо по всему ролику — значит распознаём целиком. Спрашивать не о чем.
    if local:
        text_from = 2
    else:
        text_from = choose(
            "Откуда брать текст",
            [
                "Субтитры YouTube — мгновенно, но местами врут",
                "Распознать только нарезанные куски — точный текст в кадре",
                "Распознать весь ролик — точнее и выбор моментов, но дольше",
            ],
        )

    note = lambda line: print(paint(f"  {line}", "dim"))

    if local:
        # Тот же файл и есть дорожка: и Whisper, и разбор громкости читают
        # звук из видео сами, отдельный файл заводить незачем.
        track = local
        note("распознаю речь — субтитров у файла нет…")
    else:
        # Звук забираем до фоновой закачки: два yt-dlp, пишущих в одну папку,
        # мешают друг другу, и файл попадает в обработку недокачанным.
        note("качаю звуковую дорожку…")
        try:
            track = download.fetch_audio(url, info["id"], ffmpeg)
        except download.DownloadFailed as error:
            # Последняя ступень. Если и она не настроена — пусть человек
            # видит настоящую причину, а не жалобу на ненастроенный cobalt.
            if not cobalt.configured():
                raise
            note(f"напрямую не вышло: {str(error).splitlines()[0]}")
            local = download.rescue(url, info["id"], on_note=note)
            track = local
            # Файл принесли целиком: субтитров и глав к нему нет, значит
            # текст только распознаванием.
            text_from = 2

    # Модель распознавания — та, что уже лежит на диске. Просить DEFAULT
    # вслепую нельзя: на чужой машине её может не быть, и мастер молча
    # уходил качать гигабайты, показывая застывшую строку «распознаю…».
    model = speech.ready()

    if text_from == 2:
        words = speech.transcribe(
            ffmpeg, track, download.workdir(info["id"]), model, on_note=note
        )
    else:
        note("качаю субтитры…")
        try:
            words = subs.parse_vtt(download.fetch_subs(url, info["id"]))
        except RuntimeError as error:
            # download.fetch_subs роняет именно это сообщение, когда на
            # YouTube нет вообще никаких субтитров — тогда распознаём сами,
            # тем же путём, что и режим «Распознать весь ролик» (text_from
            # == 2), вместо того чтобы ронять весь запуск.
            if "не нашлись" not in str(error):
                raise
            note(f"! {error}")
            note("субтитров нет — распознаю речь сам")
            words = speech.transcribe(
                ffmpeg, track, download.workdir(info["id"]), model, on_note=note
            )

    if not words:
        raise RuntimeError("расшифровка пустая, выбирать не по чему")

    # Название ролика подсказывает, что искать: «Собеседование DevOps»
    # значит моменты про девопс должны весить больше.
    terms = keywords.from_title(info["title"])

    # Глава короче самого шортса бесполезна — окно в неё не поместится.
    marks = chapters.from_info(info, limits[0])
    if marks:
        print(paint(f"  глав размечено: {len(marks)}", "dim"))

    energy = None
    try:
        note("слушаю дорожку…")
        # Тот же моно-16 кГц, что готовился для распознавания.
        energy = audio.per_word(
            words,
            audio.levels(ffmpeg, speech.track_wav(
                ffmpeg, track, download.workdir(info["id"])
            )),
        )
    except RuntimeError as error:
        print(paint(f"  ! звук не разобрался, продолжаю без него: {error}", "warn"))

    picks = {"boost": terms, "chapters": marks, "energy": energy}

    if mode == 2:
        # Человек сам говорит, о чём шортс. Слова обязательны: кусок без них
        # не годится, сколько бы баллов он ни набрал по остальным признакам.
        seeds = []
        while not seeds:
            seeds = ask_words("Какие слова должны прозвучать (через запятую)")
            if not seeds:
                print(paint("  без слов этот режим не работает", "warn"))

        found = scan.find(words, *limits, top=10, must=seeds, **picks)
        if not found:
            raise RuntimeError(
                f"с этими словами ничего не нашлось: {', '.join(seeds)}.\n"
                f"  Попробуй другие или выбери первый режим."
            )
        print(paint(f"\n  моментов с твоими словами: {len(found)}", "ok"))
        show_candidates(found)
        picked = pick_numbers("\nКакие резать (номера через запятую)", len(found))
        found = [found[number - 1] for number in picked]
    elif mode == 0:
        count = ask("Сколько шортсов сделать", "3")
        want = int(count) if count.isdigit() else 3
        seeds = keywords.top_terms(words, 6)

        # Шортс собирается вокруг вопроса: сперва он — из главы или из речи
        # ведущего, — потом ответ на него. В автомате человек моменты не
        # смотрит, поэтому хуки сравнивает модель, если есть чем.
        found = pairs.plan(words, marks, terms, energy, want,
                           video_title=info["title"], on_note=note)
        if not found:
            note("пар «вопрос → ответ» не нашлось — ищу по-старому")
            found = scan.find(words, *limits, top=want, **picks)
        if not found:
            raise RuntimeError("подходящих кусков не нашлось")

        print(paint(f"\n  выбрал моментов: {len(found)}", "ok"))
        show_candidates(found)
    else:
        seeds = keywords.top_terms(words, 6)
        found = scan.find(words, *limits, top=10, **picks)
        if not found:
            raise RuntimeError("подходящих кусков не нашлось")
        print(f"\n  {paint('опорные слова:', 'dim')} {', '.join(keywords.top_terms(words, 8))}")
        show_candidates(found)
        picked = pick_numbers("\nКакие резать (номера через запятую)", len(found))
        found = [found[number - 1] for number in picked]

    # Во всех режимах: смотрим, что звучит рядом с опорными словами, и
    # предлагаем дорезать по ним. В автомате тоже — иначе человек не узнает,
    # что рядом лежала тема, которую он бы взял.
    found = offer_related(words, seeds, found, limits, picks)

    OUT.mkdir(parents=True, exist_ok=True)

    if not local:
        # Ролик берём целиком один раз на все моменты: качать отрезками
        # оказалось медленнее и ненадёжно, подробности в fetch_video.
        note("качаю ролик…")
        local = download.fetch_video(url, info["id"], ffmpeg=ffmpeg)

    # Шортсы одного ролика — в свою папку: за несколько прогонов в shorts/
    # иначе сваливается каша, где не видно, что откуда.
    folder = OUT / download.safe_name(info["title"])
    folder.mkdir(parents=True, exist_ok=True)

    # Кто где сидит в кадре. Считается один раз на ролик и решает, собирать
    # шортс стопкой лиц или оставить кадр целиком на размытом фоне.
    tiles = layout.detect(
        ffmpeg, local, info["duration"],
        cache=download.workdir(info["id"]) / "layout.json", on_note=note,
    )
    if style == "auto":
        style = "stack" if len(tiles) > 1 else "blur"

    made = []
    for number, candidate in enumerate(found, 1):
        span = f"{timecode.fmt(candidate.start)}–{timecode.fmt(candidate.end)}"

        # Резать будем прямо из файла: ffmpeg отматывает к нужной секунде
        # сам, промежуточная копия только тратит время и место.
        note(f"режу отрезок {number}/{len(found)}: {span}…")

        # Точный текст нужен только там, где он ляжет в кадр: субтитров
        # YouTube хватает, чтобы момент выбрать, но в кадре видна каждая
        # ошибка. for_segment возвращает слова уже во времени ролика.
        if text_from == 1:
            note("распознаю текст этого куска…")
            spoken = speech.for_segment(
                ffmpeg, local, candidate.start, candidate.end, model,
                on_note=note,
            )
        else:
            spoken = words

        # Плашка сверху: сперва из главы, её писал автор. Глав нет — достаём
        # вопрос из самой речи куска, он и есть крючок.
        # У пары «вопрос → ответ» надпись уже готова — это её хук; у
        # кандидата из scan берём заголовок главы или вопрос из речи.
        banner = (
            chapters.hook(getattr(candidate, "hook", "") or candidate.title)
            if (getattr(candidate, "hook", "") or candidate.title)
            else chapters.from_speech(candidate.text)
        ) or None
        part = (f"ч.{candidate.part}"
                if getattr(candidate, "parts", 1) > 1 else "")

        # Под надпись в стопке отводим полосу сверху: иначе она ложится на
        # лицо первого собеседника — стопка занимает кадр целиком.
        band = render.HOOK_BAND if style == "stack" and banner else 0

        ass = download.workdir(info["id"]) / f"seg_{int(candidate.start)}.ass"
        render.write_ass(
            ass, subs.phrases(spoken), candidate.start, candidate.end,
            banner, keys=terms,
            hook_margin=render.HOOK_STACKED if band else 0, part=part,
        )
        pushed = render.fit_speed(candidate.duration, speed)
        if pushed > 1.0:
            note(f"ускоряю до {pushed}x — иначе {candidate.duration:.0f}с")

        named = f"{banner} {part}".strip() if part else banner
        out = folder / f"{download.clip_name(named, info['id'], candidate.start)}.mp4"
        made.append(
            render.render(
                ffmpeg, local, out, candidate.start, candidate.end,
                style, ass, speed=pushed, tiles=tiles, band=band,
            )
        )

    # Исходник больше не нужен: субтитры и разметку оставляем, видео убираем.
    freed = download.drop_media(info["id"])
    if freed:
        print(paint(f"  освободил {freed:.0f} МБ — исходник удалён", "dim"))

    print(paint(f"\n  готово: {len(made)} шт. в shorts/{folder.name}", "ok"))
    for path in made:
        print(f"    {path.name}  {paint(f'{path.stat().st_size / 1048576:.1f} МБ', 'dim')}")

    if ask("\nОткрыть папку? y/n", "y").lower().startswith(("y", "д")):
        # Открываем папку самого ролика, а не общий shorts/: иначе человек
        # попадает в список папок и ищет свежую нарезку глазами.
        show_folder(folder, made[0] if made else None)

    return True


def main():
    _setup_console()
    jobs.enable_kill_on_exit()
    print(paint("\n   === Нарезка шортсов ===", "head"))
    print(paint("  вертикальные ролики с субтитрами из длинного видео\n", "dim"))

    try:
        ffmpeg = render.find_ffmpeg()
    except render.FfmpegMissing as error:
        print(paint(f"  {error}", "err"))
        input("\n  Enter — закрыть ")
        return

    print(paint(f"  кодирую через: {render.encoder(ffmpeg)[1]}", "dim"))

    stale = download.sweep(hours=1.0)
    if stale:
        print(paint(f"  подчистил старое сырьё: {stale:.0f} МБ", "dim"))
    print()

    while True:
        try:
            if not run_once(ffmpeg):
                break
        except KeyboardInterrupt:
            print(paint("\n  прервано", "warn"))
            break
        except Exception as error:
            print(paint(f"\n  ! {error}", "err"))

        if not ask("\nЕщё видео? y/n", "n").lower().startswith(("y", "д")):
            break

    print(paint("\n  Посмотри готовое глазами — автомат не судья.", "dim"))
    input("  Enter — закрыть ")


if __name__ == "__main__":
    main()
