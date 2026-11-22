"""Dashboard best-of : python app.py [--port 8765] [--host 127.0.0.1]
Accès protégé par un lien privé (?k=jeton, voir data/config.json). Pour donner accès à ton pote : voir README.md."""
import argparse
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import threading

import collect
import engines
import jobs
import montage
import s3
import store
import vods

STATIC = store.ROOT / "static"
STARTED = time.time()  # le code Python n'est lu qu'au démarrage : s'il change après, le site affiche « redémarre »
LEASE_S = 180
LEASES = {}  # clip_id -> (utilisateur, expiration) : deux personnes ne trient pas le même extrait en même temps
CONF = engines.CONF  # un seul dict pour app.py et engines.py (jetons des machines, réglages)


def state():
    with store.lock:
        db = store.load()
        return {"clips": list(db["clips"].values()), "sources": list(db["sources"].values()), "project": db["project"],
                "archive": db["archive"][-20:], "exports": db["exports"][:20], "warnings": montage.warnings(db),
                "music": list(db["music"].values()),  # dans l'ordre d'ajout
                "jobs": jobs.listing(), "cats": store.CATS, "auth": store.AUTH,
                "settings": {k: CONF[k] for k in SETTINGS},
                "vods": vod_rows(db), "vods_scan": db.get("vods_scan"), "follow": vods.settings(),
                "machines": engines.machines_view(), "engine_conf": engines.engine_conf(), "hub_version": engines.VERSION,
                "stale": any(f.stat().st_mtime > STARTED for f in store.ROOT.glob("*.py"))}


SETTINGS = ("fps", "lower_third", "max_moments", "ai")


def vod_rows(db):
    """VODs < 24 h + leur état : nouvelle / en attente (file des moteurs) / en cours (sur une machine) / finalisation (Claude,
    puis extraits en attente d'un moteur) / analysée (n extraits)."""
    by_url = {s["url"]: s["id"] for s in db["sources"].values()}
    count = {}
    found = set()  # sources avec au moins un extrait trouvé par une analyse (pas seulement des passages ajoutés à la main)
    for c in db["clips"].values():
        d = count.setdefault(c["source_id"], {"new": 0, "kept": 0, "trash": 0})
        d[c["status"]] = d.get(c["status"], 0) + 1
        if not str(c.get("why") or "").startswith("ajouté"):
            found.add(c["source_id"])
    busy = {j.label: j.status for j in jobs.JOBS.values() if j.kind == "collect" and j.status in ("en attente", "en cours")}
    run, pend, turls = engines.running(), set(db.get("pending", [])), engines.task_urls()
    scan_t = time.mktime(time.strptime(db["vods_scan"], "%Y-%m-%d %H:%M:%S")) if db.get("vods_scan") else time.time()
    out = []
    for v in (db.get("vods") or {}).values():
        if v.get("restricted") or v.get("recording") or v.get("excluded"):  # réservée aux abonnés, ou stream encore en direct : rien à analyser
            continue
        v = dict(v)
        sid = by_url.get(v["url"])
        src = db["sources"].get(sid) or {}
        n = count.get(sid, {"new": 0, "kept": 0, "trash": 0})
        # 0 extrait = pas analysée ; un passage ajouté à la main ne compte pas, sinon la VOD ne serait jamais analysée
        done = any(a.get("clips") for a in src.get("analyses", [])) or sid in found
        if v["url"] in run:
            v["state"] = "en cours"
            v.update(run[v["url"]])  # machine, step
        elif engines.result_file(v["url"]).exists():
            v["state"] = "finalisation"
        elif v["url"] in turls:
            v["state"], v["step"] = "finalisation", "Téléchargement des extraits"
        elif v["url"] in pend:
            v["state"] = "en attente"
        else:  # busy : analyse lancée à la main (lien collé)
            v["state"] = busy.get(v["url"]) or ("analysée" if done else "nouvelle")
        v["source_id"], v["counts"], v["clips"] = sid, n, sum(n.values())
        v.update(db.get("vod_fails", {}).get(v["url"], {}))  # fails, last_error
        v["analyses"] = src.get("analyses", [])
        v["ended"] = round(scan_t - (v.get("age_h") or 0) * 3600)  # age_h : heures depuis la fin du stream, au moment du scan
        v["day"] = store.day(v["ended"])
        out.append(v)
    return sorted(out, key=lambda v: v.get("age_h") or 0)


