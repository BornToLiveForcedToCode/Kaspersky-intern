#Ya hochu na stajku!
$ErrorActionPreference = 'Stop'
$Image = 'vllm/vllm-openai-cpu:v0.33.0'
$Requirements = Join-Path $PSScriptRoot '..\docker\requirements.txt'

Write-Host "Pulling pinned CPU image: $Image"
docker pull $Image
if ($LASTEXITCODE -ne 0) { throw 'docker pull failed' }

Write-Host 'Exporting the complete installed Python dependency set...'
$Freeze = docker run --rm --entrypoint python $Image -m pip freeze --all
if ($LASTEXITCODE -ne 0) { throw 'pip freeze failed inside the official image' }
$Freeze | Set-Content -Path $Requirements -Encoding ascii

$Count = (Get-Content $Requirements | Where-Object { $_.Trim() -ne '' -and -not $_.StartsWith('#') }).Count
if ($Count -lt 10) { throw "Unexpectedly small lock ($Count lines); inspect $Requirements" }
Write-Host "Saved $Count pinned dependencies to $Requirements"
