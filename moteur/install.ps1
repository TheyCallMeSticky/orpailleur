# Installation du moteur Best-of sur Windows (PowerShell 5.1+), servie par le site avec {{HUB}} {{TOKEN}} {{NAME}} {{REPO}} remplaces.
# Texte en ASCII seulement ("irm | iex" peut lire le fichier dans un autre encodage que UTF-8) et jamais "exit" : le script tourne
# dans la fenetre PowerShell de l'ami. uv, Python, les modules et le code sont installes par lancer.ps1 (a chaque lancement).
& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # barre de progression de PowerShell 5.1 : telechargements bien plus lents
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $base = Join-Path $env:LOCALAPPDATA "BestOfMoteur"
    Write-Host "Installation du moteur Best-of dans $base" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force $base | Out-Null
    [IO.File]::WriteAllText((Join-Path $base "moteur.json"), @'
{"hub": "{{HUB}}", "token": "{{TOKEN}}", "name": "{{NAME}}", "repo": "{{REPO}}"}
'@)
    Write-Host "Telechargement du lanceur..."
    Invoke-WebRequest "https://raw.githubusercontent.com/{{REPO}}/main/moteur/lancer.ps1" -OutFile (Join-Path $base "lancer.ps1") -UseBasicParsing
    Set-Content (Join-Path ([Environment]::GetFolderPath("Desktop")) "Moteur Best-of.cmd") -Encoding ASCII `
        -Value '@powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\BestOfMoteur\lancer.ps1"'
    Write-Host "Raccourci cree sur le Bureau : Moteur Best-of (double-clic pour le relancer plus tard)" -ForegroundColor Green
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $base "lancer.ps1")
}
