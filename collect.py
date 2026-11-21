"""Collecte : à partir d'une VOD / d'un stream (Twitch, YouTube, Kick, fichier local), trouve les meilleurs moments avec la
détection de gta6-clipper (vendor/gta6clipper) puis télécharge chaque moment en 16:9 plein écran (1080p max).

Signaux combinés (chaque moment candidat = un groupe de signaux proches) :
  - clips créés par les viewers sur la VOD Twitch (chat.fetch_clips)      -> signal le plus fort : le public a déjà trié
  - pics de chat, relatifs au rythme normal du streamer (chat.find_spikes)  -> Twitch VOD, replay de live YouTube
  - audio : rires, réactions, crashs, poursuites... (moments.py, mode audio seul, sans OpenCV)
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import store

sys.path.insert(0, str(store.ROOT / "vendor" / "gta6clipper"))

PAD = 8              # secondes téléchargées en plus de chaque côté (pour pouvoir élargir l'extrait au réglage)
MIN_LEN, MAX_LEN = 12, 60
CLIP_MAX = 150       # une vidéo plus courte est importée telle quelle (c'est déjà un clip)
WHOLE_MAX = 1800     # « Mettre directement dans le Tri » : 30 min au plus (sinon un passage, ou la recherche des meilleurs moments)
AUDIO_W = {"funny": .9, "reaction": .7, "crash": .6, "stunt": .6, "chase": .45, "fight": .5}
AUDIO_FR = {"funny": "rires", "reaction": "réaction", "crash": "crash", "stunt": "cascade", "chase": "poursuite", "fight": "baston"}


# ---------------------------------------------------------------- outils

def tools_check():
    miss = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    for mod, name in (("yt_dlp", "yt-dlp"), ("numpy", "numpy")):
        try:
            __import__(mod)
        except ImportError:
            miss.append(name)
    if miss:
        raise RuntimeError("Outils manquants : " + ", ".join(miss) + ". Voir README.md > Installation.")


def run(cmd, job=None, timeout=None):
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-30]
        if job and re.search(r"ERROR|error", line):
            job.log("   ", line[:300])
    rc = p.wait(timeout)
    if rc:
        raise RuntimeError(f"{Path(str(cmd[0])).name} a échoué ({rc}) : " + " | ".join(tail[-3:])[:600])
    return "\n".join(tail)


def ytdlp(*a):
    return [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", "-N", "8", *a]  # 8 morceaux en parallèle


class Skip(RuntimeError):
    """VOD à écarter pour de bon. code = "restricted" (réservée aux abonnés) | "vertical" (format Shorts)."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def meta(url):
    r = subprocess.run(ytdlp("-J", "--flat-playlist", url), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    if r.returncode:
        if re.search(r"subscriber|logged in|members-only|members only|Join this channel", r.stderr):
            raise Skip("restricted", "VOD réservée aux abonnés de la chaîne : ignorée (on ne prend que les VODs gratuites).")
        raise RuntimeError("yt-dlp ne peut pas lire ce lien : " + r.stderr.strip()[-400:])
    return json.loads(r.stdout)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
                        "-of", "json", str(path)], capture_output=True, text=True)
    j = json.loads(r.stdout or "{}")
    st = j.get("streams", [])
    v = next((s for s in st if s.get("codec_type") == "video"), {})
    n, _, d = (v.get("r_frame_rate") or "30/1").partition("/")
    try:
        fps = float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return {"dur": float(j.get("format", {}).get("duration") or 0), "w": v.get("width", 0), "h": v.get("height", 0),
            "fps": round(fps, 3), "audio": any(s.get("codec_type") == "audio" for s in st)}


def hms(x):
    s = 0.0
    for p in str(x).strip().split(":"):
        s = s * 60 + float(p)
    return s


def parse_window(w):
    w = (w or "").strip()
    if not w:
        return None
    a, _, b = w.partition("-")
    try:
        lo, hi = hms(a), hms(b)
    except ValueError:
        lo = hi = 0
    if hi <= lo:
        raise ValueError("Passage invalide : écris-le comme 1:02:00-1:05:30 (début-fin).")
    return lo, hi


def fmt(s):
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def platform(url):
    for k in ("twitch", "youtube", "youtu.be", "kick"):
        if k in url:
            return "youtube" if k == "youtu.be" else k
    return "local" if not re.match(r"https?://", url) else "autre"


def ts_link(url, t):
    """Lien source qui démarre au bon moment (pour les crédits)."""
    t = int(max(0, t))
    pf = platform(url)
    if pf == "local":
        return url
    if pf == "twitch":
        return f"{url.split('?')[0]}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"
    return url + ("&" if "?" in url else "?") + f"t={t}"