def analyze_vods(urls):
    """Met des VODs dans la file (les plus courtes d'abord) : les moteurs viennent les y chercher. -> nombre ajouté."""
    n = 0
    with store.tx() as db:  # file d'attente gardée dans la base : un redémarrage la reprend
        p = db.setdefault("pending", [])
        for v in sorted(filter(None, (db.get("vods", {}).get(u) for u in urls)), key=lambda v: v.get("duration") or 0):
            if v["url"] not in p and not v.get("recording") and not v.get("restricted"):
                p.append(v["url"])
                n += 1
    return n


def auto_queue():
    """Après chaque recherche : les nouvelles VODs GTA partent en analyse sans attendre un clic."""
    with store.lock:
        rows = vod_rows(store.load())
    urls = [v["url"] for v in rows if v["state"] == "nouvelle" and v.get("gta") and (v.get("fails") or 0) < 2]
    n = analyze_vods(urls) if urls else 0
    if n:
        print(f"  {n} nouvelle(s) VOD(s) GTA mise(s) en analyse", flush=True)


def resume_pending():
    with store.tx() as db:  # une VOD sortie de la liste des 24 h pendant l'arrêt ne sera plus lancée : on la retire de la file
        p = db["pending"] = [u for u in db.get("pending", []) if u in db.get("vods", {})]
    n = engines.resume()
    if p or n:
        print(f"  {len(p)} VOD(s) en file pour les moteurs, {n} finalisation(s) reprise(s)", flush=True)


def scan_now():
    if any(j.kind == "scan" and j.status in ("en attente", "en cours") for j in jobs.JOBS.values()):
        return None
    return jobs.submit("scan", "scan", "VODs des dernières 24 h", vods.scan)


def _hourly():
    while True:
        try:
            j = scan_now()
            while j and j.status in ("en attente", "en cours"):
                time.sleep(5)
            auto_queue()
        except Exception as e:  # noqa: BLE001
            print("scan:", e, flush=True)
        time.sleep(3600)


def next_clips(user, skip, day=None, n=3):
    """day : "today" | "yesterday" | "older" (jour où l'extrait est arrivé) ; days = combien il en reste à trier par jour."""
    t = time.time()
    with store.lock:
        db = store.load()
        new = [c for c in db["clips"].values() if c["status"] == "new"]
        days = {"today": 0, "yesterday": 0, "older": 0}
        for c in new:
            days[store.day(c["added"])] += 1
        free = [c for c in new if c["id"] not in skip and (day not in days or store.day(c["added"]) == day)
                and not (c["id"] in LEASES and LEASES[c["id"]][0] != user and LEASES[c["id"]][1] > t)]
        free.sort(key=lambda c: (-(c.get("score") or 0), c["added"]))
        for k in [k for k, (u, _) in LEASES.items() if u == user]:
            del LEASES[k]
        if free:
            LEASES[free[0]["id"]] = (user, t + LEASE_S)
        out = []
        for c in free[:n]:
            c = dict(c)
            c["source"] = db["sources"].get(c["source_id"], {})
            out.append(c)
        return {"clips": out, "remaining": len(new), "days": days}


def norm_segs(pieces, dur):
    """Morceaux envoyés par le dashboard -> bornés au fichier, triés, fusionnés s'ils se chevauchent ou se touchent, sans miettes."""
    if not isinstance(pieces, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2
                                                and all(isinstance(x, (int, float)) for x in p) for p in pieces):
        raise ValueError("morceaux invalides")
    out = []
    for a, b in sorted((max(0.0, float(a)), min(float(dur), float(b))) for a, b in pieces):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        elif b > a:
            out.append([a, b])
    return [[round(a, 2), round(b, 2)] for a, b in out if b - a >= .3]


