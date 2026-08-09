"""Что за машина и чем на ней считать.

Программа должна запускаться на чужом железе и не требовать настройки. Но
«запускаться» и «работать быстро» — разные вещи, и разница тут огромная,
поэтому машину распознаём заранее и говорим человеку правду до того, как он
полчаса просмотрит в молчащее окно.

Главное, что нужно знать про наш движок распознавания: **ctranslate2 умеет
считать на видеокарте только у NVIDIA**. Ни ROCm у Radeon, ни Metal у Apple
он не поддерживает — это свойство самой библиотеки, а не нашего кода. То
есть:

* NVIDIA с CUDA — быстро, секунд сорок на часовой ролик;
* Radeon, Intel, Apple — только процессор, и это десятки минут;
* с ключом Groq — быстро на любой машине, потому что считает не она.

Отсюда правило: на машине без NVIDIA облако не удобство, а единственный
разумный путь. См. cloud.py.

Кодирование готового видео — отдельная история, там как раз поддерживаются
все: NVIDIA через nvenc, AMD через amf, Intel через qsv, Apple через
videotoolbox. См. render.GPU_ENCODERS.
"""

import os
import platform
import shutil
import subprocess
from functools import lru_cache

# Сколько ядер отдавать распознаванию на процессоре. Все забирать нельзя:
# машина в это время должна оставаться живой.
CPU_SHARE = 0.75
CPU_MIN = 2

# Во сколько раз быстрее реального времени считает каждый путь. Числа взяты
# с замера на этой машине (RTX 3060 Ti, medium, батчи) и из общих порядков
# для процессора — точности тут не нужно, нужен порядок величины, чтобы
# честно сказать человеку, сколько ждать.
SPEED = {
    "cuda": 35.0,
    "cpu": 1.2,
    "cloud": 100.0,
}


def _nvidia_ready():
    """Видит ли ctranslate2 карту NVIDIA. Спрашиваем у него, а не у системы.

    nvidia-smi может показывать карту, которой ctranslate2 всё равно не
    воспользуется: не тот CUDA, не хватает cuDNN. Врать человеку про
    скорость из-за этого не хочется, поэтому спрашиваем того, кто считает.
    """
    try:
        import ctranslate2
    except ImportError:
        return False
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _gpu_name():
    """Название видеокарты — только чтобы показать человеку."""
    if shutil.which("nvidia-smi"):
        try:
            done = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            name = done.stdout.strip().splitlines()
            if name:
                return name[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass

    if platform.system() == "Darwin":
        return "Apple " + platform.machine()
    return ""


def threads():
    """Сколько ядер отдать распознаванию на процессоре."""
    total = os.cpu_count() or 4
    return max(CPU_MIN, int(total * CPU_SHARE))


@lru_cache(maxsize=1)
def describe():
    """Всё, что нужно знать о машине, одним словарём.

    Считается один раз: опрос видеокарты стоит секунду, а спрашивают часто.
    """
    system = platform.system()
    arm = platform.machine().lower() in ("arm64", "aarch64")
    cuda = _nvidia_ready()

    if cuda:
        device, compute = "cuda", "float16"
    else:
        device = "cpu"
        # int8 на процессоре быстрее float32 в разы и почти не теряет в
        # качестве — для распознавания речи разница на слух не слышна.
        compute = "int8"

    return {
        "system": system,
        "arm": arm,
        "gpu": _gpu_name(),
        "cuda": cuda,
        "device": device,
        "compute": compute,
        "threads": threads(),
        # Быстро ли будет местное распознавание. Единственный честный ответ:
        # быстро только на NVIDIA.
        "fast": cuda,
    }


def minutes_for(seconds, where=None):
    """Сколько примерно ждать распознавания ролика такой длины, в минутах."""
    where = where or describe()["device"]
    return seconds / SPEED.get(where, 1.0) / 60.0


def verdict(duration=None):
    """Строка для человека: что за машина и чего от неё ждать.

    duration — длительность ролика в секундах, если известна: тогда вместо
    отвлечённых «в тридцать раз быстрее» получается «примерно две минуты».
    """
    info = describe()
    card = f", {info['gpu']}" if info["gpu"] else ""
    head = f"{info['system']}{card}"

    if info["cuda"]:
        body = "видеокарта NVIDIA подхвачена — считаю на ней"
    elif info["system"] == "Darwin":
        body = (
            f"на Mac наш движок считает процессором ({info['threads']} ядер): "
            f"видеокарту Apple он не умеет. С ключом Groq будет быстро"
        )
    else:
        body = (
            f"видеокарты NVIDIA нет — считаю процессором ({info['threads']} ядер). "
            f"С ключом Groq будет быстро"
        )

    if duration:
        here = minutes_for(duration)
        body += f". Этот ролик — примерно {here:.0f} мин"
        if not info["cuda"]:
            body += f" вместо {minutes_for(duration, 'cloud'):.0f} в облаке"

    return f"{head}: {body}"
