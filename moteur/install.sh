#!/bin/bash
# Installation du moteur Best-of sur Mac (ou Linux), servie par le site avec {{HUB}} {{TOKEN}} {{NAME}} {{REPO}} remplacés.
# Tout est dans main() : avec « curl | bash », bash lit ainsi le script en entier avant de l'exécuter.
# uv, Python, les modules et le code sont installés par lancer.sh (à chaque lancement).
main() {
    set -e
    B="$HOME/.bestof-moteur"
    echo "Installation du moteur Best-of dans $B"
    mkdir -p "$B" "$HOME/Desktop"
    cat > "$B/moteur.json" <<'EOF'
{"hub": "{{HUB}}", "token": "{{TOKEN}}", "name": "{{NAME}}", "repo": "{{REPO}}"}
EOF
    echo "Téléchargement du lanceur…"
    curl -fsSL "https://raw.githubusercontent.com/{{REPO}}/main/moteur/lancer.sh" -o "$B/lancer.sh"
    L="$HOME/Desktop/Moteur Best-of.command"
    printf '#!/bin/bash\nexec bash "$HOME/.bestof-moteur/lancer.sh"\n' > "$L"
    chmod +x "$L"
    echo "Raccourci créé sur le Bureau : Moteur Best-of (double-clic pour le relancer plus tard)"
    exec bash "$B/lancer.sh" < /dev/tty  # stdin = le clavier, pas le script reçu par curl
}
main
