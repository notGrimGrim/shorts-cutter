"""Своё распознавание речи вместо готовых субтитров YouTube.

Автосубтитры YouTube врут на терминах и именах: «Интервьюеверы», «спенсорсами»,
«Здик пройдут». Whisper распознаёт заметно точнее и, что важнее для выбора
моментов, расставляет пунктуацию — по ней ищутся границы фраз.

Модель бесплатная и работает локально: файл скачивается один раз, дальше
ни интернета, ни ключей, ни оплаты не нужно.
"""

import json
import os
import site
import subprocess
import tempfile
from pathlib import Path

from . import keywords, machine
from .subs import Word

# Чем крупнее модель, тем точнее и медленнее. medium заметно устойчивее small
# на терминах и именах — а именно на них расшифровка и врала. На видеокарте
# разница во времени небольшая, на процессоре — в разы, и это стоит помнить,
# когда дело дойдёт до сборки для чужой машины.
MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT = "medium"

# Беглого прохода здесь больше нет — почему, написано в vocabulary().


class Unavailable(RuntimeError):
    pass


def available():
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _find_cuda_libs():
    """Показывает Windows, где лежат библиотеки CUDA.

    Пакеты nvidia-* кладут .dll внутрь site-packages, а не в системные папки,
    и сами по себе они не находятся — путь надо назвать явно.
    """
    folders = []
    for base in {*site.getsitepackages(), site.getusersitepackages()}:
        root = Path(base) / "nvidia"
        if root.exists():
            folders += [str(p) for p in root.glob("*/bin")]

    for folder in folders:
        # add_dll_directory помогает не всегда: он действует только на вызовы
        # с особым флагом, а ctranslate2 грузит библиотеки обычным способом —
        # для таких работает единственное, PATH.
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass

    if folders:
        os.environ["PATH"] = os.pathsep.join(folders + [os.environ.get("PATH", "")])


# Где faster-whisper держит скачанные модели и как называет их папки.
_REPO = "models--Systran--faster-whisper-{}"

# Недокачанная модель оставляет папку с описанием и без веса. Самая маленькая
# из настоящих — tiny, и та занимает 75 МБ, так что порог берём с запасом.
_REAL = 40 * 1024 * 1024


def _cache():
    for name in ("HUGGINGFACE_HUB_CACHE", "HF_HOME"):
        folder = os.environ.get(name)
        if folder:
            root = Path(folder)
            return root / "hub" if name == "HF_HOME" else root
    return Path.home() / ".cache" / "huggingface" / "hub"


def _weight(model):
    """Сколько места занимает скачанное. Ноль — значит модели нет."""
    folder = _cache() / _REPO.format(model)
    if not folder.exists():
        return 0
    try:
        return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    except OSError:
        return 0


def installed():
    """Модели, готовые к работе прямо сейчас.

    Предлагать человеку то, чего на диске нет, — обман: за выбором стоит
    молчаливая закачка на гигабайты, и на плохом канале она выглядит
    зависшей программой. Поэтому в списке только скачанное.
    """
    return tuple(model for model in MODELS if _weight(model) >= _REAL)


def ready(model=DEFAULT):
    """Модель, которой можно работать: заданная либо лучшая из скачанных."""
    if _weight(model) >= _REAL:
        return model
    have = installed()
    # MODELS идут от простой к сложной, поэтому последняя — самая крупная.
    return have[-1] if have else model


# Ниже этой уверенности слово в словарь-подсказку не идёт (см. vocabulary).
# Значение взято с потолка и не проверено замером: в кэше расшифровок оценка
# модели до сих пор не сохранялась, сравнить ослышки с настоящими словами
# было не на чем. Ошибка здесь дёшева — слишком высокий порог оставит
# подсказку пустой, то есть вернёт поведение, которое и так было до словаря.
SURE_ENOUGH = 0.65

# Сколько кусков звука отдавать видеокарте разом. Без батчей она простаивает
# между короткими шагами декодирования: замер на восьми минутах речи —
# 55.5 с обычным проходом против 13.6 с батчами, вчетверо.
BATCH = 16

# Ширина луча. Дефолт библиотеки — 5, и снижать его до 1 заманчиво: замер
# даёт ещё в полтора раза быстрее. Но сходимость с независимой записью той
# же речи падает с 89.5% до 88.1%, а в тексте появляется «что с случаем в
# OCE» вместо «что случилось» — и это ложится в кадр вшитыми субтитрами.
# Поэтому луч полный.
BEAM = 5


