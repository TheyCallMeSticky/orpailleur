"""Tests de la logique de sélection, sans réseau : .venv/bin/python selftest.py"""
import json
import os
import tempfile
from pathlib import Path

# ne touche jamais aux vraies données ; pas dans /tmp (petit disque en mémoire)
os.environ["BESTOF_DATA"] = tempfile.mkdtemp(prefix="orpailleur-selftest-", dir=Path.home() / ".cache")

import ai  # noqa: E402
import collect  # noqa: E402
import montage  # noqa: E402
import vods  # noqa: E402


def sig(t0, t1, kind, w, why="x"):
    return {"t0": t0, "t1": t1, "kind": kind, "w": w, "why": why}


# collect.pick : signaux proches regroupés, bonus quand plusieurs types s'accordent, 12-60 s, pas de chevauchement
picks = collect.pick([sig(100, 120, "chat", 1.0), sig(110, 125, "audio", .5), sig(500, 503, "audio", .9),
                      sig(1000, 1200, "chat", .3), sig(2000, 2020, "clip", 2.0)], 3, 0, 3000, taken=[(1990, 2010)])
assert [p["sig"][0]["t0"] for p in picks] == [100, 500, 1000], picks  # le clip à 2000 chevauche un extrait déjà pris
assert picks[0]["score"] == round(1.0 + .5 + .6, 2)                 # bonus d'accord chat + audio
assert picks[1]["t1"] - picks[1]["t0"] >= collect.MIN_LEN             # 3 s élargi à 12 s
assert picks[2]["t1"] - picks[2]["t0"] <= collect.MAX_LEN             # 200 s ramené à 60 s
assert len(collect.pick([sig(i * 100, i * 100 + 20, "chat", 1) for i in range(30)], 10, 0, 9999, [])) == 10
# 3 min de cris (30 petits signaux audio collés) ne battent pas un moment repéré par le chat ET un clip de viewer
cris = [sig(5000 + i * 6, 5000 + i * 6 + 8, "audio", .7) for i in range(30)]
top = collect.pick(cris + [sig(8000, 8030, "chat", 1.0), sig(8005, 8035, "clip", 1.6)], 1, 0, 9999, [])
assert top[0]["sig"][0]["t0"] == 8000, top

# ai.parse : bornes limitées, entrées invalides et doublons ignorés, au plus n
cands = [{"t0": 100, "t1": 130, "score": 1, "why": "a"}, {"t0": 500, "t1": 530, "score": 1, "why": "b"}]
reply = json.dumps([{"id": "c0", "start": 50, "end": 140, "category": "fun", "note": 4, "raison": "r"},
                    {"id": "c9", "start": 1, "end": 2}, {"id": "c0", "start": 100, "end": 120},
                    {"id": "c1", "start": 505, "end": 520, "category": "EPIC", "note": 9}])
res = ai.parse("blabla " + reply, cands, 0, 1000, 5)
assert [(r["t0"], r["t1"]) for r in res] == [(80, 140), (505, 520)], res  # début limité à 20 s avant le candidat
assert res[0]["ai"]["category"] == "FUN" and res[1]["ai"]["rating"] is None  # note hors 1-5 ignorée

# montage.auto_order : meilleur (sans violence) en premier, 2e meilleur en dernier, au milieu au hasard
# mais jamais le même streamer ni la même catégorie deux fois de suite
cl = [{"id": i, "rating": r, "score": 0, "category": c, "flags": f, "source_id": s} for i, r, c, f, s in [
    ("a", 5, "EPIC", {"violence": True}, "kai"), ("b", 5, "FUN", {}, "xqc"), ("c", 4, "FAIL", {}, "kai"), ("d", 3, "WTF", {}, "kai"),
    ("e", 2, "FUN", {}, "xqc"), ("f", 2, "WTF", {}, "speed"), ("g", 1, "FAIL", {}, "speed"), ("h", 1, "EPIC", {}, "lacy")]]
info = {c["id"]: c for c in cl}
orders = set()
for _ in range(20):
    o = montage.auto_order(cl)
    assert o[0] == "b" and o[-1] == "a", o
    assert all(info[o[k]]["source_id"] != info[o[k - 1]]["source_id"] and info[o[k]]["category"] != info[o[k - 1]]["category"]
               for k in range(1, len(o))), o
    orders.add(tuple(o))
assert len(orders) > 1  # vraiment aléatoire

# remplir : au plus 2 extraits par streamer
db = {"project": {"items": [], "target": 600}, "sources": {"s1": {"streamer": "Kai"}, "s2": {"streamer": "xQc"}},
      "clips": {f"k{i}": {"id": f"k{i}", "source_id": "s1", "status": "kept", "rating": 5, "score": 0, "in": 0, "out": 30,
                          "category": "FUN", "flags": {}} for i in range(5)}}
db["clips"]["x0"] = {"id": "x0", "source_id": "s2", "status": "kept", "rating": 1, "score": 0, "in": 0, "out": 30, "category": "WTF", "flags": {}}
montage.fill(db)
assert sorted(db["project"]["items"]) == ["k0", "k1", "x0"], db["project"]["items"]

