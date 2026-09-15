"""Process-wide limits shared by standalone and package pipelines."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable


GPU = threading.BoundedSemaphore(1)
IMPORT = threading.BoundedSemaphore(1)
AUDIO = threading.BoundedSemaphore(1)
VIDEO = threading.BoundedSemaphore(1)
TRANSLATION_REQUESTS = threading.BoundedSemaphore(16)
TTS_REQUESTS = threading.BoundedSemaphore(16)
EXPORT = threading.Lock()
_context = threading.local()


def current_check() -> Callable[[], None] | None:
    return getattr(_context, "check", None)


def check_interruption() -> None:
    check = current_check()
    if check is not None:
        check()


@contextmanager
def check_context(check: Callable[[], None] | None):
    previous = current_check()
    _context.check = check
    try:
        check_interruption()
        yield
    finally:
        _context.check = previous


@contextmanager
def slot(semaphore, check: Callable[[], None] | None = None):
    if check is None:
        semaphore.acquire()
    else:
        while True:
            check()
            if semaphore.acquire(timeout=0.1):
                break
    try:
        if check is not None:
            check()
        yield
    finally:
        semaphore.release()


@contextmanager
def stage_slot(stage: str, task: dict, check: Callable[[], None] | None = None):
    semaphore = None
    if stage == "download":
        semaphore = IMPORT
    elif stage == "asr" or (stage == "separate" and task.get("audio_mode") == "keep_bgm"):
        semaphore = GPU
    elif stage == "tts" and task.get("tts_provider") == "voxcpm":
        semaphore = GPU
    elif stage == "separate":
        semaphore = IMPORT
    elif stage in ("split_audio", "merge_audio"):
        semaphore = AUDIO
    elif stage == "merge_video":
        semaphore = VIDEO
    if semaphore is None:
        if check is not None:
            check()
        yield
    else:
        with slot(semaphore, check):
            yield
