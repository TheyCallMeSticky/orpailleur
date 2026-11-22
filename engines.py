"""Moteurs : les machines (PC des amis, ce PC) qui font TOUT le travail sur les vidéos. Le site (app.py) garde deux files :
db["pending"] (VODs à analyser) et db["tasks"] (extraits à télécharger, vidéos ajoutées à la main, « 30 s avant », exports),
et prête chaque élément à un moteur par un bail renouvelé à chaque battement (toutes les 5 s). Le site ne fait que Claude
(choix des moments, tâche « finish ») et la base ; les fichiers vont sur le stockage (s3.py) par des liens signés."""
import hashlib
import json
import math
import os
import re
import secrets
import socket
import threading
import time
import traceback
from pathlib import Path

import collect
import jobs
import montage
import s3
import store

PROTOCOL = 2  # 2 : tâches (extraits, ajouts, « 30 s avant », exports) faites par les moteurs
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
PRIO = {"extend": 0, "export": 1, "clips": 2, "add": 3}  # « 30 s avant » : quelqu'un attend devant l'extrait
SLOT = ("add",)  # occupe une place du moteur (analyse, Whisper) comme une VOD ; les autres tâches passent toujours
NOSLOT_MAX = 3   # tâches hors place en même temps sur un moteur
TRACK = {}       # tâche -> Job affiché sur le site
JOB_KIND = {"add": "collect", "extend": "extend", "export": "export"}  # « collect » : listé dans Vidéos > ajoutées à la main
jobs.WORKERS["finish"] = 2


def code_version():
    """Empreinte du code d'analyse (même fonction dans moteur.py) : une machine qui diffère doit relancer son moteur."""
    h = hashlib.sha1()
    for f in ("collect.py", "ai.py", "moteur.py", "engines.py", "montage.py", "store.py"):
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
        _drop(k, f"{LEASES[k]['machine']} ne répond plus")


def _drop(k, why):
    """Bail retiré : une VOD repart en file d'elle-même (db["pending"]) ; une tâche attend un autre PC."""
    L = LEASES.pop(k)
    j = TRACK.get(L.get("task"))
    if j and j.status == "en cours":
        j.status = "en attente"
        j.log(f"{why} : en attente d'un autre PC…")


def _busy(url):
    """Déjà prêtée à un moteur, en finalisation (Claude) ou ses extraits en cours de téléchargement."""
    return (any(L["url"] == url for L in LEASES.values()) or result_file(url).exists()
            or url in task_urls())


def task_urls():
    """VODs dont les extraits attendent un moteur (tâche « clips »)."""
    return {t["data"].get("vod_url") for t in (store.load().get("tasks") or {}).values()} - {None}


def _leased(tid):
    return any(L.get("task") == tid for L in LEASES.values())


def _task_lease(task, name, t):
    d = task["data"]
    return {"task": task["id"], "kind": task["kind"], "url": d.get("vod_url") or f"task:{task['id']}", "machine": name,
            "vod": {"title": task["label"], "streamer": d.get("streamer", "")}, "given": t, "until": t + LEASE_S}


def _for(task, name, t):
    """Tâche pour ce moteur ? Celles qui visent une machine (export, « 30 s avant ») vont à n'importe quel PC si elle ne
    répond plus depuis 30 s."""
    target = task.get("machine")
    return not target or target == name or t - MACHINES.get(target, {}).get("last_seen", 0) > 30


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