# HLS : positions cumulées des morceaux et choix de ceux qui couvrent un passage
pl = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.000,\n0-muted.ts\n#EXTINF:10.000,\n1.ts\n#EXTINF:4.5,\n2.ts\n#EXT-X-ENDLIST\n"
segs = collect.parse_m3u8(pl, "https://cdn.example/vod/chunked/index-dvr.m3u8")
assert [(s[0], s[1]) for s in segs] == [(0.0, 10.0), (10.0, 10.0), (20.0, 4.5)] and segs[1][2] == "https://cdn.example/vod/chunked/1.ts"
assert [s[0] for s in collect.covering(segs, 12, 21)] == [10.0, 20.0] and collect.covering(segs, 30, 40) == []
# VODs Twitch en fMP4 : le segment d'initialisation doit être repéré (sinon les extraits sont illisibles)
assert collect.m3u8_init('#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:10,\n0.mp4\n', "https://cdn.example/v/index.m3u8") \
    == "https://cdn.example/v/init-0.mp4" and collect.m3u8_init(pl, "https://x/") is None

# poubelle vidée après un export : fichiers et fiches supprimés, passage gardé pour ne pas le reproposer
import store  # noqa: E402
(store.CLIPS / "t1.mp4").write_bytes(b"x" * 100)
db = {"sources": {"s1": {}}, "clips": {"t1": {"id": "t1", "source_id": "s1", "status": "trash", "src_start": 100, "in": 8, "out": 30,
                                              "file": "media/clips/t1.mp4", "thumb": "", "bytes": 100},
                                       "k1": {"id": "k1", "source_id": "s1", "status": "kept", "file": "media/clips/k1.mp4"}}}
db["sources"]["s1"]["streamer"] = "Kai"
db["clips"]["t1"]["ai"] = {"category": "FAIL", "rating": 4, "reason": "r"}
assert montage.purge_trash(db, {"t1", "k1"}) == (1, 100, ["media/clips/t1.mp4"]) and list(db["clips"]) == ["k1"]
assert db["sources"]["s1"]["trashed"] == [[108, 130]] and (store.CLIPS / "t1.mp4").exists()  # fichiers : hors du verrou de la base
assert montage.delete_files(["media/clips/t1.mp4", "media/clips/absent.mp4"]) == 2 and not (store.CLIPS / "t1.mp4").exists()
arch = [json.loads(x) for x in montage.ARCHIVE.read_text("utf-8").splitlines()]  # la fiche est gardée, sans la vidéo
assert arch[-1]["id"] == "t1" and arch[-1]["ai"]["category"] == "FAIL" and arch[-1]["streamer"] == "Kai" and arch[-1]["purged"], arch

# journal sur disque avec la durée de chaque étape
import jobs  # noqa: E402
j = jobs.Job("collect", "test", None, ())
with j.step("Étape"):
    pass
assert j.steps == [["Étape", 0]] and "■ Étape : 0 s" in j.file.read_text("utf-8")
assert jobs.hms(65) == "1 min 05 s" and jobs.hms(3720) == "1 h 02 min"

# filtre GTA : les hashtags ne comptent pas
assert vods.title_gta("GTA 6 LIVE | Day 2") and not vods.title_gta("chain together live #shortslive #gta6")

# streamers suivis : un clic ne change qu'une chaîne, sur la liste du moment (une page ancienne n'écrase rien)
vods.save_settings({"streamers": [{"name": "A", "platform": "twitch", "handle": "a", "lang": "fr"},
                                  {"name": "B", "platform": "kick", "handle": "b", "lang": "en"}, {"platform": "x", "handle": "c"}]})
vods.save_settings({"op": {"action": "set", "platform": "kick", "handle": "B", "on": False}})
vods.save_settings({"op": {"action": "add", "platform": "youtube", "handle": "@C", "name": "C"}})
vods.save_settings({"op": {"action": "add", "platform": "twitch", "handle": "A"}})  # déjà suivie : rien
f_ = vods.save_settings({"op": {"action": "remove", "platform": "twitch", "handle": "a"}})["streamers"]
assert [(x["name"], x["lang"], x["on"]) for x in f_] == [("C", "", True), ("B", "en", False)], f_

# liens horodatés des crédits
assert collect.ts_link("https://www.twitch.tv/videos/1", 3725) == "https://www.twitch.tv/videos/1?t=1h2m5s"
assert collect.ts_link("https://www.youtube.com/watch?v=x", 61) == "https://www.youtube.com/watch?v=x&t=61"