def streamer_of(m, url):
    name = m.get("channel") or m.get("creator") or m.get("uploader") or m.get("uploader_id") or ""
    link = m.get("channel_url") or m.get("uploader_url") or ""
    pf = platform(url)
    if pf == "twitch":
        login = m.get("uploader_id") if "/videos/" in url else ""
        mm = re.search(r"(?://|www\.|m\.)twitch\.tv/([A-Za-z0-9_]{3,})(?:/|$)", url)
        if not login and mm and mm.group(1) not in ("videos", "clip", "clips"):
            login = mm.group(1)
        login = login or name
        if login:
            link = f"https://www.twitch.tv/{login.lower()}"
    elif pf == "kick" and not link and name:
        link = "https://kick.com/" + name.lower()
    return name or "Inconnu", link


# ---------------------------------------------------------------- base

def get_source(db, url, m, override="", override_url=""):
    url = CTX.get("page_url") or url  # Kick : on télécharge le flux HLS mais la source affichée/créditée reste la page de la VOD
    for s in db["sources"].values():
        if s["url"] == url:
            return s, False
    name, link = streamer_of(m, url)
    if override:
        name = override
        if platform(url) == "twitch":
            link = "https://www.twitch.tv/" + override.lower().replace(" ", "")
    if override_url:
        link = override_url
    s = {"id": store.uid(), "url": url, "title": CTX.get("title") or m.get("title") or Path(url).name, "platform": platform(url),
         "streamer": name, "streamer_url": link, "auth": "à demander", "duration": m.get("duration"), "added": store.now()}
    if CTX.get("added_by"):  # vidéo ajoutée à la main depuis le dashboard
        s["added_by"] = CTX.get("added_by")
    db["sources"][s["id"]] = s
    return s, True


def thumb(file, t, cid):
    out = store.THUMBS / f"{cid}.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0, t):.2f}", "-i", str(file), "-frames:v", "1",
                    "-vf", "scale=480:-2", "-q:v", "4", str(out)], capture_output=True)
    return f"media/thumbs/{cid}.jpg" if out.exists() else ""


def add_clip(src, file, src_start, win_in, win_out, why, score, ai=None):
    p = probe(file)
    if not p["w"] or p["dur"] < 1:  # fichier vide ou sans image : c'est un échec, pas un extrait noir dans le Tri
        Path(file).unlink(missing_ok=True)
        raise RuntimeError("fichier téléchargé vide ou sans image")
    d = p["dur"]
    a = min(max(0.0, win_in or 0.0), max(0.0, d - 1))
    b = min(d, win_out) if win_out else d
    if b - a < 1:
        a, b = 0.0, d
    cid = Path(file).stem
    c = {"id": cid, "source_id": src["id"], "file": f"media/clips/{Path(file).name}", "thumb": thumb(file, (a + b) / 2, cid),
         "src_start": round(src_start, 2), "in": round(a, 2), "out": round(b, 2), "file_dur": round(d, 2),
         "w": p["w"], "h": p["h"], "fps": p["fps"], "why": why, "score": round(score, 2), "status": "new",
         "category": None, "rating": None, "flags": {}, "note": "", "by": None, "sorted_at": None, "used_in": None,
         "added": store.now(), "ai": ai}
    with store.tx() as db:
        db["clips"][cid] = c
    return c


_PLAYLISTS = {}  # url de la VOD -> (heure, ([(début, durée, url du morceau)], url du segment d'initialisation ou None))


def parse_m3u8(text, base):
    """Playlist HLS -> [(début en s, durée, url du morceau)], positions cumulées depuis le début de la VOD."""
    segs, t, d = [], 0.0, None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            d = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and d is not None:
            segs.append((t, d, urllib.parse.urljoin(base, line)))
            t, d = t + d, None
    return segs


def m3u8_init(text, base):
    """Segment d'initialisation (#EXT-X-MAP) des VODs en fMP4 : sans lui, les morceaux .mp4 sont illisibles. None pour du .ts."""
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    return urllib.parse.urljoin(base, m.group(1)) if m else None


def covering(segs, s0, s1):
    return [s for s in segs if s[0] + s[1] > s0 and s[0] < s1]


def hls_segments(url):
    """Morceaux (≈10 s) de la VOD Twitch/Kick en 1080p max, avec leur position exacte. Gardé 30 min (10 extraits = 1 seule lecture)."""
    import time as _t
    hit = _PLAYLISTS.get(url)
    if hit and _t.time() - hit[0] < 1800:
        return hit[1]
    r = subprocess.run(ytdlp("-g", "-f", "b", "-S", "vcodec:h264,res:1080", url), capture_output=True, text=True, timeout=120)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError("playlist introuvable : " + r.stderr.strip()[-200:])
    m3u8 = r.stdout.strip().splitlines()[0]
    text = urllib.request.urlopen(m3u8, timeout=60).read().decode()
    segs = parse_m3u8(text, m3u8)
    if not segs:
        raise RuntimeError("playlist vide")
    hit = (segs, m3u8_init(text, m3u8))
    _PLAYLISTS[url] = (_t.time(), hit)
    return hit