def beat(name, b, ip=""):
    """Battement d'un moteur : renouvelle ses baux, dit lesquels tuer, lui donne des tâches puis des VODs s'il en veut."""
    if b.get("protocol") != PROTOCOL:
        return {"ok": False, "error": "Mise à jour nécessaire : ferme le moteur et relance-le."}
    t = time.time()
    with store.lock:
        db = store.load()
        _expire(t)
        MACHINES[name] = {"last_seen": t, "os": _s(b.get("os"), 80), "gpu": _s(b.get("gpu"), 120),
                          "version": _s(b.get("version"), 20), "slots": int(_f(b.get("slots"), 1, 16)), "ip": _s(ip, 60)}
        pending, vods, kill, listed = db.setdefault("pending", []), db.get("vods") or {}, [], set()
        tasks = db.get("tasks") or {}
        for j in (b.get("jobs") if isinstance(b.get("jobs"), list) else [])[:32]:
            lid = _s(j.get("lease"), 40) if isinstance(j, dict) else ""
            if not lid:
                continue
            L, url, tid = LEASES.get(lid), _s(j.get("url"), 500), _s(j.get("task"), 40)
            if not L and lid not in DEAD:  # site relancé : on reprend le bail
                if tid:
                    if tid in tasks and not tasks[tid].get("done") and not _leased(tid):
                        L = LEASES[lid] = _task_lease(tasks[tid], name, t)
                elif url in pending and url in vods and not _busy(url):
                    L = LEASES[lid] = {"url": url, "machine": name, "vod": dict(vods[url]), "given": t}
            if not L or L["machine"] != name:
                kill.append(lid)
                continue
            listed.add(lid)
            step = _s(j.get("step"), 80)
            if L.get("task") and step and step != L.get("step") and TRACK.get(L["task"]):
                TRACK[L["task"]].log("▶", step)  # étapes du moteur en direct (le journal complet arrive à la fin)
            L.update(until=t + LEASE_S, seen=True, step=step, line=_s(j.get("line"), 300), started=_f(j.get("started"), 0, t + 60))
        # donné mais jamais lancé par le moteur : il repart en file. Un bail déjà vu puis absent (fini, résultat en route)
        # n'est plus renouvelé et expire seul : il n'est pas redonné avant l'arrivée du résultat.
        for k in [k for k, L in LEASES.items() if L["machine"] == name and k not in listed and not L.get("seen")
                  and t - L["given"] > 15]:
            _drop(k, f"{name} ne l'a pas commencée")
        paused = (db.get("engines") or {}).get(name, {}).get("paused", False)
        give, pruned = [], False
        if not paused and t - STARTED >= GRACE_S:
            want = int(_f(b.get("want"), 0, 8))  # places libres : VODs et vidéos ajoutées à analyser (Whisper)
            busy = sum(1 for L in LEASES.values() if L["machine"] == name and L.get("task") and L["kind"] not in SLOT)
            for task in sorted(tasks.values(), key=lambda x: (PRIO.get(x["kind"], 9), x["added"])):
                if task.get("done") or _leased(task["id"]) or not _for(task, name, t):
                    continue
                if task["kind"] in SLOT:
                    if want <= 0:
                        continue
                    want -= 1
                elif busy >= NOSLOT_MAX:
                    continue
                else:
                    busy += 1
                lid = secrets.token_urlsafe(9)
                L = LEASES[lid] = _task_lease(task, name, t)
                give.append({"lease": lid, "task": task["id"], "kind": task["kind"], "url": L["url"], "title": task["label"],
                             "streamer": L["vod"]["streamer"], "data": task["data"]})
                jt = TRACK.get(task["id"])
                if jt:
                    jt.status, jt.t0 = "en cours", jt.t0 or t
                    jt.log(f"Prise par {name}")
            for u in list(pending):
                if want <= 0:
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
                want -= 1
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
                _drop(lid, f"{name} s'est arrêté")
    return {"ok": True}


def finish_job(job, path):
    """Tâche « finish » sur le site : Claude choisit parmi les candidats du moteur, puis un moteur télécharge les extraits
    (tâche « clips »)."""
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
                           title=vod.get("title", ""), added_by="")
        plan = collect.finish(job, vod.get("dl_url") or vod["url"], res, int(CONF.get("max_moments") or 10),
                              vod.get("streamer", ""), machine=d.get("machine", ""))
        if plan:  # même id pour une même VOD : une finalisation rejouée ne crée pas une 2e tâche
            add_task("clips", vod.get("title") or url, dict(plan, vod_url=url, streamer=vod.get("streamer", "")),
                     tid="v" + hashlib.sha1(url.encode()).hexdigest()[:12])
        else:
            _mark(url, None)
        return len(plan["picks"]) if plan else 0
    except Exception as e:
        _mark(url, f"{str(e)[:300]} (journal : {job.file.relative_to(store.DATA)})")
        raise
    finally:  # Claude a choisi (ou erreur) : elle sort de la file des analyses
        with store.tx() as db:
            if url in db.get("pending", []):
                db["pending"].remove(url)
        p.unlink(missing_ok=True)


