"""Stockage des fichiers (extraits, vignettes, musiques, vidéos envoyées, exports). Les clés reprennent les chemins de data/
(media/clips/x.mp4…). Avec data/s3.json : S3 (OVH), le site ne fait que signer des liens et navigateurs / moteurs lisent et
écrivent directement. Sans : mode local, les fichiers restent dans data/ et le site les sert (tests, fonctionnement sans S3).
  python s3.py setup                 clés S3 (la clé secrète en saisie masquée) -> data/s3.json, essai, CORS
  python s3.py push DOSSIER PREFIXE  envoie un dossier vers S3 (migration : data/media media ; data/exports exports)"""
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import store

CONF_FILE = store.DATA / "s3.json"
EXPIRES = 12 * 3600  # liens de lecture : une page ouverte plus de 12 h doit être rechargée
ROOTS = ("media/", "exports/", "inbox/")  # seules clés servies / signées
_C = []


def conf():
    """{endpoint, region, bucket, key, secret[, prefix]} ou None (mode local). Lu une fois."""
    if not _C:
        _C.append(json.loads(CONF_FILE.read_text("utf-8")) if CONF_FILE.exists() else None)
    return _C[0]


def ok_key(key):
    return isinstance(key, str) and key.startswith(ROOTS) and ".." not in key and "\\" not in key and len(key) < 400


def ctype(key):
    """Type du fichier, gardé par S3 et renvoyé aux navigateurs (texte en UTF-8 : accents de la description)."""
    t = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return t + "; charset=utf-8" if t.startswith("text/") else t


