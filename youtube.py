"""Statistiques de la chaîne YouTube (YouTube Analytics), avec l'accord du propriétaire (OAuth Google, bibliothèque standard).
  python youtube.py login    ouvre le navigateur : choisir la chaîne puis « Autoriser » (une seule fois)
  python youtube.py report   -> data/youtube/<jour>.json : vidéos, vues, temps de visionnage, rétention, sources de trafic
Prérequis : data/google_client.json = le fichier « client OAuth » (type Application de bureau) téléchargé depuis Google Cloud.
Le jeton d'accès est gardé dans data/google_token.json (jamais dans le dépôt : data/ est ignoré par git)."""
import base64
import datetime
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request

import store

CLIENT, TOKEN, OUT = store.DATA / "google_client.json", store.DATA / "google_token.json", store.DATA / "youtube"
SCOPES = "https://www.googleapis.com/auth/yt-analytics.readonly https://www.googleapis.com/auth/youtube.readonly"


def client():
    c = json.loads(CLIENT.read_text("utf-8"))
    return c.get("installed") or c.get("web") or c


def post(url, data):
    return json.load(urllib.request.urlopen(url, urllib.parse.urlencode(data).encode(), timeout=30))


def pkce():
    v = secrets.token_urlsafe(64)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def login():
    c, (verifier, challenge), state, got = client(), pkce(), secrets.token_urlsafe(16), {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            ok = got.get("state", [""])[0] == state and "code" in got
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h2>C'est bon, tu peux fermer cette page.</h2>" if ok else "<h2>Échec : relance la connexion.</h2>").encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES, "state": state,
        "access_type": "offline", "prompt": "consent", "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Ouvre ce lien si le navigateur ne s'ouvre pas tout seul :\n" + url, flush=True)
    subprocess.run(["explorer.exe", url], capture_output=True)  # navigateur Windows (WSL) ; sans effet ailleurs
    while "code" not in got and "error" not in got:
        srv.handle_request()
    if got.get("state", [""])[0] != state or "code" not in got:
        raise SystemExit("Connexion refusée ou interrompue : " + str(got.get("error", ["?"])[0]))
    t = post(c["token_uri"] if "token_uri" in c else "https://oauth2.googleapis.com/token", {
        "client_id": c["client_id"], "client_secret": c.get("client_secret", ""), "code": got["code"][0],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect})
    TOKEN.write_text(json.dumps({"refresh_token": t["refresh_token"]}), "utf-8")
    os.chmod(TOKEN, 0o600)
    print("Connecté. Jeton gardé dans", TOKEN)


def access_token():
    c = client()
    return post("https://oauth2.googleapis.com/token", {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                "refresh_token": json.loads(TOKEN.read_text("utf-8"))["refresh_token"], "grant_type": "refresh_token"})["access_token"]


def api(tok, base, **q):
    r = urllib.request.Request(base + "?" + urllib.parse.urlencode(q), headers={"Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(r, timeout=60))


def report():
    tok, today = access_token(), datetime.date.today().isoformat()
    yt, an = "https://www.googleapis.com/youtube/v3/", "https://youtubeanalytics.googleapis.com/v2/reports"
    ch = api(tok, yt + "channels", part="snippet,statistics,contentDetails", mine="true")["items"][0]
    up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in api(tok, yt + "playlistItems", part="contentDetails", playlistId=up, maxResults=50)["items"]]
    vids = api(tok, yt + "videos", part="snippet,contentDetails,statistics", id=",".join(ids))["items"] if ids else []
    span = {"ids": "channel==MINE", "startDate": "2005-01-01", "endDate": today}
    per_video = api(tok, an, **span, dimensions="video", sort="-views", maxResults=50,
                    metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,likes,comments")
    traffic = api(tok, an, **span, dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched")
    retention = {v["id"]: api(tok, an, **span, dimensions="elapsedVideoTimeRatio", filters="video==" + v["id"],
                              metrics="audienceWatchRatio,relativeRetentionPerformance") for v in vids}
    OUT.mkdir(exist_ok=True)
    f = OUT / f"{today}.json"
    f.write_text(json.dumps({"channel": ch, "videos": vids, "per_video": per_video, "traffic": traffic, "retention": retention},
                            ensure_ascii=False, indent=1), "utf-8")
    print(f"{ch['snippet']['title']} : {ch['statistics'].get('subscriberCount')} abonnés, {len(vids)} vidéos -> {f}")


if __name__ == "__main__":
    {"login": login, "report": report}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
