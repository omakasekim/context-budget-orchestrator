#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Universal Windows PowerShell installer for context-budget-orchestrator Agent Skill.
.DESCRIPTION
  Installs, diagnoses (-Doctor), copies (-Copy), installs into a project (-Project),
  or uninstalls (-Uninstall) across Claude Code, Gemini CLI, Codex/DeepSeek/Grok,
  GitHub Copilot CLI, OpenCode, OpenClaw, Hermes Agent, and Antigravity.
#>
param(
  [switch]$Copy,
  [switch]$Uninstall,
  [switch]$Doctor,
  [string]$Project = ""
)

$ErrorActionPreference = "Stop"
$SkillName = "context-budget-orchestrator"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataHome = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME ".local\share" }
$ConfigHome = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME ".config" }
$InstallRoot = Join-Path $DataHome "agent-skills\$SkillName"

$Targets = @(
  (Join-Path $HOME ".claude\skills\$SkillName"),
  (Join-Path $HOME ".gemini\skills\$SkillName"),
  (Join-Path $HOME ".gemini\config\skills\$SkillName"),
  (Join-Path $HOME ".agents\skills\$SkillName"),
  (Join-Path $HOME ".copilot\skills\$SkillName"),
  (Join-Path $ConfigHome "opencode\skills\$SkillName"),
  (Join-Path $HOME ".openclaw\skills\$SkillName"),
  (Join-Path $HOME ".hermes\skills\$SkillName")
)

function Remove-SafePath([string]$TargetPath) {
  if (Test-Path -LiteralPath $TargetPath) {
    Remove-Item -LiteralPath $TargetPath -Recurse -Force
    Write-Host "  removed: $TargetPath"
  }
}

function Copy-SkillPayload([string]$Dest) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Dest "scripts"), (Join-Path $Dest "references") | Out-Null
  foreach ($f in @("SKILL.md", "README.md", "README.ko.md", "README.zh.md", "README.es.md", "LICENSE", "install.sh", "install.ps1")) {
    $src = Join-Path $ScriptDir $f
    if (Test-Path -LiteralPath $src) {
      Copy-Item -LiteralPath $src -Destination (Join-Path $Dest $f) -Force
    }
  }
  foreach ($s in @("budget_engine.py", "security_audit.py", "hook_adapter.py", "test_budget_engine.py")) {
    Copy-Item -LiteralPath (Join-Path $ScriptDir "scripts\$s") -Destination (Join-Path $Dest "scripts\$s") -Force
  }
  foreach ($r in @("WORKLOG_TEMPLATE.md", "ARCHITECTURE.md", "HOOKS.md", "PROMPT_RECIPES.md", "SECURITY_AUDIT.md")) {
    Copy-Item -LiteralPath (Join-Path $ScriptDir "references\$r") -Destination (Join-Path $Dest "references\$r") -Force
  }
}

function Find-Python {
  foreach ($cmd in @("python3", "python", "py")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return $cmd }
  }
  throw "Python 3.9+ is required and was not found in PATH."
}

if ($Uninstall) {
  Write-Host "Uninstalling $SkillName..."
  foreach ($t in $Targets) { Remove-SafePath $t }
  Remove-SafePath $InstallRoot
  Write-Host "Done."
  exit 0
}

$Py = Find-Python

if ($Doctor) {
  Write-Host "=== $SkillName doctor ==="
  & $Py -c "import sys; assert sys.version_info >= (3,9); print('[OK] Python', '.'.join(map(str, sys.version_info[:3])))"
  foreach ($t in $Targets) {
    if (Test-Path (Join-Path $t "SKILL.md")) { Write-Host "[OK] Installed at $t" }
    else { Write-Host "[WARN] Not installed at $t" }
  }
  & $Py (Join-Path $ScriptDir "scripts\test_budget_engine.py")
  exit 0
}

if ($Project -ne "") {
  $ProjAbs = (Resolve-Path $Project).Path
  foreach ($rel in @(".agents\skills\$SkillName", ".github\skills\$SkillName")) {
    $dest = Join-Path $ProjAbs $rel
    Remove-SafePath $dest
    Copy-SkillPayload $dest
    Write-Host "  installed: $dest"
  }
  exit 0
}

Write-Host "Installing $SkillName across all supported harnesses..."
Remove-SafePath $InstallRoot
Copy-SkillPayload $InstallRoot

foreach ($t in $Targets) {
  $parent = Split-Path -Parent $t
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  Remove-SafePath $t
  if ($Copy) {
    Copy-SkillPayload $t
    Write-Host "  copied: $t"
  } else {
    try {
      New-Item -ItemType SymbolicLink -Path $t -Target $InstallRoot | Out-Null
      Write-Host "  linked: $t -> $InstallRoot"
    } catch {
      Copy-SkillPayload $t
      Write-Host "  copied (symlink fallback): $t"
    }
  }
}

Write-Host "Installation complete! Verify with: pwsh ./install.ps1 -Doctor"
