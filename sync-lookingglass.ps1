# Push the sdrLookingGlass app (uconsole-rtl worktree) to the uConsole. Code only - the device's
# lookingglass.db (systems/tags/rids it has learned) is never overwritten.
# For developing Looking Glass from a local checkout; normal installs get it from GitHub in step 50.
param([string]$Target = $env:CO_TARGET, [string]$Src = $(if ($env:CO_LG_SRC) { $env:CO_LG_SRC } else { Join-Path $PSScriptRoot "..\..\sdr-looking-glass-uconsole" }))
$ErrorActionPreference = "Stop"
if (-not $Target) { throw "no target: pass -Target pi@<uconsole-ip> or set CO_TARGET" }
$stage = Join-Path $env:TEMP "slg-stage"; Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
foreach ($d in "dashboard", "emulator") { Copy-Item -Recurse (Join-Path $Src $d) $stage }
Get-ChildItem $stage -Recurse -Include "lookingglass.db*", "__pycache__" | Remove-Item -Recurse -Force
ssh $Target "mkdir -p ~/sdr-looking-glass"
scp -rq "$stage\dashboard" "$stage\emulator" "${Target}:sdr-looking-glass/"
"synced $Src -> ${Target}:~/sdr-looking-glass (restart: sudo systemctl restart sdr-looking-glass)"
