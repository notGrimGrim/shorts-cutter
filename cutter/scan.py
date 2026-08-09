"""Поиск кандидатов на шортс. Никаких моделей — только арифметика.

Для каждого положения окна складываем сигналы:

* плотность речи — чтобы не попасть в паузу;
* характерность слов (см. keywords) — про что этот кусок;
* слова из названия ролика — про то ли он, что заявлено в заголовке;
* глава — вес авторского заголовка, если разметка есть;
* вопрос в начале — у канала про собеседования это готовая смысловая единица;
* конкретика — суммы и сроки цепляют сильнее рассуждений;
* звук — где голос громче и живее обычного;
* чистые границы — начало после точки, конец на точке.

Паузы и знаки препинания именно поощряются, а не требуются: у автосубтитров
и время, и пунктуация приблизительные, жёсткий фильтр оставляет ноль вариантов.
"""

import re
from dataclasses import dataclass, asdict

from . import keywords

PAUSE = 0.28
STRONG_PAUSE = 0.7

# Вклад каждого сигнала. Плотность речи нормирована к единице и служит мерой.
TOPIC = 0.9
BOOST = 0.4
EDGE = 0.2
CHAPTER = 0.6

# Заголовок главы описывает то, с чего глава начинается. Чем дальше кусок
# от её начала, тем меньше надпись сверху соответствует происходящему.
#
# 0.35 → 0.9, и окно вдвое уже: с обещанным ответом выгоднее стало не
# растягивать конец, а поджимать начало — штраф за неотвеченность падал
# одинаково с обеих сторон, а короткий кусок ещё и выигрывал в плотности.
# Ринат это увидел сразу: «кусок обрезался влево, а не вправо как нужно».
# Теперь начало почти прибито к началу главы, и расти можно только вправо.
NEAR_START = 0.9
NEAR_SPAN = 30.0
QUESTION = 0.35
NUMBER = 0.25
AUDIO = 0.5

# Кусок, где не прозвучало ни одного слова по теме канала, — это болтовня
# вокруг темы. Редкие слова TF-IDF считает важными, хотя «нос» и «запахи»
# в ролике про собеседования весят ровно ничего.
OFFTOPIC = 0.5

# Зритель решает за первые секунды. Если начало куска не про тему —
# неважно, что там дальше: до «дальше» никто не досмотрит.
HEAD_SECONDS = 15.0
HEAD_PENALTY = 0.6

# Доля слов с точкой на конце, при которой пунктуации можно доверять.
PUNCTUATED = 0.04

# Реклама и самореклама. Это не штраф, а запрет: кусок с такими словами не
# берётся ни за какие баллы. Штраф здесь не работал — рекламная вставка сама
# по себе бодрая, плотная и с цифрами, поэтому набирала больше, чем теряла
# на штрафе, и пролезала в шортсы как «интересный момент».
NEVER = (
    # прямая реклама
    "промокод", "по промоко", "спонсор", "реклам", "рекламн",
    "наш партн", "при поддержке", "партнёр выпуска", "партнер выпуска",
    "скидк", "бесплатный вебинар", "бесплатный курс", "успей записаться",
    "регистрируйся", "регистрация по ссылке", "переходи по ссылке",
    "ссылка в описании", "в описании ссылк", "ссылку оставлю",
    "первый месяц бесплатно", "пробный период", "оставляйте заявку",
    "ссылки на продукты", "продукты прикреплю", "описание прикреплю",
    # самореклама канала
    "подпишись", "подпишитесь", "подписывайтесь", "подписка на канал",
    "колокольчик", "ставьте лайк", "поставь лайк", "жми лайк",
    "телеграм канал", "телеграм-канал", "тг канал", "мой канал",
    # служебная обвязка выпуска
    "всем привет", "здравствуйте", "меня зовут", "приятного просмотра",
    "с вами снова", "как обычно", "погнали", "начнём выпуск", "начнем выпуск",
    "не забудь", "не забывайте",
    # обращение к аудитории вместо разговора: в шортсе зритель не видел
    # ни выпуска, ни комментариев под ним, и такой хвост обрывает мысль
    # ровно там, где ждали ответа.
    "пишите в комментар", "пиши в комментар", "в комментариях стараюсь",
    "отвечаю в комментар", "задавайте вопросы", "задавай вопросы",
    "напишите в комментар", "жду ваших комментар",
)

# Сколько слов вперёд просматриваем, распознавая метку: «ссылка в описании»
# — четыре слова, и попасть надо любым из них.
MARK_AHEAD = 4

# С этих слов начинать кусок нельзя: они отсылают к тому, чего в шортсе нет.
DANGLING = frozenset("""
а и но вот это этот эта эти то тот та те там тут тогда потому поэтому
значит короче так таким затем далее он она они его её ее их им ими him
причём причем кстати ну да нет ведь же типа получается собственно
""".split())

