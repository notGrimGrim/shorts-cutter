"""Чтобы дочерние процессы не переживали родителя.

Без этого: Task Manager или закрытие окна убивает python.exe, а ffmpeg,
который он запустил на закачку куска, остаётся висеть в фоне. В следующий
прогон новый такой же процесс лезет качать тот же файл — и оба виснут,
деля один и тот же .part между собой. Job Object решает это на уровне
Windows: закрылась работа — все её дети закрываются вместе с ней, а не
превращаются в зомби.
"""

import ctypes
import os
from ctypes import wintypes

_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# Держим хэндл в модульной переменной: сборщик мусора закроет его вместе
# с процессом, а закрытие хэндла — это и есть сигнал убить всех детей.
_job = None


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def enable_kill_on_exit():
    """Зовётся один раз при старте cut.py / gui.py, до первой закачки.

    На не-Windows и при нехватке прав тихо ничего не делает: без этого
    поведение просто остаётся прежним, ронять программу незачем.
    """
    global _job
    if os.name != "nt":
        return

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Без явных argtypes/restype ctypes принимает хэндлы за c_int:
        # GetCurrentProcess() — псевдохэндл (-1), и на 64-битной сборке он
        # обрезается неверно. AssignProcessToJobObject тогда падает с
        # ERROR_INVALID_HANDLE, молча ничего не делает — и вся защита
        # от осиротевших процессов не работает, хотя ошибок не видно.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            _job = job
    except OSError:
        pass
