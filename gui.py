"""Окно для нарезки шортсов: всё под рукой, ничего не надо помнить.

Логику не дублируем — окно только собирает настройки и зовёт то же самое,
что и командная строка (`cut.cmd_auto`). Поэтому любая правка в отборе или
рендере действует и здесь, без второй копии кода.

Устройство простое: у каждой возможности свой флажок, и пока он не отмечен,
её настройки спрятаны. Так на виду остаётся только то, что человек включил.

Про внешний вид. tkinter рисует кнопки серыми прямоугольниками родом из
девяностых, и никакими настройками это не лечится — форму виджета он менять
не умеет. Поэтому кнопки и полоса хода работы нарисованы на Canvas вручную:
там закругления возможны. Остальное держится на цвете, отступах и порядке,
а не на украшениях.
"""

import io
import queue
import sys
import threading
import tkinter as tk
from argparse import Namespace
from pathlib import Path
from tkinter import filedialog, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cut
from cutter import cloud, download, jobs, machine, pairs, render, speech, think
from cutter.paths import OUT

# Frutiger Aero, но не корпоративный, а домашний: луг, стекло, крем.
#
# Прошлое окно было тёмным с кислотно-зелёным акцентом, и Ринат сказал про
# него «напоминает читы из 2014 года». Он прав: тёмный фон плюс ядовитый
# салатовый — ровно палитра чит-меню.
#
# Ориентир он показал картинкой: пастельная зелень с кремовым, клевер,
# трава, мягкие скруглённые иконки. Ключевые слова его же — «ностальгия,
# мечтательность». Поэтому не звонкая вода Aero, а приглушённый луг:
# насыщенность низкая, контраст мягкий, а жизнь даёт блик поверх цвета,
# а не сам цвет. Тёмный вариант отдельно отвергнут как «киберпанк».
BG = "#e8f4dc"          # луг в дымке — самый светлый тон
CARD = "#fbfdf4"        # кремовое стекло панели
RAISED = "#eef7e2"      # приподнятая деталь на панели
EDGE = "#c4dfae"        # мягкая граница, не линейка
INK = "#2f4a34"         # чернила цвета хвои, не чёрные: мягче для глаз
DIM = "#5f8062"
FAINT = "#93ad93"

ACCENT = "#7cc46a"      # молодая листва
ACCENT_HOT = "#96d982"  # та же листва на солнце — наведение
ACCENT_DEEP = "#4f9a4a" # тень под листвой — нажатое и полоски
FRESH = "#6fc9b0"       # мятная вода, второй голос палитры
SKY = "#a8d8ea"         # выцветшее небо, для редких холодных пятен
CREAM = "#fdf6e3"       # тёплый крем, чтобы зелень не звучала больнично
WARN = "#e0a95c"
BAD = "#d97b73"

# Блик и обводка — то, из чего делается «мокрый пластик».
GLOSS = "#ffffff"
RIM = "#ffffff"

FONT = "Segoe UI"
MONO = "Consolas"


def _mix(one, two, part):
    """Цвет между двумя: part=0 — первый, part=1 — второй.

    Нужен для градиентов и бликов. Считать оттенки руками и держать их
    списком констант — то, из-за чего палитра расползается при первой же
    правке; пусть лучше берутся из двух опорных цветов.
    """
    first = [int(one[i:i + 2], 16) for i in (1, 3, 5)]
    second = [int(two[i:i + 2], 16) for i in (1, 3, 5)]
    blend = [round(a + (b - a) * part) for a, b in zip(first, second)]
    return "#%02x%02x%02x" % tuple(max(0, min(255, v)) for v in blend)


# Тональные зоны: один плоский BG на всё окно и был той самой «однотонностью»,
# на которую Ринат указал по живому скриншоту — Aero держится на переходах,
# а не на одном цвете. Честный per-pixel градиент в Tk недоступен (виджеты
# непрозрачны, Frame нельзя оставить незалитым), поэтому вместо него —
# именованные зоны: шапка холоднее (к небу), низ глубже (в тень травы),
# середина — прежний луг.
BG_SKY = _mix(SKY, BG, 0.5)            # шапка
BG_DEEP = _mix(ACCENT_DEEP, BG, 0.85)  # подвал под логом

# Лог был буквально терминальным чёрным — прямая жалоба «панель кмд смотрится
# плохо». Меняем на глубокую хвою из той же палитры, а не на нейтральный
# серый: тёмный конец той же зелёной семьи держит стекло, а не спорит с ним.
LOG_BG = "#1c3322"
LOG_FG = "#bfe0c4"


# ── рисованные детали ─────────────────────────────────────────────────────

def _round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """Прямоугольник со скруглёнными углами. Canvas такого не умеет сам."""
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


# Радиус скругления. Крупнее прежних девяти: у Aero углы мягкие, при девяти
# деталь читается как прямоугольник со сточенными углами, а не как капля.
RADIUS = 14


def _glossy(canvas, x1, y1, x2, y2, radius, tone, rim=RIM):
    """Мокрый пластик: тело, блик по верхней половине, светлая обводка.

    Canvas не умеет заливать градиентом, а рисовать сотни линий на каждую
    кнопку дорого и всё равно упрётся в скруглённые углы. Поэтому глянец
    собирается из двух фигур: тело потемнее и блик посветлее поверх верхней
    половины. Нижняя граница блика скруглена — ровно так свет и ложится на
    выпуклое стекло, и именно это читается как «объёмная кнопка».

    Разница между телом и бликом раньше была 0.10/0.45 от тона — на светлом
    луговом фоне это сливалось в одно пятно. 0.16/0.62 держит контраст даже
    рядом с CARD и BG.

    Возвращает обе фигуры: их потом перекрашивают на наведении.
    """
    body = _round_rect(canvas, x1, y1, x2, y2, radius,
                       fill=_mix(tone, "#000000", 0.16), outline=rim, width=1)
    shine = _round_rect(canvas, x1 + 2, y1 + 1, x2 - 2, y1 + (y2 - y1) * 0.52,
                        radius - 3, fill=_mix(tone, GLOSS, 0.62), outline="")
    return body, shine