# stockage : signature S3 (exemple de la documentation AWS), mode local (liens signés par le site, expirés refusés)
import datetime  # noqa: E402
import s3  # noqa: E402
aws = {"endpoint": "https://s3.amazonaws.com", "region": "us-east-1", "bucket": "examplebucket", "key": "AKIAIOSFODNN7EXAMPLE",
       "secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
assert s3.presign("GET", "test.txt", 86400, aws, datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)).endswith(
    "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
assert s3.conf() is None and s3.ok_key("media/clips/a.mp4") and not s3.ok_key("data/db.json") and not s3.ok_key("media/../db.json")
lk_url = s3.url("PUT", "inbox/x.mp4", 60)
q_ = dict(p.split("=") for p in lk_url.split("?")[1].split("&"))
assert lk_url.startswith("/files/inbox/x.mp4?") and s3.local_ok("PUT", "inbox/x.mp4", q_["e"], q_["t"])
assert not s3.local_ok("GET", "inbox/x.mp4", q_["e"], q_["t"]) and not s3.local_ok("PUT", "inbox/y.mp4", q_["e"], q_["t"])
assert not s3.local_ok("PUT", "inbox/x.mp4", "1", s3.local_token("PUT", "inbox/x.mp4", "1"))  # expiré
assert s3.ctype("exports/a/description_youtube.txt") == "text/plain; charset=utf-8" and s3.ctype("media/clips/a.mp4") == "video/mp4"


def lget(k, dst):  # moteur simulé : le stockage local est le dossier de données du test
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    if Path(dst).resolve() != (store.DATA / k).resolve():
        shutil.copy(store.DATA / k, dst)


def lput(src, k):
    (store.DATA / k).parent.mkdir(parents=True, exist_ok=True)
    if Path(src).resolve() != (store.DATA / k).resolve():
        shutil.copy(src, store.DATA / k)


# moteur -> site : detect rend un RESULT, finish (Claude, sur le site) donne la tâche « clips », le moteur télécharge, mesure,
# fait la vignette et envoie ; le site enregistre les fiches et la fiche d'analyse
import shutil  # noqa: E402
import subprocess  # noqa: E402
vid = store.TMP / "test.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "60", str(vid)], check=True)  # 30 s de marge avant chaque extrait
collect.chat_signals = lambda job, url, m, lo, hi: [sig(100, 120, "chat", 1.0, "chat x2")]
collect.audio_signals = lambda job, key, lo, hi, *a: [sig(500, 510, "audio", .7, "audio rires 0.7")]
_dl0 = collect.download  # le vrai (pour le test d'erreur d'un moteur, sans réseau)
collect.download = lambda url, cid, s0=None, s1=None: Path(shutil.copy(vid, store.CLIPS / f"{cid}.mp4"))
url, jd = "https://www.youtube.com/watch?v=test", jobs.Job("collect", "test", None, ())
res = json.loads(json.dumps(collect.detect(jd, url, {"title": "T", "duration": 3600, "width": 1920, "height": 1080}, 2, "",
                                           [(95, 125)], ai_on=False)))
assert [g["t0"] for g in res["auto"]] == [499.0] and res["cands"] is None and res["meta"]["duration"] == 3600, res
plan = collect.finish(jd, url, dict(res, steps=[["Whisper", 9]], total=60), 2, "Kai", machine="PC-test")
p0 = plan["picks"][0]
assert len(plan["picks"]) == 1 and (p0["s0"], p0["s1"]) == (499 - collect.PAD, 511 + collect.PAD) and plan["input"] == url, plan
out = json.loads(json.dumps(collect.fetch_clips(jd, plan, lget, lput)))  # ce qui part au site
m0 = out["clips"][0]
assert m0["file"] == f"media/clips/{p0['cid']}.mp4" and m0["thumb"] == f"media/thumbs/{p0['cid']}.jpg" and m0["bytes"] > 0, m0
assert collect.add_clips(jd, plan, out["clips"], "PC-dl", [["Téléchargement des extraits en 1080p", 3]], 5) == 1
src = next(s for s in store.load()["sources"].values() if s["url"] == url)
an = src["analyses"][0]
assert src["streamer"] == "Kai" and an["machine"] == "PC-test" and an["total"] >= 65 and an["clips"] == 1, an
assert an["steps"][0] == ["Whisper", 9] and an["steps"][-1][0].startswith("Téléchargement"), an
c = store.load()["clips"][p0["cid"]]
assert (c["src_start"], c["in"], c["out"]) == (491, 8, 20) and (store.DATA / c["thumb"]).exists(), c
assert collect.taken_for(store.load(), url) and collect.taken_for(store.load(), "x") == []
# rejouée après un arrêt du site : pas de doublon ; même résultat d'analyse une 2e fois : rien de nouveau à télécharger
n_clips = len(store.load()["clips"])
assert collect.add_clips(jd, plan, out["clips"]) == 1 and len(store.load()["clips"]) == n_clips
assert collect.finish(jd, url, res, 2, "Kai") is None
# un moteur ne peut pas faire enregistrer n'importe quel fichier
for bad in ({**m0, "file": "media/../db.json"}, {**m0, "thumb": "media/thumbs/autre.jpg"}, {**m0, "dur": float("nan")}):
    try:
        collect.add_clip("x", bad, 0, 5, "w", 1)
        raise AssertionError(f"fiche acceptée : {bad}")
    except ValueError:
        pass
# « Récupérer 30 s avant » (fichier envoyé, sur le stockage) : 30 s de plus au début, même fin, réglages décalés
f0 = collect.cut_local(str(vid), "ext0", 40, 55)
shutil.copy(vid, store.INBOX / "src.mp4")
with store.tx() as db:
    db["sources"]["loc"] = {"id": "loc", "url": "inbox/src.mp4", "platform": "local", "streamer": "L"}
    db["clips"]["ext0"] = {"id": "ext0", "source_id": "loc", "status": "new", "file": f"media/clips/{f0.name}", "src_start": 40,
                           "in": 2, "out": 10, "segs": [[2, 5], [7, 10]], "file_dur": 15.0}
xd = collect.extend_request("ext0")
assert xd["local"] and xd["input"] == "inbox/src.mp4" and (xd["s0"], xd["s1"]) == (10, 55) and xd["name"].startswith("ext0-"), xd
r = collect.extended(xd, json.loads(json.dumps(collect.extend_clip(jd, xd, lget, lput))))
ce, delta = r["clip"], r["delta"]
assert delta == 30 and ce["src_start"] == 10 and (ce["in"], ce["out"]) == (32, 40) and ce["segs"] == [[32, 35], [37, 40]], ce
assert abs(ce["file_dur"] - 45) < .5 and not f0.exists() and (store.DATA / ce["file"]).exists(), ce
assert collect.extended(xd, {"file": ce["file"], "s0": 10, "dur": 45})["delta"] == 0  # rejouée : rien ne bouge
try:
    collect.extended(xd, {"file": "media/clips/autre.mp4", "s0": 10, "dur": 45})
    raise AssertionError("fichier inattendu accepté")
except ValueError:
    pass
try:
    collect.detect(jd, url, {"duration": 60, "width": 1080, "height": 1920}, 2, "", [])
    raise AssertionError("vidéo verticale acceptée")
except collect.Skip as e:
    assert e.code == "vertical"

# verrou de la carte graphique : un 2e Whisper attend (None tant que le port est pris)
lk = ai._gpu_lock()
if lk:  # None si un vrai Whisper tourne en ce moment
    assert ai._gpu_lock() is None
    lk.close()
# engines : baux des moteurs (une VOD à une seule machine, expiration/ré-adoption, « arrêter maintenant », résultat refusé)
import engines as E  # noqa: E402
E.STARTED = 0  # pas de délai de reprise après démarrage dans le test
U = "https://www.twitch.tv/videos/9"
with store.tx() as db:
    db["vods"], db["pending"] = {U: {"url": U, "title": "t", "duration": 3000}}, [U]
B = lambda **k: dict({"protocol": E.PROTOCOL, "want": 0, "jobs": []}, **k)  # noqa: E731
assert E.beat("m", B(protocol=0))["ok"] is False
L = E.beat("m", B(want=1))["give"][0]["lease"]
assert not E.beat("n", B(want=1))["give"]                                    # déjà prêtée à « m »
E.LEASES[L]["until"] = 0                                                      # expiré (hub relancé, réseau coupé) : repris
assert not E.beat("m", B(jobs=[{"lease": L, "url": U}]))["kill"] and E.LEASES[L]["machine"] == "m"
E.CONF.setdefault("machines", {})["t"] = {"name": "m"}
E.control("m", "stop_now")
g = E.beat("m", B(want=1, jobs=[{"lease": L, "url": U}]))
assert g["kill"] == [L] and g["paused"] and not g["give"] and U in store.load()["pending"]
assert E.result("m", {"lease": L, "url": U, "ok": False}) == {"ok": False, "error": "perdu"}
# stream encore en direct : ni échec compté ni pause de la machine, la VOD attend la prochaine recherche
E.control("m", "resume")
with store.tx() as db:
    db["pending"] = [U]
for _ in range(4):
    with store.tx() as db:
        db["pending"] = [U]
        db["vods"][U].pop("recording", None)
    Lv = E.beat("m", B(want=1))["give"][0]["lease"]
    assert E.result("m", {"lease": Lv, "url": U, "ok": False, "code": "live", "error": "en direct"})["ok"]
st_ = store.load()
assert not st_["engines"]["m"]["paused"] and U not in st_["pending"] and U not in st_.get("vod_fails", {}) and st_["vods"][U]["recording"]

# tâches des moteurs : « 30 s avant » d'abord, machine visée (sauf si elle ne répond plus), place libre seulement pour une
# analyse, liens signés limités aux fichiers de la tâche, reprise après un redémarrage du site
import time  # noqa: E402
E.control("m", "resume")
with store.tx() as db:
    db["pending"] = []
E.add_task("add", "a", {"input": "https://x/a", "key": None, "cid": "cidadd0001"})
E.add_task("extend", "e", {"cid": "ext0", "input": "inbox/src.mp4", "local": True, "name": "ext0-zz", "s0": 1, "s1": 2}, machine="n")
tid_a, tid_e = (next(k for k, v in store.load()["tasks"].items() if v["kind"] == kd) for kd in ("add", "extend"))
E.MACHINES["n"] = {"last_seen": time.time()}
assert E.beat("p", B(want=0))["give"] == []                    # l'ajout attend une place, « 30 s avant » attend « n »
g = E.beat("n", B(want=0))
assert [x["kind"] for x in g["give"]] == ["extend"] and g["give"][0]["data"]["name"] == "ext0-zz", g
L2 = g["give"][0]["lease"]
assert [x["kind"] for x in E.beat("p", B(want=1))["give"]] == ["add"]
assert E.sign("n", {"lease": L2, "get": ["inbox/src.mp4"], "put": ["media/clips/ext0-zz.mp4"]})["ok"]
assert not E.sign("n", {"lease": L2, "get": ["media/clips/autre.mp4"]})["ok"]
assert not E.sign("n", {"lease": L2, "put": ["media/clips/ext0-zzz.mp4"]})["ok"]
assert not E.sign("p", {"lease": L2, "get": ["inbox/src.mp4"]})["ok"]   # pas son bail
E.LEASES.clear()                                                      # site relancé : baux oubliés, repris au battement
g = E.beat("n", B(jobs=[{"lease": L2, "task": tid_e, "url": "task:" + tid_e}]))
assert not g["kill"] and E.LEASES[L2]["task"] == tid_e and E.TRACK[tid_e].status == "en cours"
E.LEASES[L2]["until"] = 0                                             # moteur éteint : la tâche attend un autre PC
E.beat("p", B())
assert L2 not in E.LEASES and E.TRACK[tid_e].status == "en attente" and tid_e in store.load()["tasks"]
E.MACHINES["n"]["last_seen"] = 0                                      # « n » ne répond plus : un autre PC la prend
assert [x["kind"] for x in E.beat("p", B())["give"]] == ["extend"]
for t in (tid_a, tid_e):
    E.cancel(t, "test")
E.LEASES.clear()
assert not store.load()["tasks"]

# export : le PC de la personne qui clique (même adresse), sinon le premier en ligne (ordre de l'onglet Machines), jamais
# une machine en pause ou éteinte ; aucune -> None (« Aucun PC allumé »)
E.CONF["machines"] = {"t": {"name": "m"}, "t2": {"name": "p"}, "t3": {"name": "n"}}
now = time.time()
E.MACHINES.update(m={"last_seen": now, "ip": "100.1.1.1"}, p={"last_seen": now, "ip": "100.2.2.2"}, n={"last_seen": 0, "ip": "100.3.3.3"})
assert (E.export_machine("100.2.2.2"), E.export_machine("100.9.9.9"), E.export_machine("100.3.3.3")) == ("p", "m", "m")
E.control("m", "pause")
assert E.export_machine("100.1.1.1") == "p"
E.control("m", "resume")
E.MACHINES["m"]["last_seen"] = E.MACHINES["p"]["last_seen"] = 0
assert E.export_machine("100.2.2.2") is None
E.MACHINES["p"]["last_seen"] = time.time()


def wait_task(tid):  # fin d'une tâche traitée par le site dans un fil à part
    for _ in range(300):
        if tid not in (store.load().get("tasks") or {}):
            return
        time.sleep(.1)
    raise AssertionError(f"tâche {tid} pas finie")

# moteur.py : l'enfant écrit toujours <job>.out.json (erreur + journal, Skip garde son code) ; même empreinte de code que le hub
import moteur  # noqa: E402
assert moteur.code_version() == E.code_version() and len(moteur.code_version()) == 8
jf = os.path.join(tempfile.mkdtemp(), "L.json")
json.dump({"lease": "L", "url": "u", "input": "http://127.0.0.1:1/x", "title": "T", "max_n": 3, "ai": False, "taken": [[1, 2]]},
          open(jf, "w", encoding="utf-8"))
moteur.child(jf)  # yt-dlp échoue tout de suite (connexion refusée, sans réseau)
o = json.load(open(jf[:-5] + ".out.json", encoding="utf-8"))
assert o["ok"] is False and o["code"] is None and "ERREUR" in o["log"] and "Traceback" in o["log"], o


def _skip(*a):
    raise collect.Skip("restricted", "abonnés")


_dv, collect.detect_vod = collect.detect_vod, _skip
moteur.child(jf)
collect.detect_vod = _dv
o = json.load(open(jf[:-5] + ".out.json", encoding="utf-8"))
assert (o["ok"], o["code"], o["error"]) == (False, "restricted", "abonnés") and "Écartée" in o["log"], o
# tâche du site sur un moteur : erreur et journal remontent aussi (téléchargement impossible, sans réseau)
json.dump({"lease": "L3", "task": "T3", "kind": "extend", "url": "task:T3", "title": "x", "hub": "http://127.0.0.1:1", "token": "k",
           "data": {"input": "http://127.0.0.1:1/x", "local": False, "s0": 0, "s1": 5, "name": "zz"}}, open(jf, "w", encoding="utf-8"))
_dl1, collect.download = collect.download, _dl0
moteur.child(jf)
collect.download = _dl1
o = json.load(open(jf[:-5] + ".out.json", encoding="utf-8"))
assert o["ok"] is False and "ERREUR" in o["log"], o

# coupes : morceaux bornés au fichier, triés, fusionnés s'ils se chevauchent ou se touchent, miettes retirées ; 1 morceau = in/out
import time  # noqa: E402
import app  # noqa: E402
with store.tx() as db:
    db["sources"]["sc"] = {"id": "sc", "url": "u-cut", "streamer": "Geo", "streamer_url": "https://www.twitch.tv/geo"}
    db["clips"]["cut"] = {"id": "cut", "source_id": "sc", "status": "new", "in": 2.0, "out": 20.0, "file_dur": 30.0, "added": store.now()}
c = app.patch_clip("cut", {"segs": [[20, 40], [5, 10], [8, 12], [-3, 1], [15, 15.2]]}, "u")
assert c["segs"] == [[0, 1], [5, 12], [20, 30]] and (c["in"], c["out"]) == (0, 30) and store.cdur(c) == 18 + 2 * store.XF, c  # fondus pris dans les parties coupées : ils allongent
c = app.patch_clip("cut", {"in": 6, "out": 25}, "u")  # ancien réglage début/fin : rogne les morceaux
assert c["segs"] == [[6, 12], [20, 25]] and store.cdur(c) == 11 + store.XF, c  # fondu entre les 2 morceaux
c = app.patch_clip("cut", {"segs": [[0, 5], [5, 8]]}, "u")
assert "segs" not in c and (c["in"], c["out"]) == (0, 8) and store.cdur(c) == 8, c
for bad in ([], [[3, 3.1]], [["a", 2]], "x"):
    try:
        app.patch_clip("cut", {"segs": bad, "status": "trash"}, "u")
        raise AssertionError(f"morceaux acceptés : {bad}")
    except ValueError:
        assert store.load()["clips"]["cut"]["status"] == "new"  # rien n'a changé

# jour d'arrivée (heure locale) : extraits à trier et VODs rangés en aujourd'hui / hier / plus ancien
import datetime  # noqa: E402
yday = datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=1), datetime.time(12)).timestamp()
assert (store.day(time.time()), store.day(yday), store.day(yday - 3 * 86400)) == ("today", "yesterday", "older")
assert store.day(store.now()) == "today" and store.day(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(yday))) == "yesterday"
with store.tx() as db:
    for k, cl in enumerate(db["clips"].values()):
        cl["added"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(yday if k == 0 else time.time()))
    first = next(iter(db["clips"]))
    nxt = app.next_clips("u", set(), "yesterday")
    tot = sum(cl["status"] == "new" for cl in db["clips"].values())