def _q(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def presign(method, key, expires=EXPIRES, c=None, now=None, query=None):
    """URL signée AWS SigV4 (en-tête host seul, contenu non signé) : le porteur peut faire `method` sur `key` jusqu'à expiration."""
    c = c or conf()
    t = now or datetime.datetime.now(datetime.timezone.utc)
    amz = t.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz[:8]}/{c['region']}/s3/aws4_request"
    ep = urllib.parse.urlparse(c["endpoint"])
    host = f"{c['bucket']}.{ep.netloc}"  # style « virtual-hosted » (conteneur dans le nom d'hôte), comme AWS et OVH
    path = "/" + _q(c.get("prefix", "") + key, safe="/-_.~")  # prefix : essais sur le vrai conteneur, à part
    q = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{c['key']}/{scope}", "X-Amz-Date": amz,
         "X-Amz-Expires": str(int(expires)), "X-Amz-SignedHeaders": "host", **(query or {})}
    cq = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(q.items()))
    creq = "\n".join([method, path, cq, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4" + c["secret"]).encode()
    for part in (amz[:8], c["region"], "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"{ep.scheme}://{host}{path}?{cq}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------- mode local : jeton HMAC du site à la place de la signature S3

def _secret():
    return store.config()["token"].encode()


def local_token(method, key, exp):
    return hmac.new(_secret(), f"{method}:{key}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def local_ok(method, key, exp, tok):
    try:
        return int(exp) > time.time() and hmac.compare_digest(local_token(method, key, exp), tok or "")
    except ValueError:
        return False


def url(method, key, expires=EXPIRES, download=None):
    """Lien pour lire (GET), écrire (PUT) ou supprimer (DELETE) `key`. Mode local : chemin relatif au site (/files/…)."""
    if not ok_key(key):
        raise ValueError(f"clé refusée : {key}")
    if conf():
        extra = {"response-content-disposition": f"attachment; filename*=UTF-8''{_q(download)}"} if download else None
        return presign(method, key, expires, query=extra)
    exp = str(int(time.time() + expires))
    return f"/files/{_q(key, safe='/-_.~')}?e={exp}&t={local_token(method, key, exp)}" + ("&dl=1" if download else "")


def delete(key):
    """Supprime un fichier (absent : rien). Erreur réseau -> exception (l'appelant la note)."""
    if not ok_key(key):
        return
    if not conf():
        (store.DATA / key).unlink(missing_ok=True)
        return
    try:
        urllib.request.urlopen(urllib.request.Request(url("DELETE", key, 300), method="DELETE"), timeout=30).read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


# ---------------------------------------------------------------- outils en ligne de commande

def _put_file(p, key):
    with open(p, "rb") as f:
        req = urllib.request.Request(url("PUT", key, 3600), data=f, method="PUT",
                                     headers={"Content-Length": str(os.path.getsize(p)), "Content-Type": ctype(key)})
        urllib.request.urlopen(req, timeout=600).read()


def _size(key):
    try:
        with urllib.request.urlopen(urllib.request.Request(url("HEAD", key, 300), method="HEAD"), timeout=30) as r:
            return int(r.headers.get("Content-Length") or -1)
    except urllib.error.HTTPError:
        return -1


def push(folder, prefix):
    """Envoie tous les fichiers de `folder` sous `prefix/` (ceux déjà là avec la même taille sont sautés)."""
    from concurrent.futures import ThreadPoolExecutor
    folder = Path(folder)
    files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    total, done, sent = sum(p.stat().st_size for p in files), [0], [0]

    def one(p):
        key = f"{prefix.strip('/')}/{p.relative_to(folder).as_posix()}"
        if _size(key) != p.stat().st_size:
            for k in range(3):
                try:
                    _put_file(p, key)
                    break
                except OSError:
                    if k == 2:
                        raise
                    time.sleep(5)
            sent[0] += 1
        done[0] += p.stat().st_size
        print(f"\r  {done[0] / 1e9:.1f} / {total / 1e9:.1f} Go ({sent[0]} envoyé(s))", end="", flush=True)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, files))
    print(f"\n{len(files)} fichier(s) vérifié(s), {sent[0]} envoyé(s).")


CORS = ("<CORSConfiguration><CORSRule><AllowedOrigin>*</AllowedOrigin><AllowedMethod>GET</AllowedMethod>"
        "<AllowedMethod>PUT</AllowedMethod><AllowedMethod>HEAD</AllowedMethod><AllowedHeader>*</AllowedHeader>"
        "<ExposeHeader>ETag</ExposeHeader><MaxAgeSeconds>3600</MaxAgeSeconds></CORSRule></CORSConfiguration>")


def setup():
    import base64
    import getpass
    old = conf() or {}
    ask = lambda label, d: input(f"{label} [{d}] : ").strip() or d  # noqa: E731
    c = {"endpoint": ask("Adresse", old.get("endpoint", "https://s3.gra.io.cloud.ovh.net")).rstrip("/"),
         "region": ask("Région", old.get("region", "gra")), "bucket": ask("Conteneur", old.get("bucket", "orpailleur-2206")),
         "key": ask("Clé d'accès", old.get("key", ""))}
    c["secret"] = getpass.getpass("Clé secrète (rien ne s'affiche) : ").strip() or old.get("secret", "")
    if not (c["key"] and c["secret"]):
        sys.exit("Clé d'accès et clé secrète obligatoires.")
    _C[:] = [c]
    k, body = "inbox/_essai_orpailleur.txt", b"essai " + str(time.time()).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url("PUT", k, 300), data=body, method="PUT"), timeout=30).read()
        got = urllib.request.urlopen(url("GET", k, 300), timeout=30).read()
        delete(k)
    except (OSError, urllib.error.HTTPError) as e:
        sys.exit(f"✗ Essai raté ({e}) : vérifie les clés, la région et le nom du conteneur. Rien n'a été enregistré.")
    if got != body:
        sys.exit("✗ Le fichier relu ne correspond pas : rien n'a été enregistré.")
    # CORS : la page envoie les fichiers (vidéos, musiques) directement sur S3 depuis le navigateur
    xml = CORS.encode()
    req = urllib.request.Request(presign("PUT", "", 300, c, query={"cors": ""}), data=xml, method="PUT",
                                 headers={"Content-MD5": base64.b64encode(hashlib.md5(xml).digest()).decode()})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        print(f"! CORS non réglé ({e.code} {e.read()[:200]!r}) : les envois depuis la page échoueront, dis-le à Claude.")
    CONF_FILE.write_text(json.dumps(c, indent=1), "utf-8")
    os.chmod(CONF_FILE, 0o600)
    print(f"✓ S3 prêt : {c['bucket']} ({c['endpoint']}). Clés enregistrées dans {CONF_FILE} (lisible par toi seul).")


if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"]:
        setup()
    elif sys.argv[1:2] == ["push"] and len(sys.argv) == 4:
        if not conf():
            sys.exit("Lance d'abord : python s3.py setup")
        push(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
