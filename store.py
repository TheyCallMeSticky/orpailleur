"""Petite base JSON (data/db.json) protégée par un verrou : sources, extraits, best-of en cours, archives, exports."""
import datetime
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BESTOF_DATA") or ROOT / "data").resolve()
MEDIA = DATA / "media"
CLIPS = MEDIA / "clips"
THUMBS = MEDIA / "thumbs"
EXPORTS = DATA / "exports"
TMP = DATA / "tmp"
INBOX = DATA / "inbox"
MUSIC = MEDIA / "music"  # musiques de fond (servies par /files/ pour les écouter)
for _d in (CLIPS, THUMBS, EXPORTS, TMP, INBOX, MUSIC):
    _d.mkdir(parents=True, exist_ok=True)

CATS = ("WTF", "FUN", "FAIL", "EPIC")
FLAGS = ("radio", "cinematic", "violence")
DB = DATA / "db.json"
CONF = DATA / "config.json"

lock = threading.RLock()
_db = None


def uid():
    return uuid.uuid4().hex[:10]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_project(n=1):
    # music : ids des musiques de fond dans l'ordre ; music_vol : volume en % ; transitions entre les extraits
    return {"id": uid(), "name": f"Best-of #{n}", "target": 600, "items": [], "created": now(),
            "music": [], "music_vol": 20, "transitions": True, "music_on": True}


def _empty():
    return {"sources": {}, "clips": {}, "project": new_project(1), "archive": [], "exports": [], "music": {}}


def load():
    global _db
    with lock:
        if _db is None:
            _db = json.loads(DB.read_text("utf-8")) if DB.exists() else _empty()
            for k, v in _empty().items():
                _db.setdefault(k, v)
            for k, v in new_project().items():  # best-of commencé avant la musique et les transitions
                _db["project"].setdefault(k, v)
        return _db


def save():
    with lock:
        tmp = DB.with_suffix(".tmp")
        tmp.write_text(json.dumps(_db, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, DB)


@contextmanager
def tx():
    """with tx() as db: ... -> modifications enregistrées à la sortie."""
    with lock:
        db = load()
        yield db
        save()


def config():
    c = json.loads(CONF.read_text("utf-8")) if CONF.exists() else {}
    before = dict(c)
    c.setdefault("token", secrets.token_urlsafe(18))
    c.setdefault("fps", "auto")          # auto | 30 | 60
    c.setdefault("lower_third", True)    # lien de la chaîne du streamer en haut à gauche de chaque extrait
    c.setdefault("crf", 18)
    c.setdefault("max_moments", 10)     # extraits par vidéo : au plus les 10 meilleurs
    c.setdefault("ai", True)            # Whisper + Claude choisissent les moments (repli automatique si indisponible)
    if c != before:  # n'écrit que la première fois : une réécriture à chaque lecture effaçait les jetons de machine ajoutés entre-temps
        CONF.write_text(json.dumps(c, ensure_ascii=False, indent=1), "utf-8")
    return c


def segs(c):
    """Morceaux gardés d'un extrait, en secondes dans son fichier : [[début, fin], ...] (un seul sans coupe)."""
    return c.get("segs") or [[c["in"], c["out"]]]


XF = 0.25  # fondu image + son entre deux morceaux d'un extrait coupé (les deux morceaux se chevauchent d'autant)


def joins(p):
    """Fondu entre deux morceaux d'un extrait coupé : 0,25 s pris dans la partie coupée (moins si elle est plus courte),
    pour que les morceaux gardés soient joués en entier."""
    return [min(XF, (p[k + 1][0] - p[k][1]) / 2) for k in range(len(p) - 1)]  # moitié du trou de chaque côté : rien n'est montré deux fois


def cdur(c):
    """Durée d'un extrait dans la vidéo : ses morceaux (après réglage et coupes), plus les fondus qui les relient (pris hors
    sélection)."""
    p = segs(c)
    return sum(max(0.0, float(b) - float(a)) for a, b in p) + sum(joins(p))


XT = 0.4  # transition entre deux extraits du best-of (ils se chevauchent d'autant)


def bestof_total(clips, transitions=True):
    """Durée finale du best-of : les extraits, plus 0,4 s par transition (prise juste avant et juste après chaque sélection,
    qui reste jouée en entier)."""
    return sum(cdur(c) for c in clips) + (XT * (len(clips) - 1) if transitions and clips else 0)


def day(t):
    """Jour (heure locale) de t, en secondes ou au format de now() : "today" | "yesterday" | "older"."""
    d = datetime.date.fromisoformat(t[:10]) if isinstance(t, str) else datetime.date.fromtimestamp(t)
    n = (datetime.date.today() - d).days
    return "today" if n <= 0 else "yesterday" if n == 1 else "older"
