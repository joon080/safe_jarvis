"""
Double-clap detector using sounddevice.
Calls `on_clap_detected()` callback in a daemon thread when two loud spikes
are heard within `window_ms` milliseconds.

Usage:
    from listener.clap_listener import ClapListener
    l = ClapListener(on_clap_detected=lambda: print("clap!"))
    l.start()   # non-blocking
    ...
    l.stop()
"""

import threading
import time
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
    _SD_OK = True
except Exception:
    _SD_OK = False

# RMS threshold — raise if false positives, lower if misses
_THRESHOLD    = 0.35
_WINDOW_MS    = 700    # max gap between two claps
_COOLDOWN_S   = 1.2   # ignore further detections after a double clap
_SAMPLE_RATE  = 16000
_BLOCK_SIZE   = 512


class ClapListener:
    def __init__(self, on_clap_detected: Callable, threshold: float = _THRESHOLD):
        self._cb         = on_clap_detected
        self._threshold  = threshold
        self._running    = False
        self._thread: threading.Thread | None = None
        self._last_spike: float = 0.0
        self._last_clap:  float = 0.0

    def start(self):
        if not _SD_OK:
            print("[ClapListener] sounddevice not available — clap detection disabled.")
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        def callback(indata, frames, time_info, status):
            if not self._running:
                return
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)) / 32768.0)
            if rms > self._threshold:
                now = time.monotonic()
                gap = (now - self._last_spike) * 1000  # ms
                cooldown_ok = (now - self._last_clap) > _COOLDOWN_S
                if 80 < gap < _WINDOW_MS and cooldown_ok:
                    self._last_clap = now
                    threading.Thread(target=self._cb, daemon=True).start()
                self._last_spike = now

        try:
            with sd.InputStream(
                samplerate=_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=_BLOCK_SIZE,
                callback=callback,
            ):
                while self._running:
                    time.sleep(0.05)
        except Exception as e:
            print(f"[ClapListener] mic error: {e}")
