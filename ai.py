"""Mode IA (repris du mode « Smart » de gta6-clipper) : une présélection de passages est transcrite par Whisper, puis Claude Code
(commande `claude -p`, ton abonnement, sans clé API) choisit les meilleurs, règle leurs bornes et propose catégorie / note / raison."""
import glob
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import store
from collect import fmt

CATS = ("WTF", "FUN", "FAIL", "EPIC")
CTX_S = 15      # secondes de transcription ajoutées autour de chaque candidat
WIDEN = 20      # Claude peut élargir jusqu'à 20 s autour du candidat
MIN_S, MAX_S = 6, 60


def claude_bin():
    return shutil.which("claude") or next((p for p in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude")
                                           if p.exists()), None)


def whisper_ok():
    """Whisper installé (sur un moteur, Claude n'est pas nécessaire : il ne tourne que sur le hub)."""
    return importlib.util.find_spec("faster_whisper") is not None


def _gpu_env():
    env = dict(os.environ)
    if sys.platform == "win32":  # Windows : DLL CUDA (cuBLAS, cuDNN) des paquets pip nvidia-*, introuvables sinon
        dlls = glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "*", "bin"))
        env["PATH"] = os.pathsep.join(dlls + [env.get("PATH", "")])
        return env
    libs = glob.glob(os.path.join(sys.prefix, "lib", "python*", "site-packages", "nvidia", "*", "lib"))
    if libs:
        env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    return env


GPU_PORT = 47391  # un seul Whisper à la fois par carte graphique, tous processus confondus (2 en même temps : +24 % seulement)


def _gpu_lock():
    """Verrou entre processus : port local ouvert sans SO_REUSEADDR (un 2e bind échoue, sous Linux, Windows et macOS) ; le
    système le libère si le processus meurt. -> socket à fermer en fin de transcription, ou None si la carte est prise."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", GPU_PORT))
    except PermissionError:  # Windows : port réservé (Hyper-V) -> sans verrou plutôt qu'une attente sans fin
        pass
    except OSError:
        s.close()
        return None
    return s


def transcribe(job, audio, t_audio, windows, model="medium"):
    lk = _gpu_lock()
    if lk is None:
        with job.step("Attente de la carte graphique (Whisper occupé ailleurs)"):
            while lk is None:
                time.sleep(1)
                lk = _gpu_lock()
    try:
        with job.step("Whisper"):
            return _transcribe(job, audio, t_audio, windows, model)
    finally:
        lk.close()


def gpu_state():
    """« carte graphique : 4688/8192 Mo, 52 % » (toutes applis confondues, Windows compris) : une carte déjà occupée ralentit Whisper."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        used, total, load = (x.strip() for x in r.stdout.splitlines()[0].split(","))
        return f"carte graphique : {used}/{total} Mo, {load} %"
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return "carte graphique : état inconnu"


def _transcribe(job, audio, t_audio, windows, model="medium"):
    tmp = store.TMP / f"tr_{store.uid()}"
    (tmp.with_suffix(".in.json")).write_text(json.dumps(windows), "utf-8")
    job.log(f"Transcription de {len(windows)} passages (Whisper {model}) — {gpu_state()}")
    p = subprocess.Popen([sys.executable, str(store.ROOT / "transcribe_win.py"), str(audio), str(t_audio), str(tmp.with_suffix(".in.json")),
                          str(tmp.with_suffix(".out.json")), model], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_gpu_env())
    for line in p.stdout:  # « prog 10/51 » toutes les 10 fenêtres
        if line.startswith("prog "):
            job.log(f"   Whisper : {line.split()[1]} passages — {gpu_state()}")
        elif "GPU" in line:
            job.log("   " + line.strip()[:150])
    err = p.stderr.read()
    if p.wait():
        raise RuntimeError("Whisper : " + (err.strip().splitlines() or ["échec"])[-1][:300])
    d = json.loads(tmp.with_suffix(".out.json").read_text("utf-8"))
    for f in (tmp.with_suffix(".in.json"), tmp.with_suffix(".out.json")):
        f.unlink(missing_ok=True)
    job.log(f"Transcription faite ({d['device']}, langue principale : {d['lang'] or '?'})")
    return d["windows"]


