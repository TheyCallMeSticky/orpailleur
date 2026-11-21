"""Moteurs : les machines (ce PC, celles des amis) qui analysent les VODs en file. Le hub (app.py) garde la file
(db["pending"]) et prête chaque VOD à un moteur par un bail renouvelé à chaque battement (toutes les 5 s). Le moteur renvoie
candidats + transcription ; le hub finit lui-même (Claude, téléchargement des extraits) dans une tâche « finish »."""
import hashlib
import json
import math
import os
import re
import secrets
import socket
import time
from pathlib import Path

import collect
import jobs
import store

PROTOCOL = 1
LEASE_S = 120   # bail non renouvelé pendant 2 min (moteur éteint, réseau coupé) : la VOD repart en file
GRACE_S = 40    # hub (re)démarré : les moteurs reprennent d'abord leurs VODs en cours (réessai 10 s + délai 20 s) avant toute distribution
STARTED = time.time()
STREAK_MAX = 3  # échecs de suite sur une même machine : elle est mise en pause (sinon elle grillerait toute la file)
STREAK = {}     # nom -> échecs « génériques » de suite (en mémoire)
CONF = store.config()  # partagé avec app.py (un seul dict, sinon une écriture effacerait l'autre)
MACHINES = {}   # nom -> {last_seen, os, gpu, version, slots} (en mémoire)
LEASES = {}     # bail -> {url, machine, until, vod, given, seen, step, line, started}
DEAD = set()    # shortcut: baux arrêtés depuis le site, jamais vidé (quelques ids par VOD), à purger si le hub tourne des mois
RESULTS = store.DATA / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
LOG_MAX = 500_000
NAME = re.compile(r"[\w .()-]{1,40}")  # sans guillemets ni $ : le nom est recopié dans les scripts d'installation
jobs.WORKERS["finish"] = 2


def code_version():
    """Empreinte du code d'analyse (même fonction dans moteur.py) : une machine qui diffère doit relancer son moteur."""
    h = hashlib.sha1()
    for f in ("collect.py", "ai.py", "moteur.py", "engines.py"):
        p = store.ROOT / f
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:8]


VERSION = code_version()


