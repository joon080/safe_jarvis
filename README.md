# safe_jarvis

> Clap twice, speak, or type — JARVIS orchestrates **Claude Code + Codex** to plan, implement, and review code, all on your Windows machine.

safe_jarvis is a Windows AI development assistant with two parts:

- **Part A — Dual-Agent Pipeline**: Claude plans & implements code; Codex reviews & suggests fixes. They pass markdown files back and forth automatically.
- **Part B — JARVIS Voice Assistant**: A PyQt6 UI with double-clap trigger, voice input (Google STT), and text input. The Gemini-powered brain decides which tool to call, including the pipeline above.

---

## Features

| | |
|---|---|
| 🎙 **3 input modes** | Double-clap, voice, or text |
| 🤖 **Dual-agent review** | Claude implements → Codex reviews → Claude fixes → Codex final check |
| 🧠 **Gemini brain** | Flash-model auto-selection (candidate list → 404 fallback → runtime `list_models` discovery) routes commands to the right tool |
| 🔒 **Safe mode** | No file deletion, no browser hijack, no silent commits/pushes |
| 📋 **Full audit trail** | Every step saved to `.project-ai/*.md` + raw run logs |

---

## Architecture

```
User (clap / voice / text)
        │
        ▼
  JARVIS UI (PyQt6)
        │
        ▼
  Gemini (auto-selected flash model) ── tool calls ──► open_app / web_search / weather / reminder / ...
        │
        └──► start_dual_agent_task
                    │
                    ▼
        dual_agent_pipeline.py  (orchestrator)
          ┌──────────────────────────────────┐
          │  1. claude -p  →  claude_plan.md  │
          │  2. codex exec → codex_plan_review.md │
          │  3. claude -p  →  implementation  │
          │  4. codex exec → code_review.md   │
          │  5. claude -p  →  fix             │
          │  6. codex exec → final_check.md   │
          │  7. claude -p  →  final_report.md │
          └──────────────────────────────────┘
```

---

## Requirements

- **Windows 10/11**
- **Python 3.11+** — [python.org](https://www.python.org/downloads/)
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code` → `claude login`
- **Codex CLI** — `npm install -g @openai/codex` → login
- **Node.js 18+** (for the CLIs above)
- **Gemini API key** — [aistudio.google.com](https://aistudio.google.com/) (free tier works)

Verify:
```powershell
py -3.11 --version
claude --version
codex --version
```

---

## Quick Start

### Part A — Pipeline only (no voice UI)

No extra `pip install` needed — uses Python standard library only.

```powershell
# 1. Clone
git clone https://github.com/joon080/safe_jarvis.git
cd safe_jarvis

# 2. Set your project path in config/agent_pipeline.json
#    "project_root": "C:\\projects\\my-project"
#    "allowed_base_path": "C:\\projects"

# 3. Run (dry-run first to verify prompts without spending tokens)
./run_pipeline.bat --task "Add a dark mode toggle" --project "C:\projects\my-project" --dry-run

# 4. Real run
./run_pipeline.bat --task "Add a dark mode toggle" --project "C:\projects\my-project"
```

Results appear in `<your-project>\.project-ai\`:
```
.project-ai/
├─ claude_plan.md               # Claude's plan
├─ codex_plan_review.md         # Codex review of plan
├─ claude_implementation_log.md # What Claude implemented
├─ codex_code_review.md         # Codex code review
├─ claude_fix_log.md            # Claude's fixes
├─ codex_final_check.md         # Final verdict: PASS / PASS_WITH_WARNINGS / FAIL
├─ final_report.md              ← Start here for a summary
└─ _runs/                       # Raw stdout/stderr logs per step
```

### Part B — Full JARVIS Assistant

```powershell
# 1. Install Python dependencies
py -3.11 -m pip install -r requirements.txt

# 2. Copy and fill in your API key
copy config\api_keys.json.example config\api_keys.json
# Edit config\api_keys.json → set your Gemini API key

# 3. Launch
./start_jarvis.bat
```

The UI will start. You can:
- **Type** a command in the text box and press Enter
- **Clap twice** to activate the microphone, then speak
- Say `"Fix the login bug in my project"` → pipeline runs automatically

---

## Pipeline CLI Options

```powershell
# Single step only
./run_pipeline.bat --only claude_plan --project "C:\projects\my-project"

# Resume from a specific step
./run_pipeline.bat --from-step codex_code_review --project "C:\projects\my-project"

# Reset intermediate files and re-run from scratch
./run_pipeline.bat --task "..." --project "C:\projects\my-project" --reset
```

---

## Safety Boundaries

safe_jarvis deliberately removes dangerous capabilities:

| Blocked | Reason |
|---|---|
| File delete / mass write | Prevents accidental data loss |
| `computer_control` (mouse/keyboard) | Prevents UI hijacking |
| `browser_control` (logged-in browser) | Prevents credential theft |
| `send_message` / payments | Prevents unwanted actions |
| `git commit` / `push` / deploy | Requires explicit user approval |
| Reading `.env`, `api_keys.json`, etc. | Prevents secret leakage |

The pipeline also enforces `allowed_base_path` — any path outside that folder is blocked immediately.

> **Enforcement levels** (be honest about what is a hard block vs. an instruction):
> - **Orchestrator-enforced (hard):** `allowed_base_path` — out-of-bounds project paths are rejected before anything runs.
> - **Sandbox-enforced (hard):** Codex review steps run `codex exec -s read-only`; only the final check gets `workspace-write` (to run build/tests).
> - **Deny-rules + prompt (soft):** Claude edit steps run with `--permission-mode bypassPermissions`. Destructive commands (`rm`, `Remove-Item`, `git push`, …) are blocked via `--disallowedTools` deny rules, and `blocked_files` / `blocked_actions` from the config are injected into every step prompt — but this is not an OS-level sandbox.

See [`AGENTS.md`](AGENTS.md) for the full rule set.

---

## Configuration

`config/agent_pipeline.json`:

```jsonc
{
  "project_root": "C:\\projects\\my-project",    // default project
  "allowed_base_path": "C:\\projects",           // security boundary
  "agents": {
    "claude": { "model": "" },   // blank = CLI default model
    "codex":  { "model": "" }
  },
  "execution": {
    "stop_on_failure": true,
    "timeout_seconds": 1800,
    "require_user_approval_before_commit": true
  }
}
```

---

## Based On

- [FatihMakes/Mark-XXXIX](https://github.com/FatihMakes/Mark-XXXIX) — original voice assistant UI (Gemini Live API version). safe_jarvis replaces the Gemini Live core with a listen-then-respond pattern, switches to an auto-selected Gemini flash model + Google STT, removes dangerous tools, and adds the dual-agent pipeline.

---

## License

MIT