class Button(tk.Canvas):
    """Кнопка с закруглением и подсветкой под курсором.

    Обычный tk.Button углы скруглять не умеет, а ttk на Windows подменяет
    цвета системной темой — договориться с ним про тёмный фон невозможно.
    Рисуем сами: тут ровно то, что задумано, и одинаково на любой машине.
    """

    # Запас снизу под тень: без него стеклу некуда падать, а обрезать тень
    # по кромке канваса выглядит как грязный край, а не как объём.
    LIFT = 4

    def __init__(self, parent, text, command, primary=False, width=None,
                 bg=None):
        self.primary = primary
        self.command = command
        self.enabled = True

        size = 11 if primary else 9
        weight = "bold" if primary else "normal"
        self.font = (FONT, size, weight)

        pad = 26 if primary else 15
        guess = width or (len(text) * (9 if primary else 7) + pad * 2)
        height = 42 if primary else 34
        back = bg or BG

        super().__init__(parent, width=guess, height=height + self.LIFT,
                         bg=back, highlightthickness=0, bd=0, cursor="hand2")

        self.rest = ACCENT if primary else CARD
        self.over = ACCENT_HOT if primary else "#ffffff"
        # Тёмные чернила и на светлой кнопке: белым по мягкой зелени читается
        # плохо, а тут вся палитра держится на низком контрасте.
        self.ink = "#14401a" if primary else INK

        # Тень: контур тела, сдвинутый вниз и смешанный с фоном канваса. У
        # Tk нет альфы для заливки, но фон тут известный сплошной цвет, так
        # что смешение с чёрным читается как мягкая тень под стеклом.
        _round_rect(self, 2, 1 + self.LIFT, guess - 2, height - 1 + self.LIFT,
                    RADIUS, fill=_mix(back, "#000000", 0.16), outline="")

        self.body, self.shine = _glossy(self, 1, 1, guess - 1, height - 1,
                                        RADIUS, self.rest)
        self.label = self.create_text(guess / 2, height / 2 + 1, text=text,
                                      fill=self.ink, font=self.font)

        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _paint(self, tone, ink=None):
        # Те же коэффициенты, что и в _glossy() при создании — иначе
        # контраст стекла держится только до первого <Leave>.
        self.itemconfig(self.body, fill=_mix(tone, "#000000", 0.16))
        self.itemconfig(self.shine, fill=_mix(tone, GLOSS, 0.62))
        if ink:
            self.itemconfig(self.label, fill=ink)

    def _enter(self, _=None):
        if self.enabled:
            self._paint(self.over)

    def _leave(self, _=None):
        if self.enabled:
            self._paint(self.rest)

    def _click(self, _=None):
        if self.enabled and self.command:
            self.command()

    def set_text(self, text):
        self.itemconfig(self.label, text=text)

    def set_enabled(self, on):
        self.enabled = on
        self._paint(self.rest if on else RAISED, self.ink if on else FAINT)
        self.config(cursor="hand2" if on else "arrow")


class Progress(tk.Canvas):
    """Тонкая полоса хода работы.

    Сколько осталось, мы честно не знаем: длина ролика заранее не говорит,
    сколько займёт распознавание. Поэтому полоса не показывает проценты, а
    просто бежит — её задача сказать «программа жива», а не обмануть точной
    цифрой.
    """

    WIDTH = 200
    HEIGHT = 4

    def __init__(self, parent, bg=BG):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0)
        self.track = _round_rect(self, 0, 0, self.WIDTH, self.HEIGHT, 2,
                                 fill=EDGE, outline="")
        self.bar = _round_rect(self, 0, 0, 60, self.HEIGHT, 2,
                               fill=ACCENT, outline="")
        self.at = 0.0
        self.running = False
        self.itemconfig(self.bar, state="hidden")

    def start(self):
        self.running = True
        self.itemconfig(self.bar, state="normal")
        self._step()

    def stop(self):
        self.running = False
        self.itemconfig(self.bar, state="hidden")

    def _step(self):
        if not self.running:
            return
        self.at = (self.at + 4) % (self.WIDTH + 60)
        left = self.at - 60
        self.coords(self.bar, *self._points(left, left + 60))
        self.after(16, self._step)

    def _points(self, x1, x2):
        x1, x2 = max(0, x1), min(self.WIDTH, x2)
        if x2 <= x1:
            x2 = x1 + 1
        radius = min(2, (x2 - x1) / 2)
        y1, y2 = 0, self.HEIGHT
        return [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]


class Choice(tk.Frame):
    """Переключатель из нескольких кнопок в ряд.

    Вместо выпадающего списка: вариантов три, и все они должны быть видны
    сразу. Список прячет то, что человек как раз и выбирает.
    """

    def __init__(self, parent, options, value=None, bg=CARD, on_change=None):
        super().__init__(parent, bg=bg)
        self.var = tk.StringVar(value=value or options[0][0])
        self.on_change = on_change
        self.cells = {}

        for key, title in options:
            cell = tk.Label(self, text=title, bg=bg, fg=DIM, padx=13, pady=5,
                            font=(FONT, 9), cursor="hand2")
            cell.pack(side="left", padx=(0, 4))
            cell.bind("<Button-1>", lambda _, k=key: self.set(k))
            self.cells[key] = cell

        # Первая раскраска — без обратного вызова: он обращается к полям
        # окна, которых на этом шаге ещё нет.
        self.set(self.var.get(), quiet=True)

    def set(self, key, quiet=False):
        self.var.set(key)
        for name, cell in self.cells.items():
            picked = name == key
            cell.config(bg=ACCENT_DEEP if picked else RAISED,
                        fg="#eafff1" if picked else DIM)
        if self.on_change and not quiet:
            self.on_change(key)

    def get(self):
        return self.var.get()


