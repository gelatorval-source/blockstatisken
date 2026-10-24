#!/usr/bin/env python3
"""Holt die Spielerstatistiken per FTP vom Nitrado-Server und schreibt stats.json."""
import datetime
import ftplib
import io
import json
import os
import posixpath
import re
import sys

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json$", re.I)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")


def connect():
    host = os.environ["FTP_HOST"]
    port = int(os.environ.get("FTP_PORT") or 21)
    user, pw = os.environ["FTP_USER"], os.environ["FTP_PASSWORD"]
    try:
        ftp = ftplib.FTP_TLS()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pw)
        ftp.prot_p()
        return ftp
    except Exception as e:  # Server kann kein FTPS: normales FTP versuchen
        print("FTPS nicht möglich (%s), versuche FTP" % e)
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pw)
        return ftp


def listdir(ftp, path):
    """Liefert [(name, is_dir)] oder None, wenn der Ordner nicht existiert."""
    try:
        return [(n, f.get("type") == "dir") for n, f in ftp.mlsd(path) if n not in (".", "..") and f.get("type") in ("file", "dir")]
    except ftplib.error_perm:
        return None
    except Exception:
        try:
            names = ftp.nlst(path)
        except Exception:
            return None
        return [(posixpath.basename(n.rstrip("/")), False) for n in names]


def find_stats_dir(ftp):
    configured = os.environ.get("FTP_STATS_DIR", "").strip()
    if configured:
        return configured
    for cand in ("/minecraft/world/stats", "/world/stats", "minecraft/world/stats", "world/stats"):
        items = listdir(ftp, cand)
        if items and any(UUID_RE.match(n) for n, _ in items):
            return cand
    # Suche bis 4 Ebenen tief nach einem Ordner "stats" mit UUID-Dateien
    queue = [("/", 0)]
    while queue:
        path, depth = queue.pop(0)
        items = listdir(ftp, path) or []
        if posixpath.basename(path.rstrip("/")) == "stats" and any(UUID_RE.match(n) for n, _ in items):
            return path
        if depth < 4:
            for n, is_dir in items:
                if is_dir:
                    queue.append((posixpath.join(path, n), depth + 1))
    return None


def read(ftp, path):
    buf = io.BytesIO()
    ftp.retrbinary("RETR " + path, buf.write)
    return json.loads(buf.getvalue().decode("utf-8"))


def summarize(js):
    st = js.get("stats", {})
    c = st.get("minecraft:custom", {})

    def g(k):
        return float(c.get("minecraft:" + k, 0) or 0)

    def total(d):
        return sum(float(v or 0) for v in (d or {}).values())

    def top(d, n):
        rows = [[k.replace("minecraft:", ""), int(v)] for k, v in (d or {}).items() if v and v > 0]
        return sorted(rows, key=lambda r: -r[1])[:n]

    moves = ["walk", "sprint", "crouch", "swim", "fly", "aviate", "walk_on_water", "walk_under_water",
             "boat", "horse", "minecart", "pig", "strider", "climb"]
    cm = sum(g(m + "_one_cm") for m in moves)
    return {
        "play": int(g("play_time") or g("play_one_minute")),
        "deaths": int(g("deaths")), "mobKills": int(g("mob_kills")), "playerKills": int(g("player_kills")),
        "mined": int(total(st.get("minecraft:mined"))), "crafted": int(total(st.get("minecraft:crafted"))),
        "km": round(cm / 100000, 2), "jumps": int(g("jump")),
        "dmg": round(g("damage_dealt") / 10), "dmgTaken": round(g("damage_taken") / 10),
        "fish": int(g("fish_caught")), "bred": int(g("animals_bred")), "trades": int(g("traded_with_villager")),
        "topMined": top(st.get("minecraft:mined"), 8),
        "topKilled": top(st.get("minecraft:killed"), 8),
        "killedBy": top(st.get("minecraft:killed_by"), 5),
    }


def main():
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = {}
    old_names = {p["id"]: p.get("name") for p in old.get("players", [])}

    ftp = connect()
    stats_dir = find_stats_dir(ftp)
    if not stats_dir:
        sys.exit("Ordner mit den Statistiken nicht gefunden. Setze das Secret FTP_STATS_DIR (siehe README).")
    print("Statistik-Ordner:", stats_dir)

    names = {}
    base = posixpath.dirname(posixpath.dirname(stats_dir.rstrip("/"))) or "/"
    for cand in (posixpath.join(base, "usercache.json"), "/minecraft/usercache.json", "/usercache.json"):
        try:
            for u in read(ftp, cand):
                names[u["uuid"].lower()] = u["name"]
            print("Namen aus", cand)
            break
        except Exception:
            continue

    players = []
    for fname, _ in listdir(ftp, stats_dir) or []:
        m = UUID_RE.match(fname)
        if not m:
            continue
        uuid = m.group(1).lower()
        try:
            s = summarize(read(ftp, posixpath.join(stats_dir, fname)))
        except Exception as e:
            print("Übersprungen:", fname, e)
            continue
        players.append({"id": uuid, "name": names.get(uuid) or old_names.get(uuid) or "Spieler " + uuid[:4], "s": s})
    ftp.quit()

    players.sort(key=lambda p: -p["s"]["play"])
    new = {"server": os.environ.get("SERVER_NAME", "").strip(), "players": players}
    # Zeitstempel nur ändern, wenn sich wirklich etwas geändert hat (vermeidet unnötige Commits)
    if {"server": old.get("server", ""), "players": old.get("players", [])} == new and old.get("updated"):
        new["updated"] = old["updated"]
    else:
        new["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    print("%d Spieler geschrieben." % len(players))


if __name__ == "__main__":
    main()
