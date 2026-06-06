"""
Double-clap detector using sounddevice.
Calls `on_clap_detected()` callback in a daemon thread when two sharp spikes
(claps) are heard within `window_ms` milliseconds.

Detection logic (calibrated 2026-06-06 from live mic diagnostics):
  A clap is a sudden, sharp transient — a high peak that stands far above the
  *recent background level*. Plain absolute thresholds don't separate claps
  from speech on a low-gain mic (their amplitude ranges overlap), so we use:

    1. peak  > _PEAK_THRESHOLD            (absolute floor, clears noise)
    2. peak  > _CREST_OVER_BG * baseline  (sharp transient vs running RMS)

  The running baseline rises while you talk, so sustained speech fails (2);
  during quiet idle a clap is a huge spike over near-silence and passes.

  Measured (this mic): noise peak <= 0.0023, clap peak 0.025~0.13,
  speech peak 0.07~0.29. Raising Windows mic input volume / boost makes claps
  clip (~1.0) and clearly tower over speech — then _PEAK_THRESHOLD can go to
  ~0.4 and the crest check becomes optional.

Usage:
    from listener.clap_listener import ClapListener
    l = ClapListener(on_clap_detected=lambda: print("clap!"),
                     is_busy=lambda: recording_now)
    l.start()   # non-blocking
    ...
    l.stop()
"""

import threading
import time
from typing import Callable, Optional

import numpy as np

try:
    import sounddevice as sd
    _SD_OK = True
except Exception:
    _SD_OK = False

# --- detection tuning -------------------------------------------------------
# Absolute peak floor (normalized 0..1). Measured on this mic: noise peak
# ~0.0023, claps 0.025~0.13. 0.03 sits ~13x above noise and catches normal
# claps. Trade-off: lower (→0.02) catches very soft claps but lets keyboard/
# desk taps through more; raise (→0.05) is stricter but misses soft claps.
# The crest-over-background check below adds a second line of defense.
_PEAK_THRESHOLD = 0.03
# A clap's peak must exceed this multiple of the running background RMS.
# Idle clap: peak/baseline is huge → passes. Speech: baseline rises → fails.
_CREST_OVER_BG  = 6.0
# EMA smoothing for the background RMS estimate (0..1, lower = slower).
_BG_ALPHA       = 0.15
# Floor for baseline so a perfectly silent room doesn't make crest explode
# on tiny noise (keeps the absolute peak check in control at idle).
_BG_FLOOR       = 0.0008

# --- speech gate ------------------------------------------------------------
# A clap is a single short impulse; speech is sustained energy across many
# blocks. When RMS stays above _SPEECH_RMS for _SPEECH_MIN_BLOCKS consecutive
# blocks we treat it as speech and *suspend clap detection* until the talking
# stops plus a short hangover. This is what stops speech being heard as claps.
_SPEECH_RMS        = 0.008   # same level main.py uses to detect speech
_SPEECH_MIN_BLOCKS = 5       # ~160 ms sustained (block = 512/16000 = 32 ms)
_SPEECH_HANGOVER_S = 0.8     # keep claps off this long after talking stops

_WINDOW_MS      = 700    # max gap between the two claps
_MIN_GAP_MS     = 80     # min gap (reject a single long spike / ringing)
_COOLDOWN_S     = 1.2    # ignore further detections after a double clap
_SAMPLE_RATE    = 16000
_BLOCK_SIZE     = 512


class ClapListener:
    def __init__(
        self,
        on_clap_detected: Callable,
        is_busy: Optional[Callable[[], bool]] = None,
        peak_threshold: float = _PEAK_THRESHOLD,
        crest_over_bg: float = _CREST_OVER_BG,
    ):
        self._cb            = on_clap_detected
        self._is_busy       = is_busy or (lambda: False)
        self._peak_thold    = peak_threshold
        self._crest_over_bg = crest_over_bg
        self._running       = False
        self._thread: Optional[threading.Thread] = None
        self._last_spike: float = 0.0
        self._last_clap:  float = 0.0
        self._bg_rms: float     = _BG_FLOOR
        self._speech_blocks: int   = 0     # consecutive above-speech blocks
        self._speech_until: float  = 0.0   # claps suspended until this time

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

            x    = indata.astype(np.float32)
            rms  = float(np.sqrt(np.mean(x ** 2)) / 32768.0)
            peak = float(np.max(np.abs(x)) / 32768.0)

            now = time.monotonic()

            # --- speech gate: suspend clap detection while someone is talking.
            # Count consecutive blocks above the speech level. A clap is only
            # 1-2 blocks so it never reaches _SPEECH_MIN_BLOCKS; sustained speech
            # does, and arms a hangover window during which claps are ignored.
            if rms > _SPEECH_RMS:
                self._speech_blocks += 1
                if self._speech_blocks >= _SPEECH_MIN_BLOCKS:
                    self._speech_until = now + _SPEECH_HANGOVER_S
            else:
                self._speech_blocks = 0
            speech_active = now < self._speech_until

            # Don't listen for claps while we're recording / processing / speaking
            # (mic would pick up TTS and our own voice → false triggers).
            # Still track the background so the baseline stays current.
            busy = self._is_busy()

            # Sharp-transient test BEFORE folding this block into the baseline,
            # so a clap doesn't inflate its own background reference.
            baseline = max(self._bg_rms, _BG_FLOOR)
            is_clap_spike = (
                peak > self._peak_thold
                and peak > self._crest_over_bg * baseline
            )

            # Update running background RMS (EMA). Skip frames that are clearly a
            # transient so claps don't drag the baseline up.
            if not is_clap_spike:
                self._bg_rms = (1 - _BG_ALPHA) * self._bg_rms + _BG_ALPHA * rms

            if busy or speech_active:
                return

            if is_clap_spike:
                gap = (now - self._last_spike) * 1000.0  # ms
                cooldown_ok = (now - self._last_clap) > _COOLDOWN_S
                if _MIN_GAP_MS < gap < _WINDOW_MS and cooldown_ok:
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
