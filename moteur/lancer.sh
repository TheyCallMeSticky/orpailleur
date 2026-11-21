#!/bin/bash
# Lanceur du moteur Best-of (Mac / Linux, bash 3.2) : met le code à jour depuis GitHub, installe uv, Python et les modules,
# puis démarre moteur.py (qui empêche lui-même la mise en veille du Mac avec caffeinate).
B="$HOME/.bestof-moteur"
UV="$HOME/.local/bin/uv"
cd "$B" || exit 1
set -o pipefail
fin() { echo "$1"; read -r -p "Appuie sur Entrée pour fermer " _; exit 1; }
REPO=$(sed -n 's/.*"repo": *"\([^"]*\)".*/\1/p' moteur.json)
echo "Mise à jour du moteur depuis GitHub…"
rm -rf code.new && mkdir code.new
if curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C code.new --strip-components 1 \
   && [ -f code.new/moteur.py ]; then  # l'archive contient un seul dossier « <dépôt>-main »
    rm -rf code && mv code.new code
    cp code/moteur/lancer.sh lancer.sh.new && mv lancer.sh.new lancer.sh  # mise à jour du lanceur (mv : bash garde l'ancien ouvert)
elif [ -f code/moteur.py ]; then
    echo "Mise à jour impossible : on garde la version actuelle."
else
    fin "Téléchargement du code impossible (connexion internet ?)."
fi
if [ ! -x "$UV" ]; then
    echo "Installation de uv (il installe Python et les modules)…"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh || fin "Installation de uv impossible."
fi
[ -x venv/bin/python ] || "$UV" venv -q --python 3.12 venv || fin "Création de l'environnement Python impossible."
echo "Vérification des modules (la première fois : plusieurs minutes)…"
"$UV" pip install -q --only-binary :all: --python venv/bin/python -r code/requirements.txt || fin "Installation des modules impossible."
export BESTOF_DATA="$B/data" PYTHONUTF8=1
venv/bin/python code/moteur.py --conf moteur.json
read -r -p "Appuie sur Entrée pour fermer " _
