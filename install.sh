#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="context-budget-orchestrator"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_ROOT="${DATA_HOME}/agent-skills/${SKILL_NAME}"

# Global skill directories across all major AI coding CLI harnesses:
# - Claude Code:       ~/.claude/skills/
# - Gemini CLI:        ~/.gemini/skills/
# - Codex / DeepSeek / Grok / AgentSkills universal: ~/.agents/skills/
# - GitHub Copilot CLI: ~/.copilot/skills/
# - OpenCode:          ~/.config/opencode/skills/
# - OpenClaw:          ~/.openclaw/skills/
TARGETS=(
  "${HOME}/.claude/skills/${SKILL_NAME}"
  "${HOME}/.gemini/skills/${SKILL_NAME}"
  "${HOME}/.gemini/config/skills/${SKILL_NAME}"
  "${HOME}/.agents/skills/${SKILL_NAME}"
  "${HOME}/.copilot/skills/${SKILL_NAME}"
  "${CONFIG_HOME}/opencode/skills/${SKILL_NAME}"
  "${HOME}/.openclaw/skills/${SKILL_NAME}"
  "${HOME}/.hermes/skills/${SKILL_NAME}"
)

MODE="symlink"
UNINSTALL=0
DOCTOR=0
PROJECT_DIR=""

usage() {
  cat <<EOF
Usage: bash install.sh [OPTIONS]

Install, verify, or uninstall '${SKILL_NAME}' across:
  - Claude Code           (~/.claude/skills/${SKILL_NAME})
  - Gemini CLI            (~/.gemini/skills/${SKILL_NAME})
  - OpenAI Codex / DeepSeek Harness / Grok CLI (~/.agents/skills/${SKILL_NAME})
  - GitHub Copilot CLI    (~/.copilot/skills/${SKILL_NAME})
  - OpenCode              (~/.config/opencode/skills/${SKILL_NAME})
  - OpenClaw              (~/.openclaw/skills/${SKILL_NAME})

Options:
  --copy              Copy files into each harness directory instead of symlinking
  --project <dir>     Install directly into <dir>/.agents/skills/ and <dir>/.github/skills/
  --doctor            Run Python, optional tiktoken, multi-harness, and unit-test diagnostics
  --uninstall         Remove installed skill files and symlinks from all global harness paths
  -h, --help          Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy)
      MODE="copy"
      shift
      ;;
    --project)
      if [[ $# -lt 2 ]]; then
        echo "Error: --project requires a directory path" >&2
        exit 1
      fi
      PROJECT_DIR="$2"
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --doctor)
      DOCTOR=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown option '$1'" >&2
      usage >&2
      exit 1
      ;;
  esac
done

remove_path_safely() {
  local target="$1"
  if [[ -L "$target" ]]; then
    rm -f "$target"
    echo "  removed symlink: $target"
  elif [[ -d "$target" ]]; then
    rm -rf "$target"
    echo "  removed directory: $target"
  elif [[ -e "$target" ]]; then
    rm -f "$target"
    echo "  removed file: $target"
  fi
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is required (Python 3.9+)." >&2
    exit 1
  fi
  python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    sys.stderr.write(f"Error: Python 3.9+ required, found {sys.version_info.major}.{sys.version_info.minor}\n")
    sys.exit(1)
PY
}

copy_skill_payload() {
  local dest="$1"
  mkdir -p "$dest/scripts" "$dest/references"
  for f in SKILL.md README.md README.ko.md README.zh.md README.es.md LICENSE install.sh install.ps1; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
      cp "$SCRIPT_DIR/$f" "$dest/$f"
    fi
  done
  cp "$SCRIPT_DIR/scripts/budget_engine.py" "$dest/scripts/budget_engine.py"
  cp "$SCRIPT_DIR/scripts/security_audit.py" "$dest/scripts/security_audit.py"
  cp "$SCRIPT_DIR/scripts/hook_adapter.py" "$dest/scripts/hook_adapter.py"
  cp "$SCRIPT_DIR/scripts/test_budget_engine.py" "$dest/scripts/test_budget_engine.py"
  cp "$SCRIPT_DIR/references/WORKLOG_TEMPLATE.md" "$dest/references/WORKLOG_TEMPLATE.md"
  cp "$SCRIPT_DIR/references/ARCHITECTURE.md" "$dest/references/ARCHITECTURE.md"
  cp "$SCRIPT_DIR/references/HOOKS.md" "$dest/references/HOOKS.md"
  cp "$SCRIPT_DIR/references/PROMPT_RECIPES.md" "$dest/references/PROMPT_RECIPES.md"
  cp "$SCRIPT_DIR/references/SECURITY_AUDIT.md" "$dest/references/SECURITY_AUDIT.md"
  chmod +x "$dest/install.sh" "$dest/scripts/budget_engine.py" "$dest/scripts/hook_adapter.py" "$dest/scripts/test_budget_engine.py"
}

run_doctor() {
  echo "=== ${SKILL_NAME} doctor ==="
  check_python
  python3 - <<'PY'
import sys
print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} (requires 3.9+)")
try:
    import tiktoken  # noqa: F401
    print("[OK] Optional tiktoken detected (exact BPE tokenizer active)")
except ImportError:
    print("[INFO] Optional tiktoken not installed (using calibrated stdlib estimator)")
PY

  local any_missing=0
  for target in "${TARGETS[@]}"; do
    if [[ -f "$target/SKILL.md" && -f "$target/scripts/budget_engine.py" ]]; then
      echo "[OK] Installed at $target"
    else
      echo "[WARN] Not installed at $target"
      any_missing=1
    fi
  done

  local test_script="$SCRIPT_DIR/scripts/test_budget_engine.py"
  if [[ -f "$INSTALL_ROOT/scripts/test_budget_engine.py" ]]; then
    test_script="$INSTALL_ROOT/scripts/test_budget_engine.py"
  fi
  echo "Running engine self-test ($test_script)..."
  python3 "$test_script"
  if [[ "$any_missing" -eq 0 ]]; then
    echo "[OK] All 6 cross-harness skill paths and unit tests are healthy."
  else
    echo "[INFO] Tests passed; run 'bash install.sh' to wire any missing harness paths."
  fi
}