def hls_section(url, cid, s0, s1):
    """Passage [s0, s1] d'une VOD Twitch/Kick : on ne télécharge que les morceaux qui le couvrent, puis on les assemble.
    yt-dlp --download-sections passe par ffmpeg qui relit la VOD depuis le début : sur une VOD de 47 h, au-delà de ~27 h il
    ne rend rien. -> (fichier, seconde de VOD où il commence)."""
    from concurrent.futures import ThreadPoolExecutor
    allsegs, init = hls_segments(url)
    segs = covering(allsegs, s0, s1)
    if not segs:
        raise RuntimeError("passage hors de la VOD")

    def get(seg):
        for k in range(3):
            try:
                return urllib.request.urlopen(seg[2], timeout=60).read()
            except Exception:  # noqa: BLE001
                if k == 2:
                    raise
                __import__("time").sleep(2)
    with ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(get, ([(0, 0, init)] if init else []) + segs))
    # des morceaux MPEG-TS se concatènent tels quels ; en fMP4 (de plus en plus de VODs Twitch) il faut le segment d'initialisation devant
    ts = store.TMP / f"{cid}.{'mp4' if init else 'ts'}"
    ts.write_bytes(b"".join(parts))
    out = store.CLIPS / f"{cid}.mp4"
    try:
        run(["ffmpeg", "-v", "error", "-y", "-i", ts, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out])
    finally:
        ts.unlink(missing_ok=True)
    return out, segs[0][0]


def download(url, cid, s0=None, s1=None):
    """Télécharge (une section de) la vidéo en 1080p max, H.264 de préférence (lisible partout)."""
    # son AAC obligatoire si possible : l'Opus (YouTube 251) dans un MP4 se décale à la lecture dans les navigateurs
    args = ["-f", "bv*+ba[acodec^=mp4a]/bv*+ba/b", "-S", "vcodec:h264,res:1080", "--merge-output-format", "mp4",
            "-o", str(store.CLIPS / f"{cid}.%(ext)s")]
    if s0 is not None:
        args += ["--download-sections", f"*{s0:.1f}-{s1:.1f}"]
    for k in range(3):  # erreurs réseau passagères (YouTube « Error opening input file ») : on réessaie
        try:
            run(ytdlp(*args, url))
            break
        except RuntimeError:
            if k == 2:
                raise
            for f in store.CLIPS.glob(cid + ".*"):
                f.unlink(missing_ok=True)
            __import__("time").sleep(3)
    f = next((x for x in sorted(store.CLIPS.glob(cid + ".*")) if x.suffix in (".mp4", ".mkv", ".webm", ".mov")), None)
    if not f:
        raise RuntimeError("fichier introuvable après téléchargement")
    return f


def cut_local(path, cid, s0, s1):
    out = store.CLIPS / f"{cid}.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.2f}", "-i", path, "-t", f"{s1 - s0:.2f}", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out


# ---------------------------------------------------------------- détection

CHAT_SLOTS = __import__("threading").BoundedSemaphore(32)  # 32 requêtes de chat simultanées au total : au-delà Twitch ne va pas plus vite


def fetch_twitch_fast(chat, vid, start, end):
    """Même résultat que chat.fetch_twitch (vérifié : 0 message de différence), en ~40 % de requêtes en moins : la page suivante
    démarre après la dernière seconde reçue (le repo la relisait), sauf si cette seconde est chargée et peut-être coupée."""
    import urllib.request
    out, seen, off = [], set(), start
    while off < end:
        q = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": {"videoID": vid, "contentOffsetSeconds": off},
              "extensions": {"persistedQuery": {"version": 1, "sha256Hash": chat.HASH}}}]
        r = urllib.request.Request("https://gql.twitch.tv/gql", json.dumps(q).encode(),
                                   {"Client-ID": chat.CLIENT_ID, "Content-Type": "application/json"})
        c = None
        for t in range(4):
            try:
                with CHAT_SLOTS:
                    c = json.load(urllib.request.urlopen(r, timeout=30))[0]["data"]["video"]["comments"]
                break
            except Exception:  # noqa: BLE001
                if t == 3:
                    raise
                __import__("time").sleep(2 ** t)
        if not c or not c["edges"]:
            off += 5
            continue
        secs = [e["node"]["contentOffsetSeconds"] for e in c["edges"]]
        last, new = max(secs), 0
        for e in c["edges"]:
            n = e["node"]
            if n["id"] in seen or not start <= n["contentOffsetSeconds"] < end:
                continue
            seen.add(n["id"])
            new += 1
            out.append((n["contentOffsetSeconds"], "".join(f["text"] or "" for f in n["message"]["fragments"]).strip()))
        tail = sum(1 for s in secs if s == last)
        off = last if (tail >= 8 and new) else (last + 1 if last >= off else off + 1)
    return out


