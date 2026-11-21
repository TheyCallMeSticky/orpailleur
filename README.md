# Orpailleur — best-of GTA 6

Dashboard pour fabriquer des best-of d'environ 10 minutes à partir des streams GTA 6 de tes streamers suivis.

1. **Vidéos** : les VODs gratuites terminées depuis moins de 24 h des streamers suivis (Twitch, YouTube, Kick), rangées par jour
   (**Aujourd'hui / Hier / Plus ancien**) et par état (**À faire / Faites**), mises à jour au lancement puis toutes les heures.
   Les nouvelles VODs sur GTA partent toutes seules en analyse ; chaque analyse lit la VOD en entier et garde au plus 10 extraits,
   téléchargés en 16:9 1080p avec 8 s de marge. **Ajouter une vidéo** : un lien YouTube / Twitch / Kick (vidéo, clip, Short) ou un
   fichier, puis **Mettre directement dans le Tri** (en entier ou un passage, 30 min max) ou **Trouver les meilleurs moments**.
   Les ajouts à la main arrivent en premier dans le Tri (« ajouté par <prénom> »).
2. **Tri** : un extrait à la fois (filtre Aujourd'hui / Hier / Plus ancien), catégorie **WTF / FUN / FAIL / EPIC** et note **1 à 5**.
   Claude propose une catégorie et une note : **Entrée** la valide. Le lecteur (le même dans le Best-of) règle début et fin et
   **coupe** : « Couper ici » (K) sépare le morceau rose en deux, on raccourcit ensuite un côté ; les passages coupés sont sautés à la
   lecture et à l'export (pas de chevauchement possible).
3. **Best-of** : sélection, réglage début/fin, jauge vers l'objectif (10 min), **▶ Aperçu** (les extraits joués à la suite).
   « Remplir » prend les mieux notés, **au plus 2 par streamer**. « Ordre auto » (nouveau tirage à chaque clic) : le meilleur extrait
   sans violence en premier, le 2e meilleur en dernier, le reste **au hasard sans le même streamer ni la même catégorie deux fois
   de suite**. Les extraits exportés sont marqués « déjà utilisés » et ne sont plus repris.
4. **Export** : MP4 1920×1080 H.264/AAC normalisé à -14 LUFS, **lien de la chaîne du streamer en haut à gauche** (désactivable), timeline `.fcpxml` pour DaVinci Resolve, description YouTube
   (chapitres + crédits horodatés) et `sources.csv`. Après l'export, **la poubelle est vidée pour de bon** (vidéos et vignettes
   supprimées ; une réanalyse ne repropose pas ces passages).

## Journaux

Chaque tâche (analyse, recherche, export) écrit son journal complet dans `data/logs/<jour>/<heure>_<type>_<id>.log`, avec la
durée de chaque étape (lecture du lien, chat, téléchargement et analyse de l'audio, attente de la carte graphique, Whisper,
Claude, téléchargement des extraits) et une ligne « Durées : … » à la fin. Les durées d'une analyse réussie sont aussi gardées
dans la base et affichées dans « voir les extraits » d'une VOD. `data/tmp` (audio et chat gardés pour une réanalyse) est vidé
des fichiers qui n'ont pas servi depuis 36 h.

## Sélection des extraits

1. **Signaux sur toute la VOD** (code de [gta6-clipper](https://github.com/MD-SALMAN-HOSSAIN/gta6-clipper), MIT, copié dans
   `vendor/gta6clipper/`) : clips créés par les viewers (Twitch), pics de chat par rapport au rythme normal du streamer (Twitch,
   replays YouTube), audio (rires, réactions, crashs ; seul signal sur Kick). Les signaux proches forment un moment ; plusieurs types
   d'accord = bonus. Les 30 meilleurs moments sont gardés.
2. **Whisper** (sur la carte graphique) transcrit ces 30 passages.
3. **Claude** (commande `claude`, ton abonnement) lit signaux + transcription, garde au plus 10 passages, règle début et fin, et
   propose catégorie, note et raison. Si Whisper ou Claude échouent, ce sont les 10 meilleurs scores de l'étape 1.

Sont écartées : les VODs réservées aux abonnés, les streams encore en direct, les vidéos verticales, les VODs de moins de 20 min,
et (case « GTA uniquement ») celles dont ni le jeu Twitch ni le titre — hors #hashtags — ne parlent de GTA 6.

## Installation (une fois)

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv python3-pip
cd ~/youtube/bestof && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Claude Code doit être installé et connecté (`claude`). Sans carte NVIDIA, Whisper tourne sur le processeur (plus lent).

## Lancer

Le site démarre **tout seul avec WSL** (service systemd utilisateur `orpailleur`, dans `~/.config/systemd/user/`) :

```bash
orpailleur          # redémarre le site + le moteur (après une mise à jour du code par exemple)
orpailleur logs     # journal en direct (Ctrl+C pour quitter, le site continue)
orpailleur stop     # arrête ; orpailleur start / status
```
Garde une fenêtre Ubuntu ouverte (réduite) : quand toutes sont fermées, Windows éteint WSL au bout de quelques secondes, et le
site avec. Sans le service : `~/youtube/bestof/start.sh` dans un terminal. Ouvre le **lien privé** (`http://localhost:8765/?k=…`,
jeton dans `data/config.json`) (`http://localhost:8765/?k=…`). Le moteur de ce PC démarre avec le site (`--no-engine` pour
ne pas le lancer). La file d'analyse est gardée dans la base : rien n'est perdu à un redémarrage.

Deux personnes peuvent trier en même temps sans tomber sur le même extrait. Ton ami ouvre `https://<adresse Tailscale>/?k=<jeton>`
(jeton dans `data/config.json`), voir « Moteurs » ci-dessous.

## Moteurs : plusieurs machines analysent en même temps

Ce PC est le **serveur** (le site, la file, le tri, l'export, Claude avec ton compte, le téléchargement des extraits). Chaque
**moteur** (ce PC, le PC d'un ami, un Mac) prend une VOD en attente, fait la partie lourde chez lui (chat, audio, Whisper sur sa
carte graphique) et renvoie seulement les passages candidats et leur transcription (environ 100 Ko). Une seule analyse à la fois
par machine : 2 Whisper sur la même carte ne vont pas plus vite (mesuré : +24 % à 2, 0 % à 3).

Dans l'onglet **Machines** : chaque machine, la VOD qu'elle traite et l'étape en cours. **Arrêter après les fichiers en cours**
(elle finit puis ne prend plus rien), **Arrêter maintenant** (la VOD en cours repart en file pour une autre machine),
**Reprendre**, **Retirer** (son accès est supprimé). Une machine éteinte ou déconnectée rend ses VODs au bout de 2 min.
Quand ce PC est éteint, les moteurs attendent et reprennent seuls.

### Une fois : Tailscale (le site n'est visible que de toi et de tes amis, rien n'est ouvert sur internet)

Dans WSL :
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname bestof          # ouvre le lien affiché et connecte-toi
sudo tailscale serve --bg 8765               # la 1re fois, il donne un lien pour activer HTTPS : clique-le
tailscale serve status                       # affiche l'adresse : https://bestof.<ton-réseau>.ts.net
```
Puis sur https://login.tailscale.com/admin/machines : machine **bestof** > « … » > **Share** et envoie le lien d'invitation à
chaque ami (il crée un compte Tailscale gratuit et accepte : il ne voit que cette machine).

### Une fois : le code sur GitHub (les moteurs se mettent à jour depuis là)

Crée un dépôt **public** vide sur github.com (ex. `bestof`), puis :
```bash
cd ~/youtube/bestof
git init -b main && git add . && git commit -m "Best-of GTA 6"     # data/ n'est jamais envoyé (.gitignore)
git remote add origin https://github.com/<pseudo>/bestof.git && git push -u origin main
```
Après chaque modification du code : `git add . && git commit -m "…" && git push` (sinon le site affiche « version différente »).

### Inviter un ami

Onglet **Machines** > **Inviter un ami** : tape son prénom, **Créer le message**, **Copier le message**, et envoie-le-lui en privé.
Le message contient tout : Tailscale et ton lien d'invitation, la ligne à coller sur Windows (PowerShell) ou Mac (Terminal), le
lien pour trier. Un message par ami (il contient la clé de sa machine) ; **Message** sur une machine le réaffiche. Les réglages
(adresse Tailscale, dépôt GitHub, lien d'invitation) se remplissent une fois dans **Réglages** ; un lien d'invitation Tailscale
à usage unique est à changer avant chaque ami, un lien « réutilisable » sert pour tous (6 utilisateurs max en gratuit).

Elle installe tout (dans `%LOCALAPPDATA%\BestOfMoteur` ou `~/.bestof-moteur`) et démarre le moteur. Ensuite : double-clic sur
**Moteur Best-of** sur le Bureau ; il se met à jour tout seul depuis GitHub. Fermer la fenêtre arrête le moteur. Sans carte
NVIDIA (Mac), Whisper tourne sur le processeur avec un modèle plus petit : plus lent et un peu moins précis.

## Raccourcis du tri

| Touche | Action |
|---|---|
| `Q`/`A`, `S`, `D`, `F` | WTF, FUN, FAIL, EPIC |
| `1` à `5` | note (valide et passe au suivant) |
| `Entrée` | valide la proposition de Claude |
| `X` / `↓` / `Z` | poubelle / plus tard / annuler |
| `I` / `O` | début / fin à la position actuelle (ou boutons ±1 s, ou bords de la barre) |
| `K` / `Suppr` | couper le morceau ici / supprimer le morceau choisi |
| `R`, `C`, `V` | signaler radio / cinématique / violence |
| `Espace`, `←` `→`, `M`, `+` `−` | pause, ±3 s, muet, volume |

## Fichiers

```
app.py            serveur web + API (bibliothèque standard Python)
vods.py           liste des VODs des streamers suivis
collect.py        analyse d'une VOD : signaux, sélection, téléchargement
ai.py             Whisper + Claude
transcribe_win.py transcription Whisper (processus séparé, GPU)
montage.py        ordre auto, alertes, description, CSV, export MP4 + FCPXML
store.py, jobs.py base JSON (data/db.json) et tâches de fond
engines.py        file des moteurs côté serveur : baux, résultats, finalisation (Claude + extraits), machines
moteur.py         moteur d'analyse (une VOD par processus enfant, arrêtable à tout moment)
moteur/           installation et lanceur du moteur pour Windows (PowerShell) et Mac
selftest.py       tests (.venv/bin/python selftest.py)
static/           dashboard
vendor/           modules repris de gta6-clipper (MIT)
data/             vidéos, base, exports : à sauvegarder (data/logs : journaux, data/tmp : caches, data/moteur : moteur de ce PC)
```
