#!/usr/bin/env python3
"""Holt Statistiken, Weltinfo und Server-Log per FTP vom Nitrado-Server und schreibt stats.json."""
import datetime
import ftplib
import gzip
import io
import json
import os
import posixpath
import re
import struct
import sys

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json$", re.I)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")
FEED_MAX = 500
UTC = datetime.timezone.utc


# ---------------------------------------------------------------- FTP
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


def read_raw(ftp, path):
    buf = io.BytesIO()
    ftp.retrbinary("RETR " + path, buf.write)
    return buf.getvalue()


def read(ftp, path):
    return json.loads(read_raw(ftp, path).decode("utf-8"))


# ---------------------------------------------------------------- Statistiken
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

    def pick(d, *names):
        d = d or {}
        return int(sum(float(d.get("minecraft:" + n, 0) or 0) for n in names))

    moves = ["walk", "sprint", "crouch", "swim", "fly", "aviate", "walk_on_water", "walk_under_water",
             "boat", "horse", "minecart", "pig", "strider", "climb"]
    cm = sum(g(m + "_one_cm") for m in moves)
    killed, mined = st.get("minecraft:killed"), st.get("minecraft:mined")
    return {
        "play": int(g("play_time") or g("play_one_minute")),
        "deaths": int(g("deaths")), "mobKills": int(g("mob_kills")), "playerKills": int(g("player_kills")),
        "mined": int(total(mined)), "crafted": int(total(st.get("minecraft:crafted"))),
        "km": round(cm / 100000, 2), "jumps": int(g("jump")),
        "dmg": round(g("damage_dealt") / 10), "dmgTaken": round(g("damage_taken") / 10),
        "fish": int(g("fish_caught")), "bred": int(g("animals_bred")), "trades": int(g("traded_with_villager")),
        "topMined": top(mined, 8),
        "topKilled": top(killed, 8),
        "killedBy": top(st.get("minecraft:killed_by"), 10),
        "ms": {
            "dragon": pick(killed, "ender_dragon"), "wither": pick(killed, "wither"),
            "warden": pick(killed, "warden"), "elder": pick(killed, "elder_guardian"),
            "diamonds": pick(mined, "diamond_ore", "deepslate_diamond_ore"),
            "emeralds": pick(mined, "emerald_ore", "deepslate_emerald_ore"),
            "debris": pick(mined, "ancient_debris"),
        },
    }


# ---------------------------------------------------------------- Weltinfo (level.dat, ohne Seed)
def parse_nbt(data):
    pos = 0

    def rd(fmt):
        nonlocal pos
        size = struct.calcsize(">" + fmt)
        v = struct.unpack_from(">" + fmt, data, pos)[0]
        pos += size
        return v

    def rstr():
        nonlocal pos
        n = rd("H")
        s = data[pos:pos + n].decode("utf-8", "replace")
        pos += n
        return s

    def payload(t):
        nonlocal pos
        if t == 1: return rd("b")
        if t == 2: return rd("h")
        if t == 3: return rd("i")
        if t == 4: return rd("q")
        if t == 5: return rd("f")
        if t == 6: return rd("d")
        if t == 7:
            n = rd("i"); pos += n; return None
        if t == 8: return rstr()
        if t == 9:
            it = rd("b"); n = rd("i")
            return [payload(it) for _ in range(n)]
        if t == 10:
            d = {}
            while True:
                tt = rd("b")
                if tt == 0:
                    return d
                k = rstr()
                d[k] = payload(tt)
        if t == 11:
            n = rd("i"); pos += 4 * n; return None
        if t == 12:
            n = rd("i"); pos += 8 * n; return None
        raise ValueError("unbekannter NBT-Typ %s" % t)

    t = rd("b")
    rstr()
    return payload(t)


