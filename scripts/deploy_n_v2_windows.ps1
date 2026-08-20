$ErrorActionPreference = "Stop"

# 兼容旧运维入口；正式实现已迁移到N v4脚本，避免既有快捷方式失效。
$DeployScript = Join-Path $PSScriptRoot "deploy_n_v4_windows.ps1"
& $DeployScript
