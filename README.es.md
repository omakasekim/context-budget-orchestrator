<p align="center">
  <a href="README.md">English</a> | <a href="README.ko.md">한국어</a> | <a href="README.zh.md">中文</a> | <strong>Español</strong>
</p>

# context-budget-orchestrator

[![Version 3.0](https://img.shields.io/badge/Version-3.0%20Universal-06B6D4?style=for-the-badge)](scripts/budget_engine.py)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B%20Stdlib%20Only-3B82F6?style=for-the-badge)](scripts/budget_engine.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=for-the-badge)](LICENSE)

Una **Agent Skill universal, sin dependencias externas y libre de sobrecarga**, diseñada para orquestar proyectos complejos de ingeniería e investigación (de minutos a horas) en **Claude Code, Gemini CLI, OpenAI Codex CLI, GitHub Copilot CLI, OpenClaw, OpenCode, DeepSeek Harness (`dsh`) y xAI Grok CLI**.

## Seis Pilares — Cero Sobrecarga

1. **Investigar y organizar información (`memory` + `record-decision`)** — Almacena hallazgos, fuentes, etiquetas, decisiones de arquitectura (ADR) e intentos fallidos dentro de un grafo tipado persistente (`.agent_state/context_graph.json`) vinculado a tareas y archivos.
2. **Escribir y ejecutar código verificado (`exec-task` + `compress-output`)** — Ejecuta compilaciones y pruebas comprimiendo automáticamente entre un 70% y un 95% de los registros ruidosos, conservando errores y líneas `archivo:línea` para evitar el deterioro del contexto.
3. **Delegar a subagentes especializados (`delegate` + `lease`)** — Genera briefs acotados por presupuesto de tokens para roles de `researcher`, `architect`, `implementer`, `reviewer` y `verifier`, con bloqueos atómicos TTL contra conflictos de archivos y comandos listos para los 8 entornos.
4. **Memoria en grafo para proyectos de larga duración (`context_graph.json` + `query-graph` + `resume-context`)** — Al reanudar una nueva sesión, carga únicamente el subgrafo relevante (con límite opcional `--token-budget`), detecta cambios externos en Git y mantiene el seguimiento de épocas de relevo.
5. **Enrutamiento ligero para ecosistemas extensos de skills (`discover-skills`)** — Escanea solo el encabezado YAML (`name` y `description`) de cada `SKILL.md` instalado para seleccionar las 1–2 skills necesarias sin inflar el contexto.
6. **Control proactivo del presupuesto de contexto (`inspect-session` + `auto-plan` + `gate-check` + `verify-handoff`)** — Extrae métricas reales `usage` desde sesiones JSONL, detecta bucles de edición repetitivos, descubre dependencias locales de Python/JS/TS en oleadas paralelas (`parallel_waves`), impone un **límite seguro del 68%** y verifica sintaxis/pruebas antes de cada entrega.

## Entornos (Harnesses) Compatibles

| Entorno | Ruta Global (`install.sh`) | Telemetría y Despacho |
| --- | --- | --- |
| **Claude Code** | `~/.claude/skills/context-budget-orchestrator` | `usage` JSONL exacto, hook `PreToolUse`, `claude -p` |
| **Gemini CLI** | `~/.gemini/skills/context-budget-orchestrator` | `usageMetadata`, hook `BeforeTool`, `gemini -p` |
| **OpenAI Codex CLI** | `~/.agents/skills/context-budget-orchestrator` | `total_tokens`, `AGENTS.md`, `codex exec` |
| **GitHub Copilot CLI** | `~/.copilot/skills/context-budget-orchestrator` | `/skills reload`, `.github/skills/`, `gh copilot -p` |
| **OpenCode** | `~/.config/opencode/skills/context-budget-orchestrator` | Modelos `proveedor/modelo`, `opencode run` |
| **OpenClaw** | `~/.openclaw/skills/context-budget-orchestrator` | `~/.openclaw/sessions`, `openclaw agent run` |
| **DeepSeek Harness (`dsh`)** | `~/.agents/skills/context-budget-orchestrator` | `deepseek-v4` (1M), `v3`/`r1`, `dsh run` |
| **xAI Grok CLI** | `~/.agents/skills/context-budget-orchestrator` | `grok-4` (256K), `grok-code-fast`, `grok -p` |

## Instalación en 1 Comando

```bash
git clone https://github.com/Aetherx86/context-budget-orchestrator.git
cd context-budget-orchestrator
bash install.sh
```

```bash
bash install.sh --doctor      # Verifica Python 3.9+, las 6 rutas globales y ejecuta los tests
bash install.sh --project .   # Instala en ./.agents/skills/ y ./.github/skills/ del proyecto
bash install.sh --copy        # Copia los archivos en lugar de crear enlaces simbólicos
bash install.sh --uninstall   # Elimina limpiamente todas las rutas globales instaladas
```

## Licencia

[MIT](LICENSE)