def read_world(ftp, world_dir):
    try:
        d = parse_nbt(gzip.decompress(read_raw(ftp, posixpath.join(world_dir, "level.dat"))))["Data"]
    except Exception as e:
        print("level.dat nicht lesbar:", e)
        return None
    daytime = int(d.get("DayTime", d.get("Time", 0)) or 0)
    time_total = int(d.get("Time", daytime) or 0)
    weather = "Gewitter" if d.get("thundering") else "Regen" if d.get("raining") else "Klar"
    diff = {0: "Friedlich", 1: "Leicht", 2: "Normal", 3: "Schwer"}.get(d.get("Difficulty"))
    mode = {0: "Überleben", 1: "Kreativ", 2: "Abenteuer", 3: "Zuschauer"}.get(d.get("GameType"))
    ver = (d.get("Version") or {}).get("Name")
    # Absichtlich NICHT enthalten: Seed und Spawn-Koordinaten.
    out = {"version": ver, "day": daytime // 24000 + 1, "age": time_total // 24000,
           "weather": weather, "difficulty": diff, "mode": mode, "hardcore": bool(d.get("hardcore"))}
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- Server-Log
LINE_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] \[[^\]]*/INFO\](?: \[[^\]]*\])?: (.*)$")
JOIN_RE = re.compile(r"^(\w{1,16}) joined the game$")
LEAVE_RE = re.compile(r"^(\w{1,16}) left the game$")
ADV_RE = re.compile(r"^(\w{1,16}) has (made the advancement|completed the challenge|reached the goal) \[(.+)\]$")
GEN_RE = re.compile(r"^(\w{1,16}) (.+)$")
DEATH_START = re.compile(
    r"^(was (slain|shot|fireballed|killed|blown|pummeled|impaled|stung|pricked|squashed|struck|doomed|burnt|roasted|"
    r"skewered|poked|obliterated|knocked|frozen|sniped|spitballed|pushed)|fell |hit the ground|drowned|burned|went up|"
    r"walked into|tried to|suffocated|starved|froze|withered|discovered|experienced|blew up|died|left the confines|"
    r"didn't want)")
DEATHS = [
    (r"^was slain by (.+?)(?: using .+)?$", "wurde von {0} erschlagen"),
    (r"^was shot by (.+?)(?: using .+)?$", "wurde von {0} erschossen"),
    (r"^was fireballed by (.+?)(?: using .+)?$", "wurde von {0} mit einem Feuerball getroffen"),
    (r"^was blown up by (.+?)(?: using .+)?$", "wurde von {0} in die Luft gesprengt"),
    (r"^was killed by (.+)$", "wurde von {0} getötet"),
    (r"^was pummeled by (.+?)(?: using .+)?$", "wurde von {0} verprügelt"),
    (r"^was impaled by (.+?)(?: using .+)?$", "wurde von {0} aufgespießt"),
    (r"^was stung to death", "wurde zu Tode gestochen"),
    (r"^was pricked to death", "wurde zu Tode gestochen"),
    (r"^walked into a cactus", "ist in einen Kaktus gelaufen"),
    (r"^blew up", "ist explodiert"),
    (r"^was blown up", "wurde in die Luft gesprengt"),
    (r"^drowned", "ist ertrunken"),
    (r"^burned to death|^was burnt to a crisp", "ist verbrannt"),
    (r"^went up in flames", "ging in Flammen auf"),
    (r"^walked into fire", "ist ins Feuer gelaufen"),
    (r"^tried to swim in lava", "ist in Lava geschwommen"),
    (r"^discovered (?:that )?the floor was lava", "hat entdeckt, dass der Boden Lava ist"),
    (r"^suffocated in a wall", "ist in einer Wand erstickt"),
    (r"^starved to death", "ist verhungert"),
    (r"^froze to death|^was frozen to death", "ist erfroren"),
    (r"^withered away", "ist verwelkt"),
    (r"^was struck by lightning", "wurde vom Blitz getroffen"),
    (r"^was squashed by a falling anvil", "wurde von einem Amboss zerquetscht"),
    (r"^was squashed by a falling block", "wurde von einem Block zerquetscht"),
    (r"^fell out of the world", "ist aus der Welt gefallen"),
    (r"^was doomed to fall", "war zum Fallen verdammt"),
    (r"^fell from a high place|^hit the ground too hard|^fell off|^fell while climbing", "ist von einem hohen Ort gefallen"),
    (r"^experienced kinetic energy", "hat kinetische Energie erlebt"),
    (r"^left the confines of this world", "hat die Welt verlassen"),
    (r"^died$", "ist gestorben"),
]
ADV_KINDS = {"made the advancement": "hat den Fortschritt «{0}» erreicht",
             "completed the challenge": "hat die Herausforderung «{0}» gemeistert",
             "reached the goal": "hat das Ziel «{0}» erreicht"}


def translate_death(rest):
    for pat, tpl in DEATHS:
        m = re.match(pat, rest)
        if m:
            return tpl.format(*m.groups())
    return rest