def patch_clip(cid, body, user):
    with store.tx() as db:
        c = db["clips"].get(cid)
        if not c:
            raise KeyError("extrait introuvable")
        pieces = None  # vérifiés avant toute modification : une erreur ne laisse rien à moitié changé
        if "segs" in body:
            pieces = norm_segs(body["segs"], c["file_dur"])
        elif "in" in body or "out" in body:  # ancien réglage début/fin : rogne (ou allonge) les morceaux existants
            a, b = body.get("in", c["in"]), body.get("out", c["out"])
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b - a >= .5:
                pieces = [list(p) for p in store.segs(c) if p[1] > a and p[0] < b] or [[a, b]]
                pieces[0][0], pieces[-1][1] = a, b
                pieces = norm_segs(pieces, c["file_dur"])
        if pieces == []:
            raise ValueError("aucun morceau gardé")
        if "status" in body and body["status"] in ("new", "kept", "trash"):
            c["status"] = body["status"]
            if c["status"] == "new":
                c["category"] = c["rating"] = None
            else:
                c["by"], c["sorted_at"] = user, store.now()
            if c["status"] != "kept":
                db["project"]["items"] = [i for i in db["project"]["items"] if i != cid]
        if body.get("category") in store.CATS:
            c["category"] = body["category"]
        if isinstance(body.get("rating"), int) and 1 <= body["rating"] <= 5:
            c["rating"] = body["rating"]
        if isinstance(body.get("flags"), dict):
            c["flags"] = {k: bool(v) for k, v in body["flags"].items() if k in store.FLAGS}
        if "note" in body and str(body["note"])[:500] != c.get("note", ""):
            c["note"] = str(body["note"])[:500]
            c["note_by"] = user if c["note"] else None  # qui a écrit la note (plusieurs personnes trient)
        if pieces:
            c["in"], c["out"] = pieces[0][0], pieces[-1][1]
            if len(pieces) > 1:
                c["segs"] = pieces
            else:
                c.pop("segs", None)
        LEASES.pop(cid, None)
        return c


def patch_project(body):
    with store.tx() as db:
        pj = db["project"]
        if isinstance(body.get("name"), str) and body["name"].strip():
            pj["name"] = body["name"].strip()[:120]
        if isinstance(body.get("target"), (int, float)) and 60 <= body["target"] <= 3600:
            pj["target"] = int(body["target"])
        if isinstance(body.get("items"), list):
            seen = []
            for i in body["items"]:
                if i in db["clips"] and db["clips"][i]["status"] == "kept" and i not in seen:
                    seen.append(i)
            pj["items"] = seen
        if isinstance(body.get("music"), list):
            pj["music"] = list(dict.fromkeys(m for m in body["music"] if isinstance(m, str) and m in db["music"]))[:5]
        if isinstance(body.get("music_vol"), (int, float)) and 0 <= body["music_vol"] <= 100:
            pj["music_vol"] = round(body["music_vol"])
        if isinstance(body.get("transitions"), bool):
            pj["transitions"] = body["transitions"]
        if isinstance(body.get("music_on"), bool):
            pj["music_on"] = body["music_on"]
        mode = body.get("auto")
        if mode == "order":
            pj["items"] = montage.auto_order([db["clips"][i] for i in pj["items"]], montage.streamer_of(db))
        elif mode == "fill":
            montage.fill(db)
        elif mode == "new":
            db["archive"].append({k: pj.get(k) for k in ("id", "name", "items", "created", "music", "music_vol", "transitions",
                                                          "music_on")})
            for i in pj["items"]:
                db["clips"][i]["used_in"] = db["clips"][i].get("used_in") or pj["name"]
            n = len(db["archive"]) + 1
            db["project"] = store.new_project(n)
        return db["project"]


def recv(rfile, size, dst):
    """Corps brut d'un envoi de fichier (size octets) -> dst (stockage local seulement)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as f:
        while size > 0:
            chunk = rfile.read(min(1 << 20, size))
            if not chunk:
                break
            f.write(chunk)
            size -= len(chunk)


MUSIC_EXT = ("mp3", "m4a", "aac", "wav", "ogg", "opus", "flac")
MUSIC_MAX = 60 << 20
UPLOAD_MAX = 5 << 30  # 5 Go : un seul envoi S3


def music_start(name, size, dur):
    """Musique de fond : format, poids et durée (lue par le navigateur, 10 s minimum) vérifiés avant l'envoi direct au
    stockage. -> lien d'envoi et fiche à confirmer avec music_done."""
    stem, ext = os.path.splitext(os.path.basename(str(name).replace("\\", "/")))
    ext = ext.lower().lstrip(".")
    if ext not in MUSIC_EXT:
        raise ValueError("Format de musique non pris en charge : MP3, M4A, AAC, WAV, OGG, OPUS ou FLAC.")
    if not isinstance(size, int) or not 0 < size <= MUSIC_MAX:
        raise ValueError("Musique trop lourde : 60 Mo maximum.")
    if not isinstance(dur, (int, float)) or dur != dur:
        raise ValueError("Ce fichier n'est pas une musique lisible.")
    if dur < 10:
        raise ValueError("Musique trop courte : 10 s minimum.")
    mid = store.uid()
    key = f"media/music/{mid}.{ext}"
    return {"id": mid, "key": key, "put": s3.url("PUT", key, 3600), "name": stem.strip()[:100] or "Musique", "dur": round(float(dur), 1)}