# Коды клавиш, а не буквы. Tk привязывает вставку к символу «v», и в русской
# раскладке Ctrl+V приходит как Ctrl+м — встроенная вставка молча не работает.
# Код клавиши от раскладки не зависит, поэтому смотрим на него.
_CLIP_KEYS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "all"}


def enable_clipboard(root):
    """Чтобы Ctrl+V работал при любой раскладке."""

    def pressed(event):
        action = _CLIP_KEYS.get(event.keycode)
        if not action:
            return None
        if action == "all":
            try:
                event.widget.selection_range(0, "end")
            except tk.TclError:
                event.widget.tag_add("sel", "1.0", "end")
            return "break"
        event.widget.event_generate(action)
        return "break"

    for kind in ("Entry", "TEntry", "Text"):
        root.bind_class(kind, "<Control-KeyPress>", pressed, add="+")


def add_menu(widget):
    """Меню по правой кнопке — самый надёжный способ вставить."""
    menu = tk.Menu(widget, tearoff=0, bg=RAISED, fg=INK, borderwidth=0,
                   activebackground=ACCENT_DEEP, activeforeground="white")
    menu.add_command(label="Вставить",
                     command=lambda: widget.event_generate("<<Paste>>"))
    menu.add_command(label="Копировать",
                     command=lambda: widget.event_generate("<<Copy>>"))
    menu.add_command(label="Вырезать",
                     command=lambda: widget.event_generate("<<Cut>>"))
    menu.add_separator()
    menu.add_command(label="Очистить", command=lambda: widget.delete(0, "end"))

    def show(event):
        widget.focus_set()
        menu.tk_popup(event.x_root, event.y_root)

    widget.bind("<Button-3>", show)
    return widget