def read_log(ftp, root, known):
    path = raw = None
    for cand in (posixpath.join(root, "logs/latest.log"), "/minecraft/logs/latest.log", "/logs/latest.log"):
        try:
            raw = read_raw(ftp, cand)
            path = cand
            break
        except Exception:
            continue
    if raw is None:
        print("Kein Server-Log gefunden.")
        return None
    lines = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        m = LINE_RE.match(ln)
        if m:
            lines.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), m.group(4)))
    if not lines:
        return [], []
    mdtm = None
    try:
        mdtm = datetime.datetime.strptime(ftp.sendcmd("MDTM " + path)[4:18], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except Exception:
        pass
    offset = 0
    if mdtm is not None:
        md_secs = mdtm.hour * 3600 + mdtm.minute * 60 + mdtm.second
        diff = (lines[-1][0] - md_secs) % 86400
        if diff > 43200:
            diff -= 86400
        offset = int(round(diff / 900.0)) * 900  # Zeitzone des Servers, auf Viertelstunden gerundet
    else:
        mdtm = datetime.datetime.now(UTC)
    day = (mdtm + datetime.timedelta(seconds=offset)).date()
    stamped, prev = [], lines[-1][0]
    for secs, msg in reversed(lines):
        if secs > prev:
            day -= datetime.timedelta(days=1)
        prev = secs
        local = datetime.datetime(day.year, day.month, day.day, secs // 3600, secs // 60 % 60, secs % 60)
        stamped.append((local - datetime.timedelta(seconds=offset), msg))
    stamped.reverse()

    events, online = [], []
    known = set(known)
    for dt, msg in stamped:
        ts = dt.replace(tzinfo=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        if msg == "Stopping server":
            online = []
            continue
        m = JOIN_RE.match(msg)
        if m:
            known.add(m.group(1))
            if m.group(1) not in online:
                online.append(m.group(1))
            events.append({"ts": ts, "kind": "join", "p": m.group(1), "text": "ist beigetreten"})
            continue
        m = LEAVE_RE.match(msg)
        if m:
            online = [n for n in online if n != m.group(1)]
            events.append({"ts": ts, "kind": "leave", "p": m.group(1), "text": "hat den Server verlassen"})
            continue
        m = ADV_RE.match(msg)
        if m:
            events.append({"ts": ts, "kind": "adv", "p": m.group(1), "text": ADV_KINDS[m.group(2)].format(m.group(3))})
            continue
        m = GEN_RE.match(msg)
        if m and m.group(1) in known and DEATH_START.match(m.group(2)):
            events.append({"ts": ts, "kind": "death", "p": m.group(1), "text": translate_death(m.group(2))})
    return events, online


def event_key(e):
    return "%s|%s|%s|%s" % (e["ts"], e["kind"], e["p"], e["text"])


# ---------------------------------------------------------------- main
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
    world_dir = posixpath.dirname(stats_dir.rstrip("/")) or "/"
    root = posixpath.dirname(world_dir) or "/"

    names = {}
    for cand in (posixpath.join(root, "usercache.json"), "/minecraft/usercache.json", "/usercache.json"):
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
    players.sort(key=lambda p: -p["s"]["play"])

    world = read_world(ftp, world_dir)
    known = {p["name"] for p in players} | set(names.values())
    log = read_log(ftp, root, known)
    ftp.quit()

    feed = list(old.get("feed", []))
    causes = dict(old.get("deathCauses", {}))
    online = old.get("online", [])
    if log is not None:
        events, online = log
        seen = {event_key(e) for e in feed}
        for e in events:
            if event_key(e) in seen:
                continue
            feed.append(e)
            seen.add(event_key(e))
            if e["kind"] == "death":
                causes[e["text"]] = causes.get(e["text"], 0) + 1
        feed.sort(key=lambda e: e["ts"], reverse=True)
        feed = feed[:FEED_MAX]

    new = {"server": os.environ.get("SERVER_NAME", "").strip(), "players": players,
           "world": world if world is not None else old.get("world"),
           "online": online, "feed": feed, "deathCauses": causes}
    prev = {k: v for k, v in old.items() if k != "updated"}
    if prev == new and old.get("updated"):
        new["updated"] = old["updated"]
    else:
        new["updated"] = datetime.datetime.now(UTC).isoformat(timespec="seconds")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    print("%d Spieler, %d Feed-Einträge geschrieben." % (len(players), len(feed)))


if __name__ == "__main__":
    main()
