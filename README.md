# Orpailleur — best-of GTA 6

Dashboard pour fabriquer des best-of d'environ 10 minutes à partir des streams GTA 6 de tes streamers suivis.

**Qui fait quoi** (tout marche sans ton PC, tant qu'un PC avec le moteur est allumé) :

| Où | Rôle |
|---|---|
| **Stockage S3** (OVH) | tous les fichiers : extraits, vignettes, musiques, vidéos envoyées, exports |
| **PC avec le moteur** (le tien, ceux des amis) | tout le travail sur les vidéos : analyse (chat, audio, Whisper), téléchargement des extraits, vignettes, découpes, vidéos ajoutées à la main, « 30 s avant », export |
| **Serveur** (le site) | l'affichage, la base, la file des tâches, Claude (choix des moments), la recherche des VODs, la description ; **aucun traitement vidéo, pas besoin de ffmpeg** |

Les navigateurs et les moteurs lisent et écrivent directement sur S3 avec des liens signés par le site (12 h pour lire, 24 h pour
les moteurs) : les clés S3 ne quittent jamais le serveur. Sans aucun PC allumé, on peut trier, ranger le best-of et regarder les
vidéos ; les nouveaux extraits, l'export et « 30 s avant » attendent un PC (« Aucun PC allumé »).

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
   **Musique et transitions** (carte dans l'onglet Best-of) : envoie tes musiques (MP3…, libres de droits : bibliothèque audio de
   YouTube Studio, Uppbeat, Pixabay…) puis clique sur 2 ou 3 d'entre elles dans l'ordre voulu. Elles s'enchaînent avec un fondu de
   3 s (en boucle si la vidéo est plus longue), **baissent toutes seules quand le streamer parle** (environ 12 dB) et s'éteignent en
   fondu sur les 5 dernières secondes ; la description cite les musiques. Entre deux clips : une transition de 0,4 s tirée au hasard
   parmi fondu, glissé, zoom, flash blanc, flou et pixels (case pour les couper). Dans un clip coupé, les morceaux sont reliés par un
   fondu de 0,25 s. L'assemblage final utilise la carte NVIDIA (NVENC) s'il y en a une, sinon le processeur.
   supprimées ; une réanalyse ne repropose pas ces passages).

## Journaux

Chaque tâche (analyse, recherche, export) écrit son journal complet dans `data/logs/<jour>/<heure>_<type>_<id>.log`, avec la
durée de chaque étape (lecture du lien, chat, téléchargement et analyse de l'audio, attente de la carte graphique, Whisper,
Claude, téléchargement des extraits) et une ligne « Durées : … » à la fin. Les durées d'une analyse réussie sont aussi gardées
dans la base et affichées dans « voir les extraits » d'une VOD. Le journal d'une tâche faite par un moteur arrive en entier à la
fin (lignes « [moteur] ») avec ses durées ; pendant qu'elle tourne, l'onglet Machines montre son étape et sa dernière ligne.

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
Le serveur seul (sans moteur, `--no-engine`) n'a besoin que de Python, `yt-dlp` et Claude Code : pas de ffmpeg.

### Le stockage S3 (une fois)

OVH Public Cloud > Object Storage : un conteneur « API compatible S3 » (Gravelines, sans versioning ni Object Lock) et un
utilisateur S3 qui lui est réservé. Puis, sur le serveur :
```bash
python s3.py setup      # adresse, région et conteneur pré-remplis ; la clé secrète se tape sans s'afficher
```
Les clés vont dans `data/s3.json` (lisible par toi seul, jamais dans le dépôt). Le script vérifie l'écriture, la lecture et la
suppression, puis autorise les envois depuis la page (CORS). Pour copier des fichiers déjà sur le disque :
`python s3.py push data/media media` (et `data/exports exports`, `data/inbox inbox`) ; un fichier déjà envoyé est sauté.
Sans `data/s3.json`, les fichiers restent dans `data/` et le site les sert lui-même (mode local : tests, ou une seule machine).

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

Le **serveur** garde la file, le tri et Claude (avec ton compte). Chaque **moteur** (ce PC, le PC d'un ami) prend du travail :
- une **VOD** à analyser (chat, audio, Whisper sur sa carte graphique) : il renvoie les passages candidats et leur transcription
  (environ 100 Ko), Claude choisit sur le serveur ;
- une **tâche** : télécharger les extraits retenus (1080p, vignette), une vidéo ajoutée à la main, « 30 s avant », un export.

Une seule analyse à la fois par machine (2 Whisper sur la même carte ne vont pas plus vite : +24 % à 2, 0 % à 3) ; les autres
tâches passent en plus (3 au plus). Priorités : « 30 s avant », export, extraits, vidéos ajoutées, puis VODs. **L'export** se fait
sur le PC de la personne qui clique (même adresse que son navigateur), sinon sur le premier PC en ligne ; **Arrêter maintenant**
sur cette machine l'annule. Un redémarrage du site ne perd rien : les moteurs continuent et le site reprend leurs tâches.

Dans l'onglet **Machines** : chaque machine, la VOD qu'elle traite et l'étape en cours. **Arrêter après les fichiers en cours**
(elle finit puis ne prend plus rien), **Arrêter maintenant** (la VOD en cours repart en file pour une autre machine),
**Reprendre**, **Retirer** (son accès est supprimé). Une machine éteinte ou déconnectée rend ses VODs et ses tâches au bout de
2 min. Quand le serveur est injoignable, les moteurs attendent et reprennent seuls.

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
| `Espace`, `←` `→`, `M`, `+` `−` | pause, ±3 s, muet, volume |

## Fichiers

```
app.py            serveur web + API (bibliothèque standard Python), liens /files/ vers le stockage
s3.py             stockage : liens signés S3 (OVH) ou mode local ; python s3.py setup | push
vods.py           liste des VODs des streamers suivis
collect.py        site : Claude, fiches des extraits ; moteur : détection, téléchargement des extraits, ajouts, « 30 s avant »
ai.py             Whisper + Claude
transcribe_win.py transcription Whisper (processus séparé, GPU)
montage.py        ordre auto, alertes, description, CSV ; export : instantané (site), fabrication MP4 + FCPXML (moteur), fiche (site)
store.py, jobs.py base JSON (data/db.json) et tâches de fond
engines.py        files des moteurs côté serveur : VODs, tâches, baux, liens signés, résultats, machines
moteur.py         moteur (une VOD ou une tâche par processus enfant, arrêtable à tout moment)
moteur/           installation et lanceur du moteur pour Windows (PowerShell) et Mac
selftest.py       tests (.venv/bin/python selftest.py)
static/           dashboard
vendor/           modules repris de gta6-clipper (MIT)
data/             base, réglages, clés S3 (s3.json) : à sauvegarder (data/logs : journaux, data/moteur : moteur de ce PC)
```
