# .project-ai — dual-agent 작업 워크스페이스

이 폴더는 Claude↔Codex 파이프라인이 단계별 산출물을 주고받는 공간입니다.

> 주의: **실제 작업 시에는 이 폴더가 "대상 프로젝트" 안에 생성**됩니다.
> (`config/agent_pipeline.json` 의 `project_root` 기준)
> safe_jarvis 안의 이 폴더는 구조 설명용 예시/스캐폴드입니다.

## 단계별 파일

| 파일 | 작성자 | 내용 |
|------|--------|------|
| `task.md` | 사용자/Jarvis | 작업 지시 |
| `claude_plan.md` | Claude | 구현 계획 |
| `codex_plan_review.md` | Codex | 계획 검토 |
| `claude_implementation_log.md` | Claude | 구현 내역 |
| `codex_code_review.md` | Codex | 코드 검수 |
| `claude_fix_log.md` | Claude | 수정 반영 내역 |
| `codex_final_check.md` | Codex | 최종 검수 (VERDICT) |
| `final_report.md` | Claude | 사용자 보고서 |
| `_runs/` | 오케스트레이터 | 각 단계 원시 실행 로그 |
