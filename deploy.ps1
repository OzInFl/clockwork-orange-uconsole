# Push Clockwork Orange to the uConsole and run it.
#   .\deploy.ps1                      # all steps
#   .\deploy.ps1 -Steps 20-sdr,40-op25
#   .\deploy.ps1 -Background -Steps 40-op25   # long builds: runs under nohup, log in ~/clockwork-orange.log
#   -Target pi@<uconsole-ip>, or set it once: [Environment]::SetEnvironmentVariable("CO_TARGET", "pi@<ip>", "User")
param([string]$Target = $env:CO_TARGET, [string[]]$Steps = @(), [switch]$Background)
$ErrorActionPreference = "Stop"
if (-not $Target) { throw "no target: pass -Target pi@<uconsole-ip> or set CO_TARGET" }
# Everything but this script runs on Linux: force LF line endings (editing on Windows can add CRLF,
# which bash can't parse). Binary-safe: only files with no NUL bytes are touched.
Get-ChildItem $PSScriptRoot -Recurse -File | Where-Object { $_.Extension -ne ".ps1" } | ForEach-Object {
  $b = [IO.File]::ReadAllBytes($_.FullName)
  if (($b -contains 13) -and -not ($b -contains 0)) {
    $t = [Text.Encoding]::UTF8.GetString($b) -replace "`r`n", "`n"
    [IO.File]::WriteAllText($_.FullName, $t, (New-Object Text.UTF8Encoding $false))
  }
}
ssh $Target "rm -rf ~/clockwork-orange"
scp -rq "$PSScriptRoot" "${Target}:clockwork-orange"
$cmd = "cd ~/clockwork-orange && chmod +x install.sh steps/*.sh && ./install.sh $($Steps -join ' ')"
if ($Background) { ssh $Target "nohup bash -c '$cmd' > ~/clockwork-orange.log 2>&1 < /dev/null &"; "running in background: ssh $Target tail -f clockwork-orange.log" }
else {
  $ErrorActionPreference = "Continue"
  ssh -t $Target $cmd
  if ($LASTEXITCODE) { throw "install.sh failed ($LASTEXITCODE)" }
}
