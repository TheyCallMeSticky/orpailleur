"""Petite base JSON (data/db.json) protégée par un verrou : sources, extraits, best-of en cours, archives, exports."""
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
for _d in (CLIPS, THUMBS, EXPORTS, TMP, INBOX):
    _d.mkdir(parents=True, exist_ok=True)

CATS = ("WTF", "FUN", "FAIL", "EPIC")
AUTH = ("à demander", "demandée", "obtenue", "refusée")
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
    return {"id": uid(), "name": f"Best-of #{n}", "target": 600, "items": [], "created": now()}


def _empty():
    return {"sources": {}, "clips": {}, "project": new_project(1), "archive": [], "exports": []}


def load():
    global _db
    with lock:
        if _db is None:
            _db = json.loads(DB.read_text("utf-8")) if DB.exists() else _empty()
            for k, v in _empty().items():
                _db.setdefault(k, v)
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


def cdur(c):
    """Durée utile d'un extrait (après réglage début/fin)."""
    return max(0.0, float(c["out"]) - float(c["in"]))
