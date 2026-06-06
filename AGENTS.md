# Dual Agent Development Rules (safe_jarvis 파트 A)

이 문서는 Claude Code 와 Codex 가 이 저장소에서 **상호 검토형 개발 파이프라인**으로 동작할 때 따르는 규칙이다.

## 역할 분담 (Role Split)
- **Claude Code** = 계획 수립 · 코드 작성 · 수정 반영 담당.
- **Codex** = 계획 검토 · 코드 검수 · 빌드/테스트 확인 · 수정사항 제안 담당.
- Codex 는 **기본적으로 파일을 직접 수정하지 않는다** (read-only 샌드박스).
- Claude 는 Codex 의 검토를 **이유 없이 무시하지 않는다**. 미반영 시 이유를 기록한다.
- Claude 와 Codex 가 **동시에 같은 파일을 수정하지 않는다**.

## 파이프라인 (Pipeline)
1. Claude → `.project-ai/claude_plan.md`
2. Codex  → `.project-ai/codex_plan_review.md`
3. Claude → 구현 + `.project-ai/claude_implementation_log.md`
4. Codex  → `.project-ai/codex_code_review.md`
5. Claude → 수정 + `.project-ai/claude_fix_log.md`
6. Codex  → `.project-ai/codex_final_check.md` (VERDICT: PASS | PASS_WITH_WARNINGS | FAIL)
7. 최종 보고 → `.project-ai/final_report.md`

## 보안 (Safety) — 절대 규칙
- `.env`, `.env.local`, `api_keys.json`, `secrets.json`, `credentials.json` 등 비밀 파일을 읽거나 출력하지 않는다.
- 브라우저 비밀번호에 접근하지 않는다.
- 결제, 메일/메시지 전송, 계정 설정 변경을 하지 않는다.
- 사용자 파일을 대량 삭제하지 않는다. 디스크 포맷/종료/재시작을 하지 않는다.
- 허용된 프로젝트 폴더(`allowed_base_path` 하위) 밖에서 작업하지 않는다.
- **사용자 승인 없이 commit / push / deploy 하지 않는다.**

## 테스트 (Testing)
- 기존 프로젝트 스크립트를 우선 사용한다 (`package.json` 먼저 확인).
- 명령이 없으면 지어내지 말고 "없음"으로 기록한다.
- 명령 실행 결과는 해당 검수 파일에 기록한다.
