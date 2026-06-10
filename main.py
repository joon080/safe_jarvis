#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe_jarvis 파트 B — J.A.R.V.I.S 음성/텍스트 비서 (Gemini 기반)

입력 2종:
  1. 박수 두 번  → 마이크 활성화 → Google STT → Gemini
  2. 텍스트 입력 (UI)
  (UI의 마이크 버튼은 음소거 토글 전용 — 녹음 시작 아님)

흐름:
  입력 → Gemini(function calling) → tool 실행 → TTS 응답 → UI 로그

안전 원칙 (위험 기능 제거):
  - file_controller write/delete 없음
  - computer_control (클릭/타이핑) 없음
  - send_message 없음
  - browser_control(로그인 브라우저) 없음
  - commit/push/deploy는 사용자 승인 없이 불가
"""

import json
import os
import sys
import threading
import traceback
import time
import shutil
import subprocess
import tempfile
from pathlib import Path

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = _base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
PIPELINE_SCRIPT = BASE_DIR / "actions" / "dual_agent_pipeline.py"
PIPELINE_CONFIG = BASE_DIR / "config" / "agent_pipeline.json"

def _find_python() -> str:
    # Try the standard per-user install location dynamically (%LOCALAPPDATA%)
    local_app = os.environ.get("LOCALAPPDATA", "")
    local_py  = str(Path(local_app) / "Programs" / "Python" / "Python311" / "python.exe") if local_app else ""
    candidates = [
        local_py,
        shutil.which("py") or "",
        shutil.which("python") or "",
        "python",
    ]
    for c in candidates:
        if c and (Path(c).exists() or shutil.which(c)):
            return c
    return "python"

PYTHON_EXE = _find_python()

# ---------------------------------------------------------------------------
# soft imports
# ---------------------------------------------------------------------------
try:
    import google.generativeai as genai
    _GENAI_OK = True
except ImportError:
    _GENAI_OK = False

try:
    import sounddevice as sd
    import numpy as np
    _SD_OK = True
except ImportError:
    _SD_OK = False

try:
    import speech_recognition as sr
    _SR_OK = True
except ImportError:
    _SR_OK = False

try:
    import pyttsx3 as _pyttsx3
    _TTS_OK = True
except ImportError:
    _TTS_OK = False

from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, format_memory_for_prompt
from actions.open_app         import open_app
from actions.web_search       import web_search as web_search_action
from actions.weather_report   import weather_action
from actions.reminder         import reminder
from actions.screen_processor import screen_process
from actions import screen_processor as _vision

try:
    from actions.youtube_video import youtube_video as _youtube
    _YT_OK = True
except ImportError:
    _YT_OK = False

from listener.clap_listener import ClapListener
from listener.wake_listener import WakeListener

# ---------------------------------------------------------------------------
# API key + system prompt
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, an AI development assistant. "
            "Be concise and always use tools to complete tasks."
        )

# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

# Set while TTS is actively playing, so the clap listener can ignore the mic
# (otherwise speakers → mic feedback re-triggers detection).
_SPEAKING = threading.Event()

# EdgeTTS (online, high quality, Korean-capable). Male voices to match JARVIS.
try:
    import asyncio
    import edge_tts as _edge_tts
    _EDGE_OK = True
except ImportError:
    _EDGE_OK = False

_EDGE_VOICE_KO = "ko-KR-InJoonNeural"   # Korean male
_EDGE_VOICE_EN = "en-US-GuyNeural"      # English male


def _has_korean(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


class _TTS:
    def __init__(self):
        self._engine = None
        self._lock   = threading.Lock()
        # pyttsx3 is kept only as an offline fallback (often fails on Windows
        # with "Class not registered"); EdgeTTS is the primary path.
        if _TTS_OK:
            try:
                self._engine = _pyttsx3.init()
                self._engine.setProperty("rate", 175)
                voices = self._engine.getProperty("voices")
                if voices:
                    male = next((v for v in voices if "david" in v.name.lower()), None)
                    if male:
                        self._engine.setProperty("voice", male.id)
            except Exception as e:
                print(f"[TTS] pyttsx3 init failed: {e}")
                self._engine = None

    def speak(self, text: str, ui=None):
        if not text.strip():
            return
        if ui:
            ui.set_state("SPEAKING")
        _SPEAKING.set()
        try:
            with self._lock:
                if _EDGE_OK and self._speak_edge(text):
                    return
                self._speak_fallback(text)
        except Exception as e:
            print(f"[TTS] error: {e}")
        finally:
            _SPEAKING.clear()
            if ui and not ui.muted:
                ui.set_state("LISTENING")

    def _speak_edge(self, text: str) -> bool:
        """Synthesize via EdgeTTS → temp mp3 → play. Returns False on failure
        (e.g. no internet) so the caller can fall back."""
        path = None
        try:
            voice = _EDGE_VOICE_KO if _has_korean(text) else _EDGE_VOICE_EN
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)

            async def _gen():
                await _edge_tts.Communicate(text, voice).save(path)
            asyncio.run(_gen())

            if os.path.getsize(path) == 0:
                return False
            self._play_mp3(path)
            return True
        except Exception as e:
            print(f"[TTS] edge-tts failed → fallback: {e}")
            return False
        finally:
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass

    @staticmethod
    def _play_mp3(path: str):
        """Play an mp3 using the Windows built-in MCI (no extra dependency)."""
        from ctypes import windll
        alias = "safejarvis_tts"

        def mci(cmd: str):
            windll.winmm.mciSendStringW(cmd, None, 0, None)

        mci(f'open "{path}" type mpegvideo alias {alias}')
        try:
            mci(f"play {alias} wait")   # blocks until playback finishes
        finally:
            mci(f"close {alias}")

    def _speak_fallback(self, text: str):
        """Offline fallback: pyttsx3, else PowerShell System.Speech."""
        if self._engine:
            self._engine.say(text)
            self._engine.runAndWait()
            return
        # PowerShell System.Speech: keep Korean/Unicode (no ASCII stripping).
        safe = text.replace("'", "")
        ps_cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{safe}')"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            timeout=30, capture_output=True,
        )

_tts = _TTS()

# ---------------------------------------------------------------------------
# Audio recording + STT
# ---------------------------------------------------------------------------

_SAMPLE_RATE   = 16000
_SILENCE_THOLD = 0.008
_SILENCE_AFTER = 1.8
_MAX_RECORD    = 30.0

def _record_until_silence(ui) -> bytes | None:
    if not _SD_OK:
        return None
    ui.set_state("LISTENING")
    ui.write_log("SYS: 말씀하세요...")

    chunks: list[bytes] = []
    last_speech    = time.monotonic()
    started_speech = False

    def callback(indata, frames, time_info, status):
        nonlocal last_speech, started_speech
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)) / 32768.0)
        chunks.append(indata.tobytes())
        if rms > _SILENCE_THOLD:
            last_speech    = time.monotonic()
            started_speech = True

    try:
        with sd.InputStream(samplerate=_SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=512, callback=callback):
            start = time.monotonic()
            while True:
                time.sleep(0.05)
                if time.monotonic() - start > _MAX_RECORD:
                    break
                if started_speech and (time.monotonic() - last_speech) > _SILENCE_AFTER:
                    break
    except Exception as e:
        ui.write_log(f"SYS: 마이크 오류 — {e}")
        return None

    if not started_speech or not chunks:
        return None
    return b"".join(chunks)


def _write_wav(pcm: bytes, path: str):
    import struct
    sr, ch, bps   = _SAMPLE_RATE, 1, 16
    byte_rate      = sr * ch * bps // 8
    block_align    = ch * bps // 8
    data_size      = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, ch, sr,
        byte_rate, block_align, bps,
        b"data", data_size,
    )
    with open(path, "wb") as f:
        f.write(header + pcm)


def _transcribe(audio_bytes: bytes) -> str | None:
    if not _SR_OK:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        _write_wav(audio_bytes, wav_path)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data, language="ko-KR")
    except sr.UnknownValueError:
        return None
    except Exception as e:
        print(f"[STT] error: {e}")
        return None
    finally:
        try:
            os.unlink(wav_path)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Gemini tool declarations (dict 형식 — google.generativeai 호환)
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "start_dual_agent_task",
        "description": (
            "Runs the Claude+Codex dual-agent development pipeline. "
            "Use when the user asks to implement, fix, review, refactor, or debug code. "
            "Also triggers on: 'dual agent mode', 'Claude랑 Codex로', '상호 검토', "
            "'파이프라인으로 작업해'. "
            "Claude plans and implements. Codex reviews and suggests fixes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {
                    "type": "STRING",
                    "description": "The development task description"
                },
                "project_path": {
                    "type": "STRING",
                    "description": "Target project path. Leave empty to use config default."
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "open_app",
        "description": "Opens any application on the computer.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {"type": "STRING", "description": "Application name"}
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web for information. "
            "For comparing multiple things, set mode='compare' and fill 'items'/'aspect'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query (search mode)"},
                "mode":   {"type": "STRING", "description": "'search' or 'compare'"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"},
                           "description": "Compare mode: the things to compare"},
                "aspect": {"type": "STRING",
                           "description": "Compare mode: what aspect to compare (e.g. price, performance)"},
            },
            "required": []
        }
    },
    {
        "name": "weather_report",
        "description": "Gets weather report for a city.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "YYYY-MM-DD"},
                "time":    {"type": "STRING", "description": "HH:MM (24h)"},
                "message": {"type": "STRING"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "screen_process",
        "description": "Captures and analyzes the screen or webcam.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "screen or camera"},
                "text":  {"type": "STRING", "description": "Question about the image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "save_memory",
        "description": "Save a personal fact about the user to long-term memory.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {"type": "STRING"},
                "key":      {"type": "STRING"},
                "value":    {"type": "STRING"}
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": "Shuts down the assistant when the user says goodbye.",
        "parameters": {"type": "OBJECT", "properties": {}}
    },
]

if _YT_OK:
    TOOL_DECLARATIONS.append({
        "name": "youtube_video",
        "description": "Controls YouTube: play, summarize, trending.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play|summarize|get_info|trending"},
                "query":  {"type": "STRING"},
                "url":    {"type": "STRING"},
            },
            "required": []
        }
    })

# ---------------------------------------------------------------------------
# SafeJarvis
# ---------------------------------------------------------------------------

class SafeJarvis:

    def __init__(self, ui: JarvisUI, api_key: str):
        self.ui      = ui
        self.api_key = api_key
        self._history: list = []
        self._recording = threading.Event()
        self._busy      = threading.Event()   # recording / thinking / speaking
        self._lock      = threading.Lock()
        self._model     = None
        # 마지막으로 성공한 모델 — 다음 요청부터 404 후보를 다시 돌지 않는다.
        self._preferred_model: str | None = None

        genai.configure(api_key=api_key)
        self.ui.on_text_command = self._on_text_command

        # Shared busy guard — ignore clap/wake while recording, processing,
        # speaking, or while vision audio is playing (mic would otherwise pick
        # up TTS / our own voice).
        _busy_guard = lambda: (self._busy.is_set()
                               or self._recording.is_set()
                               or _SPEAKING.is_set()
                               or _vision.PLAYING.is_set())

        self._clap = ClapListener(
            on_clap_detected=self._on_clap,
            is_busy=_busy_guard,
        )
        self._clap.start()

        # "Hey Jarvis" wake word (offline, no key — openWakeWord).
        self._wake = WakeListener(
            on_wake_detected=self._on_wake,
            is_busy=_busy_guard,
        )
        if self._wake.available():
            self._wake.start()
            self.ui.write_log('SYS: wake word 활성화 — 박수 또는 "Hey Jarvis"로 호출')
        else:
            self.ui.write_log("SYS: wake word 비활성화 (모델 로드 실패) — 박수로 호출")

    # Try current Gemini text models in order; skipped models fall through on 404.
    _MODEL_CANDIDATES = [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]

    def _get_model(self, system: str, model_name: str):
        return genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system,
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
        )

    def _is_model_not_found_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "404" in msg
            and "model" in msg
            and ("not found" in msg or "not supported" in msg)
        )

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def _on_text_command(self, text: str):
        threading.Thread(target=self._handle_input, args=(text,), daemon=True).start()

    def _on_clap(self):
        if self._recording.is_set():
            return
        self.ui.write_log("SYS: 박수 감지 — 마이크 활성화")
        threading.Thread(target=self._record_and_handle, daemon=True).start()

    def _on_wake(self):
        if self._recording.is_set():
            return
        self.ui.write_log('SYS: "Jarvis" 감지 — 마이크 활성화')
        threading.Thread(target=self._record_and_handle, daemon=True).start()

    def _record_and_handle(self):
        if self._recording.is_set():
            return
        self._recording.set()
        try:
            audio = _record_until_silence(self.ui)
            if audio:
                self.ui.set_state("THINKING")
                text = _transcribe(audio)
                if text:
                    self.ui.write_log(f"You (음성): {text}")
                    self._handle_input(text)
                else:
                    self.ui.write_log("SYS: 음성 인식 실패")
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")
        finally:
            self._recording.clear()

    # ------------------------------------------------------------------
    # 로컬 커맨드 라우터 — Gemini 호출 없이 바로 처리
    # ------------------------------------------------------------------

    # (패턴, tool명, args_builder)
    # 전체 문장이 패턴과 정확히 일치할 때만 라우팅 (fullmatch) — 긴 문장은 Gemini로.
    _LOCAL_ROUTES = [
        # 영어: 동사 + 앱 ("open chrome", "launch vscode")
        (r"(?:open|launch|start)\s+(.+)", "open_app",
         lambda m: {"app_name": m.group(1).strip()}),
        # 한국어: 앱 + 동사 ("크롬 열어줘", "VS Code 실행해 줘", "메모장 켜줘")
        (r"(.+?)\s*(?:열어|켜|띄워|실행\s*해|실행\s*시켜)\s*줘?", "open_app",
         lambda m: {"app_name": m.group(1).strip()}),
        # "날씨 서울" / "weather (in) seoul"
        (r"(?:날씨|weather(?:\s+in)?)\s+(.+)", "weather_report",
         lambda m: {"city": m.group(1).strip()}),
        # 한국어: 도시 + 날씨 ("서울 날씨", "부산 날씨 알려줘", "서울 날씨 어때")
        (r"(.+?)\s*(?:의\s*)?날씨\s*(?:는|좀)?\s*(?:알려\s*줘?|어때\??|보여\s*줘?)?", "weather_report",
         lambda m: {"city": m.group(1).strip()}),
    ]

    # 캡처된 인자가 이보다 길면 단순 명령이 아니라고 보고 Gemini로 넘긴다.
    _LOCAL_ARG_MAX_CHARS = 25
    _LOCAL_ARG_MAX_WORDS = 4

    def _try_local_route(self, text: str) -> bool:
        """로컬 라우터가 처리하면 True, 아니면 False."""
        import re
        for pattern, tool_name, args_fn in self._LOCAL_ROUTES:
            m = re.fullmatch(pattern, text.strip(), re.IGNORECASE)
            if not m:
                continue
            args = args_fn(m)
            arg  = next(iter(args.values()), "")
            if (not arg
                    or len(arg) > self._LOCAL_ARG_MAX_CHARS
                    or len(arg.split()) > self._LOCAL_ARG_MAX_WORDS):
                continue
            self.ui.write_log(f"[local] {tool_name} (Gemini 호출 없음)")
            self.ui.set_state("THINKING")
            result = self._execute_tool(tool_name, args)
            answer = f"Done: {result}" if result else "Done."
            self.ui.write_log(f"Jarvis: {answer}")
            threading.Thread(target=_tts.speak, args=(answer, self.ui), daemon=True).start()
            return True
        return False

    # ------------------------------------------------------------------
    # Core reasoning loop
    # ------------------------------------------------------------------

    def _handle_input(self, text: str):
        if not _GENAI_OK:
            self.ui.write_log("ERR: google-generativeai 없음. pip install google-generativeai")
            return

        self._busy.set()
        try:
            self._handle_input_inner(text)
        finally:
            self._busy.clear()

    def _handle_input_inner(self, text: str):
        # 단순 명령은 Gemini 쿼터 소비 없이 바로 처리
        if self._try_local_route(text):
            return

        self.ui.set_state("THINKING")

        from datetime import datetime
        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()
        now_str    = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")

        parts = [f"[현재 시각] {now_str}"]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)
        full_system = "\n\n".join(parts)

        # 성공했던 모델을 맨 앞에 — 매 요청마다 404 후보를 다시 돌지 않는다.
        candidates = list(self._MODEL_CANDIDATES)
        if self._preferred_model:
            candidates = [self._preferred_model] + [
                c for c in candidates if c != self._preferred_model
            ]

        last_error = None
        tried_discovery = False
        i = 0
        while i < len(candidates):
            model_name = candidates[i]
            i += 1
            try:
                self.ui.write_log(f"SYS: Gemini model - {model_name}")
                model = self._get_model(full_system, model_name)
                with self._lock:
                    history_snapshot = list(self._history)
                chat = model.start_chat(history=history_snapshot)
                self._run_completion_loop(chat, text)
                self._preferred_model = model_name
                return
            except Exception as e:
                last_error = e
                if self._is_model_not_found_error(e):
                    self.ui.write_log(f"SYS: Gemini model unavailable - {model_name}; trying next")
                    # 후보가 전부 404였으면 API 에서 사용 가능한 모델을 동적 탐색
                    if i == len(candidates) and not tried_discovery:
                        tried_discovery = True
                        dyn = self._discover_flash_model()
                        if dyn and dyn not in candidates:
                            self.ui.write_log(f"SYS: Gemini model auto-discovered - {dyn}")
                            candidates.append(dyn)
                    continue
                self.ui.write_log(f"ERR: {str(e)[:200]}")
                traceback.print_exc()
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                return

        if last_error is not None:
            self.ui.write_log(f"ERR: No Gemini model worked. Last error: {str(last_error)[:160]}")
            traceback.print_exception(type(last_error), last_error, last_error.__traceback__)
        if not self.ui.muted:
            self.ui.set_state("LISTENING")

    def _discover_flash_model(self) -> str | None:
        """list_models 로 generateContent 지원 모델을 찾아 flash 계열 우선 반환."""
        try:
            names = [
                m.name.removeprefix("models/")
                for m in genai.list_models()
                if "generateContent" in getattr(m, "supported_generation_methods", [])
            ]
            for name in names:
                low = name.lower()
                if "flash" in low and not any(
                    x in low for x in ("lite", "image", "tts", "audio", "live", "embedding")
                ):
                    return name
            return names[0] if names else None
        except Exception as e:
            print(f"[Gemini] list_models failed: {e}")
            return None

    def _send_with_retry(self, chat, payload, max_retries: int = 3):
        """429 quota exceeded 시 대기 후 재시도."""
        for attempt in range(max_retries):
            try:
                return chat.send_message(payload)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "quota" in msg.lower() or "RESOURCE_EXHAUSTED" in msg:
                    wait = 30 * (attempt + 1)  # 30s, 60s, 90s
                    self.ui.write_log(f"SYS: Gemini 쿼터 초과 — {wait}초 후 재시도 ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini quota exceeded after retries. 잠시 후 다시 시도하세요.")

    def _run_completion_loop(self, chat, user_message: str):
        response = self._send_with_retry(chat, user_message)

        max_iters = 6
        for _ in range(max_iters):
            # Gemini 응답에서 function_call 파트 추출
            fc_parts = []
            text_parts = []
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, "function_call") and part.function_call.name:
                        fc_parts.append(part.function_call)
                    elif hasattr(part, "text") and part.text:
                        text_parts.append(part.text)

            # 최종 텍스트 응답
            if not fc_parts:
                answer = " ".join(text_parts).strip()
                if answer:
                    with self._lock:
                        self._history = list(chat.history)
                    self.ui.write_log(f"Jarvis: {answer}")
                    threading.Thread(
                        target=_tts.speak, args=(answer, self.ui), daemon=True
                    ).start()
                else:
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")
                return

            # tool 실행 후 결과를 Gemini에 돌려보냄
            fn_responses = []
            for fc in fc_parts:
                result = self._execute_tool(fc.name, dict(fc.args))
                self.ui.write_log(f"[tool] {fc.name} → {str(result)[:100]}")
                fn_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": str(result)}
                        )
                    )
                )

            response = self._send_with_retry(chat, fn_responses)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, args: dict) -> str:
        print(f"[JARVIS] tool: {name}  args: {args}")
        self.ui.set_state("THINKING")

        try:
            if name == "start_dual_agent_task":
                return self._run_pipeline(args)

            elif name == "open_app":
                return open_app(parameters=args, player=self.ui) or "Done."

            elif name == "web_search":
                return web_search_action(parameters=args, player=self.ui) or "Done."

            elif name == "weather_report":
                return weather_action(parameters=args, player=self.ui) or "Done."

            elif name == "reminder":
                return reminder(parameters=args, player=self.ui) or "Reminder set."

            elif name == "screen_process":
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True,
                ).start()
                return "Screen capture started."

            elif name == "youtube_video" and _YT_OK:
                return _youtube(parameters=args, player=self.ui) or "Done."

            elif name == "save_memory":
                cat = args.get("category", "notes")
                key = args.get("key", "")
                val = args.get("value", "")
                if key and val:
                    update_memory({cat: {key: {"value": val}}})
                return "Saved."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown.")
                _tts.speak("Goodbye, sir.", self.ui)
                def _exit():
                    time.sleep(1.2)
                    os._exit(0)
                threading.Thread(target=_exit, daemon=True).start()
                return "Shutting down."

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            traceback.print_exc()
            return f"Tool error ({name}): {str(e)[:200]}"

    # ------------------------------------------------------------------
    # Dual-agent pipeline bridge
    # ------------------------------------------------------------------

    def _run_pipeline(self, args: dict) -> str:
        task         = args.get("task", "").strip()
        project_path = args.get("project_path", "").strip()

        if not task:
            return "Error: task is required."

        self.ui.write_log(f"SYS: dual-agent 파이프라인 시작 — {task[:80]}")
        self.ui.set_state("PROCESSING")

        cmd = [PYTHON_EXE, str(PIPELINE_SCRIPT),
               "--task", task,
               "--config", str(PIPELINE_CONFIG)]
        if project_path:
            cmd += ["--project", project_path]

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"

        # --- Popen: stream [pipeline] lines to UI in real-time ---------------
        # 한도는 파이프라인 자체 설정과 일치시킨다: 단계당 timeout × 단계 수 + 여유.
        # (이전의 30분 고정 한도는 단계당 30분을 허용하는 파이프라인 설정과 모순 —
        #  정상 진행 중인 긴 작업을 중간에 죽였음.)
        try:
            _pcfg = json.loads(PIPELINE_CONFIG.read_text(encoding="utf-8"))
            _step_timeout = int(_pcfg.get("execution", {}).get("timeout_seconds", 1800))
            _n_steps = len(_pcfg.get("steps", [])) or 7
        except Exception:
            _step_timeout, _n_steps = 1800, 7
        _PIPELINE_TIMEOUT = _step_timeout * _n_steps + 300

        stderr_lines: list[str] = []
        rc = -1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )

            # Watchdog thread: kill process if it hangs past the hard timeout.
            # Without this, a stalled claude/codex subprocess would block the
            # stdout-reading loop forever, keeping _busy set and locking
            # clap/voice input for the rest of the session.
            def _watchdog():
                self.ui.write_log(
                    f"PIPE: [timeout] {_PIPELINE_TIMEOUT//60}분 초과 — 프로세스 강제 종료"
                )
                try:
                    proc.kill()
                except Exception:
                    pass
            watchdog = threading.Timer(_PIPELINE_TIMEOUT, _watchdog)
            watchdog.start()

            try:
                # stderr reader thread — collects without blocking stdout loop
                def _read_stderr():
                    for line in proc.stderr:
                        stripped = line.rstrip()
                        if stripped:
                            stderr_lines.append(stripped)
                stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
                stderr_thread.start()

                # Stream stdout to UI line by line
                for raw_line in proc.stdout:
                    line = raw_line.rstrip()
                    if not line:
                        continue
                    # Keep [pipeline] prefix — it tells the user which agent/step
                    self.ui.write_log(f"PIPE: {line}")

                proc.stdout.close()
                stderr_thread.join(timeout=5)
                proc.stderr.close()
                rc = proc.wait(timeout=10)
            finally:
                watchdog.cancel()  # disarm if process finished normally

        except Exception as e:
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return f"Pipeline launch error: {e}"

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        if rc != 0:
            err_tail = "\n".join(stderr_lines[-4:]) if stderr_lines else "(stderr 없음)"
            return f"Pipeline failed (rc={rc}).\n{err_tail}"

        # final_report.md 읽어서 반환
        try:
            cfg      = json.loads(PIPELINE_CONFIG.read_text(encoding="utf-8"))
            proj_dir = project_path or os.path.expandvars(cfg.get("project_root", ""))
            ws_dir   = cfg.get("workspace_dir", ".project-ai")
            report   = Path(proj_dir) / ws_dir / "final_report.md"
            if report.exists():
                content = report.read_text(encoding="utf-8")
                return content[:3000] + ("\n…(생략)" if len(content) > 3000 else "")
        except Exception:
            pass

        return "Pipeline completed. Check .project-ai/final_report.md for details."


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not _GENAI_OK:
        print("[ERROR] google-generativeai 없음.")
        print("        py -3.11 -m pip install -r requirements.txt 실행 후 다시 시도하세요.")
        sys.exit(1)

    face_path = str(BASE_DIR / "mark_base" / "face.png")
    if not Path(face_path).exists():
        face_path = ""

    ui = JarvisUI(face_path)

    def _runner():
        ui.wait_for_api_key()
        try:
            api_key = _get_api_key()
        except Exception as e:
            ui.write_log(f"ERR: API 키 로딩 실패 — {e}")
            return

        jarvis = SafeJarvis(ui, api_key)
        ui.write_log("SYS: JARVIS online. 박수 두 번 또는 텍스트로 명령하세요.")
        _tts.speak("JARVIS online. Ready.", ui)

    threading.Thread(target=_runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
