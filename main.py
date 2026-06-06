#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe_jarvis 파트 B — J.A.R.V.I.S 음성/텍스트 비서 (OpenAI 기반)

입력 3종:
  1. 박수 두 번  → 마이크 활성화 → Whisper STT → GPT-4o
  2. 텍스트 입력 (UI)
  3. Voice 버튼 클릭 (UI) → 마이크 활성화 → Whisper STT

흐름:
  입력 → GPT-4o(function calling) → tool 실행 → TTS 응답 → UI 로그

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

# Python 실행 파일 (번들 Python 우선, 없으면 py 런처, 없으면 python)
def _find_python() -> str:
    candidates = [
        r"C:\Users\08seo\AppData\Local\Programs\Python\Python311\python.exe",
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
# imports — soft so startup doesn't crash if optional deps missing
# ---------------------------------------------------------------------------
try:
    import openai as _openai
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False

try:
    import sounddevice as sd
    import numpy as np
    _SD_OK = True
except ImportError:
    _SD_OK = False

try:
    import pyttsx3 as _pyttsx3
    _TTS_OK = True
except ImportError:
    _TTS_OK = False

from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, format_memory_for_prompt
from actions.open_app       import open_app
from actions.web_search     import web_search as web_search_action
from actions.weather_report import weather_action
from actions.reminder       import reminder
from actions.screen_processor import screen_process

try:
    from actions.youtube_video import youtube_video as _youtube
    _YT_OK = True
except ImportError:
    _YT_OK = False

from listener.clap_listener import ClapListener


# ---------------------------------------------------------------------------
# API key + system prompt
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["openai_api_key"]


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

class _TTS:
    def __init__(self):
        self._engine = None
        self._lock   = threading.Lock()
        if _TTS_OK:
            try:
                self._engine = _pyttsx3.init()
                self._engine.setProperty("rate", 175)
                # Windows SAPI — prefer a male voice
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
        try:
            if self._engine:
                with self._lock:
                    self._engine.say(text)
                    self._engine.runAndWait()
            else:
                # Windows SAPI via PowerShell fallback
                ps_cmd = (
                    f"Add-Type -AssemblyName System.Speech; "
                    f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.Speak([System.Text.RegularExpressions.Regex]::Replace("
                    f"'{text.replace(chr(39),'')}','[^\\x20-\\x7E]',''))"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    timeout=30, capture_output=True
                )
        except Exception as e:
            print(f"[TTS] error: {e}")
        finally:
            if ui and not ui.muted:
                ui.set_state("LISTENING")


_tts = _TTS()


# ---------------------------------------------------------------------------
# Audio recording (push-to-talk activated by clap or UI button)
# ---------------------------------------------------------------------------

_SAMPLE_RATE   = 16000
_SILENCE_THOLD = 0.008   # RMS threshold for speech detection
_SILENCE_AFTER = 1.8     # seconds of silence to end recording
_MAX_RECORD    = 30.0    # hard cap


def _record_until_silence(ui) -> bytes | None:
    if not _SD_OK:
        return None

    ui.set_state("LISTENING")
    ui.write_log("SYS: 말씀하세요... (silence 후 자동 종료)")

    chunks: list[bytes] = []
    last_speech   = time.monotonic()
    started_speech = False

    def callback(indata, frames, time_info, status):
        nonlocal last_speech, started_speech
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)) / 32768.0)
        chunks.append(indata.tobytes())
        if rms > _SILENCE_THOLD:
            last_speech   = time.monotonic()
            started_speech = True

    try:
        with sd.InputStream(samplerate=_SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=512, callback=callback):
            start = time.monotonic()
            while True:
                time.sleep(0.05)
                elapsed = time.monotonic() - start
                if elapsed > _MAX_RECORD:
                    break
                if started_speech and (time.monotonic() - last_speech) > _SILENCE_AFTER:
                    break
    except Exception as e:
        ui.write_log(f"SYS: 마이크 오류 — {e}")
        return None

    if not started_speech or not chunks:
        return None
    return b"".join(chunks)


def _transcribe(audio_bytes: bytes, api_key: str) -> str | None:
    if not _OPENAI_OK:
        return None
    try:
        client = _openai.OpenAI(api_key=api_key)
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
            raw_path = f.name
            f.write(audio_bytes)

        # convert raw PCM → wav via sounddevice/scipy or just send as-is
        # OpenAI Whisper accepts .wav; build minimal WAV header
        wav_path = raw_path + ".wav"
        _write_wav(audio_bytes, wav_path)

        with open(wav_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ko",
            )
        return result.text.strip()
    except Exception as e:
        print(f"[Whisper] error: {e}")
        return None
    finally:
        try:
            os.unlink(raw_path)
            os.unlink(wav_path)
        except Exception:
            pass


def _write_wav(pcm: bytes, path: str):
    import struct
    num_channels   = 1
    sample_rate    = _SAMPLE_RATE
    bits_per_sample = 16
    byte_rate      = sample_rate * num_channels * bits_per_sample // 8
    block_align    = num_channels * bits_per_sample // 8
    data_size      = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, num_channels, sample_rate,
        byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    with open(path, "wb") as f:
        f.write(header + pcm)


