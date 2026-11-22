"""Liste des VODs des dernières 24 h des streamers suivis (même méthode que gta6-clipper : hunt.past / live.recent_vods).

- Twitch : API GQL publique (sans compte), VODs « archive » de chaque chaîne.
- YouTube : onglet /streams de la chaîne via yt-dlp.
- Kick : API publique (souvent bloquée par Cloudflare : l'erreur est affichée, rien ne casse).
Résultat dans db["vods"] (url -> fiche), affiché dans l'onglet Collecte.
"""
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.request

import store

CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # identifiant public du lecteur web Twitch (repris de gta6-clipper)
GTA = re.compile(r"\bGTA\s?(6|VI)\b|grand theft auto|leonida|vice city", re.I)
HOURS = 24
MIN_LEN = 1200  # une VOD de moins de 20 min n'est pas un stream
FILE = store.DATA / "streamers.json"


def _defaults():
    src = json.loads((store.ROOT / "vendor" / "gta6clipper" / "streamers.json").read_text("utf-8"))
    out = []
    for lang in ("fr", "en"):
        for s in src.get(lang, []):
            out.append({"name": s["name"], "platform": s["platform"], "handle": s["handle"], "lang": lang, "on": True})
    return {"streamers": out}


def settings():
    if FILE.exists():
        return json.loads(FILE.read_text("utf-8"))
    d = _defaults()
    FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    return d


def _clean(s):
    """Une chaîne envoyée par le dashboard -> fiche propre, ou None si la plateforme ou le pseudo sont invalides."""
    h, p = str(s.get("handle", "")).strip(), s.get("platform")
    if p not in ("twitch", "youtube", "kick") or not re.fullmatch(r"@?[\w.\-]{1,60}", h):
        return None
    return {"name": str(s.get("name") or h).strip()[:60], "platform": p, "handle": h, "lang": str(s.get("lang") or "")[:5],
            "on": bool(s.get("on", True))}


def _key(s):
    return s.get("platform"), str(s.get("handle", "")).strip().lower().lstrip("@")


def save_settings(body):
    """{"streamers": [...]} remplace la liste ; {"op": {"action": "set"|"add"|"remove", "platform", "handle", ...}} change UNE
    chaîne dans la liste du moment (une page ouverte depuis longtemps n'écrase plus les changements faits ailleurs)."""
    cur = settings()
    if isinstance(body.get("streamers"), list):
        clean, seen = [], set()
        for s in body["streamers"]:
            c = _clean(s) if isinstance(s, dict) else None
            if c and _key(c) not in seen:  # même chaîne ajoutée deux fois : on garde la première
                seen.add(_key(c))
                clean.append(c)
        cur["streamers"] = clean
    op = body.get("op")
    if isinstance(op, dict):
        hit = next((s for s in cur["streamers"] if _key(s) == _key(op)), None)
        if op.get("action") == "remove":
            cur["streamers"] = [s for s in cur["streamers"] if s is not hit]
        elif op.get("action") == "add" and not hit and _clean(op):
            cur["streamers"].insert(0, _clean(op))
        elif op.get("action") == "set" and hit:
            hit["on"] = bool(op.get("on"))
    FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), "utf-8")
    return cur


# ---------------------------------------------------------------- Twitch

def gql(q):
    r = urllib.request.Request("https://gql.twitch.tv/gql", json.dumps({"query": q}).encode(),
                               {"Client-ID": CLIENT_ID, "Content-Type": "application/json"})
    j = json.load(urllib.request.urlopen(r, timeout=30))
    if j.get("errors") and not j.get("data"):
        raise RuntimeError(j["errors"][0].get("message", "erreur GQL"))
    return j


RICH = ('{user(login:%s){login displayName videos(first:5,type:ARCHIVE,sort:TIME){edges{node{'
        'id title lengthSeconds publishedAt viewCount status game{name} previewThumbnailURL(width:320,height:180)}}}}}')
BASIC = '{user(login:%s){login displayName videos(first:5,type:ARCHIVE,sort:TIME){edges{node{id title lengthSeconds publishedAt viewCount}}}}}'


