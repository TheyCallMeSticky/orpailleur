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
                                              "file": "media/clips/t1.mp4", "thumb": ""},
                                       "k1": {"id": "k1", "source_id": "s1", "status": "kept", "file": "media/clips/k1.mp4"}}}
assert montage.purge_trash(db, {"t1", "k1"}) == (1, 100) and list(db["clips"]) == ["k1"] and db["sources"]["s1"]["trashed"] == [[108, 130]]
assert not (store.CLIPS / "t1.mp4").exists()

# journal sur disque avec la durée de chaque étape
import jobs  # noqa: E402
j = jobs.Job("collect", "test", None, ())
with j.step("Étape"):
    pass
assert j.steps == [["Étape", 0]] and "■ Étape : 0 s" in j.file.read_text("utf-8")
assert jobs.hms(65) == "1 min 05 s" and jobs.hms(3720) == "1 h 02 min"

# filtre GTA : les hashtags ne comptent pas
assert vods.title_gta("GTA 6 LIVE | Day 2") and not vods.title_gta("chain together live #shortslive #gta6")

# liens horodatés des crédits
assert collect.ts_link("https://www.twitch.tv/videos/1", 3725) == "https://www.twitch.tv/videos/1?t=1h2m5s"
assert collect.ts_link("https://www.youtube.com/watch?v=x", 61) == "https://www.youtube.com/watch?v=x&t=61"

# moteur -> hub : detect rend un RESULT qui passe en JSON, finish (téléchargement simulé) crée les extraits et la fiche d'analyse
import shutil  # noqa: E402
import subprocess  # noqa: E402
vid = store.TMP / "test.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "30", str(vid)], check=True)
collect.chat_signals = lambda job, url, m, lo, hi: [sig(100, 120, "chat", 1.0, "chat x2")]
collect.audio_signals = lambda job, key, lo, hi, *a: [sig(500, 510, "audio", .7, "audio rires 0.7")]
collect.download = lambda url, cid, s0=None, s1=None: shutil.copy(vid, store.CLIPS / f"{cid}.mp4")
url, jd = "https://www.youtube.com/watch?v=test", jobs.Job("collect", "test", None, ())
res = json.loads(json.dumps(collect.detect(jd, url, {"title": "T", "duration": 3600, "width": 1920, "height": 1080}, 2, "",
                                           [(95, 125)], ai_on=False)))
assert [g["t0"] for g in res["auto"]] == [499.0] and res["cands"] is None and res["meta"]["duration"] == 3600, res
assert collect.finish(jd, url, dict(res, steps=[["Whisper", 9]], total=60), 2, "Kai", machine="PC-test") == 1
src = next(s for s in store.load()["sources"].values() if s["url"] == url)
assert src["streamer"] == "Kai" and src["analyses"][0]["machine"] == "PC-test" and src["analyses"][0]["total"] == 60
assert src["analyses"][0]["steps"][0] == ["Whisper", 9] and collect.taken_for(store.load(), url) and collect.taken_for(store.load(), "x") == []
# finalisation reprise après un arrêt du hub (même résultat une 2e fois) : rien n'est retéléchargé, pas de doublon dans le Tri
n_clips = len(store.load()["clips"])
assert collect.finish(jd, url, res, 2, "Kai") == 0 and len(store.load()["clips"]) == n_clips
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

# coupes : morceaux bornés au fichier, triés, fusionnés s'ils se chevauchent ou se touchent, miettes retirées ; 1 morceau = in/out
import time  # noqa: E402
import app  # noqa: E402
with store.tx() as db:
    db["sources"]["sc"] = {"id": "sc", "url": "u-cut", "streamer": "Geo", "streamer_url": "https://www.twitch.tv/geo"}
    db["clips"]["cut"] = {"id": "cut", "source_id": "sc", "status": "new", "in": 2.0, "out": 20.0, "file_dur": 30.0, "added": store.now()}
c = app.patch_clip("cut", {"segs": [[20, 40], [5, 10], [8, 12], [-3, 1], [15, 15.2]]}, "u")
assert c["segs"] == [[0, 1], [5, 12], [20, 30]] and (c["in"], c["out"]) == (0, 30) and store.cdur(c) == 18 - 2 * store.XF, c
c = app.patch_clip("cut", {"in": 6, "out": 25}, "u")  # ancien réglage début/fin : rogne les morceaux
assert c["segs"] == [[6, 12], [20, 25]] and store.cdur(c) == 11 - store.XF, c  # fondu entre les 2 morceaux
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

# export d'un extrait coupé en 2 morceaux (avec son, puis sans son) : un seul segment = morceaux - fondu de 0,25 s entre eux
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
rec = montage.export(jobs.Job("export", "test", None, ()))
seg = sorted((store.DATA / rec["dir"] / "segments").glob("*.mp4"))
assert [abs(collect.probe(f)["dur"] - d) < .2 for f, d in zip(seg, (6.75, 5.25))] == [True, True], [collect.probe(f) for f in seg]
assert all(collect.probe(f)["audio"] for f in seg) and abs(rec["dur"] - 12.0) < .3, rec
assert (store.DATA / rec["dir"] / "liste.txt").exists()  # ancien chemin (copie)

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
assert "0:00 Intro\n0:20 Lacy\n0:39 Jynxzi\n0:59 Final\n" in desc and "dans la vidéo" not in desc, desc
assert desc.count("▶ Kai — https://www.twitch.tv/kai") == 1 and "▶ Lacy — https://www.twitch.tv/lacy" in desc, desc
assert abs(store.bestof_total(list(d4["clips"].values())) - 78.8) < 1e-9 and store.bestof_total(list(d4["clips"].values()), False) == 80
d4["project"]["transitions"] = False
assert "0:00 Intro\n0:20 Lacy\n0:40 Jynxzi\n1:00 Final\n" in montage.description(d4)

