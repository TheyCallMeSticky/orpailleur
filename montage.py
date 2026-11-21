"""Best-of : ordre automatique, remplissage jusqu'à la durée cible, alertes, description YouTube (chapitres + crédits),
tableau des sources (CSV), export MP4 1080p + timeline FCPXML pour DaVinci Resolve."""
import csv
import random
import io
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import store
from collect import fmt, probe, run, tools_check, ts_link

FONT = store.ROOT / "fonts" / "Poppins-Bold.ttf"


def _key(c):
    return (-(c.get("rating") or 0), -(c.get("score") or 0))


MAX_PER_STREAMER = 2  # « Remplir » : au plus 2 extraits du même streamer


def streamer_of(db):
    return lambda c: (db["sources"].get(c["source_id"]) or {}).get("streamer", c["source_id"])


def auto_order(clips, who=lambda c: c.get("source_id")):
    """1er = le meilleur (sans violence : les pubs sont bloquées si violence graphique dans les 8 premières secondes),
    dernier = le 2e meilleur (annoncé dès l'intro), au milieu un ordre aléatoire sans le même streamer (who) ni la même catégorie
    deux fois de suite. Chaque appel donne un nouveau tirage."""
    cs = sorted(clips, key=_key)
    if len(cs) < 3:
        return [c["id"] for c in cs]
    first = next((c for c in cs if not (c.get("flags") or {}).get("violence")), cs[0])
    rest = [c for c in cs if c is not first]
    last = rest.pop(0)

    def clash(x, y):
        return (who(x) == who(y)) + (bool(x.get("category")) and x.get("category") == y.get("category"))

    best, best_bad = None, None
    for _ in range(300):  # tirages au hasard, on garde le premier sans aucun voisinage interdit (sinon le moins mauvais)
        pool, seq = rest[:], [first]
        random.shuffle(pool)
        while pool:
            ok = [c for c in pool if not clash(seq[-1], c) and (len(pool) > 1 or not clash(c, last))]
            c = ok[0] if ok else pool[0]
            pool.remove(c)
            seq.append(c)
        seq.append(last)
        bad = sum(clash(seq[k - 1], seq[k]) for k in range(1, len(seq)))
        if best_bad is None or bad < best_bad:
            best, best_bad = seq, bad
        if not bad:
            break
    return [c["id"] for c in best]


def fill(db):
    """Ajoute les meilleurs extraits gardés (non utilisés) jusqu'à la durée cible, au plus 2 par streamer, puis l'ordre auto."""
    pj, who, tr = db["project"], streamer_of(db), db["project"].get("transitions", True)
    cs = [db["clips"][i] for i in pj["items"] if i in db["clips"]]
    count = {}
    for c in cs:
        count[who(c)] = count.get(who(c), 0) + 1
    pool = sorted((c for c in db["clips"].values() if c["status"] == "kept" and not c.get("used_in") and c["id"] not in pj["items"]),
                  key=_key)
    for c in pool:
        if store.bestof_total(cs, tr) >= pj["target"] - 10:
            break
        if store.bestof_total(cs + [c], tr) > pj["target"] + 30 or count.get(who(c), 0) >= MAX_PER_STREAMER:
            continue
        cs.append(c)
        count[who(c)] = count.get(who(c), 0) + 1
    pj["items"] = auto_order(cs, who)


def warnings(db):
    pj = db["project"]
    cs = [db["clips"][i] for i in pj["items"] if i in db["clips"]]
    w = []
    if not cs:
        return w
    total = store.bestof_total(cs, pj.get("transitions", True))
    if total < pj["target"] * .8:
        w.append(f"Il manque {fmt(pj['target'] - total)} pour atteindre l'objectif.")
    elif total > pj["target"] * 1.25:
        w.append(f"{fmt(total - pj['target'])} de trop par rapport à l'objectif : coupe les extraits les plus faibles.")
    if (cs[0].get("flags") or {}).get("violence"):
        w.append("Le 1er extrait est marqué « violence » : une violence graphique dans les 8 premières secondes empêche les publicités.")
    best = max(c.get("rating") or 0 for c in cs)
    if (cs[0].get("rating") or 0) < best:
        w.append("Le 1er extrait n'est pas le mieux noté : ouvre sur ton meilleur moment (« Ordre auto » le fait).")
    who = streamer_of(db)
    twice = [i + 1 for i in range(1, len(cs)) if who(cs[i]) == who(cs[i - 1])]
    if twice:
        w.append("Même streamer deux fois de suite aux extraits n° " + ", ".join(f"{i - 1}-{i}" for i in twice) + " (« Ordre auto » l'évite).")
    radio = [i + 1 for i, c in enumerate(cs) if (c.get("flags") or {}).get("radio")]
    if radio:
        w.append("Radio / musique dans les extraits n° " + ", ".join(map(str, radio)) + " : musique protégée, coupe ou baisse le son.")
    cine = [i + 1 for i, c in enumerate(cs) if (c.get("flags") or {}).get("cinematic")]
    if cine:
        w.append("Cinématique / fin de l'histoire dans les extraits n° " + ", ".join(map(str, cine)) + " : Rockstar fait retirer ces vidéos.")
    no_auth = sorted({db["sources"][c["source_id"]]["streamer"] for c in cs
                      if db["sources"].get(c["source_id"], {}).get("auth") != "obtenue"})
    if no_auth:
        w.append("Autorisation pas encore obtenue : " + ", ".join(no_auth) + " (onglet Streamers).")
    return w