def fetch_chat_parallel(chat, vid, lo, hi, job=None):
    """chat.fetch_twitch lit la VOD page par page : on la coupe en 96 tranches lues 32 à la fois.
    fetch_twitch ne garde que les messages de [début, fin[ de sa tranche : la concaténation est exacte, sans doublon.
    Le résultat est gardé dans data/tmp : une réanalyse ou un redémarrage ne relit pas le chat."""
    import threading
    import time as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cache = store.TMP / f"chat_{vid}_{lo}_{hi}.json"
    if cache.exists():
        cache.touch()  # gardé tant qu'il sert (data/tmp est vidé des fichiers non utilisés depuis 36 h)
        if job:
            job.log("Chat déjà lu (cache)")
        return [tuple(m) for m in json.loads(cache.read_text("utf-8"))]
    step = max(120, -(-(hi - lo) // 96))  # 96 tranches (2 min minimum) lues 32 à la fois : la charge reste répartie jusqu'au bout
    cuts = [(a, min(hi, a + step)) for a in range(lo, hi, step)]
    done, total, t0, lk = [0], [0], _t.time(), threading.Lock()

    def one(ab):
        r = fetch_twitch_fast(chat, vid, ab[0], ab[1])
        with lk:
            done[0] += 1
            total[0] += len(r)
            if job and (done[0] % 8 == 0 or done[0] == len(cuts)):
                job.log(f"   chat : {done[0]}/{len(cuts)} tranches lues, {total[0]} messages ({_t.time() - t0:.0f} s)")
        return r
    res = []
    with ThreadPoolExecutor(min(32, len(cuts))) as ex:  # mesuré : 12 fils 350 msg/s, 32 fils 660 msg/s, 48 fils pas mieux
        for f in as_completed([ex.submit(one, ab) for ab in cuts]):
            res.extend(f.result())
    res.sort(key=lambda m: m[0])
    cache.write_text(json.dumps(res, ensure_ascii=False), "utf-8")
    return res


def chat_signals(job, url, m, lo, hi):
    with job.step("Chat et clips viewers"):
        return _chat_signals(job, url, m, lo, hi)


def _chat_signals(job, url, m, lo, hi):
    import chat  # vendor/gta6clipper/chat.py
    sig, msgs = [], None
    vid = re.search(r"twitch\.tv/videos/(\d+)", url)
    if vid:
        try:
            import vods
            allc, complete = vods.vod_clips(vid.group(1))
            cl = [c for c in allc if lo <= c["off"] < hi]
            job.log(f"Clips créés par les viewers sur cette VOD : {len(cl)}" + ("" if complete else " (grosse chaîne : seulement les plus vus)"))
            for c in cl:
                sig.append({"t0": c["off"], "t1": c["off"] + c["dur"], "kind": "clip", "w": 1.5 + min(c["views"] / 2000, 1.5),
                            "why": f"clip viewer « {c['title'][:50]} » ({c['views']} vues)", "url": c["url"], "dur": c["dur"]})
        except Exception as e:  # noqa: BLE001
            job.log("Clips viewers indisponibles :", e)
        try:
            job.log("Lecture du chat Twitch (compter quelques minutes pour une longue VOD)…")
            msgs = fetch_chat_parallel(chat, vid.group(1), int(lo), int(hi), job)
        except Exception as e:  # noqa: BLE001
            job.log("Chat Twitch indisponible :", e)
    elif platform(url) == "youtube" and (m.get("was_live") or m.get("live_status") == "was_live"):
        try:
            job.log("Lecture du chat du replay YouTube…")
            msgs = chat.fetch_youtube(url, lo, hi)
        except Exception as e:  # noqa: BLE001
            job.log("Chat YouTube indisponible :", e)
    if msgs:
        spikes, med = chat.find_spikes(msgs, int(lo), int(hi))
        job.log(f"Chat : {len(msgs)} messages, {len(spikes)} pics (rythme normal ≈ {med:.0f} msg / 10 s)")
        for b, _n, r, tp in spikes:
            words = ", ".join(f"{p} x{k}" for p, k in tp)
            # le chat réagit APRÈS le moment : on prend 20 s avant le pic
            sig.append({"t0": b - 20, "t1": b + 12, "kind": "chat", "w": min(r / 3, 1.5),
                        "why": f"chat x{r:.1f}" + (f" ({words})" if words else "")})
    return sig


def audio_signals(job, src_id, lo, hi, url=None, local=None, full=False):
    import moments  # vendor/gta6clipper/moments.py (mode audio seul : pas d'OpenCV)
    a = store.TMP / f"{src_id}_{int(lo)}_{int(hi)}.m4a"
    if a.exists():
        a.touch()  # gardé tant qu'il sert (data/tmp est vidé des fichiers non utilisés depuis 36 h)
        job.log("Audio déjà téléchargé (cache)")
    else:
        with job.step("Téléchargement de l'audio"):
            if local:
                run(["ffmpeg", "-v", "error", "-y", "-ss", f"{lo:.2f}", "-i", local, "-t", f"{hi - lo:.2f}", "-vn", "-ac", "1",
                     "-ar", "16000", "-c:a", "aac", "-b:a", "64k", a])
            else:
                # piste audio seule si elle existe (Twitch, YouTube), sinon la plus petite vidéo AVEC son (Kick : 160p) : jamais la 1080p
                # vidéo entière : téléchargement direct du fichier (rapide). « --download-sections » passe par ffmpeg en flux continu,
                # que YouTube bride à la vitesse de lecture : 16 h d'audio = des heures. On ne l'utilise que pour une fenêtre.
                sec = [] if full else ["--download-sections", f"*{lo:.0f}-{hi:.0f}"]
                # YouTube : fichier m4a d'un seul tenant (140 puis 139). Les formats HLS 233/234 vont 4x plus vite mais l'audio recollé
                # dérive d'environ 6 s par heure (mesuré) : les extraits seraient décalés. Ailleurs (Twitch, Kick) : la piste la plus légère.
                # dernier recours : la plus PETITE version avec du son (jamais la 1080p entière juste pour l'audio)
                tries = [["-f", f] for f in ("140", "139", "251", "250")] + [["-f", "ba/b", "-S", "+size,+br,+res"]] if platform(url) == "youtube" \
                    else [["-f", "ba/b", "-S", "+size,+br,+res"]]
                for k, fmt_ in enumerate(tries):  # YouTube refuse parfois un format (403) : on passe au suivant
                    try:
                        run(ytdlp(*fmt_, "-x", "--audio-format", "m4a", *sec, "-o", str(a.with_suffix(".%(ext)s")), url))
                        break
                    except RuntimeError as e:
                        if k == len(tries) - 1:
                            raise
                        job.log(f"   audio format {fmt_[1]} refusé ({str(e)[-60:]}), essai suivant")
    with job.step("Analyse audio"):  # rires, réactions, crashs…
        try:
            mo = moments.run(str(a), t0=lo, out=str(a) + ".moments.json")
        except (Exception, SystemExit) as e:  # moments.run sort par SystemExit si pas d'audio
            job.log("Analyse audio impossible :", e)
            return []
    sig = []
    for x in mo:
        if x["type"] in AUDIO_W and x["score"] >= .6:
            sig.append({"t0": x["t0"] - 6, "t1": x["t1"] + 4, "kind": "audio", "w": AUDIO_W[x["type"]] * x["score"],
                        "why": f"audio {AUDIO_FR[x['type']]} {x['score']:.1f}"})
    job.log(f"Audio : {len(sig)} passages marquants")
    return sig


def pick(sig, n, lo, hi, taken):
    """Regroupe les signaux proches, note chaque groupe (bonus si plusieurs types de signaux sont d'accord), garde les n meilleurs
    sans chevauchement (ni entre eux, ni avec les extraits déjà importés de cette source)."""
    groups = []
    for s in sorted(sig, key=lambda s: s["t0"]):
        s = dict(s, t0=max(lo, s["t0"]), t1=min(hi, s["t1"]))
        if s["t1"] <= s["t0"]:
            continue
        if groups and s["t0"] <= groups[-1]["t1"] + 4:
            g = groups[-1]
            g["t1"] = max(g["t1"], s["t1"])
            g["sig"].append(s)
        else:
            groups.append({"t0": s["t0"], "t1": s["t1"], "sig": [s]})
    for g in groups:
        best = max(g["sig"], key=lambda s: s["w"])
        if g["t1"] - g["t0"] > MAX_LEN:
            c = (best["t0"] + best["t1"]) / 2
            g["t0"] = max(g["t0"], c - MAX_LEN / 2)
            g["t1"] = min(g["t1"], g["t0"] + MAX_LEN)
        if g["t1"] - g["t0"] < MIN_LEN:
            e = (MIN_LEN - (g["t1"] - g["t0"])) / 2
            g["t0"], g["t1"] = max(lo, g["t0"] - e), min(hi, g["t1"] + e)
        # score sur la fenêtre finale, le plus fort signal de chaque type (pas la somme : 3 min de cris = des dizaines de petits
        # signaux audio qui battaient un vrai temps fort repéré par le chat et les clips), + bonus si plusieurs types s'accordent
        top = {}
        for s in g["sig"]:
            if s["t0"] < g["t1"] and s["t1"] > g["t0"]:
                top[s["kind"]] = max(top.get(s["kind"], 0), s["w"])
        g["score"] = round(sum(top.values()) + .6 * (len(top) - 1), 2)
        g["why"] = " + ".join(dict.fromkeys(s["why"] for s in sorted(g["sig"], key=lambda s: -s["w"])[:3]))
    chosen = []
    for g in sorted(groups, key=lambda g: -g["score"]):
        if len(chosen) >= n:
            break
        if any(g["t0"] < b and g["t1"] > a for a, b in taken + [(c["t0"], c["t1"]) for c in chosen]):
            continue
        chosen.append(g)
    return chosen  # meilleur score d'abord : les meilleurs moments arrivent en premier dans le Tri


# ---------------------------------------------------------------- points d'entrée

class _Ctx:
    """Infos de la VOD en cours (lien de chaîne, page Kick, titre), propres à chaque fil : plusieurs VODs sont analysées en même temps."""
    _tl = __import__("threading").local()

    def _d(self):
        if not hasattr(self._tl, "d"):
            self._tl.d = {}
        return self._tl.d

    def update(self, **kw):
        self._d().update(kw)

    def get(self, k, default=None):
        return self._d().get(k, default)


CTX = _Ctx()


def collect(job, inp, max_n=10, window="", streamer="", audio=True, streamer_url="", page_url="", title="", mode=None, by=""):
    """Tâche de collecte lancée depuis le dashboard. Par défaut : toute la vidéo, 10 moments max.
    mode : "whole" (la vidéo ou le passage va tel quel dans le Tri) | "auto" (meilleurs moments) | None (courte = whole).
    by : prénom de qui l'a ajoutée. page_url/title : pour Kick, `inp` est le flux HLS de la VOD et page_url la page kick.com."""
    tools_check()
    CTX.update(streamer_url=streamer_url, page_url=page_url, title=title, added_by=by)
    inp = inp.strip().strip('"')
    p = Path(inp).expanduser()
    if not re.match(r"https?://", inp):
        if not p.exists():
            raise RuntimeError(f"Fichier introuvable : {inp}")
        return collect_local(job, p, max_n, window, streamer, audio, mode)
    try:
        with job.step("Lecture du lien"):
            m = meta(inp)
    except Skip:
        with store.tx() as db:  # retirée de la liste des VODs : on ne la reproposera plus
            v = db.get("vods", {}).get(CTX.get("page_url") or inp)
            if v:
                v["restricted"] = True
        raise
    if m.get("_type") in ("playlist", "multi_video"):
        ents = [e for e in (m.get("entries") or []) if e]
        job.log(f"Liste de {len(ents)} vidéos : analyse des {min(len(ents), 5)} premières")
        for e in ents[:5]:
            u = e.get("webpage_url") or e.get("url")
            try:
                collect_url(job, u, meta(u), max_n, window, streamer, audio, mode)
            except Exception as ex:  # noqa: BLE001
                job.log("  ignorée :", ex)
        return None
    return collect_url(job, inp, m, max_n, window, streamer, audio, mode)


def collect_url(job, url, m, max_n, window, streamer, audio, mode=None):
    dur = float(m.get("duration") or 0)
    if m.get("is_live") or m.get("live_status") == "is_live":
        raise RuntimeError("Ce stream est en direct : attends la VOD (rediffusion) pour l'analyser.")
    if (mode or ("whole" if dur and dur <= CLIP_MAX and not window else "auto")) == "whole":
        return import_clip(job, url, m, streamer, window)
    return analyze(job, url, m, max_n, window, streamer, audio)


def import_clip(job, url, m, streamer, window="", local=None):
    """Mode « whole » : la vidéo (ou seulement le passage) va telle quelle dans le Tri, en UN extrait, en tête de pile."""
    dur = float(m.get("duration") or 0)
    lo, hi = parse_window(window) or (0.0, dur)
    hi = min(hi, dur) if dur else hi
    if hi - lo > WHOLE_MAX:
        raise RuntimeError("Trop long pour le mettre en entier : indique un passage, ou choisis « Trouver les meilleurs moments ».")
    if dur and hi <= lo:
        raise RuntimeError(f"Passage hors de la vidéo (elle dure {fmt(dur)}).")
    with store.tx() as db:
        src, _ = get_source(db, local or url, m, streamer, CTX.get("streamer_url", ""))
        dup = any(a < (hi or 1e9) and b > lo for a, b in taken_for(db, src["url"]))
    if dup:
        raise RuntimeError("Déjà ajoutée : ce passage est déjà dans le Tri (ou a été mis à la poubelle).")
    job.log(f"Ajout tel quel dans le Tri : « {src['title']} »" + (f", de {fmt(lo)} à {fmt(hi)}" if window else ""))
    cid, s0 = store.uid(), lo
    with job.step("Téléchargement des extraits en 1080p"):
        if local and window:
            f = cut_local(local, cid, lo, hi)
        elif local:
            f = store.CLIPS / f"{cid}{Path(local).suffix.lower()}"
            shutil.copyfile(local, f)
        elif window and (platform(url) in ("twitch", "kick") or ".m3u8" in url) and "clip" not in url:
            f, s0 = hls_section(url, cid, lo, hi)  # s0 = début réel (début du 1er morceau de 10 s)
        else:
            f = download(url, cid, lo, hi) if window else download(url, cid)
    by = CTX.get("added_by")
    add_clip(src, f, s0, lo - s0, hi - s0 if window else None, f"ajouté par {by}" if by else "ajouté à la main", 50)
    return 1


def taken_for(db, url):
    """Passages déjà pris d'une source : extraits existants + poubelle supprimée à l'export. [] si la source est inconnue."""
    src = next((s for s in db["sources"].values() if s["url"] == url), None)
    if not src:
        return []
    return [(c["src_start"] + c["in"], c["src_start"] + c["out"]) for c in db["clips"].values()
            if c["source_id"] == src["id"]] + [tuple(w) for w in src.get("trashed", [])]


def detect_vod(job, inp, max_n, taken, ai_on):
    """Point d'entrée du moteur : lecture du lien puis détection (chat, audio, Whisper). Réservée aux abonnés -> Skip."""
    with job.step("Lecture du lien"):
        m = meta(inp)
    if m.get("is_live") or m.get("live_status") == "is_live":
        raise RuntimeError("Ce stream est en direct : attends la VOD (rediffusion) pour l'analyser.")
    return detect(job, inp, m, max_n, "", taken, True, None, ai_on)


META = ("title", "channel", "creator", "uploader", "uploader_id", "channel_url", "uploader_url", "duration", "width", "height",
        "was_live", "live_status")


def detect(job, url, m, max_n, window, taken, audio=True, local=None, ai_on=True):
    """Partie lourde, sans base ni Claude ni téléchargement d'extraits (tourne sur un moteur) -> RESULT (JSON) pour finish()."""
    if m.get("width") and m.get("height") and m["height"] > m["width"]:  # live vertical (Shorts) : inutilisable en 16:9
        raise Skip("vertical", "Vidéo verticale (format Shorts) : ignorée, le best-of est en 16:9.")
    dur = float(m.get("duration") or 0)
    lo, hi = parse_window(window) or (0.0, dur)
    hi = min(hi, dur) if dur else hi
    if not hi:
        raise RuntimeError("Durée inconnue : indique une fenêtre, ex. 1:00:00-3:00:00")
    key = hashlib.sha1((local or url).encode()).hexdigest()[:10]  # nom des fichiers audio dans tmp (cache)
    job.log(f"Analyse de « {CTX.get('title') or m.get('title') or Path(local or url).name} », de {fmt(lo)} à {fmt(hi)}")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:  # chat et audio en même temps : ce sont deux téléchargements indépendants
        f_chat = None if local else ex.submit(chat_signals, job, url, m, lo, hi)
        f_audio = ex.submit(audio_signals, job, key, lo, hi, url, local, lo <= 1 and (not dur or hi >= dur - 2)) if audio else None
        sig = (f_chat.result() if f_chat else []) + (f_audio.result() if f_audio else [])
    if not sig:
        raise RuntimeError("Aucun moment détecté (pas de chat, audio coupé ?). Essaie une autre fenêtre ou une autre VOD.")
    auto = pick(sig, max_n, lo, hi, taken)
    if not auto:
        raise RuntimeError("Aucun nouveau moment : tous ceux repérés ont déjà été extraits ou mis à la poubelle.")
    cands = trans = None
    apath = store.TMP / f"{key}_{int(lo)}_{int(hi)}.m4a"
    if ai_on and audio and apath.exists():
        import ai
        if not ai.whisper_ok():
            job.log("Mode IA indisponible (Whisper manque) : sélection automatique.")
        else:
            try:
                cands = pick(sig, min(150, max(30, 3 * max_n)), lo, hi, taken)  # 30 candidats pour un top 10
                trans = ai.transcribe(job, apath, lo, [[max(lo, c["t0"] - ai.CTX_S), min(hi, c["t1"] + ai.CTX_S)] for c in cands])
            except Exception as e:  # noqa: BLE001
                job.log("Transcription en échec, sélection automatique à la place :", e)
                cands = trans = None

    def slim(gs):  # float() : des scores numpy ne passeraient pas en JSON
        return [{"t0": float(g["t0"]), "t1": float(g["t1"]), "score": float(g["score"]), "why": g["why"]} for g in gs]
    return {"meta": {k: m[k] for k in META if m.get(k) is not None}, "lo": lo, "hi": hi, "signals": len(sig),
            "auto": slim(auto), "cands": slim(cands) if cands else None, "trans": trans}


def finish(job, url, res, max_n, streamer="", local=None, machine=""):
    """Point d'entrée du hub, à partir du RESULT de detect() : Claude choisit parmi les candidats transcrits (sinon sélection
    automatique), puis téléchargement des extraits en 1080p. L'appelant remplit CTX (lien de chaîne, page Kick, titre)."""
    m, lo, hi = res["meta"], res["lo"], res["hi"]
    dur = float(m.get("duration") or 0)
    with store.tx() as db:
        src, _ = get_source(db, local or url, m, streamer, CTX.get("streamer_url", ""))
        taken = taken_for(db, src["url"])
    picks, mode = res["auto"], "auto"
    if res.get("cands") and res.get("trans") and store.config().get("ai", True):
        import ai
        if not ai.claude_bin():
            job.log("Mode IA indisponible (la commande « claude » manque) : sélection automatique.")
        else:
            try:
                job.log(f"Claude choisit parmi {len(res['cands'])} passages…")
                with job.step("Claude"):
                    text = ai.ask_claude(ai.build_prompt(res["cands"], res["trans"], src["streamer"], src["title"], max_n))
                out = ai.parse(text, res["cands"], lo, hi, max_n)
                if not out:
                    job.log("Claude n'a retenu aucun passage : sélection automatique à la place.")
                else:
                    job.log(f"Claude a retenu {len(out)} passages")
                    for r, g in enumerate(out):  # l'ordre de Claude devient le score : il passe en tête du Tri
                        g["score"] = round(2 * (g["ai"]["rating"] or 3) + (len(out) - r) * .1, 2)
                        g["why"] = "IA : " + (g["ai"]["reason"] or "") + " · " + g["why"]
                    picks, mode = out, "IA"
            except Exception as e:  # noqa: BLE001
                job.log("Mode IA en échec, sélection automatique à la place :", e)
    fresh = [g for g in picks if not any(g["t0"] < b and g["t1"] > a for a, b in taken)]
    if len(fresh) < len(picks):  # finalisation reprise après un arrêt du hub : ces extraits sont déjà dans le Tri
        job.log(f"{len(picks) - len(fresh)} moment(s) déjà extrait(s) : ignoré(s)")
    picks = fresh
    if not picks:
        job.log("Rien de nouveau à télécharger.")
        return 0
    job.log(f"{len(picks)} moments retenus sur {res['signals']} signaux.")
    errs = []

    def fetch_one(i, g):
        s0, s1 = max(0.0, g["t0"] - PAD), g["t1"] + PAD
        if dur:
            s1 = min(dur, s1)
        cid = store.uid()
        try:
            if local:
                f = cut_local(local, cid, s0, s1)
            elif platform(url) in ("twitch", "kick") or ".m3u8" in url:
                f, s0 = hls_section(url, cid, s0, s1)  # s0 = début réel (début du 1er morceau)
            else:
                f = download(url, cid, s0, s1)
            add_clip(src, f, s0, g["t0"] - s0, g["t1"] - s0, g["why"], g["score"], g.get("ai"))
            job.log(f"  ✓ {i}/{len(picks)}  {fmt(g['t0'])}  [{g['score']}]  {g['why']}")
            return 1
        except Exception as e:  # noqa: BLE001
            job.log(f"  ✗ {i}/{len(picks)}  {fmt(g['t0'])} échec :", e)
            errs.append(str(e))
            return 0
    from concurrent.futures import ThreadPoolExecutor
    with job.step("Téléchargement des extraits en 1080p"), ThreadPoolExecutor(6) as ex:  # 6 extraits à la fois
        ok = sum(ex.map(lambda ig: fetch_one(*ig), enumerate(picks, 1)))
    if not ok:  # avant : comptée « analysée » avec 0 extrait et jamais refaite
        raise RuntimeError(f"Aucun des {len(picks)} extraits n'a pu être téléchargé : {errs[-1][:300]}")
    job.log(f"{ok} extraits ajoutés à la pile de tri" + (f", {len(errs)} en échec." if errs else "."))
    with store.tx() as db:  # étapes du moteur (res["steps"]) puis celles du hub ; en local, tout est déjà dans job.steps
        db["sources"][src["id"]].setdefault("analyses", []).append(
            {"date": store.now(), "mode": mode, "signals": res["signals"], "clips": ok, "failed": len(errs),
             "window": f"{fmt(lo)}-{fmt(hi)}", "machine": machine, "steps": res.get("steps", []) + job.steps,
             "total": res.get("total", 0) + job.elapsed()})
    return ok


def analyze(job, url, m, max_n, window, streamer, audio, local=None):
    """Analyse dans ce processus (lien collé, fichier envoyé) : detect puis finish."""
    with store.lock:
        taken = taken_for(store.load(), CTX.get("page_url") or local or url)
    try:
        res = detect(job, url, m, max_n, window, taken, audio, local, store.config().get("ai", True))
    except Skip as e:
        if e.code == "vertical":
            with store.tx() as db:  # retirée de la liste des VODs : on ne la reproposera plus
                v = db.get("vods", {}).get(CTX.get("page_url") or url)
                if v:
                    v["excluded"] = "verticale"
        raise
    return finish(job, url, res, max_n, streamer, local)


def collect_local(job, p, max_n, window, streamer, audio, mode=None):
    p = p.resolve()
    pr = probe(p)
    m = {"title": p.stem, "duration": pr["dur"], "channel": streamer or "Inconnu"}
    if (mode or ("whole" if pr["dur"] <= CLIP_MAX and not window else "auto")) == "whole":
        return import_clip(job, None, m, streamer, window, str(p))
    return analyze(job, None, m, max_n, window, streamer, audio, local=str(p))


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", urllib.parse.unquote(name))[-80:] or "video.mp4"
