<p align="center">
  <a href="README.md">English</a> | <a href="README.ko.md">한국어</a> | <strong>中文</strong> | <a href="README.es.md">Español</a>
</p>

# context-budget-orchestrator

[![Version 3.0](https://img.shields.io/badge/Version-3.0%20Universal-06B6D4?style=for-the-badge)](scripts/budget_engine.py)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B%20Stdlib%20Only-3B82F6?style=for-the-badge)](scripts/budget_engine.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=for-the-badge)](LICENSE)

一款**零外部依赖、零冗余、跨平台通用的 AI 编排技能（Agent Skill）**，专为持续数分钟至数小时的复杂研发与研究任务设计，全面兼容 **Claude Code、Gemini CLI、OpenAI Codex CLI、GitHub Copilot CLI、OpenClaw、OpenCode、DeepSeek Harness（`dsh`）与 xAI Grok CLI**。

## 六大核心能力（极致精简，拒绝冗余）

1. **主题研究与信息组织（`memory` + `record-decision`）** — 将调研结论、来源链接、标签、架构决策（ADR）和失败教训以结构化节点存入类型化上下文图（`.agent_state/context_graph.json`），并与任务和文件自动关联。
2. **代码编写与受控执行（`exec-task` + `compress-output`）** — 执行编译与测试命令时自动压缩 70%–95% 的冗余日志，保留报错堆栈与 `file:line` 锚点，防止工具输出撑爆上下文窗口。
3. **专业子智能体委派（`delegate` + `lease`）** — 面向 `researcher`、`architect`、`implementer`、`reviewer`、`verifier` 等专职角色生成按预算剪枝的任务简报，自动加锁（TTL 文件冲突锁）并输出 8 大框架的直接调度命令。
4. **跨会话持久化图记忆（`context_graph.json` + `query-graph` + `resume-context`）** — 告别线性膨胀的纯文本日志：冷启动恢复时仅按依赖图提取下一任务必需的上游子图（支持 `--token-budget` 硬上限），同时自动检测 Git 漂移并记录交接轮次（Epoch）。
5. **海量技能库的零膨胀路由（`discover-skills`）** — 仅扫描各 CLI 技能目录中 `SKILL.md` 的 YAML 元数据（`name` / `description`），无需把几十个完整技能塞进上下文即可精准匹配当前任务所需的 1–2 个辅助技能。
6. **主动式上下文预算门禁（`inspect-session` + `auto-plan` + `gate-check` + `verify-handoff`）** — 解析 JSONL 会话日志中的精确供应商 `usage` 计数，预警循环修改（thrash loop）与日志膨胀；通过静态 AST/导入分析自动拆分并行波次（`parallel_waves`），严格守住 **68% 安全上限**，并在交接前校验语法与测试。
7. **内置安全与代码审计引擎（`audit`）** — 零依赖静态扫描 **30 类核心 CWE 漏洞与配置缺陷** + 自定义 JSON 规则（`--rules-file`），支持 OpenAPI 安全契约审计（`openapi`）、自然语言范围（`--scope-nl`）、SHA-256 指纹去重、基线差异对比（`diff` / `--retest-new`）、合规映射（OWASP Top 10 · PCI-DSS v4 · SOC 2 · ISO 27001）、七问分诊门禁、凭据脱敏以及 6 种企业级报告导出（`json` / `sarif` / `html` / `junit` / `csv` / `md`）和 Windows `install.ps1`。
8. **基于真实证据的智能体核验与仲裁内核（`verify`）** — 不轻信智能体自报的“已完成”：通过 Git 祖先历史与真实 Diff 核验交付（`shipped`，退出码 `0`/`1`），识破空壳 `assert True` 测试、跳过断言与名不副实的提交（`commit-audit`），通过真实工作区指纹检测死循环/空转（`liveness`），确定性仲裁多智能体文件锁与前置依赖（`arbitrate`），并维护人工决策升级队列（`escalate`）。

## 兼容的 AI 编程框架与模型

| 框架 / CLI | 全局安装路径（`install.sh` 自动配置） | 原生遥测与调度 |
| --- | --- | --- |
| **Claude Code** | `~/.claude/skills/context-budget-orchestrator` | 精确 JSONL `usage`、`PreToolUse` 钩子、`claude -p` |
| **Gemini CLI** | `~/.gemini/skills/context-budget-orchestrator` | `usageMetadata`、`BeforeTool` 钩子、`gemini -p` |
| **OpenAI Codex CLI** | `~/.agents/skills/context-budget-orchestrator` | `total_tokens`、`AGENTS.md`、`codex exec` |
| **GitHub Copilot CLI** | `~/.copilot/skills/context-budget-orchestrator` | `/skills reload`、`.github/skills/`、`gh copilot -p` |
| **OpenCode** | `~/.config/opencode/skills/context-budget-orchestrator` | 支持 `provider/model` 前缀、`opencode run` |
| **OpenClaw** | `~/.openclaw/skills/context-budget-orchestrator` | `~/.openclaw/sessions`、`openclaw agent run` |
| **DeepSeek Harness (`dsh`)** | `~/.agents/skills/context-budget-orchestrator` | `deepseek-v4` (1M)、`v3`/`r1`、`dsh run` |
| **xAI Grok CLI** | `~/.agents/skills/context-budget-orchestrator` | `grok-4` (256K)、`grok-code-fast`、`grok -p` |

## 一键安装与自检

```bash
git clone https://github.com/Aetherx86/context-budget-orchestrator.git
cd context-budget-orchestrator
bash install.sh
```

```bash
bash install.sh --doctor      # 检查 Python 3.9+、6 个框架安装路径并运行完整测试套件
bash install.sh --project .   # 直接安装到当前仓库的 .agents/skills/ 与 .github/skills/
bash install.sh --copy        # 使用复制模式替代符号链接
bash install.sh --uninstall   # 一键卸载全部全局链接与文件
```

## 许可证

[MIT](LICENSE)
