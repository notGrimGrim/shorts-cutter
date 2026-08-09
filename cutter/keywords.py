"""Опорные слова видео — без моделей, ключей и внешних словарей.

TF-IDF обычно требует корпус текстов, которого у нас нет. Но корпусом может
служить само видео: режем расшифровку на отрезки и считаем каждый отдельным
документом. Слово, которое звучит во всех отрезках подряд, — это общий фон
(«кубернетес» в видео про кубернетес), оно не выделяет ничего. Слово,
собравшееся в двух-трёх местах, — примета конкретной мысли или истории.

Русский язык склоняет всё подряд, поэтому слова грубо обрезаются до основы:
«собеседование» и «собеседования» должны считаться одним словом.
"""

import math
import re

STEM = 6
CHUNK = 30.0

_CLEAN = re.compile(r"[^\w\-]+", re.UNICODE)

# Служебные слова и разговорный мусор: частотные везде и не значат ничего.
STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только её ее мне было вот от меня ещё еще нет о из ему теперь когда даже ну
вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была
сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому
этого какой совсем ним здесь этом один почти мой тем чтобы неё нее сейчас были
куда зачем всех никогда можно при наконец два об другой хоть после над больше
тот через эти нас про всего них какая много разве сказал сказала три эту моя
впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между это эта эти этих такие такая мною нами вами ими
типа вообще просто короче значит скажем допустим слушай смотри ага угу эм ээ э
мм да-да ну-ну то-есть тобой собой оно оба весь вся всё
""".split())

_STOP_STEMS = frozenset(word[:STEM] for word in STOPWORDS)


# Как айтишные термины реально звучат в русской речи — субтитры YouTube
# пишут их то латиницей, то на слух, поэтому держим оба написания.
IT_TERMS = {
    "devops": ("девопс", "devops"),
    "kubernetes": ("кубернетес", "куберы", "кубер", "kubernetes", "k8s"),
    "docker": ("докер", "docker", "контейнер"),
    "linux": ("линукс", "linux", "убунту", "центос"),
    "ansible": ("ансибл", "ansible"),
    "terraform": ("терраформ", "terraform"),
    "jenkins": ("дженкинс", "jenkins"),
    "gitlab": ("гитлаб", "gitlab", "гит", "git"),
    "python": ("питон", "python", "пайтон"),
    "bash": ("баш", "bash", "скрипт"),
    "nginx": ("нжинкс", "нгинкс", "nginx"),
    "postgres": ("постгрес", "postgres", "постгре"),
    "kafka": ("кафка", "kafka"),
    "prometheus": ("прометей", "прометеус", "prometheus", "графана", "grafana"),
    "aws": ("амазон", "aws", "облако", "облачн"),
    "mlops": ("млопс", "mlops", "модель"),
    "sre": ("сре", "sre", "надёжност", "надежност"),
    "cicd": ("сиай", "пайплайн", "pipeline", "деплой", "выкатыва"),
}

# Как те же термины положено писать в кадре. Распознаватель сам выбирает
# между «кубернетес» и «Kubernetes», и без подсказки выбирает на слух.
IT_SPELLING = {
    "devops": "DevOps",
    "kubernetes": "Kubernetes",
    "docker": "Docker",
    "linux": "Linux",
    "ansible": "Ansible",
    "terraform": "Terraform",
    "jenkins": "Jenkins",
    "gitlab": "GitLab",
    "python": "Python",
    "bash": "Bash",
    "nginx": "nginx",
    "postgres": "PostgreSQL",
    "kafka": "Kafka",
    "prometheus": "Prometheus",
    "aws": "AWS",
    "mlops": "MLOps",
    "sre": "SRE",
    "cicd": "CI/CD",
}

# Всегда чуть подтягиваем то, вокруг чего у айтишного канала строится смысл.
IT_BOOST = (
    "собеседован", "собес", "оффер", "зарплат", "резюме", "ваканс", "найм",
    "джун", "мидл", "сеньор", "стажир", "тимлид", "эйчар", "грейд", "вилка",
    "испытательн", "удалёнк", "удаленк", "релокац", "оклад", "опыт",
    "сервер", "кластер", "продакшн", "прод", "мониторинг", "нагрузк",
    "бэкап", "инфраструктур", "микросервис", "архитектур", "база",
    "задач", "проект", "команд", "техническ", "вопрос", "ошибк",
)


def from_title(title):
    """Слова для поиска, вытащенные из названия ролика.

    Если в заголовке стоит DevOps — значит и в субтитрах ищем «девопс»,
    и моменты, где об этом действительно рассказывают, весят больше.
    """
    lowered = title.lower()
    terms = []

    for canon, variants in IT_TERMS.items():
        if canon in lowered or any(variant in lowered for variant in variants):
            terms.extend(variants)

    # Плюс сами слова заголовка — служебные отсеются в normalize.
    terms += [word for word in re.findall(r"[\w-]+", lowered) if normalize(word)]
    terms += IT_BOOST

    return list(dict.fromkeys(terms))


def learned_terms(words, limit=10, least=3, sure=0.6):
    """Словарь ролика, добытый из него самого — по беглой расшифровке.

    Отличие от top_terms: там мы показываем человеку, что скрипт понял,
    и одного упоминания достаточно. Здесь слова пойдут обратно в модель
    подсказкой, а это опасная петля: подсказать ослышку — значит закрепить
    ошибку. Поэтому два фильтра.

    least — слово должно прозвучать несколько раз. Ловит случайный шум,
    но против системной ослышки бессилен: говорящего с акцентом модель
    перевирает одинаково каждый раз, и «дыхательные» станут «дикательными»
    во всех отрезках сразу.

    sure — своя оценка модели. Вот она как раз проседает там, где модель
    гадала, и это единственный признак ошибки, доступный без эталона.
    У субтитров YouTube оценки нет, там всегда единица, и фильтр молчит.
    """
    if not words:
        return []

    scores = weights(words)
    said = {}
    best = {}
    for word, score in zip(words, scores):
        stem = normalize(word.text)
        if not stem or getattr(word, "sure", 1.0) < sure:
            continue
        said[stem] = said.get(stem, 0) + 1
        if score > best.get(stem, (0.0, ""))[0]:
            best[stem] = (score, word.text.strip(".,!?:;«»\"'()"))

    ranked = sorted(
        (row for stem, row in best.items() if said[stem] >= least),
        key=lambda row: row[0],
        reverse=True,
    )
    return [text for _, text in ranked[:limit]]


NEAR = 12


def related(words, seeds, limit=6, near=NEAR):
    """Слова, которые в этом ролике ходят рядом с заданными.

    Нужно, чтобы предложить «а ещё поищи вот по этим». Готовый словарь
    синонимов тут хуже: он выдаст «жалованье» к «зарплате», а если этого
    слова в записи нет, то и резать по нему нечего. Соседство же берётся
    из самой расшифровки, поэтому предложенное точно найдётся.

    Считаем не просто частоту рядом, а перекос: слово должно встречаться
    возле нашего чаще, чем вообще по ролику. Иначе в советы полезут те,
    что и так звучат в каждом предложении.
    """
    stems = [normalize(word.text) for word in words]
    targets = {normalize(seed) for seed in seeds}
    targets.discard("")
    if not targets:
        return []

    total = {}
    for stem in stems:
        if stem:
            total[stem] = total.get(stem, 0) + 1

    beside = {}
    for index, stem in enumerate(stems):
        if stem not in targets:
            continue
        left = max(0, index - near)
        for other, text in zip(stems[left:index + near], words[left:index + near]):
            if not other or other in targets:
                continue
            score, _ = beside.get(other, (0, ""))
            beside[other] = (score + 1, text.text.strip(".,!?:;«»\"'()"))

    ranked = sorted(
        (
            (count * count / total[stem], text)
            for stem, (count, text) in beside.items()
            if count >= 2 and total.get(stem)
        ),
        reverse=True,
    )
    return [text for _, text in ranked[:limit]]


def speech_hint(title, heard=(), limit=8):
    """Подсказка распознавателю: как пишутся термины, которые он услышит.

    Whisper принимает initial_prompt — текст, который он считает сказанным
    прямо перед записью. Это не приказ, а образец: по нему модель подхватывает
    и написание терминов, и манеру расставлять знаки. Поэтому подсказка —
    не список слов, а обычные предложения с точками.

    Длинной её делать нельзя: она занимает то же окно, что и речь, а на тишине
    модель начинает повторять подсказку вместо текста. Отсюда и limit.

    heard — слова, которые в ролике реально прозвучали (см. learned_terms).
    Это главный источник: он работает на любой теме, а не только на айтишной,
    и не требует держать словарь на все случаи жизни.
    """
    lowered = (title or "").lower()

    named = [
        IT_SPELLING.get(canon, canon)
        for canon, variants in IT_TERMS.items()
        if canon in lowered or any(variant in lowered for variant in variants)
    ]

    parts = []
    if lowered.strip():
        # Само название — это ещё и имена собственные, написанные верно.
        parts.append(title.strip().rstrip(".!?") + ".")

    # Айтишный словарь подсовываем, только если ролик сам о нём заявил.
    # Навязать его всем подряд — значит на ролике про дыхательные практики
    # подсказать модели «Kubernetes»: она послушная и найдёт, что услышать.
    if named:
        parts.append("Разговор об IT: " + ", ".join(named[:limit]) + ".")
        parts.append("Обсуждаем собеседования, офферы, грейды, продакшн и найм.")

    heard = [word for word in heard if word]
    if heard:
        parts.append("В ролике звучит: " + ", ".join(heard[:limit]) + ".")

    return " ".join(parts)


def normalize(text):
    """Слово -> основа для сравнения, либо пустая строка, если это не слово."""
    # Дефис по краям — это обрывок: «-то» от «что-то», «-нибудь» от «когда-нибудь».
    # Внутри слова он законный («что-то», «из-за»), поэтому режем только края.
    cleaned = _CLEAN.sub("", text.lower().replace("ё", "е")).strip("-")
    if len(cleaned) < 3 or cleaned.isdigit():
        return ""
    stem = cleaned[:STEM]
    return "" if stem in _STOP_STEMS else stem


def weights(words, chunk=CHUNK):
    """Вес каждого слова: насколько оно характерно для своего места в видео.

    Возвращает список той же длины, что и words, со значениями 0..1.
    Служебные слова получают ноль и на выбор момента не влияют.
    """
    if not words:
        return []

    stems = [normalize(word.text) for word in words]

    seen = {}
    said = {}
    every = set()
    for stem, word in zip(stems, words):
        every.add(int(word.start // chunk))
        if stem:
            seen.setdefault(stem, set()).add(int(word.start // chunk))
            said[stem] = said.get(stem, 0) + 1

    # Не размах времени от первого слова до последнего, а число отрезков,
    # где вообще есть текст. На плотной расшифровке разницы нет — отрезки
    # идут подряд. А беглый проход слушает 5 разбросанных кусков по всему
    # ролику: размах может быть «час», а реальных отрезков — семь. Взять
    # размах вместо счёта — значит посчитать почти любое слово редким и
    # получить в словаре не термины, а то, что просто попалось в выборку.
    total = max(len(every), 1)
    if total < 3:
        # Ролик короче полутора минут — делить не на что, все слова равны.
        return [1.0 if stem else 0.0 for stem in stems]

    # Одной редкости мало: в получасовом ролике почти каждое содержательное
    # слово попадает ровно в один отрезок и получает одинаковый максимум.
    # Опора — это слово и редкое, и повторённое, поэтому домножаем на частоту.
    weight = {
        stem: math.log(1 + said[stem]) * math.log(1 + total / (1 + len(chunks)))
        for stem, chunks in seen.items()
    }
    top = max(weight.values()) or 1.0

    return [round(weight[stem] / top, 4) if stem else 0.0 for stem in stems]


def top_terms(words, limit=12):
    """Самые характерные слова видео — показываем человеку, что скрипт понял."""
    scores = weights(words)
    best = {}
    for word, score in zip(words, scores):
        stem = normalize(word.text)
        if stem and score > best.get(stem, (0.0, ""))[0]:
            best[stem] = (score, word.text.strip(".,!?:;"))

    ranked = sorted(best.values(), key=lambda row: row[0], reverse=True)
    return [text for _, text in ranked[:limit]]