def music_done(b, by=""):
    mid, key = str(b.get("id") or ""), str(b.get("key") or "")
    if not re.fullmatch(r"\w{10}", mid) or not re.fullmatch(rf"media/music/{mid}\.(?:{'|'.join(MUSIC_EXT)})", key):
        raise ValueError("envoi inconnu")
    dur = b.get("dur")
    if not isinstance(dur, (int, float)) or not 10 <= dur <= 36000:
        raise ValueError("Musique trop courte : 10 s minimum.")
    t = {"id": mid, "name": str(b.get("name") or "Musique").strip()[:100] or "Musique", "file": key, "dur": round(float(dur), 1),
         "added": store.now(), "by": by}
    with store.tx() as db:
        db["music"][mid] = t
        pl = db["project"].setdefault("music", [])
        if len(pl) < 5:  # envoyée = ajoutée au best-of en cours (un clic sur la musique l'en retire)
            pl.append(mid)
    return t


def del_music(mid):
    with store.tx() as db:
        t = db["music"].pop(mid, None)
        if not t:
            raise KeyError("musique introuvable")
        db["project"]["music"] = [m for m in db["project"].get("music", []) if m != mid]
    s3.delete(t["file"])
    return {"ok": True}


def patch_source(sid, body):
    with store.tx() as db:
        s = db["sources"].get(sid)
        if not s:
            raise KeyError("source introuvable")
        for k in ("streamer", "streamer_url"):
            if isinstance(body.get(k), str):
                s[k] = body[k].strip()[:200]
        if body.get("auth") in store.AUTH:
            s["auth"] = body["auth"]
        return s


def check_add(mode, window):
    if mode not in (None, "whole", "auto"):
        raise ValueError("mode inconnu")
    collect.parse_window(window)