def resume():
    """Démarrage du site : les résultats reçus mais pas encore finalisés repartent en finalisation ; les tâches en file
    réapparaissent sur le site, celles finies sur un moteur mais pas encore enregistrées le sont."""
    with store.lock:
        tasks = list((store.load().get("tasks") or {}).values())
    for t in tasks:
        if not t.get("done"):
            _track(t)
        elif (RESULTS / f"task_{t['id']}.json").exists():
            _track(t)
            threading.Thread(target=_handle, args=(t["id"],), daemon=True).start()
        else:
            cancel(t["id"], "résultat perdu")
    n = 0
    for p in sorted(RESULTS.glob("*.json")):
        try:
            d = json.loads(p.read_text("utf-8"))
            label = d["vod"].get("title") or d["url"]
        except (OSError, ValueError, KeyError, AttributeError):
            label = p.name
        jobs.submit("finish", "finish", label, finish_job, str(p))
        n += 1
    return n + len(tasks)


# ---------------------------------------------------------------- tâches des moteurs (tout ce qui touche aux vidéos)

def add_task(kind, label, data, machine=None, tid=None):
    """Nouvelle tâche pour les moteurs, gardée dans la base (un redémarrage la reprend). machine : PC visé en priorité.
    -> Job affiché sur le site (mis à jour par les battements du moteur)."""
    tid = tid or store.uid()
    with store.tx() as db:
        ts = db.setdefault("tasks", {})
        if tid not in ts:
            ts[tid] = {"id": tid, "kind": kind, "label": str(label)[:200], "data": data, "machine": machine, "added": store.now()}
        task = ts[tid]
    return TRACK.get(tid) or _track(task)


def _track(task):
    kind = JOB_KIND.get(task["kind"]) or ("finish" if task["data"].get("vod_url") else "collect")
    j = TRACK[task["id"]] = jobs.track(kind, task["label"])
    j.log("En attente d'un PC" + (f" ({task['machine']} d'abord)" if task.get("machine") else "") + "…")
    return j


def cancel(tid, why):
    with store.tx() as db:
        (db.get("tasks") or {}).pop(tid, None)
    j = TRACK.pop(tid, None)
    if j:
        jobs.close(j, False, why)


def _scope(task):
    """Fichiers qu'un moteur peut lire / écrire pour cette tâche -> (lecture(clé), écriture(clé))."""
    d, kind = task["data"], task["kind"]
    if kind == "export":
        files = {c["file"] for c in d["db"]["clips"].values()} | {m["file"] for m in d["db"]["music"].values()}
        return files.__contains__, lambda k: k.startswith(d["dir"] + "/")
    src = {d.get("input") if d.get("local") else None, d.get("key")} - {None}
    ids = [p["cid"] for p in d["picks"]] if kind == "clips" else [d["cid"]] if kind == "add" else [d["name"]]
    pat = re.compile("|".join(rf"media/clips/{re.escape(i)}\.\w{{2,4}}|media/thumbs/{re.escape(i)}\.jpg" for i in ids))
    return src.__contains__, lambda k: bool(pat.fullmatch(k))


