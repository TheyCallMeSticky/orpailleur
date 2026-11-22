"""Tâches de fond : une file pour la collecte (téléchargements, analyses), une pour les exports.
Chaque tâche écrit son journal complet dans data/logs/<jour>/<heure>_<type>_<id>.log, avec la durée de chaque étape."""
import queue
import threading
import time
import traceback
from contextlib import contextmanager

import store

JOBS = {}
_QUEUES = {}


class Job:
    def __init__(self, kind, label, fn, args):
        self.id = store.uid()
        self.kind, self.label, self.fn, self.args = kind, label, fn, args
        self.status = "en attente"
        self.lines = []
        self.result = None
        self.created = store.now()
        self.ended = None
        self.steps = []  # [[étape, secondes], ...] dans l'ordre où elles finissent
        self.t0 = None
        self.queued = time.time()
        d = store.DATA / "logs" / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        self.file = d / f"{time.strftime('%H%M%S')}_{kind}_{self.id}.log"
        self._lk = threading.Lock()
        self.log(label)

    def log(self, *a):
        line = " ".join(str(x) for x in a)
        stamped = time.strftime("%H:%M:%S ") + line
        with self._lk:
            self.lines.append(stamped)
            del self.lines[:-500]
            try:
                with self.file.open("a", encoding="utf-8") as f:
                    f.write(stamped + "\n")
            except OSError:  # disque plein : la tâche continue, la ligne reste visible dans le dashboard
                pass
        print(f"[{self.kind}] {line}", flush=True)

    @contextmanager
    def step(self, name):
        """with job.step("Whisper"): ... -> « ▶ Whisper » puis « ■ Whisper : 1 min 05 s » (aussi en cas d'erreur)."""
        t = time.time()
        self.log("▶", name)
        try:
            yield
        finally:
            d = round(time.time() - t)
            self.steps.append([name, d])
            self.log(f"■ {name} : {hms(d)}")

    def elapsed(self):
        return round(time.time() - self.t0) if self.t0 else 0

    def view(self, full=False):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status, "created": self.created,
                "ended": self.ended, "result": self.result, "lines": self.lines if full else self.lines[-6:]}


def _worker(q):
    while True:
        j = q.get()
        j.status = "en cours"
        j.t0 = time.time()
        j.log(f"Démarrage (attente dans la file : {hms(j.t0 - j.queued)})")
        try:
            j.result = j.fn(j, *j.args)
            j.status = "terminé"
            j.log("Terminé.")
        except Exception as e:  # noqa: BLE001 - toute erreur doit finir dans le journal visible
            j.status = "erreur"
            j.log("ERREUR :", e)
            traceback.print_exc()
            try:
                with j._lk, j.file.open("a", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except OSError:
                pass
        if j.steps:
            j.log("Durées : " + " · ".join(f"{n} {hms(d)}" for n, d in j.steps) + f" · total {hms(j.elapsed())}")
        j.ended = store.now()


def hms(s):
    s = int(s)
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


WORKERS = {"collect": 3}  # 3 VODs analysées en même temps ; exports et recherche : une à la fois


def submit(queue_name, kind, label, fn, *args):
    if queue_name not in _QUEUES:
        _QUEUES[queue_name] = queue.Queue()
        for _ in range(WORKERS.get(queue_name, 1)):
            threading.Thread(target=_worker, args=(_QUEUES[queue_name],), daemon=True).start()
    j = Job(kind, label, fn, args)
    JOBS[j.id] = j
    _QUEUES[queue_name].put(j)
    return j


def track(kind, label):
    """Tâche faite par un moteur : affichée comme les autres, mise à jour par engines.py (pas de fil ici)."""
    j = Job(kind, label, None, ())
    JOBS[j.id] = j
    return j


def close(j, ok, err=""):
    if j.status in ("terminé", "erreur"):
        return
    j.status = "terminé" if ok else "erreur"
    j.log("Terminé.") if ok else j.log("ERREUR :", err)
    if j.steps:
        j.log("Durées : " + " · ".join(f"{n} {hms(d)}" for n, d in j.steps) + (f" · total {hms(j.elapsed())}" if j.t0 else ""))
    j.ended = store.now()


def listing(n=25):
    """En cours d'abord (toujours toutes), puis erreurs / terminées récentes, puis en attente (dans l'ordre où elles passeront)."""
    rank = {"en cours": 0, "erreur": 1, "terminé": 1, "en attente": 2}
    js = sorted(JOBS.values(), key=lambda j: (rank.get(j.status, 3), j.created if j.status == "en attente" else "",
                                              "" if j.status == "en attente" else (j.ended or j.created)), reverse=False)
    run = [j for j in js if j.status == "en cours"]
    done = sorted((j for j in js if j.status in ("erreur", "terminé")), key=lambda j: j.ended or j.created, reverse=True)[:12]
    wait = [j for j in js if j.status == "en attente"]
    return [j.view() for j in run + done + wait][:max(n, len(run) + len(done) + 40)]
