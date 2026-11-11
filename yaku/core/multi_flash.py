"""Helpers for simultaneous flashing to several drives.

The writer backends still report per-drive ``(done, total)`` bytes.  The
aggregator combines those channels into one fraction so the wizard can show
a single progress bar while several drives are being written at once.
"""
import threading


class AggregateProgress:
    """Thread-safe aggregation of several per-drive progress callbacks."""

    def __init__(self, count: int, callback):
        self._done = [0] * max(0, count)
        self._total = [0] * max(0, count)
        self._callback = callback
        self._lock = threading.Lock()

    def channel(self, index: int):
        """Return a ``progress(done, total)`` callback for one drive."""
        def report(done, total):
            with self._lock:
                self._done[index] = max(0, int(done))
                self._total[index] = max(0, int(total))
                done_sum = sum(self._done)
                total_sum = sum(self._total)
            if total_sum > 0:
                self._callback(done_sum / total_sum, done_sum, total_sum)

        return report

    def reset(self):
        with self._lock:
            for i in range(len(self._done)):
                self._done[i] = 0
                self._total[i] = 0
