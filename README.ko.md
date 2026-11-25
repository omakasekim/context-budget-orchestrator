<p align="center">
  <a href="README.md">English</a> | <strong>한국어</strong> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a>
</p>

# context-budget-orchestrator

[![Version 3.0](https://img.shields.io/badge/Version-3.0%20Universal-06B6D4?style=for-the-badge)](scripts/budget_engine.py)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B%20Stdlib%20Only-3B82F6?style=for-the-badge)](scripts/budget_engine.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=for-the-badge)](LICENSE)

**Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub Copilot CLI, OpenClaw, OpenCode, DeepSeek Harness(`dsh`), xAI Grok CLI** 전반에서 몇 분에서 수 시간까지 이어지는 복잡한 엔지니어링·리서치 프로젝트를 안정적으로 완수하기 위한 **무의존성(Zero-Dependency) 범용 AI 오케스트레이터 스킬**입니다.

## 핵심 기능 6가지 (불필요한 비대화 없음)

1. **주제 리서치 및 정보 구조화 (`memory` + `record-decision`)** — 조사 결과, 출처, 태그, 아키텍처 결정(ADR), 실패한 시도(Dead End)를 타입이 지정된 컨텍스트 그래프(`.agent_state/context_graph.json`)에 구조화하여 저장합니다.
2. **안전한 코드 작성 및 검증 실행 (`exec-task` + `compress-output`)** — 빌드/테스트 명령을 실행하고 장황한 로그를 자동으로 70~95% 압축해 컨텍스트 오염을 방지하며 실행 영수증을 그래프에 기록합니다.
3. **전문 AI 서브에이전트 위임 (`delegate` + `lease`)** — `researcher`, `architect`, `implementer`, `reviewer`, `verifier` 역할별로 토큰 예산에 맞게 가지치기된 작업 브리프와 파일 충돌 방지 잠금(TTL Lease), 8개 하네스별 실행 명령을 자동 생성합니다.
4. **장기 프로젝트용 그래프 메모리 (`context_graph.json` + `query-graph` + `resume-context`)** — 단순 선형 Markdown 요약 대신 의존성 그래프를 역추적하여 다음 작업에 꼭 필요한 상위 노드·파일·결정사항만 `--token-budget` 한도 내로 복원하고 Git 드리프트까지 감지합니다.
5. **대규모 스킬 라이브러리 지능형 라우팅 (`discover-skills`)** — 설치된 모든 CLI 스킬 디렉터리에서 YAML 프론트매터(`name`, `description`)만 읽어 컨텍스트 낭비 없이 현재 작업에 맞는 1~2개의 스킬을 즉시 추천합니다.
6. **선제적 컨텍스트 예산 게이트 (`inspect-session` + `auto-plan` + `gate-check` + `verify-handoff`)** — JSONL 세션 로그에서 프로바이더의 실제 토큰 사용량을 추출하고, 반복 수정 루프·로그 비대화를 경고하며, 68% 안전 상한선과 핸드오프 전 구문/테스트 무결성을 강제합니다.
7. **내장 보안·버그 감사 엔진 (`audit`)** — 14개 핵심 CWE 취약점/버그 클래스 정적 스캔(`scan`), 승인 범위 가드(`init-scope`), 7-질문 트리아지 게이트(`triage`), 시크릿·PII 자동 마스킹(`redact`), 원클릭 수정 태스크 자동 생성(`--create-tasks`) 및 Windows PowerShell 설치(`install.ps1`)를 지원합니다.

## 지원 AI 하네스

| 하네스 | 글로벌 설치 경로 (`install.sh`) | 지원 모델 및 연동 |
| --- | --- | --- |
| **Claude Code** | `~/.claude/skills/context-budget-orchestrator` | JSONL `usage`, `PreToolUse` 훅, `claude -p` |
| **Gemini CLI** | `~/.gemini/skills/context-budget-orchestrator` | `usageMetadata`, `BeforeTool` 훅, `gemini -p` |
| **OpenAI Codex CLI** | `~/.agents/skills/context-budget-orchestrator` | `total_tokens`, `AGENTS.md`, `codex exec` |
| **GitHub Copilot CLI** | `~/.copilot/skills/context-budget-orchestrator` | `/skills reload`, `.github/skills/`, `gh copilot -p` |
| **OpenCode** | `~/.config/opencode/skills/context-budget-orchestrator` | `provider/model` 접두사 지원, `opencode run` |
| **OpenClaw** | `~/.openclaw/skills/context-budget-orchestrator` | `~/.openclaw/sessions`, `openclaw agent run` |
| **DeepSeek Harness (`dsh`)** | `~/.agents/skills/context-budget-orchestrator` | `deepseek-v4` (1M), `v3`/`r1`, `dsh run` |
| **xAI Grok CLI** | `~/.agents/skills/context-budget-orchestrator` | `grok-4` (256K), `grok-code-fast`, `grok -p` |

## 1-명령 설치 및 진단

```bash
git clone https://github.com/Aetherx86/context-budget-orchestrator.git
cd context-budget-orchestrator
bash install.sh
```

```bash
bash install.sh --doctor      # Python 3.9+, 6개 하네스 설치 상태 및 전체 테스트 점검
bash install.sh --project .   # 현재 저장소의 .agents/skills/ 및 .github/skills/에 설치
bash install.sh --copy        # 심볼릭 링크 대신 파일 복사 모드로 설치
bash install.sh --uninstall   # 모든 글로벌 설치 경로 정리
```

## 새 세션 재개 방법

핸드오프 이후 새 세션을 열고 **`resume worklog`** 라고 말하면 에이전트가 `resume-context`를 실행해 전체 저장소를 다시 읽지 않고 핵심 서브그래프와 `resume_pointer`만으로 즉시 작업을 이어갑니다.

## 라이선스

[MIT](LICENSE)