# musiques de fond : format, son et durée vérifiés à l'envoi (un fichier refusé est supprimé)
import io  # noqa: E402


def tone(sec, f):
    w = store.TMP / f"{f}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=44100:duration={sec}",
                    "-ac", "1", str(w)], check=True)
    return w.read_bytes()


for name, data in (("note.txt", b"x"), ("faux.mp3", b"pas du son" * 100), ("court.wav", tone(3, 300))):
    try:
        app.add_music(name, len(data), io.BytesIO(data))
        raise AssertionError(f"musique acceptée : {name}")
    except ValueError:
        pass
assert not any(store.MUSIC.iterdir())
b1, b2 = tone(11, 440), tone(11, 660)
m1, m2 = app.add_music("C:\\Musique\\Piste A.wav", len(b1), io.BytesIO(b1), "u"), app.add_music("Piste B.WAV", len(b2), io.BytesIO(b2))
assert (m1["name"], m2["name"]) == ("Piste A", "Piste B") and abs(m1["dur"] - 11) < .2 and (store.DATA / m1["file"]).is_file(), m1
assert [m["id"] for m in app.state()["music"]] == [m1["id"], m2["id"]]
assert store.load()["project"]["music"] == [m1["id"], m2["id"]]  # envoyée = déjà dans le best-of en cours
pj = app.patch_project({"music": [m1["id"], "zz", m2["id"], m1["id"]], "music_vol": 30, "transitions": True})
assert (pj["music"], pj["music_vol"], pj["transitions"]) == ([m1["id"], m2["id"]], 30, True), pj

# export avec transitions + 2 musiques (répétées : 19 s de musique pour 20,2 s de vidéo) : 3 × 7 s - 2 transitions
with store.tx() as db:
    for k in range(3):
        db["clips"][f"t{k}"] = {"id": f"t{k}", "source_id": "sc", "status": "kept", "file": "media/clips/av.mp4", "src_start": 0,
                                "in": k * 8, "out": k * 8 + 7, "file_dur": 30.0, "fps": 30, "rating": 4, "category": "FUN", "flags": {}}
    db["project"]["items"] = ["t0", "t1", "t2"]
rec = montage.export(jobs.Job("export", "test", None, ()))
sd = montage.stream_durs(store.DATA / rec["files"]["mp4"])
assert abs(rec["dur"] - (21 - 2 * store.XT)) < .3 and abs(sd["audio"] - sd["video"]) < .1, (rec, sd)
assert "Musique : Piste A, Piste B" in (store.DATA / rec["files"]["description"]).read_text("utf-8")
assert not (store.DATA / rec["dir"] / "liste.txt").exists() and not (store.DATA / rec["dir"] / "musique.wav").exists()
assert app.del_music(m2["id"]) and store.load()["project"]["music"] == [m1["id"]] and not (store.DATA / m2["file"]).exists()
assert [m["id"] for m in store.load()["music"].values()] == [m1["id"]]

# aucune musique choisie : tirée au hasard dans la bibliothèque à l'export et gardée dans le best-of ; case décochée : aucune
app.patch_project({"music": []})
rec = montage.export(jobs.Job("export", "test", None, ()))
assert store.load()["project"]["music"] == [m1["id"]] and "Musique : Piste A" in (store.DATA / rec["files"]["description"]).read_text("utf-8")
app.patch_project({"music_on": False})
assert montage.music_of(store.load()) == [] and store.load()["project"]["music_on"] is False
app.patch_project({"music_on": True})

# ajout à la main « tel quel dans le Tri » (fichier local) : un passage = un extrait en tête du Tri ; > 30 min refusé
long = store.TMP / "long.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=64x36:rate=1", "-t", "1900", str(long)], check=True)
jw = jobs.Job("collect", "test", None, ())
try:
    collect.collect(jw, str(long), mode="whole", by="Azer")
    raise AssertionError("vidéo de 31 min acceptée en entier")
except RuntimeError as e:
    assert "Trop long" in str(e)
try:
    collect.parse_window("2:00-1:00")
    raise AssertionError("passage à l'envers accepté")
except ValueError:
    pass
assert collect.collect(jw, str(long), 10, "1:00-1:20", "Peanut", mode="whole", by="Azer") == 1
c = max(store.load()["clips"].values(), key=lambda c: c.get("added") or "")
src = store.load()["sources"][c["source_id"]]
assert (c["score"], c["why"], c["src_start"], src["added_by"], src["streamer"]) == (50, "ajouté par Azer", 60, "Azer", "Peanut"), c
assert abs(store.cdur(c) - 20) < 1.5, c
assert app.next_clips("v", set())["clips"][0]["id"] == c["id"]  # en tête du Tri
try:
    collect.collect(jw, str(long), 10, "1:10-1:30", "Peanut", mode="whole", by="Azer")
    raise AssertionError("passage déjà ajouté accepté")
except RuntimeError as e:
    assert "Déjà ajoutée" in str(e)
assert collect.streamer_of({"channel": "xQc"}, "https://clips.twitch.tv/AwkwardHelplessSalamander")[1] == "https://www.twitch.tv/xqc"

shutil.rmtree(store.DATA, ignore_errors=True)
print("selftest ok")