if [[ "$DOCTOR" -eq 1 ]]; then
  run_doctor
  exit 0
fi

if [[ "$UNINSTALL" -eq 1 ]]; then
  echo "Uninstalling ${SKILL_NAME}..."
  for target in "${TARGETS[@]}"; do
    remove_path_safely "$target"
  done
  remove_path_safely "$INSTALL_ROOT"
  echo "Done. ${SKILL_NAME} has been uninstalled."
  exit 0
fi

check_python

if [[ -n "$PROJECT_DIR" ]]; then
  mkdir -p "$PROJECT_DIR"
  PROJ_ABS="$(cd "$PROJECT_DIR" && pwd)"
  echo "Installing ${SKILL_NAME} into project: ${PROJ_ABS}"
  for rel in ".agents/skills/${SKILL_NAME}" ".github/skills/${SKILL_NAME}"; do
    dest="${PROJ_ABS}/${rel}"
    remove_path_safely "$dest"
    copy_skill_payload "$dest"
    echo "  installed: $dest"
  done
  echo "Project installation complete!"
  exit 0
fi

echo "Installing ${SKILL_NAME} globally (mode: ${MODE})..."
if [[ "$MODE" == "symlink" ]]; then
  remove_path_safely "$INSTALL_ROOT"
  copy_skill_payload "$INSTALL_ROOT"
  echo "  installed canonical payload: $INSTALL_ROOT"

  for target in "${TARGETS[@]}"; do
    mkdir -p "$(dirname "$target")"
    remove_path_safely "$target"
    ln -s "$INSTALL_ROOT" "$target"
    echo "  linked: $target -> $INSTALL_ROOT"
  done
else
  for target in "${TARGETS[@]}"; do
    mkdir -p "$(dirname "$target")"
    remove_path_safely "$target"
    copy_skill_payload "$target"
    echo "  copied: $target"
  done
fi

if command -v git >/dev/null 2>&1; then
  EXCLUDES_FILE="$(git config --global --path --get core.excludesfile 2>/dev/null || true)"
  if [[ -n "$EXCLUDES_FILE" && -f "$EXCLUDES_FILE" ]]; then
    if ! grep -Fxq ".agent_state/" "$EXCLUDES_FILE" 2>/dev/null; then
      printf '\n# context-budget-orchestrator local session state\n.agent_state/\n' >> "$EXCLUDES_FILE"
      echo "  appended .agent_state/ to global gitignore: $EXCLUDES_FILE"
    fi
  fi
fi

echo ""
echo "Installation complete across Claude Code, Gemini CLI, Codex CLI, Copilot CLI, OpenCode, OpenClaw, DeepSeek Harness & Grok CLI!"
echo "Verify with: bash install.sh --doctor"