def save_conf():
    with store.lock:  # écriture atomique : collect relit config.json pendant une finalisation
        tmp = store.CONF.with_suffix(".tmp")
        tmp.write_text(json.dumps(CONF, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, store.CONF)


def local_token():
    """Jeton du moteur de ce PC (créé au premier lancement)."""
    with store.lock:
        ms = CONF.setdefault("machines", {})
        tok = next((t for t, m in ms.items() if m.get("local")), None)
        if not tok:
            tok = secrets.token_urlsafe(18)
            ms[tok] = {"name": f"Ce PC ({socket.gethostname()})", "created": store.now(), "local": True}
            save_conf()
        return tok


def machine_name(tok):
    m = CONF.get("machines", {}).get(tok) if tok else None
    return m["name"] if m else None


def result_file(url):
    return RESULTS / f"{hashlib.sha1(url.encode()).hexdigest()[:12]}.json"


def _s(x, n=300):
    return "" if x is None else str(x)[:n]


def _f(x, lo, hi):
    """Nombre fini ramené dans [lo, hi] (lo s'il est illisible)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return lo
    return min(hi, max(lo, x)) if math.isfinite(x) else lo


def _expire(t):
    for k in [k for k, L in LEASES.items() if L["until"] < t]:
        del LEASES[k]


def _busy(url):
    """Déjà prêtée à un moteur ou en finalisation."""
    return any(L["url"] == url for L in LEASES.values()) or result_file(url).exists()


def _mark(url, err):
    """Échec gardé (affiché dans Collecte) ; après 2 échecs la VOD n'est plus relancée toute seule. À part de db["vods"],
    que chaque recherche reconstruit : une chaîne injoignable une fois ne remet pas le compteur à zéro."""
    with store.tx() as db:
        fails = db.setdefault("vod_fails", {})
        if err:
            fails[url] = {"fails": fails.get(url, {}).get("fails", 0) + 1, "last_error": str(err)[:500]}
        else:
            fails.pop(url, None)


# ---------------------------------------------------------------- API des moteurs (en-tête X-Machine)

def beat(name, b):
    """Battement d'un moteur : renouvelle ses baux, dit lesquels tuer, lui donne des VODs s'il en veut."""
    if b.get("protocol") != PROTOCOL:
        return {"ok": False, "error": "Mise à jour nécessaire : ferme le moteur et relance-le."}
    t = time.time()
    with store.lock:
        db = store.load()
        _expire(t)
        MACHINES[name] = {"last_seen": t, "os": _s(b.get("os"), 80), "gpu": _s(b.get("gpu"), 120),
                          "version": _s(b.get("version"), 20), "slots": int(_f(b.get("slots"), 1, 16))}
        pending, vods, kill, listed = db.setdefault("pending", []), db.get("vods") or {}, [], set()
        for j in (b.get("jobs") if isinstance(b.get("jobs"), list) else [])[:32]:
            lid = _s(j.get("lease"), 40) if isinstance(j, dict) else ""
            if not lid:
                continue
            L, url = LEASES.get(lid), _s(j.get("url"), 500)
            if not L and lid not in DEAD and url in pending and url in vods and not _busy(url):  # hub relancé : on reprend le bail
                L = LEASES[lid] = {"url": url, "machine": name, "vod": dict(vods[url]), "given": t}
            if not L or L["machine"] != name:
                kill.append(lid)
                continue
            listed.add(lid)
            L.update(until=t + LEASE_S, seen=True, step=_s(j.get("step"), 80), line=_s(j.get("line"), 300),
                     started=_f(j.get("started"), 0, t + 60))
        # donné mais jamais lancé par le moteur : la VOD repart en file. Un bail déjà vu puis absent (VOD finie, résultat en
        # route) n'est plus renouvelé et expire seul : il n'est pas redonné avant l'arrivée du résultat.
        for k in [k for k, L in LEASES.items() if L["machine"] == name and k not in listed and not L.get("seen")
                  and t - L["given"] > 15]:
            del LEASES[k]
        paused = (db.get("engines") or {}).get(name, {}).get("paused", False)
        want = 0 if paused or t - STARTED < GRACE_S else int(_f(b.get("want"), 0, 8))
        give, pruned = [], False
        for u in list(pending):
            if len(give) >= want:
                break
            if _busy(u):
                continue
            v = vods.get(u)
            if not v:  # sortie de la liste des 24 h : plus rien à analyser
                pending.remove(u)
                pruned = True
                continue
            lid = secrets.token_urlsafe(9)
            LEASES[lid] = {"url": u, "machine": name, "vod": dict(v), "given": t, "until": t + LEASE_S}
            give.append({"lease": lid, "url": u, "input": v.get("dl_url") or u, "title": v.get("title", ""),
                         "streamer": v.get("streamer", ""), "max_n": int(CONF.get("max_moments") or 10),
                         "ai": bool(CONF.get("ai", True)), "taken": collect.taken_for(db, u)})
        if pruned:
            store.save()
    return {"ok": True, "name": name, "paused": paused, "kill": kill, "give": give}


def _clean(r):
    """RESULT envoyé par un moteur (non fiable) : seulement les champs connus, textes tronqués, nombres finis et bornés."""
    m = r["meta"]
    if not isinstance(m, dict):
        raise ValueError("meta absent")
    meta = {k: _s(m.get(k)) for k in ("title", "channel", "creator", "uploader", "uploader_id", "live_status")}
    meta.update({k: _s(m.get(k)) if _s(m.get(k)).startswith("https://") else "" for k in ("channel_url", "uploader_url")})
    meta.update(duration=_f(m.get("duration"), 0, 1e6), width=int(_f(m.get("width"), 0, 1e5)),
                height=int(_f(m.get("height"), 0, 1e5)), was_live=bool(m.get("was_live")))
    lo = _f(r.get("lo"), 0, meta["duration"] or 1e6)
    hi = _f(r.get("hi"), lo, meta["duration"] or 1e6)

    def groups(gs, n):
        out = []
        for g in gs[:n] if isinstance(gs, list) else []:
            if isinstance(g, dict):
                t0 = _f(g.get("t0"), lo, hi)  # au plus 60 s, comme pick() : sinon un moteur ferait télécharger une VOD entière
                out.append({"t0": t0, "t1": min(_f(g.get("t1"), t0, hi), t0 + collect.MAX_LEN), "score": _f(g.get("score"), -1e3, 1e3), "why": _s(g.get("why"), 500)})
        return out
    auto, cands, trans = groups(r.get("auto"), int(CONF.get("max_moments") or 10)), groups(r.get("cands"), 150), r.get("trans")
    if not auto:
        raise ValueError("aucun moment")
    if cands and isinstance(trans, list) and len(trans) == len(cands) and all(isinstance(w, list) for w in trans):
        trans = [[[_f(s[0], lo, hi), _f(s[1], lo, hi), _s(s[2], 2000)] for s in w[:400] if isinstance(s, list) and len(s) == 3]
                 for w in trans]
    else:  # transcription absente ou qui ne correspond pas aux candidats : sélection automatique
        cands = trans = None
    return {"meta": meta, "lo": lo, "hi": hi, "signals": int(_f(r.get("signals"), 0, 1e6)), "auto": auto, "cands": cands,
            "trans": trans}


def result(name, b):
    """VOD finie sur un moteur : résultat gardé dans data/results puis finalisé ici, ou échec noté."""
    lid = _s(b.get("lease"), 40)
    with store.lock:
        db = store.load()
        _expire(time.time())
        L = LEASES.get(lid)
        if L and L["machine"] == name:
            url, vod = L["url"], L["vod"]
        else:  # bail expiré (réseau coupé, hub relancé) : accepté si personne d'autre n'a repris la VOD
            url = _s(b.get("url"), 500)
            if L or lid in DEAD or url not in db.get("pending", []) or url not in (db.get("vods") or {}) or _busy(url):
                return {"ok": False, "error": "perdu"}
            vod = dict(db["vods"][url])
        LEASES.pop(lid, None)
        err, code = _s(b.get("error")) or "échec sans message", b.get("code") if b.get("code") in ("restricted", "vertical") else None
        if b.get("ok") is True:
            try:
                res = _clean(b.get("result"))
            except (TypeError, KeyError, ValueError, IndexError) as e:
                err, code = f"résultat du moteur illisible ({e})", None
            else:
                steps = [[_s(s[0], 80), _f(s[1], 0, 1e6)] for s in (b.get("steps") or [])[:40]
                         if isinstance(s, list) and len(s) == 2] if isinstance(b.get("steps"), list) else []
                p = result_file(url)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps({"url": url, "machine": name, "vod": vod, "result": res, "steps": steps,
                                           "total": _f(b.get("total"), 0, 1e6), "log": _s(b.get("log"), LOG_MAX)},
                                          ensure_ascii=False), "utf-8")
                os.replace(tmp, p)
                jobs.submit("finish", "finish", vod.get("title") or url, finish_job, str(p))
                STREAK[name] = 0
                return {"ok": True}
        d = store.DATA / "logs" / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{time.strftime('%H%M%S')}_moteur_{store.uid()}.log"
        f.write_text(_s(b.get("log"), LOG_MAX), "utf-8")
        _mark(url, f"{err} (sur {name} ; journal : {f.relative_to(store.DATA)})")
        STREAK[name] = 0 if code else STREAK.get(name, 0) + 1
        with store.tx() as db:
            if STREAK[name] >= STREAK_MAX:  # problème sur cette machine (ffmpeg, réseau, disque…) plutôt que sur les VODs
                db.setdefault("engines", {})[name] = {"paused": True, "reason": f"mise en pause après {STREAK_MAX} échecs de suite"
                                                      f" (dernier : {err[:150]}) — journal : {f.relative_to(store.DATA)}"}
                STREAK[name] = 0
            v = (db.get("vods") or {}).get(url)
            if v and code == "restricted":
                v["restricted"] = True  # retirée de la liste : on ne prend que les VODs gratuites
            elif v and code == "vertical":
                v["excluded"] = "verticale"
            if url in db.get("pending", []):
                db["pending"].remove(url)
    return {"ok": True}


def release(name, b):
    """Moteur arrêté (Ctrl+C, fenêtre fermée) : ses VODs repartent en file tout de suite."""
    with store.lock:
        for lid in (b.get("leases") if isinstance(b.get("leases"), list) else [])[:32]:
            if isinstance(lid, str) and LEASES.get(lid, {}).get("machine") == name:
                del LEASES[lid]
    return {"ok": True}


def finish_job(job, path):
    """Tâche « finish » sur le hub : Claude choisit parmi les candidats du moteur, puis téléchargement des extraits."""
    p = Path(path)
    try:
        d = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        p.unlink(missing_ok=True)  # illisible : sinon reprise (et échec) à chaque démarrage
        raise
    url, vod = d["url"], d["vod"]
    try:
        for line in (d.get("log") or "").splitlines():
            job.log("   [moteur]", line)
        res = dict(d["result"], steps=d.get("steps") or [], total=d.get("total") or 0)
        collect.CTX.update(streamer_url=vod.get("streamer_url", ""), page_url=vod["url"] if vod.get("dl_url") else "",
                           title=vod.get("title", ""))
        n = collect.finish(job, vod.get("dl_url") or vod["url"], res, int(CONF.get("max_moments") or 10),
                           vod.get("streamer", ""), machine=d.get("machine", ""))
        _mark(url, None)
        return n
    except Exception as e:
        _mark(url, f"{str(e)[:300]} (journal : {job.file.relative_to(store.DATA)})")
        raise
    finally:  # terminée ou en erreur : elle sort de la file persistante
        with store.tx() as db:
            if url in db.get("pending", []):
                db["pending"].remove(url)
        p.unlink(missing_ok=True)


def resume():
    """Démarrage du hub : les résultats reçus mais pas encore finalisés repartent en finalisation."""
    n = 0
    for p in sorted(RESULTS.glob("*.json")):
        try:
            d = json.loads(p.read_text("utf-8"))
            label = d["vod"].get("title") or d["url"]
        except (OSError, ValueError, KeyError, AttributeError):
            label = p.name
        jobs.submit("finish", "finish", label, finish_job, str(p))
        n += 1
    return n


# ---------------------------------------------------------------- dashboard (cookie)

def running():
    """url -> machine et étape, pour la liste des VODs."""
    with store.lock:
        _expire(time.time())
        return {L["url"]: {"machine": L["machine"], "step": L.get("step", "")} for L in LEASES.values()}


def engine_conf():
    return {"public_url": CONF.get("public_url", ""), "github": CONF.get("github", "")}


def machines_view():
    t = time.time()
    with store.lock:
        _expire(t)
        eng = store.load().get("engines") or {}
        out = []
        for m in CONF.get("machines", {}).values():
            n, s = m["name"], MACHINES.get(m["name"], {})
            out.append({"name": n, "local": bool(m.get("local")), "online": t - s.get("last_seen", 0) < 20,
                        "last_seen": s.get("last_seen"), "os": s.get("os", ""), "gpu": s.get("gpu", ""),
                        "version": s.get("version", ""), "version_ok": s.get("version") == VERSION,
                        "paused": eng.get(n, {}).get("paused", False), "reason": eng.get(n, {}).get("reason", ""),
                        "jobs": [{"url": L["url"], "title": L["vod"].get("title", ""), "streamer": L["vod"].get("streamer", ""),
                                  "step": L.get("step", ""), "line": L.get("line", ""), "started": L.get("started") or L["given"]}
                                 for L in LEASES.values() if L["machine"] == n]})
        return sorted(out, key=lambda m: (not m["local"], m["name"].lower()))


def add_machine(name):
    name = _s(name, 100).strip()
    if not NAME.fullmatch(name):
        raise ValueError("Nom invalide : lettres, chiffres, espaces et - _ . ( ), 40 caractères au plus.")
    hub, repo = CONF.get("public_url"), CONF.get("github")
    if not hub or not repo:
        raise ValueError("Renseigne d'abord l'adresse Tailscale du serveur et le dépôt GitHub.")
    with store.lock:
        if any(m["name"].lower() == name.lower() for m in CONF.setdefault("machines", {}).values()):
            raise ValueError(f"Le nom « {name} » est déjà pris.")
        tok = secrets.token_urlsafe(18)
        CONF["machines"][tok] = {"name": name, "created": store.now(), "local": False}
        save_conf()
    return {"name": name, "token": tok, "win": f'irm "{hub}/moteur/install.ps1?m={tok}" | iex',
            "mac": f'curl -fsSL "{hub}/moteur/install.sh?m={tok}" | bash'}


def control(name, action):
    """pause = finir les fichiers en cours puis rien de plus ; stop_now = pause + tuer les fichiers en cours (ils repartent en
    file, leur résultat sera refusé) ; resume ; remove = stop_now + jeton supprimé."""
    if action not in ("pause", "stop_now", "resume", "remove"):
        raise ValueError("action inconnue")
    with store.lock:
        tok = next((k for k, m in CONF.get("machines", {}).items() if m["name"] == name), None)
        if not tok:
            raise KeyError("machine introuvable")
        if action == "remove" and CONF["machines"][tok].get("local"):
            raise ValueError("Le moteur de ce PC ne peut pas être retiré : mets-le en pause.")
        with store.tx() as db:
            e = db.setdefault("engines", {})
            if action == "remove":
                e.pop(name, None)
            else:
                e[name] = {"paused": action != "resume"}
        if action in ("stop_now", "remove"):
            for k in [k for k, L in LEASES.items() if L["machine"] == name]:
                DEAD.add(k)  # le moteur le tue au prochain battement
                del LEASES[k]
        if action == "remove":
            del CONF["machines"][tok]
            MACHINES.pop(name, None)
            save_conf()
    return {"ok": True}


def engine_settings(b):
    url = _s(b.get("public_url", CONF.get("public_url", "")), 200).strip().rstrip("/")
    repo = re.sub(r"^(https?://)?github\.com/", "", _s(b.get("github", CONF.get("github", "")), 200).strip()).removesuffix(".git").strip("/")
    if url and not re.fullmatch(r"https?://[\w.:-]+(/[\w.-]*)*", url):
        raise ValueError("Adresse invalide : elle commence par https:// (ex. https://bestof.tailXXXX.ts.net)")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Dépôt GitHub invalide : pseudo/depot")
    with store.lock:
        CONF.update(public_url=url, github=repo)
        save_conf()
    return engine_conf()


def install_script(fname, tok):
    """Script d'installation d'un ami (?m=<jeton>), avec ses réglages déjà dedans. None si jeton ou fichier inconnu."""
    m = CONF.get("machines", {}).get(tok)
    f = store.ROOT / "moteur" / fname
    if not m or not f.exists():
        return None
    return (f.read_text("utf-8").replace("{{HUB}}", CONF.get("public_url", "")).replace("{{TOKEN}}", tok)
            .replace("{{NAME}}", m["name"]).replace("{{REPO}}", CONF.get("github", "")))