# Насколько глубоко в куске ещё считается, что он «начинается с вопроса».
QUESTION_HEAD = 16

# Сколько секунд ответа должно уместиться после вопроса и насколько такому
# куску разрешено превысить обычную длину, чтобы ответ договорился.
#
# Было 22 — для здешнего гостя это едва начало мысли, а не ответ: штраф
# обнулялся через 22с после вопроса, и дальше искать было незачем, даже если
# по сути ответ только начинался. На живых роликах кусок так и останавливался
# ровно на +22с к вопросу, независимо от того, сколько там ещё оставалось
# места по ANSWER_STRETCH/ANSWER_ROOM.
# Порядок важностей здесь обратный привычному: раскрытая тема важнее длины.
# Формулировка Рината — «пусть даже ускоренный будет 100 или 120 сек, плевать,
# главное чтобы был контекст». Поэтому вопрос-кусок тянется, пока ответ не
# договорён, а формат вытягивает потом render.fit_speed разгоном.
#
# 22 → 45 → 60 → 75: каждый предыдущий потолок обрывал ответ на середине.
# На 45 из шести шортсов три получили «тема не раскрыта».
ANSWER_MIN = 75.0
ANSWER_STRETCH = 2.0
ANSWER_PENALTY = 1.2

# Но не бесконечно: даём договорить, а не рассказать ещё историю. Потолок
# теперь второй по счёту после ANSWER_STRETCH и держит запас в секундах, а
# не в разах — на коротком заказе (--max 30) множитель сам по себе почти
# ничего не прибавляет, а ответу нужны те же секунды.
#
# 20 → 35 → 75: и 20, и 35 упирались раньше, чем ответ договаривался.
ANSWER_ROOM = 75.0

# Хвост обещанного ответа: последние секунды должны нести либо конкретику,
# либо тему. Пустой хвост — признак, что человека прервали на середине мысли,
# и зритель уходит без того, что ему пообещала надпись в кадре.
TAIL_SECONDS = 12.0
TAIL_TOPIC = 0.12
TAIL_PENALTY = 0.7

# Между двумя кусками должно остаться столько разговора. Без этого два
# соседних окна — это один и тот же эпизод, разрезанный надвое: шортсы выходят
# похожими, и зритель, увидев оба, второй раз не досмотрит.
APART = 90.0

# Ниже этого кусок не берём, сколько бы их ни просили. Запрошенное число —
# потолок, а не цель: три хороших момента лучше, чем три хороших и пять
# натянутых, потому что натянутые уносят просмотры у хороших.
FLOOR = 1.0

_ENDS = (".", "!", "?", "…")
_DIGIT = re.compile(r"\d")
_BIG = re.compile(r"тысяч|миллион|лям|\bк\b|\bкк\b", re.IGNORECASE)


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    text: str
    title: str = ""

    @property
    def duration(self):
        return self.end - self.start


def _gap_before(words, index):
    return None if index <= 0 else words[index].start - words[index - 1].end


def _gap_after(words, index):
    if index >= len(words) - 1:
        return None
    return words[index + 1].start - words[index].end


def _pause_bonus(gap):
    if gap is None or gap >= STRONG_PAUSE:
        return EDGE
    return EDGE * 0.6 if gap >= PAUSE else 0.0


def _entry(words, index):
    """Чистый вход: после конца предложения либо после паузы."""
    if index == 0 or words[index - 1].text.endswith(_ENDS):
        return EDGE
    return _pause_bonus(_gap_before(words, index))


def _exit(words, index):
    """Чистый выход: мысль договорена, а не оборвана на полуслове."""
    if words[index].text.endswith(_ENDS):
        return EDGE
    return _pause_bonus(_gap_after(words, index))


def _prefix(values):
    """Накопительные суммы: иначе одно и то же считалось бы миллионы раз."""
    running = [0.0]
    for value in values:
        running.append(running[-1] + value)
    return running


def _is_concrete(text):
    """Сумма, срок, количество — то, на чём взгляд останавливается."""
    return bool(_DIGIT.search(text) or _BIG.search(text))


def _question_at(words, first):
    """Где заканчивается вопрос, если кусок с него начинается. Иначе None."""
    for index in range(first, min(first + QUESTION_HEAD, len(words))):
        if words[index].text.endswith("?"):
            return index
        if words[index].text.endswith((".", "!")):
            return None
    return None


def _bare(text):
    return text.lower().strip(".,!?:;—–-…\"'«»()")


def _never_flags(words):
    """Отмечает слова, попавшие в рекламную или служебную вставку."""
    lowered = [_bare(word.text) for word in words]
    flags = []
    for index in range(len(words)):
        ahead = " ".join(lowered[index : index + MARK_AHEAD])
        flags.append(1.0 if any(mark in ahead for mark in NEVER) else 0.0)
    return flags


