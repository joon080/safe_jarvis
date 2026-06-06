# safe_jarvis — 파트 A: Claude↔Codex Dual-Agent 파이프라인 (Windows)

박수/음성/텍스트 비서(파트 B)는 추후 추가. **현재 구현된 것은 파트 A — dual-agent 개발 파이프라인**이다.

Claude 가 계획·구현·수정하고, Codex 가 검토·테스트·수정제안하는 흐름을 비대화형으로 자동 실행한다.

## 1. 요구사항
- Windows 10/11, PowerShell
- Python 3.11+ (표준 라이브러리만 사용 — 추가 pip 설치 불필요)
- `claude` CLI (Claude Code) 로그인 완료
- `codex` CLI 로그인 완료

확인:
```powershell
py -3.11 --version   # Windows py 런처 (추천)
# 또는
python --version     # python 이 PATH 에 있을 때
claude --version
codex --version
```

## 2. 설정
`config/agent_pipeline.json` 을 연다.
- `project_root` : 작업할 대상 프로젝트 경로 (CLI `--project` 로 덮어쓸 수 있음)
- `allowed_base_path` : 이 경로 하위만 작업 허용 (보안 경계)
- `agents.claude.model` / `agents.codex.model` : 비우면 각 CLI 기본 모델 사용
- `execution.timeout_seconds` : 단계별 타임아웃
- `blocked_files` / `blocked_actions` / `claude_disallowed_tools` : 안전장치

## 3. 실행

```powershell
# 방법 1 — 배치 스크립트 (python PATH 없어도 됨, 추천)
run_pipeline.bat --task "로그인 버그 고쳐줘" --project "C:\Users\08seo\projects\AAU-niversity"

# 방법 2 — py 런처 직접 (python PATH 없어도 됨)
py -3.11 actions/dual_agent_pipeline.py --task "..." --project <대상경로>

# 방법 3 — python 이 PATH 에 있을 때
python actions/dual_agent_pipeline.py --task "..." --project <대상경로>
```

주요 옵션:
```powershell
# 토큰 소비 없이 명령/프롬프트 확인만 (파일 변경 없음, 먼저 이걸로 점검 추천)
run_pipeline.bat --task "..." --project <대상경로> --dry-run

# 단일 단계만
run_pipeline.bat --only claude_plan --project <대상경로>

# 중간 단계부터 재개
run_pipeline.bat --from-step codex_code_review --project <대상경로>

# 이전 산출물 초기화 후 재실행 (task.md 는 유지)
run_pipeline.bat --task "..." --project <대상경로> --reset
```

## 4. 결과물
대상 프로젝트의 `.project-ai/` 폴더에 단계별로 생성된다:
```
.project-ai/
├─ task.md                        # 작업 지시
├─ claude_plan.md                 # Claude 계획
├─ codex_plan_review.md           # Codex 계획검토
├─ claude_implementation_log.md   # Claude 구현내역
├─ codex_code_review.md           # Codex 코드검수
├─ claude_fix_log.md              # Claude 수정내역
├─ codex_final_check.md           # Codex 최종검수 (VERDICT)
├─ final_report.md                # 최종 보고서  ← 사용자는 보통 이것만 읽으면 됨
└─ _runs/                         # 각 단계 원시 실행 로그(stdout/stderr)
```

## 5. 동작 원리 (요약)
- 각 단계마다 `prompts/*.txt` 템플릿 + 입력 파일 내용을 합쳐 프롬프트를 만들고, stdin 으로 전달.
- **Claude**: `claude -p` 로 호출. read 단계는 편집 불가 / edit 단계는 `--permission-mode bypassPermissions` + 위험 도구 차단. stdout 을 산출 파일로 저장.
- **Codex**: `codex exec --cd <project> -s read-only -o <파일>` 로 호출. 최종검수만 `workspace-write`(빌드/테스트 실행용).
- 한 단계라도 실패하면 기본적으로 중단(`stop_on_failure`).

## 6. 안전 경계
- `project_root` 가 `allowed_base_path` 밖이면 즉시 차단.
- 비밀 파일 읽기/출력 금지, 삭제·포맷·종료 금지, commit/push/deploy 는 사용자 승인 전 금지.
- 자세한 규칙은 [`AGENTS.md`](AGENTS.md) 참고.

## 7. 다음 (파트 B)
`mark_base/`(FatihMakes/Mark-XXXIX 원본)의 UI·음성 구조를 안전모드로 가져와,
`start_dual_agent_task` 도구로 이 파이프라인을 음성/텍스트에서 호출하도록 연결할 예정.