assert [x["id"] for x in nxt["clips"]] == [first] and nxt["days"] == {"today": tot - 1, "yesterday": 1, "older": 0}, nxt
assert len(app.next_clips("u", set())["clips"]) == min(3, tot) and app.next_clips("u", set(), "older")["clips"] == []
with store.tx() as db:
    db["vods"], db["vods_scan"] = {"v": {"url": "v", "title": "t", "age_h": 2}}, store.now()
row = app.vod_rows(store.load())[0]
assert abs(row["ended"] - (time.time() - 7200)) < 5 and row["day"] == store.day(row["ended"]), row

def export_now():  # site : instantané ; moteur : fabrication + envoi ; site : fiche, « utilisés », poubelle
    d = montage.snapshot()
    je = jobs.Job("export", "test", None, ())
    return montage.save(je, d, json.loads(json.dumps(montage.render_task(je, d, lget, lput))))


# export d'un extrait coupé en 2 morceaux (avec son, puis sans son) : un seul segment = morceaux + fondu de 0,25 s pris dans la coupure
av = store.TMP / "av.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000", "-t", "30", "-shortest", "-c:a", "aac", str(av)], check=True)
shutil.copy(av, store.CLIPS / "av.mp4")
shutil.copy(vid, store.CLIPS / "mute.mp4")
with store.tx() as db:
    for cid in ("av", "mute"):
        db["clips"][cid] = {"id": cid, "source_id": "sc", "status": "kept", "file": f"media/clips/{cid}.mp4", "src_start": 0,
                            "in": 0, "out": 30, "file_dur": 30.0, "fps": 30, "rating": 4, "category": "FUN", "flags": {}}
    db["project"]["items"] = ["av", "mute"]
    db["project"]["transitions"] = False  # ni transition ni musique : segments mis bout à bout sans réencodage
