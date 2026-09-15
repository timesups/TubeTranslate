"""Coordinate package runs and destructive operations in the single worker process."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps
from typing import Callable


class PackageBusyError(RuntimeError):
    pass


operation_lock = threading.RLock()
_active: dict[str, list[Callable[[], None]]] = {}


def is_active(package_id: str) -> bool:
    with operation_lock:
        return package_id in _active


@contextmanager
def run(package_id: str):
    with operation_lock:
        acquired = package_id not in _active
        if acquired:
            _active[package_id] = []
    try:
        yield acquired
    finally:
        if acquired:
            with operation_lock:
                callbacks = _active.pop(package_id)
            for callback in callbacks:
                callback()


def defer_cleanup(package_id: str, cleanup: Callable[[], None]) -> bool:
    """Caller holds operation_lock while removing the DB record and scheduling cleanup."""
    with operation_lock:
        if package_id not in _active:
            return False
        _active[package_id].append(cleanup)
        return True


def require_idle(func):
    @wraps(func)
    def wrapped(package_id: str, *args, **kwargs):
        with operation_lock:
            if package_id in _active:
                raise PackageBusyError("Package workers are still finishing; try again after they stop.")
            return func(package_id, *args, **kwargs)
    return wrapped
