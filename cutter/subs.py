"""Разбор .vtt субтитров YouTube в чистый поток слов с таймкодами.

Автосубтитры YouTube — это "бегущая строка": каждая реплика повторяет хвост
предыдущей плюс пара новых слов, а внутри лежат теги вида
``<00:00:01.019><c>слово</c>`` с точным временем каждого слова.
Наивный парсер выдаёт текст с тройными повторами, поэтому здесь мы
вытаскиваем именно слова с их временем и режем дубли по таймкоду.
"""

import html
import re
from dataclasses import dataclass

_CUE_TIME = re.compile(
    r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})"
)
_WORD_TAG = re.compile(r"<(\d{2}:\d{2}:\d{2}\.\d{3})><c>(.*?)</c>", re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]*>")

# Хвост последнего слова, когда следующего уже нет.
_TAIL = 0.6


# Пометки YouTube внутри реплик: [смех], [музыка], [ __ ] вместо мата.
_NOTE = re.compile(r"^\[.*\]$|^_+$")
_LAUGH = re.compile(r"смех|хохот|laugh", re.IGNORECASE)


@dataclass
class Word:
    start: float
    end: float
    text: str
    # Растёт на каждом «>>» — так субтитры помечают смену говорящего.
    speaker: int = 0
    # Прямо перед этим словом в субтитрах отмечен смех.
    laugh: bool = False
    # Насколько распознаватель уверен в этом слове, 0..1. У субтитров YouTube
    # такой оценки нет, поэтому по умолчанию считаем, что слово верное.
    sure: float = 1.0


def _ts(value):
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _cues(raw):
    """Разбивает файл на (start, end, строки реплики).

    Идём построчно, а не блоками через пустую строку: у YouTube внутри
    реплики попадается строка из одного пробела, и разбиение по пустым
    строкам разрывает реплику пополам, теряя её текст.
    """
    start = end = None
    body = []

    def flush():
        # Номер реплики стоит перед её таймкодом — он прилипает к предыдущей.
        if body and body[-1].isdigit():
            body.pop()
        if start is not None and body:
            return start, end, body
        return None

    for line in raw.replace("\r\n", "\n").split("\n"):
        match = _CUE_TIME.search(line)
        if match:
            ready = flush()
            if ready:
                yield ready
            start, end = _ts(match.group(1)), _ts(match.group(2))
            body = []
        elif start is not None and line.strip():
            # В файле всё экранировано: «>>» лежит как «&gt;&gt;», неразрывный
            # пробел — как «&nbsp;». Без этого мусор уедет прямо в кадр.
            body.append(html.unescape(line).strip())

    ready = flush()
    if ready:
        yield ready


