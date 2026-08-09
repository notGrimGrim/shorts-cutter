"""Текстовая модель на этой машине — через Ollama, без сети и без квот.

Зачем. Groq бесплатен, но конечен: 8 000 токенов в минуту и 200 000 в сутки.
Упёршись в суточную квоту, программа теряет то, ради чего модель и звали, —
понимание смысла. Локальная модель этого лимита не знает: она медленнее и
глупее, но всегда на месте.

Порядок такой: Groq, пока есть квота → эта модель, если Ollama установлена →
свой счёт в pairs.rate, если нет ничего. Ни одна ступень не обязательна.

Про железо. Семимиллиардная модель в четырёхбитном сжатии занимает около
пяти гигабайт видеопамяти. На машине Рината (RTX 3060 Ti, 8 ГБ) она влезает,
но не одновременно с Whisper medium — поэтому зовём её после расшифровки,
когда та уже выгрузилась.
"""

import json
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:11434"

# Какую модель звать — решаем не жёстким именем, а списком предпочтений:
# что скачано на машину, руками решает человек, и на разных машинах там
# разное. Жёстко зашитое имя однажды разошлось с диском («ollama list»
# показывал только gemma4:12b) — и ready() была False на исправной Ollama.
#
# Qwen2.5 7B-instruct — первый выбор: разумный минимум для русского,
# меньшие модели путаются в падежах, а этот квант влезает в 8 ГБ видеопамяти
# вместе с контекстом. Остальные строки — не рейтинг силы моделей (мы его не
# мерили), а просто то, что может встретиться на диске; ниже списка берём
# любую установленную — не смочь работать хуже, чем работать с чем есть.
PREFERENCE = (
    "qwen2.5:7b-instruct-q4_K_M",
    "qwen2.5",
    "gemma4:12b",
    "gemma2",
    "llama3.1",
)

# Ollama держит модель загруженной, но первый запрос после простоя тянет её
# с диска — это до полуминуты. Дальше отвечает за секунды.
TIMEOUT = 180

# Сколько ждать ответа на «ты вообще тут?». Если Ollama не стоит, порт молчит
# сразу, и эта проверка ничего не стоит.
PING = 2.0


def running():
    """Отвечает ли служба на порту — без проверки модели (см. ready()).

    installed() не различает «служба не стоит» и «служба стоит, но моделей
    нет» (оба дают []) — этой функции нужно ровно первое, чтобы не поднимать
    второй `ollama serve`, если один уже слушает порт.
    """
    try:
        urllib.request.urlopen(f"{HOST}/api/tags", timeout=PING)
        return True
    except (urllib.error.URLError, OSError):
        return False


def installed():
    """Список моделей, уже скачанных на эту машину."""
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=PING) as answer:
            reply = json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [row.get("name", "") for row in reply.get("models") or ()]


def pick():
    """Какой моделью считать: по списку предпочтений, иначе — что есть.

    Ollama показывает модели с тегом: «qwen2.5:7b-instruct-q4_K_M».
    Сравниваем по началу имени, чтобы не спотыкаться о «:latest». Ничего не
    установлено — None, и вызывающий знает, что считать здесь нечем.
    """
    have = installed()
    if not have:
        return None
    for name in PREFERENCE:
        base = name.split(":")[0]
        for row in have:
            if row.split(":")[0] == base:
                return row
    return have[0]


def ready():
    """Есть ли чем считать здесь и сейчас.

    Проверяем не только службу, но и саму модель: Ollama, установленная без
    моделей, отвечает на запрос ошибкой «model not found», и без этой
    проверки мы бы узнали об этом в середине разбора.
    """
    return pick() is not None


def ask(prompt, system="", answer_tokens=800, model=None):
    """Один вопрос модели. Возвращает разобранный JSON или None.

    Тот же договор, что у think._ask: молчание и чепуха — не исключение, а
    обычный ответ «не смогла», и вызывающий продолжает своим счётом.
    """
    model = model or pick()
    if not model:
        return None
    body = json.dumps({
        "model": model,
        "messages": (
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}]
        ),
        "stream": False,
        # Ollama умеет требовать JSON тем же способом, что и Groq.
        "format": "json",
        # Часть моделей (gemma4 и другие «думающие») тратит весь answer_tokens
        # на скрытые рассуждения и не успевает дойти до самого ответа — на
        # gemma4:12b с 30 парами в списке это дало пустой content за две
        # минуты. Промпту тут нечего обдумывать, только сравнить и выбрать.
        "think": False,
        # seed фиксирует, помимо temperature=0, ещё и то, что от temperature
        # не зависит: жадный выбор токена сам по себе не случаен, но seed —
        # штатный параметр детерминизма в Ollama, и он ничего не стоит.
        #
        # num_ctx не был задан явно — Ollama брала свой дефолт (2048–4096
        # токенов). Промпт _JUDGE сам по себе длинный, плюс до 2 500 знаков
        # текста окна (это уже под тысячу-полторы токена на кириллице) — при
        # дефолте это подходит вплотную к границе или обрезается, и часть
        # странного поведения модели (протечки промпта в ответ, обрезки без
        # причины) может быть не глупостью модели, а нехваткой места в
        # контексте. 8192 — с запасом под любой из промптов и ответ.
        "options": {
            "temperature": 0, "seed": 0,
            "num_predict": answer_tokens, "num_ctx": 8192,
        },
    }, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        f"{HOST}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            reply = json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None

    try:
        return json.loads(reply["message"]["content"])
    except (KeyError, TypeError, ValueError):
        return None