app.patch_clip("av", {"segs": [[2, 5], [10, 14]]}, "u")
app.patch_clip("mute", {"segs": [[1, 3], [20, 23.5]]}, "u")
rec = export_now()
seg = sorted((store.DATA / rec["dir"] / "segments").glob("*.mp4"))
assert [abs(collect.probe(f)["dur"] - d) < .2 for f, d in zip(seg, (7.25, 5.75))] == [True, True], [collect.probe(f) for f in seg]
assert all(collect.probe(f)["audio"] for f in seg) and abs(rec["dur"] - (13.0 + montage.END_S)) < .3, rec  # + écran de fin
assert (store.DATA / rec["dir"] / "segments" / "99_ecran_de_fin.mp4").exists()  # écran de fin de 20 s

# transitions : les 6, jamais la même deux fois de suite
for n in list(range(15)) * 20 + [100]:
    t = montage.pick_transitions(n)
    assert len(t) == n and set(t) <= set(montage.TRANSITIONS) and all(t[k] != t[k - 1] for k in range(1, n)), t
assert set(montage.pick_transitions(6)) == set(montage.TRANSITIONS)

# chapitres et crédits avec transitions : chaque extrait commence 0,4 s plus tôt
d4 = {"project": {"name": "T", "items": ["c0", "c1", "c2", "c3"], "transitions": True}, "music": {},
      "sources": {f"s{k}": {"streamer": n, "url": "https://www.twitch.tv/videos/1", "platform": "twitch",
                            "streamer_url": f"https://www.twitch.tv/{n.lower()}"} for k, n in enumerate(("Kai", "Lacy", "Jynxzi", "Kai"))},
      "clips": {f"c{k}": {"id": f"c{k}", "source_id": f"s{k}", "category": cat, "src_start": 0, "in": 0, "out": 20}
                for k, cat in enumerate(("FUN", "WTF", "FAIL", "EPIC"))}}
