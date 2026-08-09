"""Запасной путь загрузки: попросить файл у cobalt.

Когда yt-dlp не может достучаться до YouTube, дело обычно не в нём: провайдер
режет соединение у нас на входе. cobalt — открытый сервер-посредник: он качает
ролик у себя и отдаёт нам прямую ссылку на файл. Наш провайдер про YouTube при
этом ничего не знает, и обход блокировок не нужен.

Чего cobalt не умеет и уметь не может:

- **главы и субтитры** — он отдаёт только медиафайл. А главы у нас самый
  сильный сигнал выбора моментов, поэтому качество нарезки просядет;
- **кусок ролика** — файл приходит целиком, отрезками не выходит.

Поэтому это именно последняя ступень: пробуем, когда напрямую не вышло совсем.

Публичный `api.cobalt.tools` с десятой версии требует ключ, поэтому адрес
сервера и ключ берутся из окружения и по умолчанию пусты — без них ступень
просто пропускается:

    SHORTS_COBALT      — адрес сервера, например https://cobalt.example.org
    SHORTS_COBALT_KEY  — ключ, если сервер его спрашивает
"""

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 30
CHUNK = 1024 * 1024


class CobaltFailed(RuntimeError):
    pass


def instance():
    return os.environ.get("SHORTS_COBALT", "").strip().rstrip("/")


def configured():
    """Есть ли куда идти. Без адреса ступень пропускаем молча."""
    return bool(instance())


def _ask(url, audio_only=False):
    """Спрашивает у cobalt прямую ссылку на файл."""
    body = json.dumps({
        "url": url,
        "downloadMode": "audio" if audio_only else "auto",
        # Просим готовый mp4, а не отдельные дорожки: склеивать нам нечем,
        # ffmpeg тут уже не при делах.
        "youtubeVideoContainer": "mp4",
    }).encode("utf-8")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = os.environ.get("SHORTS_COBALT_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Api-Key {key}"

    request = urllib.request.Request(instance(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            reply = json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise CobaltFailed(f"cobalt ответил {error.code}: {detail}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise CobaltFailed(f"до cobalt не достучаться: {error}") from None

    status = reply.get("status")
    if status in ("tunnel", "redirect"):
        return reply["url"]
    if status == "picker":
        # Пикер — это когда вариантов несколько; берём первый с видео.
        for item in reply.get("picker") or ():
            if item.get("url"):
                return item["url"]
        raise CobaltFailed("cobalt предложил выбор, но без единой ссылки")

    raise CobaltFailed(
        f"cobalt отказал: {reply.get('error', {}).get('code') or status or reply}"
    )


def fetch(url, target, audio_only=False):
    """Качает ролик через cobalt в указанный файл. Возвращает путь."""
    if not configured():
        raise CobaltFailed(
            "cobalt не настроен — укажи адрес сервера в SHORTS_COBALT"
        )

    direct = _ask(url, audio_only)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with urllib.request.urlopen(direct, timeout=TIMEOUT) as source:
            with open(target, "wb") as file:
                shutil.copyfileobj(source, file, CHUNK)
    except (urllib.error.URLError, OSError) as error:
        raise CobaltFailed(f"файл от cobalt не докачался: {error}") from None

    if not target.exists() or target.stat().st_size == 0:
        raise CobaltFailed("cobalt отдал пустой файл")

    return target