def _chapter_of(chapters, moment):
    for chapter in chapters:
        if chapter.start <= moment < chapter.end:
            return chapter
    return None


def find(
    words, min_dur=40.0, max_dur=90.0, top=10,
    boost=(), chapters=(), energy=None, must=(),
):
    """Возвращает непересекающихся кандидатов, лучшие сверху.

    boost — слова, которые тянуть вверх (обычно из названия ролика).
    must — слова, без которых кусок не берём вовсе. Разница с boost
    принципиальная: boost меняет порядок, must меняет сам набор. Нужно,
    когда человек ищет конкретное — «покажи, где он говорит про зарплату».
    chapters — авторская разметка; если она есть, кусок не должен вылезать
    за границы своей главы, иначе шортс склеит две разные темы.
    energy — громкость на каждое слово, 0..1, если звук уже разобран.
    """
    if not words:
        return []

    topic = _prefix(keywords.weights(words))

    wanted = {keywords.normalize(word) for word in boost}
    wanted.discard("")
    hits = _prefix(
        [1.0 if keywords.normalize(w.text) in wanted else 0.0 for w in words]
    )

    # Обязательные слова считаем отдельно от boost: их не должно быть жалко
    # смешивать с темой канала, иначе «зарплата» растворится среди «девопса».
    needed = {keywords.normalize(word) for word in must}
    needed.discard("")
    need = _prefix(
        [1.0 if keywords.normalize(w.text) in needed else 0.0 for w in words]
    ) if needed else None
    concrete = _prefix([1.0 if _is_concrete(w.text) else 0.0 for w in words])
    never = _prefix(_never_flags(words))
    sound = _prefix(energy) if energy else None

    dead = [0.0]
    for previous, following in zip(words, words[1:]):
        gap = following.start - previous.end
        dead.append(dead[-1] + (gap if gap > STRONG_PAUSE else 0.0))

    entry = [_entry(words, i) for i in range(len(words))]
    exit_ = [_exit(words, i) for i in range(len(words))]

    # Если пунктуация в расшифровке есть, требуем начинать и заканчивать
    # на границе предложения: иначе шортс стартует с «или не буду?».
    ends = sum(1 for w in words if w.text.endswith(_ENDS))
    strict = ends / len(words) >= PUNCTUATED

    # Где заканчивается «начало» каждого возможного куска.
    heads = []
    edge = 0
    for first in range(len(words)):
        edge = max(edge, first)
        while (
            edge + 1 < len(words)
            and words[edge + 1].end <= words[first].start + HEAD_SECONDS
        ):
            edge += 1
        heads.append(edge)

    scored = []
    for first in range(len(words)):
        if strict and first and not words[first - 1].text.endswith(_ENDS):
            continue
        if _bare(words[first].text) in DANGLING:
            continue

        chapter = _chapter_of(chapters, words[first].start) if chapters else None
        if chapters and chapter is None:
            continue

        asked_at = _question_at(words, first)

        # Вопрос бывает задан не в самом куске, а в заголовке главы: «Заменят
        # ли нейронки рекрутеров». Для зрителя разницы никакой — надпись висит
        # в кадре весь шортс и обещает ответ, — значит и спрос с куска тот же.
        # Без этого крючок был хорош, а ответа за ним не оказывалось.
        # Любой заголовок главы — уже обещание: он висит в кадре весь шортс,
        # и зритель ждёт, что тему раскроют. «Курсы „войти в ИБ за 3 месяца“»
        # формально не вопрос, но обещает разбор ничуть не меньше, чем
        # «Стоит ли идти на курсы» — а без promised кусок не имел права
        # растянуться до ответа и поджимался вместо этого с начала.
        promised = asked_at is not None or bool(chapter)

        # Кусок с вопросом обязан вместить ответ, поэтому ему разрешено
        # тянуться дольше обычного: оборванный ответ хуже длинного шортса,
        # а лишнюю длину потом отыграем ускорением.
        room = max_dur
        if promised:
            room = min(max_dur * ANSWER_STRETCH, max_dur + ANSWER_ROOM)
        limit = words[first].start + room
        # Глава не пускает кусок за свою границу — иначе шортс склеит две
        # разные темы. Но у главы-вопроса ответ по смыслу — прямое продолжение
        # той же темы, даже если автор разметил его отдельной главой: вопрос
        # часто идёт своей маленькой главой, а ответ — уже следующей. Держать
        # тут потолок chapter.end и обещанный запас на ответ разом нельзя:
        # без этой оговорки данные из-под ANSWER_STRETCH/ANSWER_ROOM никогда
        # не срабатывали — глава обрезала кусок раньше, чем запас кончался.
        if chapter and not promised:
            limit = min(limit, chapter.end)

        head = QUESTION if asked_at is not None else 0.0
        if chapter:
            offset = (words[first].start - chapter.start) / NEAR_SPAN
            head += NEAR_START * max(0.0, 1.0 - offset)
        # Тема должна прозвучать сразу, а не когда-нибудь потом.
        opening = min((hits[heads[first] + 1] - hits[first]) / 2.0, 1.0)
        head -= HEAD_PENALTY * (1.0 - opening)

        best = None
        last = first

        while last + 1 < len(words) and words[last + 1].end <= limit:
            last += 1
            duration = words[last].end - words[first].start
            if duration < min_dur:
                continue
            if strict and not words[last].text.endswith(_ENDS):
                continue

            # Заказанного слова в куске нет — он не нужен ни за какие баллы.
            if need is not None and need[last + 1] - need[first] <= 0:
                continue

            # Реклама и «подпишитесь» — не контент. Выбрасываем сразу, а не
            # штрафуем: штраф такая вставка перебивала бодростью и цифрами.
            if never[last + 1] - never[first] > 0:
                continue

            count = last - first + 1
            density = min(count / duration / 2.8, 1.0)
            weight = (topic[last + 1] - topic[first]) / count
            # Сколько раз кусок вообще коснулся темы канала. Четырёх упоминаний
            # хватает, чтобы считать его своим; ноль — это болтовня рядом с темой.
            asked = min((hits[last + 1] - hits[first]) / 4.0, 1.0)
            facts = min((concrete[last + 1] - concrete[first]) * 0.25, 1.0)
            drag = min((dead[last] - dead[first]) / duration, 0.5)
            loud = (sound[last + 1] - sound[first]) / count if sound else 0.0

            # Вопрос без ответа — худшее, что может случиться с шортсом:
            # начало цепляет, а зритель уходит ни с чем. Считаем от места,
            # где вопрос дозвучал; если он стоял в заголовке главы — от
            # начала куска, потому что отвечать начинают сразу.
            unanswered = 0.0
            if promised:
                since = words[asked_at].end if asked_at is not None else words[first].start
                answer = words[last].end - since
                unanswered = ANSWER_PENALTY * max(0.0, 1.0 - answer / ANSWER_MIN)

            # Обещанный ответ должен ещё и договориться до конца. Обрыв на
            # полуслове ловит strict, но «закончилось на точке» и «мысль
            # доведена» — разные вещи: хвост без единого факта и без падения
            # темпа обычно значит, что человека прервали на середине.
            if promised and TAIL_SECONDS < duration:
                tail_from = last
                while (tail_from > first
                       and words[last].end - words[tail_from].start < TAIL_SECONDS):
                    tail_from -= 1
                tail_facts = concrete[last + 1] - concrete[tail_from]
                tail_topic = (topic[last + 1] - topic[tail_from]) / max(
                    last - tail_from + 1, 1
                )
                if tail_facts <= 0 and tail_topic < TAIL_TOPIC:
                    unanswered += TAIL_PENALTY

            score = (
                density
                + TOPIC * weight
                + BOOST * asked
                + NUMBER * facts
                + AUDIO * loud
                + head
                + entry[first]
                + exit_[last]
                + (CHAPTER * chapter.weight if chapter else 0.0)
                - drag
                - OFFTOPIC * (1.0 - asked)
                - unanswered
            )
            if best is None or score > best[0]:
                best = (round(score, 3), last)

        if best:
            scored.append((best[0], first, best[1], chapter))

    scored.sort(key=lambda row: row[0], reverse=True)

    picked = []
    used = set()

    def take(row, once):
        score, first, last, chapter = row
        if score < FLOOR:
            return
        start, end = words[first].start, words[last].end
        # Не просто «не пересекаются», а разнесены: между кусками должен
        # остаться разговор, иначе это один эпизод, разрезанный надвое.
        if any(start < p.end + APART and p.start - APART < end for p in picked):
            return
        # Сильная глава иначе забирает весь список себе, и три шортса
        # подряд оказываются про одно и то же.
        if once and chapter and chapter.title in used:
            return
        if chapter:
            used.add(chapter.title)
        picked.append(
            Candidate(
                start=round(start, 2),
                end=round(end, 2),
                score=score,
                text=" ".join(w.text for w in words[first : last + 1]),
                title=chapter.title if chapter else "",
            )
        )

    # Сначала по одному куску на главу — чтобы темы не повторялись.
    for row in scored:
        if len(picked) >= top:
            break
        take(row, once=True)

    # Если глав меньше, чем нужно кусков, добираем остальным.
    for row in scored:
        if len(picked) >= top:
            break
        take(row, once=False)

    return picked


def to_json(candidates):
    return [asdict(c) for c in candidates]
