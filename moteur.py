"""Moteur : tourne sur chaque machine (PC des amis, Mac, PC du hub). Demande des VODs au site (le hub), fait la partie lourde
(chat, audio, Whisper) dans un processus enfant par VOD, et renvoie candidats + transcriptions. Claude et les extraits : sur le hub.
  python moteur.py --conf moteur.json                 (amis ; moteur.json = {"hub", "token", "name"})
  python moteur.py --hub URL --token JETON [--slots N]  (moteur local lancé par le hub)
  python moteur.py --run <job.json>                   (processus enfant : UNE VOD ; interne)"""
import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("BESTOF_DATA", str(ROOT / "data" / "moteur"))  # jamais les données du hub (data/) par défaut
os.environ.setdefault("PYTHONUTF8", "1")

import collect  # noqa: E402
import jobs  # noqa: E402
import store  # noqa: E402

PROTOCOL = 1
JOBDIR = store.DATA / "jobs"
NETERR = (OSError, ValueError, http.client.HTTPException)  # serveur injoignable / réponse illisible : on réessaie
HUB = TOKEN = ""
RUN = {}  # bail -> {url, title, streamer, step, line, started, proc, killed, tail}
LK = threading.Lock()


def code_version():
    """Empreinte du code d'analyse (même fonction que engines.code_version) : le site signale un moteur pas à jour."""
    h = hashlib.sha1()
    for f in ("collect.py", "ai.py", "moteur.py", "engines.py"):
        p = ROOT / f
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:8]