def sign(name, b):
    """Liens signés (24 h) pour les fichiers d'une tâche en cours sur ce moteur : rien d'autre (le secret S3 reste ici)."""
    lid = _s(b.get("lease"), 40)
    with store.lock:
        L = LEASES.get(lid)
        task = (store.load().get("tasks") or {}).get(L["task"]) if L and L["machine"] == name and L.get("task") else None
    if not task:
        return {"ok": False, "error": "bail inconnu (site relancé ?) : nouvel essai"}
    can = dict(zip(("get", "put"), _scope(task)))
    out = {"ok": True, "get": {}, "put": {}}
    for kind, method in (("get", "GET"), ("put", "PUT")):
        for k in (b.get(kind) if isinstance(b.get(kind), list) else [])[:500]:
            k = _s(k, 400)
            if not (s3.ok_key(k) and can[kind](k)):
                return {"ok": False, "error": f"accès refusé : {k}"}
            out[kind][k] = s3.url(method, k, 24 * 3600)
    return out


def done(name, b):
    """Tâche finie (ou ratée) sur un moteur : résultat gardé dans data/results, puis traité ici dans un fil à part (Claude
    peut prendre une minute)."""
    lid, tid = _s(b.get("lease"), 40), _s(b.get("task"), 40)
    with store.lock:
        db = store.load()
        _expire(time.time())
        L, tasks = LEASES.get(lid), db.get("tasks") or {}
        if L and L["machine"] == name and L.get("task"):
            tid = L["task"]
        elif lid in DEAD or tid not in tasks or _leased(tid):  # bail expiré : accepté si personne d'autre n'a repris la tâche
            return {"ok": False, "error": "perdu"}
        task = tasks.get(tid)
        if not task or task.get("done"):
            return {"ok": False, "error": "perdu"}
        LEASES.pop(lid, None)
        steps = [[_s(s[0], 80), _f(s[1], 0, 1e6)] for s in (b.get("steps") or [])[:40]
                 if isinstance(s, list) and len(s) == 2] if isinstance(b.get("steps"), list) else []
        p = RESULTS / f"task_{tid}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"machine": name, "ok": b.get("ok") is True, "out": b.get("out") if isinstance(b.get("out"), dict) else {},
                                   "error": _s(b.get("error")), "code": _s(b.get("code"), 20), "steps": steps,
                                   "total": _f(b.get("total"), 0, 1e6), "log": _s(b.get("log"), LOG_MAX)}, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)
        task["done"] = True
        store.save()
    threading.Thread(target=_handle, args=(tid,), daemon=True).start()
    return {"ok": True}


def _handle(tid):
    with store.lock:
        task = json.loads(json.dumps((store.load().get("tasks") or {}).get(tid) or {}))
    p = RESULTS / f"task_{tid}.json"
    if not task:
        p.unlink(missing_ok=True)
        return
    j = TRACK.get(tid) or _track(task)
    j.status, j.t0 = "en cours", j.t0 or time.time()
    ok, err, r = False, "", {}
    try:
        r = json.loads(p.read_text("utf-8"))
        for line in (r.get("log") or "").splitlines():
            j.log("   [moteur]", line)
        if r.get("steps"):
            j.log(f"Sur {r['machine']} : " + " · ".join(f"{n} {jobs.hms(d)}" for n, d in r["steps"]) + f" · total {jobs.hms(r.get('total') or 0)}")
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "échec sans message")
        j.result = HANDLERS[task["kind"]](j, task, r)
        ok = True
    except Exception as e:  # noqa: BLE001 - toute erreur finit dans le journal visible
        err = str(e)[:500] or type(e).__name__
        traceback.print_exc()
        _failed(task, err, r, j)
    finally:
        with store.tx() as db:
            (db.get("tasks") or {}).pop(tid, None)
        p.unlink(missing_ok=True)
        TRACK.pop(tid, None)
        jobs.close(j, ok, err)


def _failed(task, err, r, j):
    d = task["data"]
    if task["kind"] == "clips" and d.get("vod_url"):
        _mark(d["vod_url"], f"{err} (journal : {j.file.relative_to(store.DATA)})")
    if task["kind"] == "add" and r.get("code") in ("restricted", "vertical"):
        with store.tx() as db:  # retirée de la liste des VODs : on ne la reproposera plus
            v = (db.get("vods") or {}).get(d.get("input"))
            if v:
                v["restricted" if r["code"] == "restricted" else "excluded"] = True if r["code"] == "restricted" else "verticale"


