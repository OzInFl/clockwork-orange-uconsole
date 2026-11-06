# Cut a release: bump VERSION, add a CHANGELOG section, commit, tag vX.Y.Z, push, publish a GitHub Release.
# A published (non-pre) release is what Update Suite and get.sh install. Works for any repo with a VERSION file.
#   .\tools\release.ps1 patch                      # 1.0.0 -> 1.0.1   (minor / major / or an exact 1.2.0)
#   .\tools\release.ps1 minor -Notes "New: ADS-B map app"
#   .\tools\release.ps1 1.1.0-beta.1 -Pre          # pre-release: only CO_CHANNEL=beta installs it
#   .\tools\release.ps1 patch -Repo ..\..\sdr-looking-glass-uconsole-public
#   -DryRun shows what would happen without changing anything.
param([Parameter(Mandatory = $true)][string]$Bump, [string]$Repo = (Split-Path $PSScriptRoot -Parent),
      [string]$Notes = "", [switch]$Pre, [switch]$DryRun)
$ErrorActionPreference = "Stop"
Push-Location $Repo
try {
  function git_ { $o = & git @args 2>&1; if ($LASTEXITCODE) { throw "git $args failed: $o" }; $o }
  if ((git_ rev-parse --abbrev-ref HEAD) -ne "main") { throw "release from main (git checkout main)" }
  if (git_ status --porcelain) { throw "uncommitted changes: commit or stash them first" }
  git_ fetch -q --tags origin | Out-Null
  $cur = (Get-Content VERSION -Raw).Trim()
  $p = @(($cur -split '[-+]')[0].Split('.') | ForEach-Object { [int]$_ })
  switch ($Bump) {
    "major" { $new = "$($p[0] + 1).0.0" }
    "minor" { $new = "$($p[0]).$($p[1] + 1).0" }
    "patch" { $new = "$($p[0]).$($p[1]).$($p[2] + 1)" }
    default { if ($Bump -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$') { throw "bump must be major|minor|patch|X.Y.Z" }; $new = $Bump }
  }
  if (git tag -l "v$new") { throw "tag v$new already exists" }
  $last = git describe --tags --abbrev=0 2>$null
  $range = if ($last) { "$last..HEAD" } else { "HEAD" }
  $log = @(git_ log --no-merges --format="- %s" $range | Where-Object { $_ -notmatch '^- Release v' })
  if (-not $log -and -not $Notes) { throw "nothing to release since $last" }
  $section = "## [$new] - $(Get-Date -Format yyyy-MM-dd)`n" + $(if ($Notes) { "$Notes`n`n" } else { "" }) + ($log -join "`n") + "`n"
  "$cur -> $new$(if ($Pre) { ' (pre-release)' })`n`n$section"
  if ($DryRun) { "dry run: nothing changed"; return }

  [IO.File]::WriteAllText((Join-Path $Repo "VERSION"), "$new`n")
  $cl = Join-Path $Repo "CHANGELOG.md"
  $old = if (Test-Path $cl) { [IO.File]::ReadAllText($cl) -replace "`r`n", "`n" } else { "# Changelog`n`n" }
  $i = $old.IndexOf("`n## ")
  $body = if ($i -ge 0) { $old.Substring(0, $i + 1) + $section + "`n" + $old.Substring($i + 1) } else { $old.TrimEnd() + "`n`n" + $section }
  [IO.File]::WriteAllText($cl, $body, (New-Object Text.UTF8Encoding $false))
  git_ add VERSION CHANGELOG.md | Out-Null
  git_ commit -q -m "Release v$new" | Out-Null
  git_ tag -a "v$new" -m "v$new" | Out-Null
  git_ push -q origin main "v$new" | Out-Null
  $nf = New-TemporaryFile; [IO.File]::WriteAllText($nf, $section)
  $ghArgs = @("release", "create", "v$new", "--title", "v$new", "--notes-file", $nf, "--verify-tag")
  if ($Pre) { $ghArgs += "--prerelease" }
  & gh @ghArgs; if ($LASTEXITCODE) { throw "gh release create failed (tag v$new is pushed; publish it on GitHub)" }
  Remove-Item $nf
  "released v$new"
} finally { Pop-Location }