def post(path, body, timeout=20):
    req = urllib.request.Request(HUB + path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 {"Content-Type": "application/json", "X-Machine": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {**json.loads(e.read()), "status": e.code}  # erreur du site (jeton refusé…) : réponse JSON
        except ValueError:
            raise OSError(f"HTTP {e.code}") from None  # page d'erreur de tailscale serve : app.py arrêté sur le PC


def gpu_name():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        name, mb = r.stdout.splitlines()[0].rsplit(",", 1)
        return f"{name.strip()} ({round(int(mb) / 1024)} Go)"
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return "aucune (processeur)"


def kill_tree(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # groupe de l'enfant : yt-dlp, ffmpeg, Whisper avec lui
        except (ProcessLookupError, PermissionError):
            pass


def clean_tmp():
    """Vide tmp du moteur (audio, chat) quand plus rien ne tourne : le moteur ne réanalyse pas, le cache ne sert qu'au hub."""
    if store.DATA == store.ROOT / "data":
        return
    for f in store.TMP.iterdir():
        shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)


def start(g):
    path = JOBDIR / f"{g['lease']}.json"
    path.write_text(json.dumps(g, ensure_ascii=False), "utf-8")
    p = subprocess.Popen([sys.executable, __file__, "--run", str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt",
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    r = {"url": g["url"], "title": g.get("title") or g["url"], "streamer": g.get("streamer", ""), "step": "Démarrage",
         "line": "", "started": int(time.time()), "proc": p, "killed": False, "tail": []}
    RUN[g["lease"]] = r
    print(f"▶ Analyse de « {r['title']} »" + (f" ({r['streamer']})" if r["streamer"] else ""), flush=True)
    threading.Thread(target=follow, args=(g["lease"], r, path), daemon=True).start()


def follow(lease, r, path):
    """Suit l'enfant (étape, dernière ligne), puis envoie son résultat au site (réessaie jusqu'à ~2 h si le PC est éteint)."""
    for s in r["proc"].stdout:
        s = s.rstrip()
        s = s[10:] if s.startswith("[collect] ") else s
        if not s:
            continue
        r["tail"] = r["tail"][-39:] + [s]
        if s.startswith("▶ "):
            r["step"] = s[2:][:120]
            print(f"   · {r['step']}", flush=True)
        else:
            r["line"] = s[:300]
    r["proc"].wait()
    out_p = path.with_suffix(".out.json")
    if r["killed"]:
        print(f"■ « {r['title']} » arrêté (demandé depuis le site)", flush=True)
    else:
        try:
            out = json.loads(out_p.read_text("utf-8"))
        except (OSError, ValueError):
            out = {"ok": False, "code": None, "error": "le moteur s'est arrêté", "log": "\n".join(r["tail"])}
        took = jobs.hms(time.time() - r["started"])
        print(f"✓ « {r['title']} » terminé en {took}" if out.get("ok") else f"✗ échec : {str(out.get('error'))[:300]}", flush=True)
        r["step"], t0, delay = "Envoi du résultat", time.time(), 5
        while not r["killed"]:
            try:
                ans = post("/api/engine/result", {"lease": lease, "url": r["url"], **out}, timeout=120)
                if not ans.get("ok"):
                    print("   résultat refusé par le site :", ans.get("error"), flush=True)
                break
            except NETERR:
                if time.time() - t0 > 7200:
                    print("✗ résultat abandonné : serveur injoignable depuis 2 h", flush=True)
                    break
                time.sleep(delay)
                delay = min(delay * 2, 300)
    with LK:
        RUN.pop(lease, None)
        path.unlink(missing_ok=True)
        out_p.unlink(missing_ok=True)
        if not RUN:
            clean_tmp()


def engine(hub, token, slots):
    global HUB, TOKEN
    HUB, TOKEN = hub.rstrip("/"), token
    sys.stdout.reconfigure(errors="replace")
    try:
        import certifi  # le Python autonome de uv sur Mac ne trouve pas les certificats du système
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass
    try:
        import static_ffmpeg  # ffmpeg + ffprobe sur Windows / Mac (téléchargés au 1er lancement)
        static_ffmpeg.add_paths(weak=True)
    except ImportError:
        pass
    collect.tools_check()
    if os.name == "nt":  # pas de mise en veille tant que le moteur tourne
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    elif sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    for s in ("SIGTERM", "SIGHUP"):  # hub qui s'arrête, fenêtre du Terminal fermée (Mac) : on tue les enfants avant de partir
        if hasattr(signal, s):
            signal.signal(getattr(signal, s), lambda *a: sys.exit(0))
    global ONE  # un seul moteur par dossier de données : un 2e lancement effacerait les fichiers de travail du 1er
    ONE = socket.socket()
    try:  # shortcut: port tiré du dossier (47400-47899), un autre programme sur ce port bloquerait le moteur ; passer à un verrou de fichier si ça arrive
        ONE.bind(("127.0.0.1", 47400 + int(hashlib.sha1(str(store.DATA).encode()).hexdigest(), 16) % 500))
    except OSError:
        print("Le moteur tourne déjà dans une autre fenêtre : ferme celle-ci.", flush=True)
        return
    shutil.rmtree(JOBDIR, ignore_errors=True)  # restes d'un arrêt brutal : leurs VODs sont déjà revenues en attente
    JOBDIR.mkdir(parents=True)
    clean_tmp()
    osname = f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else f"{platform.system()} {platform.release()}"
    info = {"protocol": PROTOCOL, "version": code_version(), "os": osname, "gpu": gpu_name(), "slots": slots}
    print(f"Moteur Best-of {info['version']} — {info['os']}, carte graphique : {info['gpu']}", flush=True)
    name, paused, offline, ppid = None, False, False, os.getppid()
    try:
        while os.getppid() == ppid:  # parent disparu (hub tué) : on s'arrête, le hub relancera un moteur
            with LK:
                mine = [{"lease": k, **{f: r[f] for f in ("url", "step", "line", "started")}} for k, r in RUN.items()]
            try:
                ans = post("/api/engine/beat", {**info, "want": 0 if paused else max(0, slots - len(mine)), "jobs": mine})
            except NETERR:
                if not offline:
                    print("Serveur injoignable (ton PC est peut-être éteint), nouvel essai…", flush=True)
                offline = True
                time.sleep(10)
                continue
            if ans.get("status") == 401:  # machine retirée du site : ses analyses en cours sont tuées (bloc finally)
                print("✗", ans.get("error") or "Machine inconnue du site.", "Moteur arrêté.", flush=True)
                return
            if not ans.get("ok"):
                print("✗", ans.get("error") or "réponse inattendue du site", "— nouvel essai dans 1 min", flush=True)
                name = None
                time.sleep(60)
                continue
            if offline or ans.get("name") != name:
                name, offline = ans.get("name"), False
                print(f"Connecté à {HUB} comme {name}", flush=True)
            if bool(ans.get("paused")) != paused:
                paused = bool(ans.get("paused"))
                print("En pause (demandé depuis le site)" if paused else "Reprise : le moteur accepte de nouveaux fichiers", flush=True)
            with LK:
                for k in ans.get("kill") or []:
                    if k in RUN:
                        RUN[k]["killed"] = True
                        kill_tree(RUN[k]["proc"])
                for g in ans.get("give") or []:
                    if g.get("lease") and g["lease"] not in RUN:
                        start(g)
            time.sleep(5)
    finally:  # Ctrl+C, SIGTERM du hub, parent disparu : enfants tués, VODs rendues au site
        with LK:
            for r in RUN.values():
                r["killed"] = True
                kill_tree(r["proc"])
            leases = list(RUN)
        if leases:
            try:
                post("/api/engine/release", {"leases": leases}, timeout=5)
            except NETERR:
                pass
        print("Moteur arrêté.", flush=True)


def child(path):
    """Une VOD : détection complète -> <job>.out.json. Code de sortie 0 dans tous les cas prévus."""
    import ai
    g = json.loads(Path(path).read_text("utf-8"))
    collect.CTX.update(title=g.get("title", ""))
    j = jobs.Job("collect", g.get("title") or g["url"], None, ())
    j.t0 = time.time()
    try:
        res = collect.detect_vod(j, g["input"], int(g.get("max_n") or 10), [tuple(w) for w in g.get("taken") or []],
                                 bool(g.get("ai")) and ai.whisper_ok())
        out = {"ok": True, "result": res, "steps": j.steps, "total": j.elapsed()}
    except collect.Skip as e:
        j.log("Écartée :", e)
        out = {"ok": False, "code": e.code, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - toute erreur doit remonter au site avec son journal
        j.log("ERREUR :", e)
        out = {"ok": False, "code": None, "error": str(e)[:500] or type(e).__name__, "tb": traceback.format_exc()}
    out["log"] = j.file.read_text("utf-8", errors="replace") + out.pop("tb", "")
    tmp = Path(path).with_suffix(".out.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    os.replace(tmp, Path(path).with_suffix(".out.json"))


def main():
    ap = argparse.ArgumentParser(description="Moteur Best-of : analyse des VODs pour le site.")
    ap.add_argument("--conf", help="moteur.json {hub, token, name}")
    ap.add_argument("--hub")
    ap.add_argument("--token")
    ap.add_argument("--slots", type=int, default=1, help="VODs analysées en même temps (une seule carte graphique : 1)")
    ap.add_argument("--run", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.run:
        return child(a.run)
    if a.conf:
        c = json.loads(Path(a.conf).read_text("utf-8-sig"))
        a.hub, a.token = a.hub or c["hub"], a.token or c["token"]
    if not (a.hub and a.token):
        ap.error("--conf ou --hub + --token")
    try:
        engine(a.hub, a.token, max(1, a.slots))
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:  # outils manquants…
        print("✗", e, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