# ---------------------------------------------------------------- textes

def credit(s):
    """Texte du crédit à l'écran : le lien de la chaîne sans « https://www. » (ex. twitch.tv/kaicenat), sinon le nom."""
    return re.sub(r"^https?://(www\.)?", "", (s.get("streamer_url") or "").rstrip("/")) or s.get("streamer", "")


def _rows(db, durs=None):
    """[(clip, source, start_in_bestof, dur)] dans l'ordre du best-of (avec transitions : chaque extrait commence 0,4 s plus tôt)."""
    out, t = [], 0.0
    xt = store.XT if db["project"].get("transitions", True) else 0
    for cid in db["project"]["items"]:
        c = db["clips"].get(cid)
        if not c:
            continue
        d = durs[len(out)] if durs else store.cdur(c)
        out.append((c, db["sources"].get(c["source_id"], {}), t, d))
        t += d - xt
    return out


def music_of(db):
    """Musiques choisies pour le best-of, dans l'ordre (celles supprimées de la bibliothèque sont ignorées) ; aucune si la case
    « Musique de fond » est décochée."""
    lib = db.get("music") or {}
    if not db["project"].get("music_on", True):
        return []
    return [lib[m] for m in db["project"].get("music") or [] if m in lib]


def description(db, durs=None):
    rows = _rows(db, durs)
    L = [db["project"]["name"], ""]
    chapters = []
    for k, (c, _s, t, d) in enumerate(rows):
        name = "Intro" if not chapters else "Final" if k == len(rows) - 1 else _s.get("streamer") or "Moments"
        if chapters and chapters[-1][1] == name:
            continue
        if chapters and t - chapters[-1][0] < 10:   # YouTube : chaque chapitre doit durer au moins 10 s
            continue
        chapters.append((t, name))
    if len(chapters) >= 3:
        L += ["Chapters:"] + [f"{fmt(t)} {n}" for t, n in chapters] + [""]
    L.append("Credits: thanks to the streamers! Go follow them:")  # description YouTube en anglais
    seen = {}
    for _c, s, _t, _d in rows:  # un streamer par ligne, avec le lien de sa chaîne (les liens horodatés restent dans le CSV)
        seen.setdefault(s.get("streamer", "Inconnu"), s)
    for name, s in seen.items():
        L.append(f"▶ {name}" + (f" — {s['streamer_url']}" if s.get("streamer_url") else ""))
    music = music_of(db)
    if music:
        L += ["", "Music: " + ", ".join(m["name"] for m in music)]
    L += ["", "Subscribe for the next GTA 6 best-of!"]
    return "\n".join(L) + "\n"


def sources_csv(db, durs=None):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["N°", "Créateur", "Chaîne", "Plateforme", "Source (horodatée)", "Catégorie", "Note", "Durée",
                "Début dans le best-of", "Autorisation", "Pourquoi détecté"])
    for i, (c, s, t, d) in enumerate(_rows(db, durs), 1):
        w.writerow([i, s.get("streamer", ""), s.get("streamer_url", ""), s.get("platform", ""),
                    ts_link(s.get("url", ""), c["src_start"] + c["in"]), c.get("category") or "", c.get("rating") or "",
                    fmt(d), fmt(t), s.get("auth", ""), c.get("why", "")])
    return "﻿" + buf.getvalue()   # BOM : accents corrects dans Excel / Google Sheets


# ---------------------------------------------------------------- export