def _h_clips(j, task, r):
    d = task["data"]
    n = collect.add_clips(j, d, (r.get("out") or {}).get("clips"), r["machine"], r.get("steps") or [], r.get("total") or 0)
    if d.get("vod_url"):
        _mark(d["vod_url"], None)
    return n


def _h_add(j, task, r):
    d = task["data"]
    nxt = collect.added(j, d, r.get("out") or {}, r.get("steps") or [], r.get("total") or 0, r["machine"])
    if nxt and nxt.get("expand"):
        for u in nxt["expand"]:
            add_task("add", u, dict(d, input=u, key=None, cid=store.uid(), taken=[]))
        j.log(f"{len(nxt['expand'])} vidéo(s) de la liste mise(s) en file")
    elif nxt and nxt.get("clips"):  # analyse faite, Claude a choisi : un moteur télécharge les extraits
        add_task("clips", task["label"], dict(nxt["clips"], streamer=d.get("streamer", "")), tid=task["id"] + "c")
    return None


HANDLERS = {"clips": _h_clips, "add": _h_add, "extend": lambda j, task, r: collect.extended(task["data"], r.get("out") or {}),
            "export": lambda j, task, r: montage.save(j, task["data"], r.get("out") or {})}


def export_machine(ip):
    """Le PC qui fera l'export : celui de la personne qui clique (même adresse que son navigateur), sinon le premier en ligne
    (ordre de l'onglet Machines), sinon None."""
    on = [m for m in machines_view() if m["online"] and not m["paused"]]
    mine = [m for m in on if ip and MACHINES.get(m["name"], {}).get("ip") == ip]
    return (mine or on or [{}])[0].get("name")


# ---------------------------------------------------------------- dashboard (cookie)

def running():
    """url -> machine et étape, pour la liste des VODs."""
    with store.lock:
        _expire(time.time())
        return {L["url"]: {"machine": L["machine"], "step": L.get("step", "")} for L in LEASES.values()}


def engine_conf():
    return {"public_url": CONF.get("public_url", ""), "github": CONF.get("github", ""), "invite": CONF.get("invite", "")}


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
    return _message(name, tok)


def _message(name, tok):
    """Tout ce qu'il faut envoyer à un ami : ses lignes d'installation (avec la clé de sa machine), l'invitation Tailscale, le lien de tri."""
    hub = CONF.get("public_url", "")
    return {"name": name, "token": tok, "win": f'irm "{hub}/moteur/install.ps1?m={tok}" | iex',
            "mac": f'curl -fsSL "{hub}/moteur/install.sh?m={tok}" | bash', "sort": f"{hub}/?k={CONF['token']}",
            "invite": CONF.get("invite", "")}


def machine_message(name):
    """Le message d'une machine déjà créée (perdu, ou à renvoyer)."""
    tok = next((k for k, m in CONF.get("machines", {}).items() if m["name"] == name and not m.get("local")), None)
    if not tok:
        raise KeyError("machine introuvable")
    if not CONF.get("public_url") or not CONF.get("github"):
        raise ValueError("Renseigne d'abord l'adresse Tailscale du serveur et le dépôt GitHub.")
    return _message(name, tok)


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
                if LEASES[k].get("kind") == "export":  # un export arrêté est annulé (le reste repart en file)
                    cancel(LEASES.pop(k)["task"], "Export arrêté depuis le site.")
                else:
                    _drop(k, "arrêté depuis le site")
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
    invite = _s(b.get("invite", CONF.get("invite", "")), 300).strip()
    if invite and not re.fullmatch(r"https://[\w.-]+/\S*", invite):
        raise ValueError("Lien d'invitation invalide : il commence par https://login.tailscale.com/…")
    with store.lock:
        CONF.update(public_url=url, github=repo, invite=invite)
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