def parse_vtt(path):
    """Читает .vtt и возвращает список Word без повторов, по возрастанию времени."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    words = []
    last = -1.0
    speaker = 0
    laughed = False

    for cue_start, cue_end, body in _cues(raw):
        # Строки над последней — это хвост предыдущей реплики, который
        # бегущая строка тащит за собой. Живой текст всегда в последней.
        active = body[-1]
        tagged = _WORD_TAG.findall(active)

        if tagged:
            # Слово до первого тега звучит с самого начала реплики.
            head = _ANY_TAG.sub("", active.split("<", 1)[0]).strip()
            timed = [(cue_start, w) for w in head.split() if w]
            timed += [(_ts(t), _ANY_TAG.sub("", w).strip()) for t, w in tagged]
        else:
            # Реплика на пару миллисекунд без тегов — это YouTube "схлопывает"
            # только что показанную строку, нового текста в ней нет.
            if cue_end - cue_start < 0.05:
                continue
            # Ручные субтитры: времени по словам нет, раскладываем равномерно.
            text = _ANY_TAG.sub("", " ".join(body)).strip()
            if not text:
                continue
            # Маркеры смены говорящего мешают сверке с уже разобранным.
            chunks = [c for c in text.split() if not c.startswith(">")]
            if not chunks:
                continue
            span = max(cue_end - cue_start, 0.1) / len(chunks)
            # Реплика без тегов часто начинается с уже сказанного — здесь
            # время не подскажет, что это повтор, сверяем сам текст.
            fresh = _drop_said(words, chunks)
            shift = len(chunks) - len(fresh)
            timed = [
                (cue_start + (shift + i) * span, w) for i, w in enumerate(fresh)
            ]

        for start, text in timed:
            text = text.replace("\xa0", " ").strip()
            if not text:
                continue

            # «>>» — смена говорящего. Сам знак в кадр не нужен, а вот факт
            # смены полезен: диалог живее монолога.
            if text.startswith(">"):
                speaker += 1
                text = text.lstrip("> ").strip()
                if not text:
                    continue

            # [смех], [музыка], [ __ ] вместо мата — это пометки, а не речь.
            if _NOTE.match(text):
                laughed = laughed or bool(_LAUGH.search(text))
                continue

            # Бегущая строка повторяет уже сказанное — время не растёт, значит дубль.
            if start <= last:
                continue

            words.append(Word(start, start + _TAIL, text, speaker, laughed))
            laughed = False
            last = start

    words = _glue_punctuation(words)

    # У тегов YouTube есть только начало слова. Если считать концом слова
    # начало следующего, паузы в речи схлопываются в ноль и границы фраз
    # найти уже нечем — поэтому прикидываем, сколько слово звучит.
    for current, following in zip(words, words[1:]):
        current.end = min(following.start, current.start + _spoken(current.text))

    return words


def _drop_said(words, chunks, look_back=60):
    """Срезает у реплики начало, которое уже прозвучало.

    Ищем самое длинное совпадение конца уже разобранного текста с началом
    новой реплики — ровно так выглядит бегущая строка YouTube.
    """
    tail = [word.text for word in words[-look_back:]]
    for size in range(min(len(tail), len(chunks)), 0, -1):
        if tail[-size:] == chunks[:size]:
            return chunks[size:]
    return chunks


def _glue_punctuation(words):
    """Знак препинания отдельным токеном — приклеиваем к предыдущему слову."""
    merged = []
    for word in words:
        if merged and not any(char.isalnum() for char in word.text):
            merged[-1].text += word.text
            continue
        merged.append(word)
    return merged


def _spoken(text):
    """Грубая оценка длительности слова — ~14 знаков в секунду плюс призвук."""
    return max(0.18, 0.07 * len(text))


def phrases(words, max_words=6, max_gap=0.7, max_dur=3.0):
    """Группирует слова в короткие фразы — по такой строке за раз и показываем.

    Рвём группу по смыслу: на конце предложения, на запятой, на паузе в речи
    и на смене говорящего. Ограничение по числу слов — последнее средство,
    иначе строка рвётся посреди мысли.
    """
    groups = []
    current = []

    for word in words:
        if current:
            too_long = len(current) >= max_words
            too_slow = word.start - current[-1].end > max_gap
            too_wide = word.end - current[0].start > max_dur
            # Строка, склеившая реплики двух людей, читается как обрывок.
            new_voice = word.speaker != current[-1].speaker
            if too_long or too_slow or too_wide or new_voice:
                groups.append(current)
                current = []
        current.append(word)
        # Конец предложения рвёт строку всегда, запятая — только если
        # накопилось достаточно слов, чтобы строка не вышла куцей.
        if word.text.endswith((".", "!", "?", "…")) or (
            word.text.endswith((",", ";", ":", "—")) and len(current) >= 3
        ):
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    # Отдаём сами слова, а не склеенную строку: субтитры показываются
    # по мере проговаривания, и время каждого слова для этого нужно.
    return [(g[0].start, g[-1].end, g) for g in groups if g]


def text_between(words, start, end):
    """Расшифровка куска — чтобы показать человеку, что в кандидате звучит."""
    return " ".join(w.text for w in words if start <= w.start < end)
