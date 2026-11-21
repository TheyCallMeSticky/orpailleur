# Lanceur du moteur Best-of (Windows) : met le code à jour depuis GitHub, installe uv, Python et les modules, puis démarre moteur.py.
# Enregistré en UTF-8 avec BOM : sans BOM, PowerShell 5.1 lit les accents de travers.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # barre de progression de PowerShell 5.1 : téléchargements bien plus lents
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = $PSScriptRoot
$code = Join-Path $base "code"
$py = Join-Path $base "venv\Scripts\python.exe"
$env:UV_INSTALL_DIR = Join-Path $HOME ".local\bin"
$uv = Join-Path $env:UV_INSTALL_DIR "uv.exe"
try {
    $repo = (Get-Content (Join-Path $base "moteur.json") -Raw | ConvertFrom-Json).repo
    try {
        Write-Host "Mise à jour du moteur depuis GitHub..."
        $zip = Join-Path $base "code.zip"
        $new = Join-Path $base "code.new"
        Invoke-WebRequest "https://github.com/$repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
        if (Test-Path $new) { Remove-Item $new -Recurse -Force }
        Expand-Archive $zip $new
        if (Test-Path $code) { Remove-Item $code -Recurse -Force }
        Move-Item (Get-ChildItem $new)[0].FullName $code   # le zip contient un seul dossier « <dépôt>-main »
        Remove-Item $new, $zip -Recurse -Force
        Copy-Item (Join-Path $code "moteur\lancer.ps1") (Join-Path $base "lancer.ps1") -Force   # le lanceur se met à jour lui aussi
    } catch {
        if (-not (Test-Path (Join-Path $code "moteur.py"))) { throw "téléchargement du code impossible ($_)" }
        Write-Host "Mise à jour impossible ($_) : on garde la version actuelle." -ForegroundColor Yellow
    }
    if (-not (Test-Path $uv)) {
        Write-Host "Installation de uv (il installe Python et les modules)..."
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if (-not (Test-Path $uv)) { throw "installation de uv impossible" }
    }
    if (-not (Test-Path $py)) {
        & $uv venv --python 3.12 (Join-Path $base "venv")
        if ($LASTEXITCODE) { throw "création de l'environnement Python impossible" }
    }
    Write-Host "Vérification des modules (la première fois : plusieurs minutes, environ 3 Go à télécharger)..."
    & $uv pip install -q --only-binary ':all:' --python $py -r (Join-Path $code "requirements.txt")
    if ($LASTEXITCODE) { throw "installation des modules impossible" }
    $env:BESTOF_DATA = Join-Path $base "data"
    $env:PYTHONUTF8 = "1"
    & $py (Join-Path $code "moteur.py") --conf (Join-Path $base "moteur.json")
} catch {
    Write-Host "Erreur : $_" -ForegroundColor Red
}
Read-Host "Appuie sur Entrée pour fermer"