desc = montage.description(d4)  # chapitres au nom du streamer ; crédits : une ligne par streamer, sans liens horodatés
assert "0:00 Intro\n0:20 Lacy\n0:40 Jynxzi\n1:01 Final\n" in desc and "dans la vidéo" not in desc, desc
assert desc.count("▶ Kai — https://www.twitch.tv/kai") == 1 and "▶ Lacy — https://www.twitch.tv/lacy" in desc, desc
assert abs(store.bestof_total(list(d4["clips"].values())) - 81.2) < 1e-9 and store.bestof_total(list(d4["clips"].values()), False) == 80
d4["project"]["transitions"] = False
assert "0:00 Intro\n0:20 Lacy\n0:40 Jynxzi\n1:00 Final\n" in montage.description(d4)

# musiques de fond : format, poids et durée (lue par le navigateur) vérifiés avant l'envoi direct au stockage


def tone(sec, f):
    w = store.TMP / f"{f}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=44100:duration={sec}",
                    "-ac", "1", str(w)], check=True)
    return w.read_bytes()


for name, size, dur in (("note.txt", 10, 30), ("lourd.mp3", 70 << 20, 30), ("court.wav", 1000, 3), ("faux.mp3", 1000, None)):
    try:
        app.music_start(name, size, dur)
        raise AssertionError(f"musique acceptée : {name}")
    except ValueError:
        pass