# ---------------------------------------------------------------------------
# Tool declarations (safe subset)
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "type": "function",
        "function": {
            "name": "start_dual_agent_task",
            "description": (
                "Runs the Claude+Codex dual-agent development pipeline. "
                "Use when the user asks to implement, fix, review, refactor, or debug code. "
                "Also triggers on: 'dual agent mode', 'Claude랑 Codex로', '상호 검토', "
                "'파이프라인으로 작업해'. "
                "Claude plans and implements. Codex reviews and suggests fixes. "
                "Final report is saved to .project-ai/final_report.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The development task description"
                    },
                    "project_path": {
                        "type": "string",
                        "description": "Target project path. Leave empty to use config default."
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Opens any application on the computer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "Application name"}
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Searches the web for information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode":  {"type": "string", "description": "search or compare"},
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather_report",
            "description": "Gets weather report for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reminder",
            "description": "Sets a timed reminder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":    {"type": "string", "description": "YYYY-MM-DD"},
                    "time":    {"type": "string", "description": "HH:MM (24h)"},
                    "message": {"type": "string"}
                },
                "required": ["date", "time", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_process",
            "description": "Captures and analyzes the screen or webcam.",
            "parameters": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string", "description": "screen or camera"},
                    "text":  {"type": "string", "description": "Question about the image"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save a personal fact about the user to long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "key":      {"type": "string"},
                    "value":    {"type": "string"}
                },
                "required": ["category", "key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "shutdown_jarvis",
            "description": "Shuts down the assistant when the user says goodbye.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]

if _YT_OK:
    TOOL_DECLARATIONS.append({
        "type": "function",
        "function": {
            "name": "youtube_video",
            "description": "Controls YouTube: play, summarize, trending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "play|summarize|get_info|trending"},
                    "query":  {"type": "string"},
                    "url":    {"type": "string"},
                },
                "required": []
            }
        }
    })


# ---------------------------------------------------------------------------
# Main Jarvis class
# ---------------------------------------------------------------------------

class SafeJarvis:

    def __init__(self, ui: JarvisUI, api_key: str):
        self.ui      = ui
        self.api_key = api_key
        self._client = _openai.OpenAI(api_key=api_key) if _OPENAI_OK else None
        self._messages: list[dict] = []
        self._recording = threading.Event()
        self._lock      = threading.Lock()

        # text command callback → thread-safe entry point
        self.ui.on_text_command = self._on_text_command

        # clap listener → start recording
        self._clap = ClapListener(on_clap_detected=self._on_clap)
        self._clap.start()

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def _on_text_command(self, text: str):
        threading.Thread(target=self._handle_input, args=(text,), daemon=True).start()

    def _on_clap(self):
        if self._recording.is_set():
            return  # already recording
        self.ui.write_log("SYS: 박수 감지 — 마이크 활성화")
        self.ui.set_state("LISTENING")
        threading.Thread(target=self._record_and_handle, daemon=True).start()

    def _record_and_handle(self):
        if self._recording.is_set():
            return
        self._recording.set()
        try:
            audio = _record_until_silence(self.ui)
            if audio:
                self.ui.set_state("THINKING")
                text = _transcribe(audio, self.api_key)
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
    # Core reasoning loop
    # ------------------------------------------------------------------

    def _handle_input(self, text: str):
        if not self._client:
            self.ui.write_log("ERR: openai SDK 없음. pip install openai 실행 필요.")
            return

        self.ui.set_state("THINKING")

        from datetime import datetime
        memory    = load_memory()
        mem_str   = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()
        now_str   = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")

        parts = [f"[현재 시각] {now_str}"]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)
        full_system = "\n\n".join(parts)

        with self._lock:
            self._messages.append({"role": "user", "content": text})
            messages_snapshot = [
                {"role": "system", "content": full_system}
            ] + list(self._messages)

        try:
            self._run_completion_loop(messages_snapshot)
        except Exception as e:
            err = f"ERR: {str(e)[:200]}"
            self.ui.write_log(err)
            traceback.print_exc()
            if not self.ui.muted:
                self.ui.set_state("LISTENING")

    def _run_completion_loop(self, messages: list[dict]):
        max_iters = 6
        for _ in range(max_iters):
            resp = self._client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=TOOL_DECLARATIONS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message

            # no tool call → final answer
            if not msg.tool_calls:
                text = (msg.content or "").strip()
                if text:
                    with self._lock:
                        self._messages.append({"role": "assistant", "content": text})
                    self.ui.write_log(f"Jarvis: {text}")
                    threading.Thread(
                        target=_tts.speak, args=(text, self.ui), daemon=True
                    ).start()
                else:
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")
                return

            # execute tool calls
            messages.append(msg)
            tool_results = []
            for tc in msg.tool_calls:
                result = self._execute_tool(tc.function.name, tc.function.arguments)
                tool_results.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      str(result),
                })
                self.ui.write_log(f"[tool] {tc.function.name} → {str(result)[:100]}")

            messages.extend(tool_results)

    # ------------------------------------------------------------------
    # Tool execution (safe subset only)
    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, args_json: str) -> str:
        try:
            args = json.loads(args_json or "{}")
        except Exception:
            args = {}

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

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.ui.set_state("LISTENING")
            return "Pipeline timeout (30 min)."
        except Exception as e:
            self.ui.set_state("LISTENING")
            return f"Pipeline launch error: {e}"

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        if result.returncode != 0:
            last_lines = "\n".join(result.stdout.strip().splitlines()[-6:])
            return f"Pipeline failed (rc={result.returncode}).\n{last_lines}"

        # Try to read final_report.md from the project's .project-ai/
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
    if not _OPENAI_OK:
        print("[ERROR] openai SDK 없음. 설치: pip install openai")
        print("        파트 B(UI/음성) 실행 전에 requirements.txt 를 설치하세요.")
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