def _engine(model, device=None, batched=True):
    """Модель распознавания. batched — грузить видеокарту пачками.

    device по умолчанию выбирает machine.describe(): на чужой машине карты
    NVIDIA может не быть вовсе, и просить у ctranslate2 «cuda» там значит
    просто упасть. На процессоре батчинг не помогает — там узкое место не
    простой между шагами, а сама арифметика, — поэтому туда обычный движок.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise Unavailable(
            "не установлен faster-whisper — поставь: pip install faster-whisper"
        ) from None

    here = machine.describe()
    device = device or here["device"]

    if device != "cuda":
        return WhisperModel(
            model, device="cpu", compute_type="int8",
            cpu_threads=here["threads"],
        )

    _find_cuda_libs()
    engine = WhisperModel(model, device="cuda", compute_type=here["compute"])
    if not batched:
        return engine

    try:
        from faster_whisper import BatchedInferencePipeline
    except ImportError:
        # Старая версия библиотеки — просто работаем как раньше, медленнее.
        return engine
    return BatchedInferencePipeline(engine)


def _run(engine, track, language, hint=None):
    """Гоняет распознавание до конца.

    Модель отдаёт отрезки лениво, поэтому обходим их прямо здесь: нехватка
    библиотек CUDA всплывает только на первом реальном вычислении.
    """
    options = dict(
        language=language,
        word_timestamps=True,
        # Тишину и музыку не распознаём: иначе модель придумывает слова.
        vad_filter=True,
        # Словарь темы: показываем, как пишутся термины, которые сейчас
        # прозвучат. См. keywords.speech_hint.
        initial_prompt=hint or None,
        beam_size=BEAM,
        # Модель НЕ кормится собственным прошлым выводом. По умолчанию
        # библиотека делает наоборот, и на музыке, скандировании и криках это
        # кончается петлёй: замер на стриме CS2 (xtxjqOJJvhM) — 571 слово, из
        # них «победа» 125 раз, «турнире» 135, то есть ~70% расшифровки одна
        # фраза по кругу. Модель повторяла то, что сама же выдала шагом раньше.
        # Связность между отрезками от этого страдает, но связный бред хуже
        # разрозненной правды.
        condition_on_previous_text=False,
        # Встроенный антизацикливатель: если кусок вышел слишком
        # повторяющимся (compression_ratio) или слишком неуверенным
        # (log_prob), библиотека перегоняет его заново с температурой повыше.
        # Раньше здесь стоял жёсткий temperature=0 — то есть ровно этот
        # механизм был выключен, и петлю ловить было нечем.
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        # Кусок, который модель сама сочла тишиной, в текст не идёт: на
        # рекламных вставках и музыке она охотно придумывает слова.
        no_speech_threshold=0.6,
    )
    # Батчевый движок отличается только этим параметром; обычный его не знает.
    if type(engine).__name__ == "BatchedInferencePipeline":
        options["batch_size"] = BATCH

    segments, _ = engine.transcribe(str(track), **options)

    words = []
    for segment in segments:
        for word in segment.words or ():
            text = word.word.strip()
            if text:
                words.append(Word(
                    float(word.start), float(word.end), text,
                    # Своя оценка модели: на ослышках она заметно проседает,
                    # и это единственный признак ошибки, который у нас есть.
                    sure=float(getattr(word, "probability", 1.0) or 0.0),
                ))
    return words


# Приметы того, что модель не скачалась: сеть, а не вычисление.
_NETWORK = ("connection", "timeout", "timed out", "temporary failure",
            "cas client", "reconstruction", "sending request", "max retries",
            "ssl", "getaddrinfo", "connectionerror", "hfhub", "huggingface")


def _download_failed(message):
    return any(mark in message.lower() for mark in _NETWORK)


def _transcribe(track, model, language, on_note=None, hint=None):
    """Считает тем, что есть на этой машине; не вышло — процессором.

    Устройство не задаём жёстко: на машине без карты NVIDIA просить «cuda»
    значит гарантированно упасть и только потом откатиться. Спрашиваем
    machine.describe(), который проверяет это у самого ctranslate2.
    """
    try:
        return _run(_engine(model), track, language, hint)
    except Exception as error:
        text = str(error)

        # Модель не докачалась. Процессор тут ничем не поможет: он полезет
        # за тем же файлом по тому же каналу, и человек будет смотреть на
        # молчащее окно ещё столько же. Берём то, что уже лежит на диске.
        if _download_failed(text):
            have = installed()
            spare = have[-1] if have else None
            if spare and spare != model:
                if on_note:
                    on_note(
                        f"модель {model} не скачалась ({text.splitlines()[0][:90]}). "
                        f"Беру {spare} — она уже на диске"
                    )
                return _run(_engine(spare), track, language, hint)
            raise Unavailable(
                f"модель {model} не скачалась, а других на диске нет.\n"
                f"  Проверь сеть и повтори — качается она один раз."
            ) from None

        # Видеокарте нужны библиотеки CUDA, которых на машине может не быть.
        # Это не повод падать — процессор посчитает то же самое, только дольше.
        # Но сваливать на видеокарту всё подряд нельзя: если дело не в ней,
        # так и скажем, иначе потом будем чинить не то.
        if on_note:
            blame = "видеокарта" if "cud" in text.lower() else "распознавание"
            on_note(f"{blame} не сработало ({error}), пробую процессором")
        return _run(_engine(model, "cpu", batched=False), track, language, hint)


def _cache_file(workdir, model):
    return workdir / f"whisper-{model}.json"


def _to_wav(ffmpeg, track, folder, start=None, duration=None, name="piece.wav"):
    """Готовит звук для модели: моно, 16 кГц, обычный WAV.

    Своим декодером Whisper спотыкается на контейнерах вроде m4a, а ffmpeg
    у нас и так есть — пусть он и разбирает файл.
    """
    piece = Path(folder) / name
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(track)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", str(piece)]

    done = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"не смог подготовить звук из {Path(track).name}: "
            f"{done.stderr.strip() or 'ffmpeg молча отказался'}"
        )
    return piece


# Имя общей дорожки в рабочей папке ролика. Расширение .wav попадает в
# download.MEDIA, поэтому файл уходит вместе с остальным сырьём и на диске
# не копится.
TRACK_WAV = "audio-16k.wav"


def track_wav(ffmpeg, source, workdir):
    """Дорожка ролика одним файлом: моно, 16 кГц, рядом с прочим сырьём.

    Тот же звук нужен и распознаванию, и разбору громкости, и каждый из них
    раньше декодировал исходник заново — а сорокаминутный mp4 разбирается
    десяток секунд. Готовим один раз и отдаём всем.
    """
    ready = Path(workdir) / TRACK_WAV
    # Пустой или обрезанный файл остаётся после прерванного прогона: лучше
    # переделать, чем скормить модели тишину.
    if ready.exists() and ready.stat().st_size > 1024:
        return ready

    Path(workdir).mkdir(parents=True, exist_ok=True)
    return _to_wav(ffmpeg, source, workdir, name=TRACK_WAV)


def for_segment(ffmpeg, track, start, end, model=DEFAULT, language="ru",
                on_note=None, hint=None):
    """Распознаёт только выбранный кусок и возвращает слова в его времени.

    Гонять модель по всему ролику незачем: для выбора моментов хватает
    субтитров YouTube, а точный текст нужен лишь там, где он ляжет в кадр.
    Минута вместо получаса — это в разы быстрее любой видеокарты.
    """
    with tempfile.TemporaryDirectory() as folder:
        piece = _to_wav(ffmpeg, track, folder, start, end - start)
        words = _transcribe(piece, model, language, on_note, hint)

    # Время внутри куска отсчитывалось от нуля — возвращаем к времени ролика.
    for word in words:
        word.start += start
        word.end += start
    return words


def vocabulary(workdir, title="", on_note=None):
    """Словарь ролика для подсказки распознавателю.

    Whisper принимает initial_prompt — образец текста, по которому подхватывает
    написание терминов и имён. Образец должен быть из этого же разговора,
    иначе он не помогает.

    Раньше словарь добывался беглым проходом: лёгкая модель слушала пять
    кусков по 45 секунд по всей длине. На живых роликах это дало «знаю,
    компании, целом, нам, почему» — частотную лексику вместо терминов, и
    понятно почему: пять окон — это два процента сорокаминутного разговора,
    и трижды в такой выборке попадается только то, что говорят всегда и все.
    Пользы ноль, а вред возможен: подсказать ослышку — значит закрепить её,
    модель послушная. Заодно проход стоил 13 секунд и грузил видеокарту даже
    тогда, когда расшифровка шла в облако.

    Поэтому словарь берётся из полной расшифровки, которая уже лежит в папке
    ролика: на ней тот же keywords.learned_terms выдаёт «Полиграф»,
    «Headhunter», «IDE», «нейронкой» — то, ради чего подсказка и нужна.
    На первом прогоне расшифровки ещё нет, и подсказка стоит на названии;
    на повторном (другая длина шортса, другие моменты) она уже настоящая.
    """
    cache = workdir / "hint.txt"
    known = sorted(workdir.glob("whisper-*.json"))

    # Подсказка, записанная до расшифровки, собрана вслепую — по одному
    # названию. Появилась расшифровка — пересобираем по ней.
    if cache.exists() and (
        not known or cache.stat().st_mtime > known[0].stat().st_mtime
    ):
        return cache.read_text(encoding="utf-8")

    heard = ()
    if known:
        try:
            saved = json.loads(known[0].read_text(encoding="utf-8"))
            # Только то, в чём модель сама уверена. Подсказка — это образец
            # ПРАВОПИСАНИЯ, и ослышка в ней закрепляет ошибку: на стриме CS2
            # в словарь попали «ПОПЕДА» и «КАЕВ», и следующий прогон получал
            # их как эталон. Модель послушная — писала бы так и дальше.
            trusted = [
                Word(w["start"], w["end"], w["text"], sure=w.get("sure", 1.0))
                for w in saved
                if w.get("sure", 1.0) >= SURE_ENOUGH
            ]
            heard = keywords.learned_terms(trusted)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Без словаря распознавание просто останется прежним — это не повод
            # ронять весь запуск.
            if on_note:
                on_note(f"словарь собрать не вышло ({error}), обойдусь названием")

    hint = keywords.speech_hint(title, heard)
    if on_note and heard:
        on_note(f"словарь ролика: {', '.join(heard)}")

    cache.write_text(hint, encoding="utf-8")
    return hint


def where_to_run(duration=None):
    """Чем считать: облаком или своей машиной. Возвращает 'cloud' или 'local'.

    Правило простое и объяснимое человеку:

    * нет ключа — считаем сами, других вариантов нет;
    * есть карта NVIDIA — считаем сами: две минуты на часовой ролик, ради
      сорока секунд бесплатную квоту жечь незачем, она пригодится в дороге;
    * карты нет — идём в облако, потому что процессор это полчаса, и
      человек просто закроет программу.

    Квоту здесь не проверяем: это забота cloud.transcribe, который умеет
    сказать точную причину. Наше дело — предпочтение.
    """
    from . import cloud

    if not cloud.ready():
        return "local"
    return "local" if machine.describe()["fast"] else "cloud"


def _from_cloud(ffmpeg, track, duration, language, hint, on_note):
    """Пробует облако. Не вышло — возвращает None, и считаем сами."""
    from . import cloud

    try:
        return cloud.transcribe(ffmpeg, track, duration, language, hint, on_note)
    except cloud.QuotaSpent as stop:
        # Не ошибка: бесплатная квота кончилась, это ожидаемый исход.
        if on_note:
            on_note(str(stop))
    except cloud.CloudFailed as error:
        if on_note:
            on_note(f"облако не сработало ({error}), считаю на этой машине")
    return None


def transcribe(ffmpeg, track, workdir, model=DEFAULT, language="ru",
               on_note=None, hint=None, duration=None, where="auto"):
    """Распознаёт дорожку и отдаёт слова с таймкодами.

    Результат кладётся рядом с видео: распознавание идёт минуты, повторять
    его на том же ролике незачем.

    where — 'auto', 'cloud' или 'local'. При 'auto' решает where_to_run.
    Облако при любой неудаче тихо откатывается на местный расчёт: программа
    обязана работать и без ключа, и без сети.
    """
    cache = _cache_file(workdir, model)
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if on_note:
            on_note(f"беру готовую расшифровку ({len(saved)} слов)")
        # sure в старых кэшах нет: там оценка модели терялась при записи.
        # Единица значит «сомнений не заявлено» — то же, что было раньше.
        return [
            Word(w["start"], w["end"], w["text"], sure=w.get("sure", 1.0))
            for w in saved
        ]

    if where == "auto":
        where = where_to_run(duration)

    words = None
    if where == "cloud" and duration:
        words = _from_cloud(ffmpeg, track, duration, language, hint, on_note)

    if words is None:
        words = _transcribe(
            track_wav(ffmpeg, track, workdir), model, language, on_note, hint
        )

    cache.write_text(
        json.dumps(
            # sure пишем в кэш: это единственный признак ослышки, который у нас
            # есть, а терялся он ровно здесь — на повторном прогоне все слова
            # становились одинаково «верными», и словарь-подсказка собирался
            # из мусора наравне с настоящими терминами (см. vocabulary).
            [
                {"start": w.start, "end": w.end, "text": w.text,
                 "sure": round(w.sure, 3)}
                for w in words
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if on_note:
        on_note(f"распознано слов: {len(words)}")

    return words