PROMPT = """Tu es le monteur d'une chaîne YouTube de best-of GTA 6 : des compilations d'environ 10 minutes des meilleurs moments de streams.
Voici {k} passages candidats repérés automatiquement dans un stream de {streamer} (« {title} »), avec les signaux qui les ont fait
remonter (pics de chat, clips créés par les viewers, audio) et la transcription horodatée. Les temps sont en secondes depuis le début de la VOD.
Le streamer peut parler n'importe quelle langue.

Choisis AU PLUS {n} passages qui feraient de très bons extraits pour le best-of : action, chaos, moment drôle, situation WTF,
fail ou accident, cascade ou poursuite impressionnante. Un bon extrait se comprend sans contexte et a une chute.
Écarte : discussion avec le chat sans action à l'écran, menus, chargements, pubs ou sponsors, attente, cinématiques de l'histoire,
fin de l'histoire, simple lecture de dons. S'il y a moins de {n} bons passages, rends-en moins.

Pour chaque passage retenu, donne des bornes précises : début juste avant ce qui déclenche l'action (le spectateur doit voir la mise en place),
fin juste après la chute ou la réaction. Durée entre 10 et 45 s. Tu peux sortir des bornes du candidat de {widen} s au maximum.

Réponds UNIQUEMENT avec un tableau JSON, du meilleur au moins bon, sans texte autour :
[{{"id": "c3", "start": 1234.5, "end": 1262.0, "category": "WTF|FUN|FAIL|EPIC", "note": 1-5, "raison": "moins de 15 mots, en français"}}]

{body}"""


def build_prompt(cands, trans, streamer, title, n):
    parts = []
    for i, (c, tr) in enumerate(zip(cands, trans)):
        lines = "\n".join(f"  [{a:.1f}-{b:.1f}] {t}" for a, b, t in tr) or "  (aucune parole détectée)"
        parts.append(f"### c{i}  candidat {c['t0']:.1f}-{c['t1']:.1f} s ({fmt(c['t0'])})  score {c['score']}\n"
                     f"Signaux : {c['why']}\nTranscription :\n{lines}")
    return PROMPT.format(k=len(cands), streamer=streamer, title=title, n=n, widen=WIDEN, body="\n\n".join(parts))


def ask_claude(prompt, timeout=900):
    exe = claude_bin()
    if not exe:
        raise RuntimeError("commande « claude » introuvable")
    r = subprocess.run([str(exe), "-p", "--output-format", "text", "--permission-mode", "dontAsk"], input=prompt,
                       capture_output=True, text=True, timeout=timeout, cwd=str(store.TMP))
    if r.returncode:
        raise RuntimeError("Claude : " + (r.stderr or r.stdout).strip()[-300:])
    return r.stdout


def parse(text, cands, lo, hi, n):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("réponse de Claude illisible : " + text.strip()[:200])
    out, seen = [], set()
    for x in json.loads(m.group(0)):
        try:
            i = int(str(x["id"]).lstrip("c"))
            c = cands[i]
            a, b = float(x["start"]), float(x["end"])
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        a = max(lo, c["t0"] - WIDEN, a)
        b = min(hi, c["t1"] + WIDEN, b)
        if b - a < MIN_S:
            a, b = c["t0"], c["t1"]
        if b - a > MAX_S:
            b = a + MAX_S
        if i in seen or any(a < y["t1"] and b > y["t0"] for y in out):
            continue
        seen.add(i)
        cat = str(x.get("category", "")).upper()
        note = x.get("note")
        ai = {"category": cat if cat in CATS else None, "rating": note if isinstance(note, int) and 1 <= note <= 5 else None,
              "reason": str(x.get("raison") or x.get("reason") or "")[:160]}
        out.append({"t0": a, "t1": b, "score": c["score"], "why": c["why"], "ai": ai})
        if len(out) >= n:
            break
    return out