def scrollable(parent):
    """Прокручиваемая область: с раскрытыми разделами настройки не влезают."""
    canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=BG)

    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    slot = canvas.create_window((0, 0), window=inner, anchor="nw")

    def resized(_=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(slot, width=canvas.winfo_width())

    inner.bind("<Configure>", resized)
    canvas.bind("<Configure>", resized)

    def wheel(event):
        # Лог прокручивается сам, иначе колесо над ним двигало бы окно.
        if isinstance(event.widget, tk.Text):
            return None
        canvas.yview_scroll(-event.delta // 120, "units")
        return None

    canvas.bind_all("<MouseWheel>", wheel)
    return inner


def parse_stamp(text):
    """«12:30», «1:02:05» или просто секунды — в секунды. Иначе None."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(piece) for piece in text.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


class Section:
    """Заголовок-переключатель и спрятанная под ним панель настроек."""

    def __init__(self, parent, title, note=""):
        self.on = tk.BooleanVar(value=False)

        self.head = tk.Frame(parent, bg=BG, cursor="hand2")
        self.head.pack(fill="x", pady=(10, 0))

        # Треугольник вместо галочки: он показывает не «включено», а
        # «раскрыто», и это ближе к правде — раздел именно разворачивается.
        self.mark = tk.Label(self.head, text="▸", bg=BG, fg=DIM,
                             font=(FONT, 9), cursor="hand2")
        self.mark.pack(side="left", padx=(0, 6))

        self.title = tk.Label(self.head, text=title, bg=BG, fg=INK,
                              font=(FONT, 10, "bold"), cursor="hand2")
        self.title.pack(side="left")

        self.note = None
        if note:
            self.note = tk.Label(self.head, text=note, bg=BG, fg=FAINT,
                                 font=(FONT, 9), cursor="hand2")
            self.note.pack(side="left", padx=(8, 0))

        # Раньше единственным сигналом «действует ли настройка при закрытой
        # панели» был цвет треугольника — слишком тихо: свёрнутый раздел не
        # говорит, что будет взято, дефолт или введённое значение. Эта
        # метка держит текущий эффективный результат на виду всегда, не
        # только при раскрытии — set_status() зовут явно из Window.
        self.status = tk.Label(self.head, text="", bg=BG, fg=FAINT,
                               font=(FONT, 9, "bold"), cursor="hand2")
        self.status.pack(side="left", padx=(8, 0))

        for widget in (self.head, self.mark, self.title, self.note,
                       self.status):
            if widget is not None:
                widget.bind("<Button-1>", self._toggle)
                widget.bind("<Enter>", self._hover)
                widget.bind("<Leave>", self._plain)

        # Полоска слева: по ней видно, какие настройки раскрыты, даже когда
        # открыто несколько разделов сразу.
        self.shell = tk.Frame(parent, bg=ACCENT_DEEP)
        self.body = tk.Frame(self.shell, bg=CARD, padx=15, pady=12)
        self.body.pack(fill="both", expand=True, padx=(2, 0))

    _TONES = {"off": FAINT, "on": ACCENT_DEEP, "warn": WARN}

    def set_status(self, text, tone="off"):
        """Показывает, что сейчас реально будет применено.

        tone="off" — раздел выключен, действует дефолт (тускло); "on" —
        настройка реально применяется (акцент); "warn" — раздел включён, но
        так, что толку от него нет (например пустой список обязательных
        слов) — цвет предупреждения, чтобы это не пришлось искать самому.
        """
        self.status.config(text=text, fg=self._TONES.get(tone, FAINT))

    def _hover(self, _=None):
        if not self.on.get():
            self.title.config(fg=ACCENT_HOT)
            self.mark.config(fg=ACCENT_HOT)

    def _plain(self, _=None):
        if not self.on.get():
            self.title.config(fg=INK)
            self.mark.config(fg=DIM)

    def _toggle(self, _=None):
        self.on.set(not self.on.get())
        if self.on.get():
            self.shell.pack(fill="x", padx=(18, 0), pady=(6, 0))
            self.title.config(fg=ACCENT)
            self.mark.config(text="▾", fg=ACCENT)
        else:
            self.shell.pack_forget()
            self.title.config(fg=INK)
            self.mark.config(text="▸", fg=DIM)


def slider(parent, label, low, high, start, step=0.05, fmt="{:.2f}", bg=CARD):
    """Ползунок с подписью, которая показывает текущее значение."""
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=4)

    tk.Label(row, text=label, bg=bg, fg=INK, width=21, anchor="w",
             font=(FONT, 9)).pack(side="left")

    value = tk.DoubleVar(value=start)
    shown = tk.Label(row, text=fmt.format(start), bg=bg, fg=ACCENT,
                     width=6, anchor="e", font=(MONO, 10, "bold"))
    shown.pack(side="right")

    def moved(_):
        # Ползунок отдаёт дробь любой длины, поэтому округляем по шагу.
        exact = round(value.get() / step) * step
        value.set(exact)
        shown.config(text=fmt.format(exact))

    ttk.Scale(row, from_=low, to=high, variable=value, command=moved).pack(
        side="left", fill="x", expand=True, padx=10
    )
    return value


def field(parent, label, start="", width=18, bg=CARD, hint=""):
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=4)
    tk.Label(row, text=label, bg=bg, fg=INK, width=21, anchor="w",
             font=(FONT, 9)).pack(side="left")
    var = tk.StringVar(value=start)
    entry = tk.Entry(row, textvariable=var, bg=BG, fg=INK, width=width,
                     insertbackground=ACCENT, relief="flat", font=(FONT, 9),
                     highlightthickness=1, highlightbackground=EDGE,
                     highlightcolor=ACCENT_DEEP)
    entry.pack(side="left", fill="x", expand=True, ipady=3)
    add_menu(entry)
    if hint:
        tk.Label(parent, text=hint, bg=bg, fg=FAINT, font=(FONT, 8),
                 wraplength=330, justify="left").pack(anchor="w", pady=(0, 2))
    return var


def flag(parent, label, bg=CARD):
    var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        parent, text=label, variable=var, bg=bg, fg=INK, selectcolor=BG,
        activebackground=bg, activeforeground=ACCENT_HOT, anchor="w",
        font=(FONT, 9), borderwidth=0, highlightthickness=0, cursor="hand2",
    ).pack(fill="x", pady=2)
    return var


def card(parent, pad=15):
    """Карточка: приподнятый прямоугольник с воздухом внутри и тенью снизу.

    Frame прямоугольный без закруглений, альфы у заливки тоже нет — тень
    имитируется тем же приёмом, что и в _glossy(): более тёмный слой позади,
    выглядывающий на пару пикселей снизу и справа сквозь смещённый паддинг.
    """
    outer = tk.Frame(parent, bg=BG)
    outer.pack(fill="x")
    shadow = tk.Frame(outer, bg=_mix(BG, "#000000", 0.14))
    shadow.pack(fill="x")
    shell = tk.Frame(shadow, bg=CARD)
    shell.pack(fill="x", padx=(0, 3), pady=(0, 3))
    inner = tk.Frame(shell, bg=CARD, padx=pad, pady=pad - 3)
    inner.pack(fill="both", expand=True)
    return inner


class Window:
    def __init__(self, root):
        self.root = root
        self.lines = queue.Queue()
        self.busy = False
        self.made = []

        root.title("Нарезка шортсов")
        root.configure(bg=BG)
        root.geometry("900x880")
        root.minsize(760, 640)
        self._icon()

        self._style()

        wrap = tk.Frame(root, bg=BG, padx=22, pady=16)
        wrap.pack(fill="both", expand=True)

        self._header(wrap)
        self._source(wrap)

        middle = tk.Frame(wrap, bg=BG)
        middle.pack(fill="both", expand=True, pady=(4, 0))
        self._sections(scrollable(middle))

        self._footer(wrap)
        self.root.after(120, self._drain)

    def _icon(self):
        """Иконка окна и панели задач. Без файла — тоже не беда."""
        path = Path(__file__).resolve().parent / "cutter" / "assets" / "icon.ico"
        try:
            self.root.iconbitmap(str(path))
        except tk.TclError:
            pass

    def _style(self):
        """Ползунок ttk — единственное, что нельзя нарисовать самому."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TScale", background=CARD, troughcolor=EDGE,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Vertical.TScrollbar", background=EDGE,
                        troughcolor=BG, borderwidth=0, arrowcolor=DIM)
        style.map("Vertical.TScrollbar", background=[("active", FAINT)])

    # --- шапка ------------------------------------------------------------

    def _header(self, parent):
        # Своя, более холодная зона — первое, что видно в окне, и там
        # переход к небу читается заметнее всего.
        tk.Frame(parent, bg=BG_SKY, height=8).pack(fill="x")
        row = tk.Frame(parent, bg=BG_SKY, pady=6)
        row.pack(fill="x")

        left = tk.Frame(row, bg=BG_SKY)
        left.pack(side="left", anchor="w")

        title = tk.Frame(left, bg=BG_SKY)
        title.pack(anchor="w")
        tk.Label(title, text="Нарезка", bg=BG_SKY, fg=INK,
                 font=(FONT, 19, "bold")).pack(side="left")
        tk.Label(title, text="шортсов", bg=BG_SKY, fg=ACCENT,
                 font=(FONT, 19, "bold")).pack(side="left", padx=(7, 0))

        tk.Label(left, text="ссылка или файл — на выходе вертикальные ролики "
                            "с субтитрами и крючком сверху",
                 bg=BG_SKY, fg=DIM, font=(FONT, 9)).pack(anchor="w", pady=(1, 0))

        # Что за машина — сразу, а не после первого получаса ожидания.
        right = tk.Frame(row, bg=BG_SKY)
        right.pack(side="right", anchor="e")
        info = machine.describe()
        mark = ACCENT if info["fast"] else WARN
        tk.Label(right, text="● " + (info["gpu"] or info["system"]), bg=BG_SKY,
                 fg=mark, font=(FONT, 9, "bold")).pack(anchor="e")
        tk.Label(right,
                 text="видеокарта считает" if info["fast"]
                      else f"процессор, {info['threads']} ядер — лучше через Groq",
                 bg=BG_SKY, fg=DIM, font=(FONT, 8)).pack(anchor="e")

        tk.Frame(parent, bg=EDGE, height=1).pack(fill="x", pady=(13, 14))

    # --- источник ---------------------------------------------------------

    def _source(self, parent):
        box = card(parent, pad=16)

        tk.Label(box, text="ЧТО РЕЖЕМ", bg=CARD, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w")

        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", pady=(8, 0))

        self.source = tk.StringVar()
        entry = tk.Entry(row, textvariable=self.source, bg=BG, fg=INK,
                         insertbackground=ACCENT, relief="flat",
                         font=(FONT, 11), highlightthickness=1,
                         highlightbackground=EDGE, highlightcolor=ACCENT_DEEP)
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        add_menu(entry)
        entry.focus_set()

        Button(row, "Вставить", self._paste).pack(side="left", padx=(9, 0))
        Button(row, "Файл…", self._pick_file).pack(side="left", padx=(6, 0))

        tk.Label(box, text="youtube.com/watch?v=… или перетащи путь к файлу",
                 bg=CARD, fg=FAINT, font=(FONT, 8)).pack(anchor="w",
                                                          pady=(7, 0))

    def _paste(self):
        """Вставка кнопкой: работает, даже если горячие клавиши подводят."""
        try:
            self.source.set(self.root.clipboard_get().strip().strip('"'))
        except tk.TclError:
            self._say("! в буфере обмена нечего вставлять")

    def _pick_file(self):
        chosen = filedialog.askopenfilename(
            title="Выбери видео",
            filetypes=[("Видео", "*.mp4 *.mkv *.webm *.mov *.avi"),
                       ("Любые файлы", "*.*")],
        )
        if chosen:
            self.source.set(chosen)

    # --- настройки --------------------------------------------------------

    def _sections(self, parent):
        area = tk.Frame(parent, bg=BG)
        area.pack(fill="both", expand=True)

        left = tk.Frame(area, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(area, bg=BG)
        right.pack(side="right", fill="both", expand=True, padx=(18, 0))

        # Сколько резать — нужно всегда, поэтому без флажка и сразу наверху.
        top = card(left, pad=14)
        self.count = slider(top, "Сколько шортсов", 1, 10, 3, step=1,
                            fmt="{:.0f}")
        tk.Label(top, text="это потолок, а не цель: слабые куски не берутся",
                 bg=CARD, fg=FAINT, font=(FONT, 8)).pack(anchor="w",
                                                          pady=(4, 0))

        # Как строить кадр — здесь же, без флажка: это первое, что видно в
        # готовом шортсе, и решать это человек должен сам, а не узнавать
        # постфактум. «Сам» смотрит, разобралась ли раскладка собеседников.
        row = tk.Frame(top, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="Кадр", bg=CARD, fg=INK, width=21, anchor="w",
                 font=(FONT, 9)).pack(side="left")
        # По умолчанию — прежний кадр во всю ширину: он привычный и не может
        # разъехаться на незнакомой раскладке. Стопка включается выбором.
        self.shape = Choice(
            row,
            [("blur", "по ширине"), ("stack", "стопкой"), ("auto", "сам")],
            value="blur",
        )
        self.shape.pack(side="left")
        tk.Label(top,
                 text="стопкой — собеседники друг над другом, каждый во весь "
                      "экран; по ширине — как было раньше: кадр целиком, "
                      "сверху и снизу размытие",
                 bg=CARD, fg=FAINT, font=(FONT, 8), wraplength=330,
                 justify="left").pack(anchor="w", pady=(4, 0))

        self.span = Section(left, "Своя длина", "иначе 40–90 с")
        self.span_min = slider(self.span.body, "Минимум, секунд", 15, 180, 40,
                               step=5, fmt="{:.0f}")
        self.span_max = slider(self.span.body, "Максимум, секунд", 20, 240, 90,
                               step=5, fmt="{:.0f}")
        tk.Label(self.span.body,
                 text="куску с вопросом даётся ещё до 20 с, чтобы ответ "
                      "договорился",
                 bg=CARD, fg=FAINT, font=(FONT, 8), wraplength=330,
                 justify="left").pack(anchor="w", pady=(4, 0))

        self.cut_at = Section(left, "Свой таймкод", "режу ровно этот кусок")
        self.cut_from = field(self.cut_at.body, "От (мм:сс)", "")
        self.cut_to = field(self.cut_at.body, "До (мм:сс)", "",
                            hint="когда задан таймкод, моменты не ищутся")

        self.pace = Section(left, "Ускорение", "речь звучит бодрее")
        self.speed = slider(self.pace.body, "Скорость", 1.0, 1.4, 1.15)
        self.fit_minute = flag(self.pace.body,
                               "Подобрать под минуту (перебивает ползунок)")

        self.words = Section(left, "Свои слова", "о чём должен быть шортс")
        self.must = field(self.words.body, "Обязательные", "")
        self.boost = field(self.words.body, "Подтянуть вверх", "",
                           hint="через запятую; без обязательных слов кусок "
                                "не берётся вовсе")

        # --- правая колонка ---

        self.hear = Section(right, "Распознавать самим",
                            "точнее субтитров YouTube")

        row = tk.Frame(self.hear.body, bg=CARD)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Считать", bg=CARD, fg=INK, width=21, anchor="w",
                 font=(FONT, 9)).pack(side="left")
        self.engine = Choice(
            row,
            [("auto", "сам"), ("cloud", "Groq"), ("local", "тут")],
            value="auto", on_change=lambda _: self._engine_note(),
        )
        self.engine.pack(side="left")

        self.engine_note = tk.Label(
            self.hear.body, text="", bg=CARD, fg=FAINT, font=(FONT, 8),
            wraplength=330, justify="left",
        )
        self.engine_note.pack(anchor="w", pady=(3, 6))

        row = tk.Frame(self.hear.body, bg=CARD)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Модель", bg=CARD, fg=INK, width=21, anchor="w",
                 font=(FONT, 9)).pack(side="left")
        # Только скачанные: за выбором ненайденной модели стоит молчаливая
        # закачка на гигабайты, и на плохом канале это выглядит зависанием.
        have = list(speech.installed())
        self.model = tk.StringVar(value=speech.ready())
        ttk.Combobox(row, textvariable=self.model, values=have or [speech.DEFAULT],
                     state="readonly", width=11).pack(side="left")

        self.lang = field(self.hear.body, "Язык", "ru", width=6)
        self.key = field(self.hear.body, "Ключ Groq", cloud.key(), width=26,
                         hint="бесплатный на console.groq.com, у каждого свой. "
                              "Без ключа всё считается на этой машине")

        self.frame = Section(right, "Крупный план", "вместо размытого фона")
        self.zoom = slider(self.frame.body, "Приблизить", 1.0, 1.8, 1.3)
        self.pan = slider(self.frame.body, "Сдвиг вбок", -0.5, 0.5, 0.0)
        self.tilt = slider(self.frame.body, "Сдвиг вверх/вниз", -0.5, 0.5, 0.0)

        self.extra = Section(right, "Прочее", "мелкие переключатели")
        self.no_title = flag(self.extra.body, "Без плашки сверху")
        self.no_subs = flag(self.extra.body, "Без субтитров в кадре")
        self.no_audio = flag(self.extra.body, "Не разбирать звук — быстрее")
        self.cpu = flag(self.extra.body, "Кодировать процессором")

        # Каждый раздел на self.on гейтит эффективное поведение в _args(),
        # а единственным сигналом об этом был цвет треугольника — не видно,
        # что реально сработает, пока раздел свёрнут. Статус-метка в
        # заголовке (Section.set_status) держит это на виду; трогается
        # каждый раз, когда меняется исход раздела или сам раздел
        # открывается/закрывается.
        for var in (self.span.on, self.span_min, self.span_max):
            var.trace_add("write", lambda *_: self._refresh_statuses())
        for var in (self.cut_at.on, self.cut_from, self.cut_to):
            var.trace_add("write", lambda *_: self._refresh_statuses())
        for var in (self.pace.on, self.speed, self.fit_minute):
            var.trace_add("write", lambda *_: self._refresh_statuses())
        for var in (self.words.on, self.must):
            var.trace_add("write", lambda *_: self._refresh_statuses())
        self.hear.on.trace_add("write", lambda *_: self._refresh_statuses())
        for var in (self.frame.on, self.zoom):
            var.trace_add("write", lambda *_: self._refresh_statuses())
        self._refresh_statuses()

        self._engine_note()

    def _refresh_statuses(self):
        """Пересчитывает статус-метки всех разделов на self.on."""
        if self.span.on.get():
            self.span.set_status(
                f"{self.span_min.get():.0f}–{self.span_max.get():.0f} с", "on")
        else:
            self.span.set_status(f"{pairs.LOW:.0f}–{pairs.HIGH:.0f} с", "off")

        if self.cut_at.on.get():
            frm, to = self.cut_from.get().strip(), self.cut_to.get().strip()
            if frm and to:
                self.cut_at.set_status(f"{frm} → {to}", "on")
            else:
                self.cut_at.set_status("таймкод не задан", "warn")
        else:
            self.cut_at.set_status("ищет сам", "off")

        if self.pace.on.get():
            if self.fit_minute.get():
                self.pace.set_status("под минуту", "on")
            else:
                self.pace.set_status(f"{self.speed.get():.2f}×", "on")
        else:
            # Не «1.0×»: выключенная галка означает автоподбор — разгон
            # включится сам и только если кусок не влезает в формат.
            self.pace.set_status("сам", "off")

        if self.words.on.get():
            if self.must.get().strip():
                self.words.set_status("фильтр включён", "on")
            else:
                self.words.set_status("слов нет — не возьмёт", "warn")
        else:
            self.words.set_status("без фильтра", "off")

        # Короче остальных статусов нарочно: у заголовка и так самая длинная
        # note в макете («точнее субтитров YouTube»), и любой текст статуса
        # длиннее пары слов вылезает за скроллбар правой колонки — проверено
        # скриншотом, не на глаз.
        if self.hear.on.get():
            self.hear.set_status("вкл", "on")
        else:
            self.hear.set_status("выкл", "off")

        if self.frame.on.get():
            self.frame.set_status(f"×{self.zoom.get():.2f}", "on")
        else:
            self.frame.set_status("по умолчанию", "off")

    def _engine_note(self):
        """Объясняет выбранный способ счёта — и чем он обернётся по времени."""
        picked = self.engine.get()
        info = machine.describe()
        room = cloud.left() / 60

        if picked == "cloud":
            if not cloud.ready():
                text = "ключа нет — посчитаю на этой машине"
            else:
                text = f"через Groq, бесплатно осталось {room:.0f} мин звука"
        elif picked == "local":
            text = ("на видеокарте, ~2 мин на часовой ролик" if info["fast"]
                    else "процессором — на часовой ролик это полчаса")
        else:
            where = speech.where_to_run()
            text = ("сам решит: сейчас выбрал "
                    + ("Groq" if where == "cloud" else "эту машину"))
            if where == "local" and info["fast"]:
                text += " — карта быстрая, квоту бережём"
        self.engine_note.config(text=text)

    # --- низ окна ---------------------------------------------------------

    def _footer(self, parent):
        tk.Frame(parent, bg=EDGE, height=1).pack(fill="x", pady=(14, 0))

        # Своя, более глубокая зона — низ окна не должен быть тем же плоским
        # BG, что и середина, иначе весь низ читается одним пятном.
        row = tk.Frame(parent, bg=BG_DEEP, pady=6)
        row.pack(fill="x", pady=(0, 4))

        self.go = Button(row, "Нарезать", self._start, primary=True,
                         width=150, bg=BG_DEEP)
        self.go.pack(side="left")

        Button(row, "Открыть папку", self._open_folder,
              bg=BG_DEEP).pack(side="left", padx=(10, 0))

        holder = tk.Frame(row, bg=BG_DEEP)
        holder.pack(side="left", padx=(16, 0))
        self.state = tk.Label(holder, text="готов", bg=BG_DEEP, fg=DIM,
                              font=(FONT, 9))
        self.state.pack(anchor="w")
        self.progress = Progress(holder, bg=BG_DEEP)
        self.progress.pack(anchor="w", pady=(4, 0))

        # Лог не растягиваем: место должно доставаться настройкам, а не
        # пустой консоли. Девяти строк хватает, чтобы видеть, что идёт.
        shell = tk.Frame(parent, bg=CARD)
        shell.pack(fill="x")
        self.log = tk.Text(shell, height=9, bg=LOG_BG, fg=LOG_FG,
                           insertbackground=LOG_FG, relief="flat", wrap="word",
                           font=(MONO, 9), padx=12, pady=9,
                           highlightthickness=0, spacing1=1)
        self.log.pack(fill="x", padx=1, pady=1)

        # Цвет строки говорит о ней больше, чем сам текст: готовое видно
        # сразу, ошибку не пропустишь.
        self.log.tag_config("ok", foreground=ACCENT_HOT)
        self.log.tag_config("bad", foreground=BAD)
        self.log.tag_config("warn", foreground=WARN)
        self.log.tag_config("head", foreground=CREAM)
        self.log.configure(state="disabled")

    def _open_folder(self):
        OUT.mkdir(parents=True, exist_ok=True)
        # explorer, а не os.startfile: из процесса от админа тот молчит.
        import subprocess
        subprocess.run(["explorer", str(OUT)], check=False)

    # --- работа -----------------------------------------------------------

    def _args(self):
        """Собирает те же самые параметры, что принимает командная строка."""
        # Своя длина не задана — не задаём её и командной строке: None там
        # значит «коридор как в pairs», а не «40–90». Прибив тут числа, окно
        # молча сужало основной путь до тех, что стоят на ползунках.
        low = high = None
        if self.span.on.get():
            low, high = self.span_min.get(), self.span_max.get()
            if high < low + 5:
                high = low + 5

        # Ускорение выключено — это «подбери сам, если кусок не влезает», а
        # не «ровно 1.0». Единица здесь означала бы заданную вручную скорость
        # (см. render.fit_speed), и разгон в окне не работал вовсе, в отличие
        # от командной строки, где --speed по умолчанию None.
        speed = None
        if self.pace.on.get():
            speed = 0.0 if self.fit_minute.get() else round(self.speed.get(), 2)

        return Namespace(
            sources=[self.source.get().strip().strip('"')],
            source=self.source.get().strip().strip('"'),
            min=float(low) if low is not None else None,
            max=float(high) if high is not None else None,
            top=10,
            count=int(self.count.get()),
            boost=self.boost.get() if self.words.on.get() else "",
            must=self.must.get() if self.words.on.get() else "",
            no_audio=self.no_audio.get(),
            whisper=self.hear.on.get(),
            model=self.model.get(),
            lang=self.lang.get() or "ru",
            subs=None,
            no_subs=self.no_subs.get(),
            no_title=self.no_title.get(),
            # Галка «Крупный план» перебивает выбор кадра: там человек уже
            # сам задал рамку ползунками, и раскладка ни при чём.
            mode="crop" if self.frame.on.get() else self.shape.get(),
            zoom=round(self.zoom.get(), 2) if self.frame.on.get() else 1.0,
            pan=round(self.pan.get(), 2) if self.frame.on.get() else 0.0,
            tilt=round(self.tilt.get(), 2) if self.frame.on.get() else 0.0,
            speed=speed, height=1080, ffmpeg=None, cpu=self.cpu.get(),
            engine=self.engine.get(),
            # Отдельного флажка нет намеренно: моменты в окне никто глазами
            # не отбирает, значит сравнивать хуки должна модель — всегда,
            # когда есть чем. Сперва Groq, потом локальная через Ollama;
            # нет ни той, ни другой — тихо работает свой счёт.
            brain=bool(think.anyone()),
        )

    def _start(self):
        if self.busy:
            return
        if not self.source.get().strip():
            self._say("! сначала укажи ссылку или файл", "bad")
            return

        # Ключ запоминаем при запуске, а не по отдельной кнопке: человек
        # вставил его и нажал «Нарезать» — этого достаточно.
        cloud.remember_key(self.key.get())

        args = self._args()

        if self.cut_at.on.get():
            start = parse_stamp(self.cut_from.get())
            end = parse_stamp(self.cut_to.get())
            if start is None or end is None or end <= start:
                self._say("! таймкод не разобрал — нужно «от» и «до», "
                          "например 12:30 и 13:15", "bad")
                return
        else:
            start = end = None

        self.busy = True
        self.made = []
        self.go.set_text("Режу…")
        self.go.set_enabled(False)
        self.state.config(text="работаю, окно не закрывай", fg=ACCENT)
        self.progress.start()

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        threading.Thread(target=self._work, args=(args, start, end),
                         daemon=True).start()

    def _work(self, args, start, end):
        """Тот же проход, что в командной строке. Печать уводим в лог."""
        stream = _Pipe(self.lines)
        saved = sys.stdout
        sys.stdout = stream
        try:
            if start is None:
                cut.cmd_auto(args)
            else:
                self._one_piece(args, start, end)
        except Exception as error:
            print(f"! {type(error).__name__}: {error}")
        finally:
            sys.stdout = saved
            self.lines.put(None)

    def _one_piece(self, args, start, end):
        """Ровно заданный кусок: моменты не ищем, режем что сказали."""
        ffmpeg = render.find_ffmpeg(args.ffmpeg)
        print(f"кодирую через: {render.encoder(ffmpeg, not args.cpu)[1]}")

        slug, words, _, _, _ = cut._analyze(args.source, args, ffmpeg)
        video = cut._video(args.source, slug, args.height, ffmpeg)
        # Раскладку считаем и здесь. Без неё «Кадр стопкой» вместе со «Своим
        # таймкодом» выходил ни тем ни другим: render не знал, кого куда
        # ставить, и рисовал обычный кадр на размытом фоне, а место сверху
        # под плашку всё равно отводилось по-стопочному — надпись висела
        # в пустоте над картинкой.
        cut._make(
            ffmpeg, video, slug, None if args.no_subs else words,
            start, end, args, tiles=cut._tiles(ffmpeg, video, slug),
        )
        print(f"\nГотово: 1 шортс в {OUT}/")

    # --- лог --------------------------------------------------------------

    def _tag_for(self, text):
        low = text.lower()
        if text.lstrip().startswith("!") or "ошибка" in low or "не сработ" in low:
            return "bad"
        if "готово" in low or "готов:" in low:
            return "ok"
        if low.lstrip().startswith("["):
            return "head"
        if "не хватит" in low or "квот" in low or "пропускаю" in low:
            return "warn"
        return None

    def _say(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or self._tag_for(text) or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        """Забирает из очереди то, что напечатал рабочий поток."""
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                self._finished()
                continue
            self._say(line.rstrip("\n"))
        self.root.after(120, self._drain)

    def _finished(self):
        self.busy = False
        self.progress.stop()
        self.go.set_text("Нарезать")
        self.go.set_enabled(True)

        ready = sorted(OUT.glob("*.mp4")) if OUT.exists() else []
        self.state.config(text=f"готово — {len(ready)} в папке", fg=ACCENT)


class _Pipe(io.TextIOBase):
    """Подменяет stdout: печать из рабочего потока уходит в очередь окна."""

    def __init__(self, target):
        self.target = target
        self.rest = ""

    def write(self, text):
        self.rest += text
        while "\n" in self.rest:
            line, self.rest = self.rest.split("\n", 1)
            self.target.put(line)
        return len(text)

    def flush(self):
        if self.rest:
            self.target.put(self.rest)
            self.rest = ""


# Настоящий блюр фона через DWM (SetWindowCompositionAttribute,
# ACCENT_ENABLE_BLURBEHIND / ACRYLICBLURBEHIND) пробовался и снят.
# Проверено вживую скриншотами (ImageGrab), не в теории: и BLURBEHIND (3),
# и ACRYLIC (4) с разными AccentFlags/альфой стабильно выбеливают всё окно
# целиком в сплошное пятно, местами вместо этого — пятна с рабочим столом,
# просвечивающим сквозь текст и кнопки. Причина в том, что Tk перерисовывает
# каждый пиксель окна непрозрачно, и DWM в этой связке трактует уже готовую
# картинку окна как сырьё для размытия, а не смешивает блюр только с пустыми
# местами. Чтобы получить смешение только там, где нужно, требуется
# colorkey-прозрачность (`wm_attributes("-transparentcolor", …)`) поверх
# каждого места, залитого BG, — а это уже не точечная правка, а пересмотр
# заливки по всему окну, который не проверить без ещё одного захода вживую.
# Оставлено как честный предел: «стекло» в этой сборке — только усиленный
# псевдо-глянец на Canvas (_glossy(), тени в Button и card()).


def main():
    jobs.enable_kill_on_exit()
    root = tk.Tk()
    enable_clipboard(root)
    Window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