def _file_url(p):
    """URL utilisable par DaVinci Resolve sous Windows quand l'app tourne dans WSL (\\\\wsl.localhost\\...)."""
    try:
        w = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True).stdout.strip()
    except OSError:
        w = ""
    if w.startswith("\\\\"):
        return "file:" + urllib.parse.quote(w.replace("\\", "/"), safe="/:")
    if re.match(r"[A-Za-z]:\\", w):
        return "file:///" + urllib.parse.quote(w.replace("\\", "/"), safe="/:")
    return Path(p).resolve().as_uri()


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def fcpxml(name, segs, fps):
    """segs: [(path, frames, clip, source)] -> FCPXML 1.8 (DaVinci Resolve : Fichier > Importer > Timeline)."""
    fd = f"1/{fps}s"
    res = [f'<format id="r0" name="FFVideoFormat1080p{fps}" frameDuration="{fd}" width="1920" height="1080"/>']
    spine, off = [], 0
    for i, (p, fr, c, s) in enumerate(segs, 1):
        res.append(f'<asset id="a{i}" name="{_esc(p.stem)}" src="{_esc(_file_url(p))}" start="0s" duration="{fr}/{fps}s" '
                   f'hasVideo="1" hasAudio="1" format="r0" audioSources="1" audioChannels="2" audioRate="48000"/>')
        label = f"{c.get('category') or '?'} {'★' * (c.get('rating') or 0)} — {s.get('streamer', '')}"
        spine.append(f'<asset-clip ref="a{i}" name="{_esc(p.stem)}" offset="{off}/{fps}s" start="0s" duration="{fr}/{fps}s" '
                     f'format="r0" tcFormat="NDF"><marker start="0s" duration="{fd}" value="{_esc(label)}"/></asset-clip>')
        off += fr
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.8">\n<resources>\n'
            + "\n".join(res) + f'\n</resources>\n<library><event name="{_esc(name)}"><project name="{_esc(name)}">'
            f'<sequence format="r0" duration="{off}/{fps}s" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k"><spine>\n'
            + "\n".join(spine) + "\n</spine></sequence></project></event></library>\n</fcpxml>\n")


TRANSITIONS = ("fade", "smoothleft", "zoomin", "fadewhite", "hblur", "pixelize")  # fondu, glissé, zoom, flash blanc, flou, pixels


def pick_transitions(n):
    """n transitions xfade : les 6 dans un ordre aléatoire, puis encore les 6…, jamais la même deux fois de suite."""
    out = []
    while len(out) < n:
        bag = random.sample(TRANSITIONS, len(TRANSITIONS))
        if out and bag[0] == out[-1]:
            bag.reverse()
        out += bag
    return out[:n]


def stream_durs(p):
    """{"video": s, "audio": s} : durée de chaque flux (celle du fichier est la plus longue des deux)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(p)],
                       capture_output=True, text=True)
    return {s["codec_type"]: float(s.get("duration") or 0) for s in json.loads(r.stdout or "{}").get("streams", [])}


def music_bed(dst, music, total, vol):
    """Musique de fond de la durée de la vidéo : les morceaux dans l'ordre (répétés si la vidéo est plus longue) reliés par
    des fondus de 3 s, volume vol, fondu d'entrée 1 s, fondu de sortie 5 s qui finit pile à la fin."""
    seq, L = [], 0.0
    while L < total + 1:  # morceaux de 10 s minimum (vérifié à l'ajout) : chacun avance d'au moins 7 s
        m = music[len(seq) % len(music)]
        L += m["dur"] - (3 if seq else 0)
        seq.append(m)
    fc = [f"[{k}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[m{k}]" for k in range(len(seq))]
    c = "[m0]"
    for k in range(1, len(seq)):
        fc.append(f"{c}[m{k}]acrossfade=d=3[c{k}]")
        c = f"[c{k}]"
    fc.append(f"{c}atrim=end={total:.3f},volume={vol:.3f},afade=t=in:d=1,afade=t=out:st={max(0, total - 5):.3f}:d=5[out]")
    run(["ffmpeg", "-v", "error", "-y", *[x for m in seq for x in ("-i", store.DATA / m["file"])], "-filter_complex", ";".join(fc),
         "-map", "[out]", "-c:a", "pcm_s16le", dst])


