#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe_jarvis 파트 A — Claude <-> Codex dual-agent 개발 파이프라인 오케스트레이터.

흐름:
  task.md -> Claude(계획) -> Codex(계획검토) -> Claude(구현) -> Codex(코드검수)
          -> Claude(수정) -> Codex(최종검수) -> Claude(최종보고) -> final_report.md

역할:
  - Claude = 계획 / 구현 / 수정 (파일 직접 편집)
  - Codex  = 검토 / 테스트 / 수정제안 (기본 직접수정 X, read-only 샌드박스)

특징:
  - Python 표준 라이브러리만 사용 (pip 설치 불필요).
  - claude / codex 를 비대화형으로 호출하고, 각 단계 산출물을 .project-ai/*.md 로 저장.
  - 모든 단계는 파일 기반으로 주고받는다.
  - 보안: 프로젝트 폴더 밖 금지, 비밀파일 차단, 커밋/푸시/배포는 사용자 승인 전 금지.

사용 예 (PowerShell):
  python actions/dual_agent_pipeline.py --task "로그인 버그를 고쳐줘" --project "C:\\projects\\my-project"
  python actions/dual_agent_pipeline.py --task-file task.txt --project <경로>
  python actions/dual_agent_pipeline.py --only claude_plan          # 단일 단계만
  python actions/dual_agent_pipeline.py --from-step codex_code_review  # 중간부터 재개
  python actions/dual_agent_pipeline.py --task "..." --project <경로> --dry-run   # 명령만 출력(토큰 소비 없음)
"""

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 경로 / 상수
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    # actions/ 의 부모 = 프로젝트(safe_jarvis) 루트
    return Path(__file__).resolve().parent.parent

BASE_DIR = _base_dir()
DEFAULT_CONFIG = BASE_DIR / "config" / "agent_pipeline.json"

# Windows 콘솔(cp1252 등)에서도 한글/이모지 출력이 깨지지 않도록 UTF-8 강제
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_file() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 설정 로딩 / 경로 검증
# ---------------------------------------------------------------------------

def _expand(value: str) -> str:
    """환경변수(%USERNAME% 등)를 확장한다."""
    return os.path.expandvars(value)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def resolve_project_root(cfg: dict, cli_project: str | None) -> Path:
    raw = cli_project if cli_project else cfg.get("project_root", "")
    root = Path(_expand(raw)).resolve()
    allowed_raw = cfg.get("allowed_base_path", "")
    if allowed_raw:
        allowed = Path(_expand(allowed_raw)).resolve()
        try:
            root.relative_to(allowed)
        except ValueError:
            raise SystemExit(
                f"[보안 차단] project_root 가 허용 경로 밖입니다.\n"
                f"  project_root : {root}\n"
                f"  allowed_base : {allowed}\n"
                f"  config 의 allowed_base_path 를 바꾸거나 허용 경로 내부를 지정하세요."
            )
    if not root.exists():
        raise SystemExit(f"[오류] 대상 프로젝트 경로가 존재하지 않습니다: {root}")
    return root


# ---------------------------------------------------------------------------
# 명령 빌드
# ---------------------------------------------------------------------------

def _resolve_executable(name: str) -> str:
    found = shutil.which(name)
    return found if found else name


def _fill(args: list[str], mapping: dict[str, str]) -> list[str]:
    out = []
    for a in args:
        for k, v in mapping.items():
            a = a.replace("{" + k + "}", v)
        out.append(a)
    return out


def build_command(step: dict, cfg: dict, project_root: Path, output_path: Path) -> list[str]:
    agent = step["agent"]
    mode = step["mode"]
    agent_cfg = cfg["agents"][agent]
    mapping = {
        "project_root": str(project_root),
        "output_path": str(output_path),
        "workspace": str(output_path.parent),
    }

    exe = _resolve_executable(agent_cfg["command"])
    args = _fill(list(agent_cfg[f"{mode}_args"]), mapping)
    cmd = [exe] + args

    # 모델 옵션 (설정돼 있을 때만)
    model = (agent_cfg.get("model") or "").strip()
    if model:
        if agent == "claude":
            cmd += ["--model", model]
        elif agent == "codex":
            cmd += ["-m", model]

    # Claude edit 모드: 위험 도구 차단
    if agent == "claude" and mode == "edit":
        disallowed = cfg.get("claude_disallowed_tools") or []
        if disallowed:
            cmd += ["--disallowedTools", ",".join(disallowed)]

    return cmd


# ---------------------------------------------------------------------------
# 컨텍스트(입력 파일) 구성
# ---------------------------------------------------------------------------

def build_prompt(step: dict, cfg: dict, project_root: Path, workspace: Path) -> str:
    template_path = BASE_DIR / step["prompt"]
    template = template_path.read_text(encoding="utf-8")

    blocks = [template.strip(), "", "=== 작업 컨텍스트 (오케스트레이터 자동 첨부) ==="]
    blocks.append(f"PROJECT_ROOT: {project_root}")
    blocks.append(f"WORKSPACE_DIR: {workspace}")
    blocks.append("")
    blocks.append("아래는 이번 단계의 입력 파일들의 현재 내용이다:")
    blocks.append("")

    for fname in step.get("inputs", []):
        fpath = workspace / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8", errors="replace")
        else:
            content = "(파일 없음 — 아직 생성되지 않음)"
        blocks.append(f"----- FILE: {cfg['workspace_dir']}/{fname} -----")
        blocks.append(content.rstrip())
        blocks.append("")

    blocks.append(
        f"위 지시에 따라 작업하고, 결과 markdown 을 응답으로 출력하라. "
        f"이 출력은 오케스트레이터가 {cfg['workspace_dir']}/{step['output']} 에 저장한다."
    )
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 단계 실행
# ---------------------------------------------------------------------------

def run_step(step: dict, cfg: dict, project_root: Path, workspace: Path,
             runs_dir: Path, timeout: int, dry_run: bool) -> bool:
    sid = step["id"]
    agent = step["agent"]
    output_path = workspace / step["output"]
    cmd = build_command(step, cfg, project_root, output_path)
    prompt = build_prompt(step, cfg, project_root, workspace)

    log(f"── 단계: {sid}  (agent={agent}, mode={step['mode']})")
    log(f"   명령: {' '.join(cmd)}")

    if dry_run:
        preview = prompt if len(prompt) < 600 else prompt[:600] + " …(생략)"
        log(f"   [dry-run] 프롬프트 미리보기:\n{preview}\n")
        return True

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"

    started = _ts()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"   ✗ 타임아웃 ({timeout}s) — {sid}")
        _write_run_log(runs_dir, sid, started, cmd, "", "TIMEOUT", -1)
        return False
    except FileNotFoundError as e:
        log(f"   ✗ 실행 파일을 찾을 수 없음: {e}")
        return False

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    rc = proc.returncode

    # 산출물 저장
    #  - claude: stdout 을 산출 파일로 저장
    #  - codex : -o 로 codex 가 직접 파일을 씀. 비어 있으면 stdout 으로 보강.
    if agent == "claude":
        output_path.write_text(stdout.strip() + "\n", encoding="utf-8")
    else:  # codex
        if (not output_path.exists()) or output_path.stat().st_size == 0:
            output_path.write_text(stdout.strip() + "\n", encoding="utf-8")

    _write_run_log(runs_dir, sid, started, cmd, stdout, stderr, rc)

    if rc != 0:
        log(f"   ✗ 종료코드 {rc} — {sid}  (stderr 일부: {stderr.strip()[:160]})")
        return False

    log(f"   ✓ 완료 → {cfg['workspace_dir']}/{step['output']}")
    return True


def _write_run_log(runs_dir: Path, sid: str, started: str, cmd: list[str],
                   stdout: str, stderr: str, rc) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"{_ts_file()}_{sid}.log"
    parts = [
        f"step      : {sid}",
        f"started   : {started}",
        f"finished  : {_ts()}",
        f"returncode: {rc}",
        f"command   : {' '.join(cmd)}",
        "",
        "===== STDOUT =====",
        stdout,
        "",
        "===== STDERR =====",
        stderr,
    ]
    p.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# 워크스페이스 초기화
# ---------------------------------------------------------------------------

WORKSPACE_FILES = [
    "task.md",
    "claude_plan.md",
    "codex_plan_review.md",
    "claude_implementation_log.md",
    "codex_code_review.md",
    "claude_fix_log.md",
    "codex_final_check.md",
    "final_report.md",
]


def init_workspace(workspace: Path, task_text: str | None, reset: bool) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "_runs").mkdir(parents=True, exist_ok=True)

    if reset:
        for f in WORKSPACE_FILES:
            fp = workspace / f
            if fp.exists() and f != "task.md":
                fp.write_text("", encoding="utf-8")

    if task_text is not None:
        header = f"# Task\n\n작성 시각: {_ts()}\n\n---\n\n"
        (workspace / "task.md").write_text(header + task_text.strip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Claude<->Codex dual-agent 개발 파이프라인 (safe_jarvis 파트 A)"
    )
    ap.add_argument("--task", help="개발 작업 지시 텍스트")
    ap.add_argument("--task-file", help="작업 지시를 담은 파일 경로")
    ap.add_argument("--project", help="대상 프로젝트 경로 (config 의 project_root 덮어씀)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="설정 파일 경로")
    ap.add_argument("--only", help="이 단계 하나만 실행 (step id)")
    ap.add_argument("--from-step", help="이 단계부터 끝까지 실행 (step id)")
    ap.add_argument("--reset", action="store_true", help="task.md 제외 산출물 초기화 후 실행")
    ap.add_argument("--dry-run", action="store_true", help="명령/프롬프트만 출력, 실제 호출 안 함")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    project_root = resolve_project_root(cfg, args.project)
    workspace = project_root / cfg.get("workspace_dir", ".project-ai")
    runs_dir = workspace / "_runs"

    # task 텍스트 확보
    task_text = None
    if args.task_file:
        task_text = Path(args.task_file).read_text(encoding="utf-8")
    elif args.task:
        task_text = args.task

    # --only / --from-step 이 아니고 task 도 없으면 기존 task.md 사용
    full_run = not (args.only or args.from_step)
    if full_run and task_text is None and not (workspace / "task.md").exists():
        raise SystemExit("[오류] --task 또는 --task-file 로 작업 지시를 주세요.")

    # dry-run 은 파일을 건드리지 않는다 (명령/프롬프트 미리보기만)
    if not args.dry_run:
        init_workspace(workspace, task_text, reset=args.reset)

    log(f"대상 프로젝트 : {project_root}")
    log(f"워크스페이스  : {workspace}")
    if args.dry_run:
        log("모드: DRY-RUN (파일 변경 없음, 실제 호출 없음)")

    # 실행할 단계 선택
    steps = cfg["steps"]
    if args.only:
        steps = [s for s in steps if s["id"] == args.only]
        if not steps:
            raise SystemExit(f"[오류] --only 단계 '{args.only}' 를 찾을 수 없습니다.")
    elif args.from_step:
        ids = [s["id"] for s in steps]
        if args.from_step not in ids:
            raise SystemExit(f"[오류] --from-step '{args.from_step}' 를 찾을 수 없습니다.")
        idx = ids.index(args.from_step)
        steps = steps[idx:]

    execu = cfg.get("execution", {})
    timeout = int(execu.get("timeout_seconds", 1800))
    stop_on_failure = bool(execu.get("stop_on_failure", True))

    results = []
    for step in steps:
        ok = run_step(step, cfg, project_root, workspace, runs_dir, timeout, args.dry_run)
        results.append((step["id"], ok))
        if not ok and stop_on_failure and not args.dry_run:
            log(f"중단: '{step['id']}' 실패, stop_on_failure=true")
            break

    # 요약
    log("──────────── 요약 ────────────")
    for sid, ok in results:
        log(f"   {'✓' if ok else '✗'}  {sid}")
    final = workspace / "final_report.md"
    if final.exists() and final.stat().st_size > 0 and not args.dry_run:
        log(f"최종 보고서: {final}")

    all_ok = all(ok for _, ok in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
