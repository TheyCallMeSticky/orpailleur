"""Transcrit des fenêtres d'un fichier audio avec Whisper (GPU si possible, sinon CPU). Lancé dans un processus à part par ai.py.
usage : transcribe_win.py <audio> <seconde de VOD du début de l'audio> <fenetres.json> <sortie.json> [modèle]
fenetres.json = [[debut, fin], ...] en secondes de VOD ; sortie = {"device", "lang", "windows": [[[debut, fin, texte], ...], ...]}"""
import glob
import json
import os
import subprocess
import sys
import sysconfig

import numpy as np

if sys.platform == "win32":  # Windows : DLL CUDA (cuBLAS, cuDNN) des paquets pip nvidia-*, à déclarer avant de charger Whisper
    for d in glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "*", "bin")):
        os.add_dll_directory(d)
from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402

audio, t_audio, wins, out = sys.argv[1], float(sys.argv[2]), json.load(open(sys.argv[3])), sys.argv[4]
size = sys.argv[5] if len(sys.argv) > 5 else "medium"
VOCAB = "GTA 6, Grand Theft Auto VI, Leonida, Vice City, Lucia, Jason, Rockstar, chat, KEKW, LUL, bro."


def load(device):
    """Mode « batched » : plusieurs morceaux de chaque passage décodés ensemble sur le GPU (bien plus rapide)."""
    if device == "cuda":
        # int8_float16 + lots de 8 : ~2x moins de mémoire vidéo. Sur une carte de 8 Go, un débordement dans la mémoire système
        # (WSL/Windows) rend tout 5 à 10x plus lent.
        return BatchedInferencePipeline(WhisperModel(size, device="cuda", compute_type="int8_float16"))
    return WhisperModel("small", device="cpu", compute_type="int8")


def run(x):
    if device == "cuda":
        return model.transcribe(x, batch_size=8, initial_prompt=VOCAB, without_timestamps=False)  # horodatage phrase par phrase
    return model.transcribe(x, vad_filter=True, beam_size=1, initial_prompt=VOCAB)


def pcm(a, b):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, a - t_audio):.2f}", "-t", f"{b - a:.2f}", "-i", audio,
                          "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768


device = "cuda"
try:
    model = load(device)
except Exception as e:  # noqa: BLE001
    print("GPU indisponible :", str(e)[:120], "-> CPU", flush=True)
    device, model = "cpu", load("cpu")
res, langs = [], {}
for k, (a, b) in enumerate(wins, 1):
    if k % 10 == 0:
        print(f"prog {k}/{len(wins)}", flush=True)
    x = pcm(a, b)
    if len(x) < 16000:
        res.append([])
        continue
    try:
        segs, info = run(x)
        segs = list(segs)  # le travail se fait ici : une lib CUDA manquante échoue ici, pas au chargement
    except Exception as e:  # noqa: BLE001
        if device != "cuda":
            raise
        print("GPU en échec :", str(e)[:120], "-> CPU", flush=True)
        device, model = "cpu", load("cpu")
        segs, info = run(x)
        segs = list(segs)
    langs[info.language] = langs.get(info.language, 0) + 1
    res.append([[round(a + s.start, 1), round(a + s.end, 1), s.text.strip()] for s in segs])
json.dump({"device": device, "lang": max(langs, key=langs.get) if langs else "", "windows": res},
          open(out, "w", encoding="utf-8"), ensure_ascii=False)
print("ok", device, len(res), "fenêtres", flush=True)