def assemble(job, mp4, files, durs, names, music, vol, fps, crf):
    """Une passe sur tous les segments : transitions xfade (names, une par raccord) ou simple mise bout à bout, musique de fond
    baissée par la voix des streamers, encodage par la carte graphique (NVENC), sinon par le processeur."""
    n, xt = len(files), store.XT
    total = sum(durs) - xt * len(names)
    # son de chaque segment complété ou coupé pile à la durée de son image (compté en échantillons à 48 kHz, ses horodatages
    # ne sont pas fiables) : il dépasse de quelques dizaines de ms, ce qui décalerait le son de l'image au fil des extraits
    fc = [f"[{k}:a]asetpts=N/SR/TB,apad,atrim=end_sample={round(d * 48000)}[s{k}]" for k, d in enumerate(durs)]
    v, a, L = "[0:v]", "[s0]", durs[0]
    for k, name in enumerate(names, 1):  # raccord k : la fin de la vidéo jusqu'ici et le début du segment k se chevauchent de xt
        fc += [f"{v}[{k}:v]xfade=transition={name}:duration={xt}:offset={L - xt:.3f}[v{k}]", f"{a}[s{k}]acrossfade=d={xt}[a{k}]"]
        v, a, L = f"[v{k}]", f"[a{k}]", L + durs[k] - xt
    if not names:
        fc.append("".join(f"[{k}:v][s{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
        v, a = "[v]", "[a]"
    inputs = [x for f in files for x in ("-i", f)]
    bed = mp4.with_name("musique.wav")
    if music:
        music_bed(bed, music, total, vol)
        inputs += ["-i", bed]
        fc += [f"{a}asplit=2[voix][sc]", f"[{n}:a][sc]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=350[md]",
               "[voix][md]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ao]"]
        a = "[ao]"
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", v, "-map", a]
    out = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", mp4]
    try:
        run(cmd + ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-g", str(fps * 2), *out])
    except RuntimeError:
        job.log("Carte graphique indisponible pour l'encodage : la vidéo est encodée par le processeur (plus lent).")
        run(cmd + ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-g", str(fps * 2), *out])
    bed.unlink(missing_ok=True)


def export(job):
    tools_check()
    conf = store.config()
    with store.lock:
        db = json.loads(json.dumps(store.load()))
    pj = db["project"]
    clips = [db["clips"][i] for i in pj["items"] if i in db["clips"]]
    if not clips:
        raise RuntimeError("Le best-of est vide.")
    fps = int(conf["fps"]) if str(conf["fps"]) in ("30", "60") else (60 if min(c.get("fps") or 30 for c in clips) >= 50 else 30)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", pj["name"]).strip("_") or "bestof"
    out = store.EXPORTS / f"{time.strftime('%Y%m%d_%H%M%S')}_{slug}"
    segdir = out / "segments"
    segdir.mkdir(parents=True)
    job.log(f"Export de {len(clips)} extraits en 1920×1080 {fps} i/s, son normalisé à -14 LUFS")
    segs, durs = [], []
    for i, c in enumerate(clips, 1):
        s = db["sources"].get(c["source_id"], {})
        src = store.DATA / c["file"]
        d = store.cdur(c)
        dst = segdir / f"{i:02d}_{c.get('category') or 'X'}_{re.sub(r'[^A-Za-z0-9]+', '', s.get('streamer', ''))[:20]}_{c['id']}.mp4"
        job.log(f"  {i}/{len(clips)}  {s.get('streamer', '')}  {fmt(d)}")
        vf = (f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
              f"setsar=1,fps={fps},format=yuv420p")
        label = credit(s)
        if conf.get("lower_third") and label and FONT.exists():  # lien de la chaîne en haut à gauche, tout l'extrait
            tf = segdir / f"{i:02d}.txt"
            tf.write_text(label, "utf-8")
            vf += (f",drawtext=fontfile='{FONT}':textfile='{tf}':x=40:y=40:fontsize=34:fontcolor=white:"
                   f"box=1:boxcolor=black@0.5:boxborderw=12")
        af = f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,afade=t=in:d=0.05,afade=t=out:st={max(0, d - .08):.3f}:d=0.08"
        has_audio, pieces = probe(src)["audio"], store.segs(c)
        silence = [] if has_audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]  # extrait sans son : silence
        if len(pieces) == 1:
            cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{c['in']:.3f}", "-i", src, *silence, "-t", f"{d:.3f}", "-map", "0:v:0",
                   "-map", "0:a:0" if has_audio else "1:a:0", "-vf", vf, "-af", af]
        else:  # extrait coupé : ses morceaux reliés par un léger fondu (image + son), puis la même mise en forme
            fc = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,fps={fps},settb=AVTB[v{k}];"
                         for k, (a, b) in enumerate(pieces))
            if has_audio:
                fc += "".join(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{k}];" for k, (a, b) in enumerate(pieces))
            v, a_, L = "[v0]", "[a0]", pieces[0][1] - pieces[0][0]
            for k, x in enumerate(store.joins(pieces), 1):  # fondu k entre les morceaux k-1 et k : ils se chevauchent de x s
                fc += f"{v}[v{k}]xfade=transition=fade:duration={x:.3f}:offset={L - x:.3f}[x{k}];"
                if has_audio:
                    fc += f"{a_}[a{k}]acrossfade=d={x:.3f}[y{k}];"
                v, a_, L = f"[x{k}]", f"[y{k}]", L + pieces[k][1] - pieces[k][0] - x
            fc += f"{v}{vf}[vo];[{a_.strip('[]') if has_audio else '1:a'}]{af}[ao]"
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, *silence, "-filter_complex", fc, "-map", "[vo]", "-map", "[ao]",
                   "-t", f"{d:.3f}"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", str(conf.get("crf", 18)), "-g", str(fps * 2),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", dst]
        run(cmd)
        segs.append((dst, c, s))
        durs.append(stream_durs(dst).get("video") or d)
    mp4 = out / f"{slug}.mp4"
    if pj.get("music_on", True) and not music_of(db) and db.get("music"):  # aucune choisie : tirées au hasard, gardées pour le site
        pj["music"] = random.sample(list(db["music"]), min(3, len(db["music"])))  # (copie locale : la description les cite)
        with store.tx() as live:
            live["project"]["music"] = list(pj["music"])
        job.log("Aucune musique choisie : tirage au hasard dans la bibliothèque.")
    tr, music = pj.get("transitions", True) and len(segs) > 1, music_of(db)
    if tr and min(durs) < 2 * store.XT + .1:  # pas la place pour une transition à chaque bout d'un extrait si court
        tr = pj["transitions"] = False  # (copie locale : description et CSV sans transitions)
        job.log("Transitions désactivées : un extrait dure moins d'une seconde.")
    job.log("Assemblage…")
    if tr or music:
        names = pick_transitions(len(segs) - 1) if tr else []
        if names:
            job.log(f"Transitions : 6 types, ordre aléatoire ({len(names)} × 0,4 s)")
        if music:
            job.log("Musique : " + " → ".join(m["name"] for m in music))
        assemble(job, mp4, [p for p, _c, _s in segs], durs, names, music, pj.get("music_vol", 20) / 100 * 1.75, fps,
                 conf.get("crf", 18))
    else:  # ni transition ni musique : les segments mis bout à bout sans réencodage
        lst = out / "liste.txt"
        lst.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p, _c, _s in segs), "utf-8")
        run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", mp4])
    (out / f"{slug}.fcpxml").write_text(fcpxml(pj["name"], [(p, round(d * fps), c, s) for (p, c, s), d in zip(segs, durs)], fps), "utf-8")
    (out / "description_youtube.txt").write_text(description(db, durs), "utf-8")
    (out / "sources.csv").write_text(sources_csv(db, durs), "utf-8")
    total = probe(mp4)["dur"]
    rel = out.relative_to(store.DATA).as_posix()
    rec = {"id": store.uid(), "project": pj["name"], "created": store.now(), "dur": round(total, 1), "dir": rel,
           "files": {"mp4": f"{rel}/{mp4.name}", "fcpxml": f"{rel}/{slug}.fcpxml",
                     "description": f"{rel}/description_youtube.txt", "csv": f"{rel}/sources.csv"}}
    with store.tx() as live:
        live["exports"].insert(0, rec)
        for c in clips:
            if c["id"] in live["clips"]:
                live["clips"][c["id"]]["used_in"] = pj["name"]
        n, size = purge_trash(live, {c["id"] for c in db["clips"].values() if c["status"] == "trash"})  # pas celle d'après
    job.log(f"Vidéo prête : {fmt(total)} — {mp4}")
    if n:
        job.log(f"Poubelle vidée : {n} extrait(s) supprimé(s) définitivement ({size / 1e9:.1f} Go libérés)")
    return rec


def purge_trash(db, ids):
    """Après un export : les extraits à la poubelle sont supprimés pour de bon (vidéo, vignette, fiche). Leur passage reste
    noté dans la source (« trashed ») pour qu'une réanalyse ne le repropose pas. -> (nombre, octets libérés)"""
    gone, size = [c for c in db["clips"].values() if c["status"] == "trash" and c["id"] in ids], 0
    for c in gone:
        for rel in (c.get("file"), c.get("thumb")):
            f = store.DATA / rel if rel else None
            if f and f.is_file():
                size += f.stat().st_size
                f.unlink()
        s = db["sources"].get(c["source_id"])
        if s is not None:
            s.setdefault("trashed", []).append([round(c["src_start"] + c["in"], 2), round(c["src_start"] + c["out"], 2)])
        del db["clips"][c["id"]]
    return len(gone), size
