"""
Wake-word detector using openWakeWord (fully offline, no API key / signup).

Listens continuously for the pretrained "hey jarvis" model and calls
`on_wake_detected()` in a daemon thread when heard. Runs alongside the clap
listener — either one can trigger the assistant.

Models are bundled/downloaded once into the openwakeword package dir; if missing
on first run, they are fetched automatically (needs internet that one time).
If the libraries are missing, the listener disables itself quietly (clap still
works).

Usage:
    from listener.wake_listener import WakeListener
    w = WakeListener(on_wake_detected=lambda: print("jarvis!"),
                     is_busy=lambda: recording_now)
    w.start()   # non-blocking
    ...
    w.stop()
"""

import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

try:
    import sounddevice as sd
    _SD_OK = True
except Exception:
    _SD_OK = False

try:
    import openwakeword            # noqa: F401
    from openwakeword.model import Model
    _OWW_OK = True
except Exception:
    _OWW_OK = False

_MODEL_NAME  = "hey_jarvis"
_THRESHOLD   = 0.5     # 0..1 — raise to reduce false positives, lower if missed
_SAMPLE_RATE = 16000
_FRAME       = 1280    # 80 ms @ 16 kHz (openWakeWord's expected chunk)
_COOLDOWN_S  = 1.5     # ignore further wakes right after one fires


class WakeListener:
    def __init__(
        self,
        on_wake_detected: Callable,
        is_busy: Optional[Callable[[], bool]] = None,
        threshold: float = _THRESHOLD,
    ):
        self._cb        = on_wake_detected
        self._is_busy   = is_busy or (lambda: False)
        self._threshold = threshold
        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self._last_wake: float = 0.0
        self._model = None
        self._model_ready = self._load_model() if (_OWW_OK and _SD_OK) else False

    def _load_model(self) -> bool:
        try:
            self._model = Model(wakeword_models=[_MODEL_NAME],
                                inference_framework="onnx")
            return True
        except Exception:
            # Models probably not downloaded yet — fetch once, then retry.
            try:
                from openwakeword.utils import download_models
                download_models()
                self._model = Model(wakeword_models=[_MODEL_NAME],
                                    inference_framework="onnx")
                return True
            except Exception as e:
                print(f"[WakeListener] model load failed — wake word 비활성화: {e}")
                return False

    def available(self) -> bool:
        return _OWW_OK and _SD_OK and self._model_ready

    def start(self):
        if not self.available():
            if not (_OWW_OK and _SD_OK):
                print("[WakeListener] openwakeword/sounddevice 미설치 — wake word 비활성화.")
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        q: "queue.Queue[bytes]" = queue.Queue(maxsize=50)

        def callback(indata, frames, time_info, status):
            try:
                q.put_nowait(bytes(indata))
            except queue.Full:
                pass  # drop frames if we fall behind

        try:
            with sd.InputStream(samplerate=_SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=_FRAME, callback=callback):
                while self._running:
                    try:
                        raw = q.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    # Ignore wake word while recording / processing / speaking
                    # (mic would pick up TTS / our own voice → false triggers).
                    if self._is_busy():
                        # drain backlog so we don't act on stale audio later
                        while not q.empty():
                            try:
                                q.get_nowait()
                            except queue.Empty:
                                break
                        continue

                    audio = np.frombuffer(raw, dtype=np.int16)
                    scores = self._model.predict(audio)
                    if scores.get(_MODEL_NAME, 0.0) > self._threshold:
                        now = time.monotonic()
                        if (now - self._last_wake) > _COOLDOWN_S:
                            self._last_wake = now
                            threading.Thread(target=self._cb, daemon=True).start()
        except Exception as e:
            print(f"[WakeListener] mic error: {e}")