def add_music(name, data, by=""):  # navigateur : durée lue, envoi direct (ici : écrit sur le stockage local), confirmation
    st = app.music_start(name, len(data), 11.0)
    assert st["put"].startswith("/files/media/music/"), st
    (store.DATA / st["key"]).write_bytes(data)
    return app.music_done({"id": st["id"], "key": st["key"], "name": st["name"], "dur": st["dur"]}, by)


for bad in ({"id": "x", "key": "media/music/x.mp3", "dur": 20}, {"id": "abcdefghij", "key": "media/clips/abcdefghij.mp3", "dur": 20}):
    try:
        app.music_done(bad)
        raise AssertionError(f"musique acceptée : {bad}")
    except ValueError:
        pass
b1, b2 = tone(11, 440), tone(11, 660)
m1, m2 = add_music("C:\\Musique\\Piste A.wav", b1, "u"), add_music("Piste B.WAV", b2)
assert (m1["name"], m2["name"]) == ("Piste A", "Piste B") and abs(m1["dur"] - 11) < .2 and (store.DATA / m1["file"]).is_file(), m1
assert [m["id"] for m in app.state()["music"]] == [m1["id"], m2["id"]]
assert store.load()["project"]["music"] == [m1["id"], m2["id"]]  # envoyée = déjà dans le best-of en cours
pj = app.patch_project({"music": [m1["id"], "zz", m2["id"], m1["id"]], "music_vol": 30, "transitions": True})
assert (pj["music"], pj["music_vol"], pj["transitions"]) == ([m1["id"], m2["id"]], 30, True), pj

# export avec transitions + 2 musiques (répétées : 19 s de musique pour 21,8 s de vidéo) : 3 × 7 s + 2 transitions
with store.tx() as db:
    for k in range(3):
        db["clips"][f"t{k}"] = {"id": f"t{k}", "source_id": "sc", "status": "kept", "file": "media/clips/av.mp4", "src_start": 0,
                                "in": k * 8, "out": k * 8 + 7, "file_dur": 30.0, "fps": 30, "rating": 4, "category": "FUN", "flags": {}}
    db["project"]["items"] = ["t0", "t1", "t2"]
rec = export_now()
sd = montage.stream_durs(store.DATA / rec["files"]["mp4"])
assert abs(rec["dur"] - (21 + 2 * store.XT + montage.END_S)) < .3 and abs(sd["audio"] - sd["video"]) < .1, (rec, sd)  # transitions hors sélection + fin
assert "Music: Piste A, Piste B" in (store.DATA / rec["files"]["description"]).read_text("utf-8")
assert not (store.DATA / rec["dir"] / "musique.wav").exists()
assert app.del_music(m2["id"]) and store.load()["project"]["music"] == [m1["id"]] and not (store.DATA / m2["file"]).exists()
assert [m["id"] for m in store.load()["music"].values()] == [m1["id"]]

# aucune musique choisie : tirée au hasard dans la bibliothèque à l'export et gardée dans le best-of ; case décochée : aucune
app.patch_project({"music": []})
rec = export_now()
assert store.load()["project"]["music"] == [m1["id"]] and "Music: Piste A" in (store.DATA / rec["files"]["description"]).read_text("utf-8")
app.patch_project({"music_on": False})
assert montage.music_of(store.load()) == [] and store.load()["project"]["music_on"] is False
app.patch_project({"music_on": True})

# ajout à la main « tel quel dans le Tri » (fichier envoyé sur le stockage, traité par un moteur) : un passage = un extrait en
# tête du Tri ; > 30 min refusé ; passage déjà pris refusé sur le site (et le fichier envoyé par le moteur supprimé)
long = store.INBOX / "kk_long.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=64x36:rate=1", "-t", "1900", str(long)], check=True)
jw = jobs.Job("collect", "test", None, ())
dw = {"key": "inbox/kk_long.mp4", "name": "long.mp4", "mode": "whole", "window": "", "streamer": "Peanut", "by": "Azer",
      "cid": store.uid(), "taken": []}
