"""Thread-safe async helpers for Yaku."""
import threading
import queue
import functools
from typing import Callable, Any, Optional, TypeVar
from gi.repository import GLib

T = TypeVar("T")


class AsyncResult:
    """Container for async operation results."""

    def __init__(self):
        self._result = None
        self._error = None
        self._done = False
        self._callbacks = []

    def set_result(self, result):
        self._result = result
        self._done = True
        self._run_callbacks()

    def set_error(self, error):
        self._error = error
        self._done = True
        self._run_callbacks()

    def add_callback(self, callback: Callable[[Any], None]):
        if self._done:
            GLib.idle_add(callback, self._result if not self._error else self._error)
        else:
            self._callbacks.append(callback)

    def _run_callbacks(self):
        for callback in self._callbacks:
            if self._error:
                GLib.idle_add(callback, self._error)
            else:
                GLib.idle_add(callback, self._result)
        self._callbacks.clear()


def run_in_thread(func: Callable[..., T], *args, **kwargs) -> AsyncResult:
    """Run a function in a background thread and return an AsyncResult.

    The result/error will be delivered via GLib.idle_add to the main thread.
    """
    result = AsyncResult()

    def worker():
        try:
            res = func(*args, **kwargs)
            result.set_result(res)
        except Exception as e:
            result.set_error(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return result


def run_in_thread_sync(func: Callable[..., T], *args, **kwargs) -> T:
    """Run a function in a background thread and wait for result (blocking).

    Use only from non-GUI threads!
    """
    result_queue = queue.Queue()

    def worker():
        try:
            res = func(*args, **kwargs)
            result_queue.put(("ok", res))
        except Exception as e:
            result_queue.put(("error", e))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    status, value = result_queue.get()
    thread.join()

    if status == "error":
        raise value
    return value


class PeriodicTask:
    """Run a function periodically on the main thread."""

    def __init__(self, interval_seconds: float, callback: Callable[[], bool]):
        self.interval = interval_seconds
        self.callback = callback
        self.source_id = None
        self.running = False

    def start(self):
        if not self.running:
            self.running = True
            self.source_id = GLib.timeout_add_seconds(
                int(self.interval), self._run
            )

    def stop(self):
        if self.running and self.source_id:
            GLib.source_remove(self.source_id)
            self.running = False
            self.source_id = None

    def _run(self):
        if not self.running:
            return False
        try:
            return self.callback()
        except Exception:
            import traceback
            traceback.print_exc()
            return True  # Continue on error


def idle_add(func: Callable[..., T], *args, **kwargs) -> int:
    """Thread-safe wrapper for GLib.idle_add."""
    return GLib.idle_add(func, *args, **kwargs)


def timeout_add_seconds(seconds: int, func: Callable[..., bool], *args, **kwargs) -> int:
    """Thread-safe wrapper for GLib.timeout_add_seconds."""
    return GLib.timeout_add_seconds(seconds, func, *args, **kwargs)