class H(BaseHTTPRequestHandler):
    server_version = "bestof/1"

    def log_message(self, *a):
        pass

    # ------------------------------------------------ réponses
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_text(self, text, ctype="text/plain; charset=utf-8", code=200, extra=None):
        b = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, p, download=False):
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".fcpxml":
            ctype = "application/xml"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        a, b = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                a = int(rng.group(1))
                b = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                a = max(0, size - int(rng.group(2)))
            b = min(b, size - 1)
            if a > b:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if download:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(p.name))
        self.end_headers()
        try:
            with p.open("rb") as f:
                f.seek(a)
                left = b - a + 1
                while left > 0:
                    chunk = f.read(min(1 << 18, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def signed_file(self, method, key, q):
        """Stockage local (sans S3) : lien signé par le site (s3.url). GET = lire, PUT = écrire (moteurs, envois du navigateur)."""
        e, t = q.get("e", [""])[0], q.get("t", [""])[0]
        if s3.conf() or not s3.ok_key(key) or not s3.local_ok(method, key, e, t):
            return self.send_json({"error": "lien invalide ou expiré"}, 403)
        p = store.DATA / key
        if method == "GET":
            return self.send_file(p, "dl" in q) if p.is_file() else self.send_json({"error": "fichier introuvable"}, 404)
        if method == "PUT":
            part = p.with_name(p.name + ".part")
            recv(self.rfile, int(self.headers.get("Content-Length") or 0), part)
            os.replace(part, p)
            return self.send_json({"ok": True})
        return self.send_json({"error": "méthode non prise en charge"}, 405)

    # ------------------------------------------------ accès
    def authed(self, q):
        tok = CONF["token"]
        if q.get("k", [""])[0] == tok:
            return "set"
        cookie = self.headers.get("Cookie") or ""
        return "ok" if re.search(r"(?:^|;\s*)bk=" + re.escape(tok) + r"(?:;|$)", cookie) else None

    def user(self):
        return urllib.parse.unquote(self.headers.get("X-User") or "")[:40] or "?"

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def add_video(self, label, n, window, streamer, mode, url=None, key=None, name=""):
        """Vidéo ajoutée à la main (lien, ou fichier déjà envoyé sur le stockage) : tâche « add » pour un moteur. mode "whole"
        (telle quelle dans le Tri) | "auto" (meilleurs moments) | None (vidéo courte = whole, sinon auto). Passage et mode
        vérifiés ici : une erreur de saisie répond tout de suite."""
        check_add(mode, window)
        by = "" if self.user() == "?" else self.user()
        with store.lock:
            taken = collect.taken_for(store.load(), url) if url else []
        data = {"input": url, "key": key, "name": name, "mode": mode, "window": window, "streamer": streamer, "max_n": n,
                "by": by, "ai": bool(CONF.get("ai", True)), "taken": taken, "cid": store.uid()}
        return engines.add_task("add", label, data).view()

    def ip(self):
        """Adresse du navigateur ou du moteur (tailscale serve la donne dans X-Forwarded-For) : même PC = même adresse."""
        return (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]

    def engine_route(self, method, path, q):
        """Moteurs : jeton de machine (en-tête X-Machine, ou ?m= pour le script d'installation) à la place du cookie."""
        if method == "GET" and path in ("/moteur/install.ps1", "/moteur/install.sh"):
            text = engines.install_script(path.rsplit("/", 1)[1], q.get("m", [""])[0])
            return self.send_text(text) if text else self.send_text("Lien d'installation invalide : demande un nouveau lien.\n", code=404)
        name = engines.machine_name(self.headers.get("X-Machine") or "")
        if not name:
            return self.send_json({"ok": False, "error": "Machine inconnue : elle a été retirée du site, ou le lien d'installation est faux."}, 401)
        fn = {"/api/engine/beat": lambda n, b: engines.beat(n, b, self.ip()), "/api/engine/result": engines.result,
              "/api/engine/release": engines.release, "/api/engine/sign": engines.sign, "/api/engine/done": engines.done}.get(path)
        if method != "POST" or not fn:
            return self.send_json({"ok": False, "error": "introuvable"}, 404)
        if int(self.headers.get("Content-Length") or 0) > 50 << 20:
            return self.send_json({"ok": False, "error": "envoi trop gros"}, 413)
        try:
            b = self.body()
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        if not isinstance(b, dict):
            return self.send_json({"ok": False, "error": "JSON attendu"}, 400)
        return self.send_json(fn(name, b))

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.startswith(("/api/engine/", "/moteur/")):
            return self.engine_route(method, u.path, q)
        if u.path.startswith("/files/") and "t" in q:  # stockage local : lien signé par le site (moteurs, envois)
            return self.signed_file(method, urllib.parse.unquote(u.path[len("/files/"):]), q)
        auth = self.authed(q)
        if not auth:
            return self.send_text("<h1>Accès privé</h1><p>Ouvre le lien complet (avec ?k=…) donné au lancement.</p>",
                                  "text/html; charset=utf-8", 401)
        path = u.path
        if auth == "set":  # première visite avec ?k= : cookie puis URL propre
            self.send_response(302)
            self.send_header("Set-Cookie", f"bk={CONF['token']}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")
            self.send_header("Location", path)
            self.end_headers()
            return
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.send_file(STATIC / "index.html")
            if method == "GET" and path.startswith(("/static/", "/fonts/")):  # logo, icône, police des titres
                base, _, name = path[1:].partition("/")
                name = urllib.parse.unquote(name)
                p = (STATIC if base == "static" else store.ROOT / "fonts") / name
                ok = name and "/" not in name and ".." not in name and p.is_file()
                return self.send_file(p) if ok else self.send_json({"error": "fichier introuvable"}, 404)
            if method == "GET" and path.startswith("/files/"):  # S3 : redirection vers un lien signé (12 h)
                rel = urllib.parse.unquote(path[len("/files/"):])
                if not s3.ok_key(rel):
                    return self.send_json({"error": "fichier introuvable"}, 404)
                if s3.conf():
                    self.send_response(302)
                    self.send_header("Location", s3.url("GET", rel, download=Path(rel).name if "dl" in q else None))
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    return
                p = (store.DATA / rel).resolve()
                ok = p.is_relative_to(store.DATA) and p.is_file()
                return self.send_file(p, "dl" in q) if ok else self.send_json({"error": "fichier introuvable"}, 404)
            if method == "GET" and path == "/api/state":
                return self.send_json(state())
            if method == "GET" and path == "/api/next":
                skip = set(filter(None, q.get("skip", [""])[0].split(",")))
                return self.send_json(next_clips(self.user(), skip, q.get("day", [""])[0]))
            if method == "GET" and path.startswith("/api/job/"):
                j = jobs.JOBS.get(path.rsplit("/", 1)[1])
                return self.send_json(j.view(full=True) if j else {"error": "inconnu"}, 200 if j else 404)
            if method == "GET" and path == "/api/description.txt":
                with store.lock:
                    return self.send_text(montage.description(store.load()))
            if method == "GET" and path == "/api/sources.csv":
                with store.lock:
                    return self.send_text(montage.sources_csv(store.load()), "text/csv; charset=utf-8",
                                          extra={"Content-Disposition": "attachment; filename=sources.csv"})
            if method == "POST" and path == "/api/collect":
                b = self.body()
                inp = str(b.get("input", "")).strip().strip('"')
                if not re.match(r"https?://", inp):
                    return self.send_json({"error": "Colle un lien (https://…), ou choisis un fichier."}, 400)
                n = max(1, min(50, int(b.get("max") or CONF["max_moments"] or 10)))
                return self.send_json(self.add_video(inp[:120], n, str(b.get("window") or ""),
                                                     str(b.get("streamer") or "").strip(), b.get("mode") or None, url=inp))
            if method == "POST" and path == "/api/scan":
                j = scan_now()
                return self.send_json(j.view() if j else {"status": "déjà en cours"})
            if method == "POST" and path == "/api/analyze":
                return self.send_json({"queued": analyze_vods([str(u) for u in self.body().get("urls", [])])})
            if method == "POST" and path == "/api/machines/add":
                return self.send_json(engines.add_machine(self.body().get("name")))
            if method == "POST" and path == "/api/invite/sort":  # trier seulement : pas de machine
                return self.send_json(engines.sort_message())
            if method == "POST" and path == "/api/machines/message":
                return self.send_json(engines.machine_message(str(self.body().get("name"))))
            if method == "POST" and path == "/api/machines/control":
                b = self.body()
                return self.send_json(engines.control(str(b.get("name")), b.get("action")))
            if method == "POST" and path == "/api/engine-settings":
                return self.send_json(engines.engine_settings(self.body()))
            if method == "POST" and path == "/api/follow":
                return self.send_json(vods.save_settings(self.body()))
            if method == "POST" and path == "/api/upload/start":  # fichier vidéo : le navigateur l'envoie au stockage
                b = self.body()
                check_add(b.get("mode") or None, str(b.get("window") or ""))  # avant d'envoyer le fichier
                if not isinstance(b.get("size"), int) or not 0 < b["size"] <= UPLOAD_MAX:
                    return self.send_json({"error": "Fichier trop lourd : 5 Go maximum."}, 400)
                key = f"inbox/{store.uid()}_{collect.safe_name(str(b.get('name') or 'video.mp4'))}"
                return self.send_json({"key": key, "put": s3.url("PUT", key, 6 * 3600)})
            if method == "POST" and path == "/api/upload/done":
                b = self.body()
                key = str(b.get("key") or "")
                if not re.fullmatch(r"inbox/\w{10}_[A-Za-z0-9._-]{1,80}", key):
                    return self.send_json({"error": "envoi inconnu"}, 400)
                n = max(1, min(50, int(b.get("max") or CONF["max_moments"] or 10)))
                name = key.split("_", 1)[1]
                return self.send_json(self.add_video(name, n, str(b.get("window") or ""), str(b.get("streamer") or "").strip(),
                                                     b.get("mode") or None, key=key, name=name))
            if method == "POST" and path == "/api/music/start":
                b = self.body()
                return self.send_json(music_start(b.get("name", ""), b.get("size"), b.get("dur")))
            if method == "POST" and path == "/api/music/done":
                return self.send_json(music_done(self.body(), "" if self.user() == "?" else self.user()))
            if method == "POST" and re.fullmatch(r"/api/music/\w+/delete", path):
                return self.send_json(del_music(path.split("/")[3]))
            if method == "POST" and path.startswith("/api/clip/") and path.endswith("/extend"):  # « Récupérer 30 s avant »
                d = collect.extend_request(path.split("/")[3])
                m = engines.export_machine(self.ip())  # le PC de la personne d'abord, comme pour l'export
                if not m:
                    return self.send_json({"error": "Aucun PC allumé : lance le moteur sur ton PC, puis réessaie."}, 400)
                return self.send_json(engines.add_task("extend", f"30 s avant : {d['streamer'] or d['cid']}", d, m).view())
            if method == "POST" and path.startswith("/api/clip/"):
                return self.send_json(patch_clip(path.rsplit("/", 1)[1], self.body(), self.user()))
            if method == "POST" and path.startswith("/api/source/"):
                return self.send_json(patch_source(path.rsplit("/", 1)[1], self.body()))
            if method == "POST" and path == "/api/project":
                return self.send_json(patch_project(self.body()))
            if method == "POST" and path == "/api/export":  # fabriqué par un PC : celui de la personne, sinon le premier en ligne
                m = engines.export_machine(self.ip())
                if not m:
                    return self.send_json({"error": "Aucun PC allumé : lance le moteur sur ton PC (ou demande à un ami), puis réessaie."}, 400)
                d = montage.snapshot()
                j = engines.add_task("export", f"{d['db']['project']['name']} — sur {m}", d, m)
                j.log(f"Demandé depuis {self.ip()} : export sur {m}")
                return self.send_json(j.view())
            if method == "POST" and path == "/api/settings":
                b = self.body()
                if str(b.get("fps")) in ("auto", "30", "60"):
                    CONF["fps"] = str(b["fps"])
                if "lower_third" in b:
                    CONF["lower_third"] = bool(b["lower_third"])
                if "ai" in b:
                    CONF["ai"] = bool(b["ai"])
                if isinstance(b.get("max_moments"), int) and 1 <= b["max_moments"] <= 50:
                    CONF["max_moments"] = b["max_moments"]
                engines.save_conf()
                return self.send_json({k: CONF[k] for k in SETTINGS})
            return self.send_json({"error": "introuvable"}, 404)
        except KeyError as e:
            return self.send_json({"error": str(e)}, 404)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({"error": str(e)}, 400)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")


ENGINE = [None]  # processus du moteur de ce PC


def local_engine(port):
    """Moteur de ce PC : un processus à part, comme chez les amis ; relancé 10 s après s'il s'arrête."""
    env = dict(os.environ, BESTOF_DATA=str(store.DATA / "moteur"), PYTHONUTF8="1")
    cmd = [sys.executable, str(store.ROOT / "moteur.py"), "--hub", f"http://127.0.0.1:{port}", "--token", engines.local_token()]
    while True:
        ENGINE[0] = subprocess.Popen(cmd, env=env)
        ENGINE[0].wait()
        print("  Moteur de ce PC arrêté : relance dans 10 s", flush=True)
        time.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 pour le rendre visible sur le réseau (VPS)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-scan", action="store_true", help="ne pas chercher les VODs automatiquement")
    ap.add_argument("--no-engine", action="store_true", help="ne pas lancer le moteur d'analyse de ce PC")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # kill : passe aussi par le finally (arrêt du moteur de ce PC)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    if not a.no_engine:
        engines.local_token()  # la machine « Ce PC » (son moteur est lancé par ce site)
    resume_pending()
    if not a.no_engine:
        threading.Thread(target=local_engine, args=(a.port,), daemon=True).start()
    if not a.no_scan:
        threading.Thread(target=_hourly, daemon=True).start()  # liste des VODs au démarrage puis toutes les heures
    link = f"http://localhost:{a.port}/?k={CONF['token']}"
    print(f"\n  Best-of GTA 6 — dashboard prêt\n  Lien privé : {link}\n  Données     : {store.DATA}\n  (Ctrl+C pour arrêter)\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Arrêt.")
        sys.exit(0)
    finally:  # le moteur arrête lui-même ses analyses en cours
        if ENGINE[0] and ENGINE[0].poll() is None:
            ENGINE[0].terminate()


if __name__ == "__main__":
    main()