try:
    collect.add_video(jw, dw, lget, lput)
    raise AssertionError("vidéo de 31 min acceptée en entier")
except RuntimeError as e:
    assert "Trop long" in str(e)
try:
    collect.parse_window("2:00-1:00")
    raise AssertionError("passage à l'envers accepté")
except ValueError:
    pass
d2 = dict(dw, window="1:00-1:20", cid=store.uid())
assert collect.added(jw, d2, json.loads(json.dumps(collect.add_video(jw, d2, lget, lput)))) is None
c = store.load()["clips"][d2["cid"]]
src = store.load()["sources"][c["source_id"]]
assert (c["score"], c["why"], c["src_start"], src["added_by"], src["streamer"], src["url"]) == \
    (50, "ajouté par Azer", 60, "Azer", "Peanut", "inbox/kk_long.mp4"), (c, src)
assert abs(store.cdur(c) - 20) < 1.5, c
assert app.next_clips("v", set())["clips"][0]["id"] == c["id"]  # en tête du Tri
d3 = dict(dw, window="1:10-1:30", cid=store.uid())
o3 = collect.add_video(jw, d3, lget, lput)
try:
    collect.added(jw, d3, o3)
    raise AssertionError("passage déjà ajouté accepté")
except RuntimeError as e:
    assert "Déjà ajoutée" in str(e) and not (store.DATA / o3["clip"]["file"]).exists()

# tâche « add » de bout en bout par le site : file, moteur, résultat, fiche dans le Tri (traitée dans un fil à part)
shutil.copy(av, store.INBOX / "kk_av.mp4")
ja = E.add_task("add", "av.mp4", {"key": "inbox/kk_av.mp4", "name": "av.mp4", "mode": None, "window": "", "streamer": "Zed",
                                  "by": "Bob", "cid": "addviadone", "taken": []})
tid = next(iter(store.load()["tasks"]))
g = E.beat("p", B(want=1))["give"][0]
assert g["task"] == tid and ja.status == "en cours"
assert E.sign("p", {"lease": g["lease"], "get": ["inbox/kk_av.mp4"], "put": ["media/clips/addviadone.mp4", "media/thumbs/addviadone.jpg"]})["ok"]
o = json.loads(json.dumps(collect.add_video(jobs.Job("add", "t", None, ()), g["data"], lget, lput)))
assert E.done("p", {"lease": g["lease"], "task": tid, "ok": True, "out": o, "steps": [["Envoi", 1]], "log": "journal du moteur"})["ok"]
assert E.done("p", {"lease": g["lease"], "task": tid, "ok": True, "out": o})["ok"] is False  # déjà reçue
wait_task(tid)
assert "addviadone" in store.load()["clips"] and ja.status == "terminé" and any("journal du moteur" in x for x in ja.lines), ja.lines

# export de bout en bout par le site, sur le PC « p » : poubelle vidée à la fin, fichiers supprimés du stockage
with store.tx() as db:
    db["clips"]["addviadone"]["status"] = "trash"
    db["project"]["items"] = ["t0"]
jx = E.add_task("export", "x", montage.snapshot(), "p")
tid = next(iter(store.load()["tasks"]))
g = E.beat("p", B(want=0))["give"][0]   # hors place : passe même sans place libre
assert g["kind"] == "export" and E.sign("p", {"lease": g["lease"], "put": [g["data"]["dir"] + "/segments/01.mp4"]})["ok"]
assert not E.sign("p", {"lease": g["lease"], "put": ["exports/autre/x.mp4"]})["ok"]
o = json.loads(json.dumps(montage.render_task(jobs.Job("export", "t", None, ()), g["data"], lget, lput)))
assert E.done("p", {"lease": g["lease"], "task": tid, "ok": True, "out": o})["ok"]
wait_task(tid)
assert jx.status == "terminé" and store.load()["exports"][0]["dir"] == g["data"]["dir"], jx.lines
assert "addviadone" not in store.load()["clips"] and not (store.CLIPS / "addviadone.mp4").exists()
assert (store.DATA / store.load()["exports"][0]["files"]["mp4"]).exists()
assert collect.streamer_of({"channel": "xQc"}, "https://clips.twitch.tv/AwkwardHelplessSalamander")[1] == "https://www.twitch.tv/xqc"

# onglet Streamers : gardés / jetés / à trier / exportés par streamer ; les jetés déjà supprimés (archivés) comptent aussi
st_, db_ = app.streamer_stats(store.load()), store.load()
assert sum(v["kept"] for v in st_.values()) == sum(c["status"] == "kept" for c in db_["clips"].values())
assert sum(v["trash"] for v in st_.values()) == (sum(1 for _ in open(montage.ARCHIVE, encoding="utf-8"))
                                                 + sum(c["status"] == "trash" for c in db_["clips"].values()))
assert st_["kai"]["trash"] >= 1 and sum(v["used"] for v in st_.values()) == sum(bool(c.get("used_in")) for c in db_["clips"].values())

shutil.rmtree(store.DATA, ignore_errors=True)
print("selftest ok")