def twitch_vods(login, name=None):
    # depuis le serveur (datacenter), Twitch répond « service error » sur les vidéos environ une fois sur deux, au hasard
    # (mesuré le 2026-11-22) : une nouvelle tentative passe presque toujours
    for _ in range(6):
        j = gql(RICH % json.dumps(login.lower()))
        if j.get("errors"):  # un champ refusé par l'API : on retombe sur la requête minimale du repo
            j = gql(BASIC % json.dumps(login.lower()))
        u = (j.get("data") or {}).get("user")
        if not u:
            raise RuntimeError("chaîne introuvable")
        if u.get("videos"):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Twitch ne répond pas (« service error ») : nouvel essai à la prochaine recherche")
    out = []
    for e in u["videos"]["edges"]:
        n = e["node"]
        out.append({"url": "https://www.twitch.tv/videos/" + n["id"], "platform": "twitch", "title": n.get("title") or "",
                    "streamer": name or u.get("displayName") or login, "streamer_url": "https://www.twitch.tv/" + u["login"],
                    "duration": n.get("lengthSeconds") or 0, "published": n.get("publishedAt") or "", "views": n.get("viewCount") or 0,
                    "game": ((n.get("game") or {}).get("name")) or "", "recording": n.get("status") == "RECORDING",
                    "thumb": (n.get("previewThumbnailURL") or "") if "404_processing" not in (n.get("previewThumbnailURL") or "") else ""})
    return out


# ---------------------------------------------------------------- YouTube / Kick

def youtube_vods(handle, name=None):
    h = handle if handle.startswith("@") else "@" + handle
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", "--playlist-end", "3", "--no-warnings",
                        f"https://www.youtube.com/{h}/streams"], capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or ["aucun live trouvé"])[-1][:150])
    out = []
    for e in (json.loads(r.stdout).get("entries") or [])[:3]:
        u = e.get("url") or e.get("webpage_url") or ""
        if not u.startswith("http"):
            u = "https://www.youtube.com/watch?v=" + (e.get("id") or "")
        m = {}
        try:  # la liste « à plat » n'a pas la date : on la lit vidéo par vidéo
            rr = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", "--skip-download", u],
                                capture_output=True, text=True, timeout=90)
            m = json.loads(rr.stdout) if rr.returncode == 0 else {}
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        ts = m.get("release_timestamp") or m.get("timestamp")
        out.append({"url": u, "platform": "youtube", "title": m.get("title") or e.get("title") or "",
                    "streamer": name or m.get("channel") or handle, "streamer_url": f"https://www.youtube.com/{h}",
                    "duration": m.get("duration") or e.get("duration") or 0,
                    "published": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else "",
                    "views": m.get("view_count") or 0, "game": "", "recording": m.get("live_status") == "is_live",
                    "thumb": m.get("thumbnail") or ""})
    return out


