# Project Rescue installer (Windows, experimental):  irm https://raw.githubusercontent.com/itzmoksh01/project-rescue/main/install.ps1 | iex
$ErrorActionPreference = "Stop"
$raw = "https://raw.githubusercontent.com/itzmoksh01/project-rescue/main/project_rescue.py"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Project Rescue needs git." }
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { throw "Project Rescue needs Python 3.8+." }
$dir = Join-Path $env:USERPROFILE ".project-rescue"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -UseBasicParsing $raw -OutFile (Join-Path $dir "project_rescue.py")
& $py.Source (Join-Path $dir "project_rescue.py") install