def kick_vods(handle, name=None):
    r = urllib.request.Request(f"https://kick.com/api/v2/channels/{handle}/videos", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    d = json.load(urllib.request.urlopen(r, timeout=20))
    out = []
    for v in (d if isinstance(d, list) else d.get("data", []))[:5]:
        uuid = (v.get("video") or {}).get("uuid") or v.get("uuid") or ""
        out.append({"url": f"https://kick.com/{handle}/videos/{uuid}", "platform": "kick", "title": v.get("session_title") or "",
                    "streamer": name or handle, "streamer_url": f"https://kick.com/{handle}",
                    "duration": int((v.get("duration") or 0) / 1000), "published": (v.get("created_at") or v.get("start_time") or ""),
                    "views": v.get("views") or 0, "game": ", ".join(c.get("name", "") for c in v.get("categories") or []),
                    "recording": bool(v.get("is_live")), "dl_url": v.get("source") or "", "thumb": (v.get("thumbnail") or {}).get("src", "") if isinstance(v.get("thumbnail"), dict) else ""})
    return out


FETCH = {"twitch": twitch_vods, "youtube": youtube_vods, "kick": kick_vods}


def _age_h(published, now):
    if not published:
        return None
    try:
        t = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (now - t.timestamp()) / 3600
    except ValueError:
        return None


def vod_clips(vid, tries=3):
    """Clips des viewers sur la VOD -> (clips, complet). Twitch ne donne que les 100 clips les plus vus de la chaîne par période sans
    connexion (page 2 = « failed integrity check ») : complet=True seulement si la chaîne a moins de 100 clips sur la période
    (alors on a vraiment TOUS les clips de la VOD)."""
    login = gql('{video(id:"%s"){owner{login}}}' % vid)["data"]["video"]["owner"]["login"]
    out, complete = {}, False
    for per in ("LAST_DAY", "LAST_WEEK"):
        c = None
        for _ in range(tries):  # une période répond parfois « server error » : on réessaie
            try:
                j = gql('{user(login:"%s"){clips(first:100,criteria:{period:%s,sort:VIEWS_DESC}){pageInfo{hasNextPage} edges{node{'
                        'slug url title viewCount videoOffsetSeconds durationSeconds video{id}}}}}}' % (login, per))
                c = ((j.get("data") or {}).get("user") or {}).get("clips")
            except Exception:  # noqa: BLE001
                c = None
            if c:
                break
            time.sleep(1)
        if not c:
            continue
        if per == "LAST_DAY":
            complete = not c["pageInfo"]["hasNextPage"]
        for e in c["edges"]:
            n = e["node"]
            if n["video"] and n["video"]["id"] == vid and n["videoOffsetSeconds"] is not None:
                out[n["slug"]] = {"off": n["videoOffsetSeconds"], "dur": n["durationSeconds"], "views": n["viewCount"],
                                  "title": n["title"] or "", "url": n["url"] or f"https://clips.twitch.tv/{n['slug']}"}
    return sorted(out.values(), key=lambda c: c["off"]), complete


def title_gta(title):
    """GTA dans le vrai titre : les #hashtags et @mentions ne comptent pas (« chain together live #shortslive #gta6 » = pas GTA)."""
    return bool(GTA.search(re.sub(r"[#@]\S+", " ", title or "")))


def is_gta(v):
    return bool(GTA.search(v.get("game", "")) or title_gta(v.get("title", "")))


def scan(job):
    """Tâche « Actualiser » : récupère les VODs < 24 h de tous les streamers suivis."""
    st = settings()
    targets = [(s["platform"], s["handle"], s["name"]) for s in st["streamers"] if s.get("on")]
    now, found, errors = time.time(), {}, 0
    for p, h, name in targets:
        try:
            vs = FETCH[p](h, name)
        except Exception as e:  # noqa: BLE001
            errors += 1
            job.log(f"  {name} ({p}) : {str(e)[:120]}")
            continue
        n = 0
        for v in vs:
            age = _age_h(v["published"], now)  # heures depuis le DÉBUT du stream
            if age is not None and not v["recording"]:
                age -= (v["duration"] or 0) / 3600  # on compte depuis la FIN : un stream de 12 h fini il y a 13 h est récent
            if age is None or age > HOURS or (v["duration"] or 0) < MIN_LEN and not v["recording"]:
                continue
            v.update(age_h=round(age, 1), gta=is_gta(v), handle=h)
            found[v["url"]] = v
            n += 1
        if n:
            job.log(f"  {name} : {n} VOD(s) < 24 h")
    with store.lock:
        prev = dict(store.load().get("vods") or {})
    import collect
    for u, v in found.items():  # accès : publique ou réservée aux abonnés (dans ce cas ignorée)
        if v["platform"] != "twitch" or (prev.get(u) or {}).get("restricted") is False:
            v["restricted"] = (prev.get(u) or {}).get("restricted", False)
            continue
        try:
            r = subprocess.run(collect.ytdlp("-J", u), capture_output=True, text=True, timeout=60)
            v["restricted"] = r.returncode != 0 and bool(re.search(r"subscriber|logged in", r.stderr))
        except subprocess.TimeoutExpired:
            v["restricted"] = None
    locked = [u for u, v in found.items() if v.get("restricted")]
    for u in locked:  # on ne garde que les VODs gratuites
        del found[u]
    if locked:
        job.log(f"{len(locked)} VOD(s) réservée(s) aux abonnés ignorée(s)")
    with store.tx() as db:  # la liste est remplacée ; les marques posées à l'analyse (ex. « verticale ») sont gardées
        db["vods"] = {u: {**prev.get(u, {}), **v} for u, v in found.items()}
        db["vods_scan"] = store.now()
    job.log(f"{len(found)} VODs des dernières 24 h ({sum(v['gta'] for v in found.values())} sur GTA), {errors} chaîne(s) en erreur")
    return len(found)
