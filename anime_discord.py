#!/usr/bin/env python3
"""Notifications Discord : un message quand un épisode d'anime sort sur Crunchyroll.

Utilise un webhook Discord (pas besoin de créer un bot). Aucune dépendance : Python 3.9+ seulement.

  export DISCORD_WEBHOOK="https://discord.com/api/webhooks/...."
  python3 anime_discord.py            # tourne en continu (laisser ouvert)
  python3 anime_discord.py --once     # une seule vérification (pour cron / GitHub Actions)
  python3 anime_discord.py --test     # envoie un message de test
  python3 anime_discord.py --list     # affiche les 15 prochaines sorties

Options (variables d'environnement) :
  DISCORD_WEBHOOK  URL du webhook (obligatoire)
  DISCORD_MENTION  ex. "<@123456789012345678>" pour être notifié par une mention (facultatif)
  GRACE_MIN        retard maximal rattrapé, en minutes (10 par défaut ; 45 avec GitHub Actions)
  SUIVIS           titres suivis séparés par des virgules (sinon : tous), ex. "Black Clover,PSYREN"
Les horaires sont ceux de la page (heure de Paris). Le fichier animes.json contient les données.
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, tzinfo, date

HERE = os.path.dirname(os.path.abspath(__file__))



def _last_sunday(year, month):
    d = date(year, month, 31 if month in (3, 10) else 30)
    return d - timedelta(days=(d.weekday() + 1) % 7)


class ParisTZ(tzinfo):
    """Heure de Paris sans dépendre de la base de fuseaux (absente sur Windows)."""
    def _dst(self, dt):
        y = dt.year
        start = datetime(y, 3, _last_sunday(y, 3).day, 2)
        end = datetime(y, 10, _last_sunday(y, 10).day, 3)
        return timedelta(hours=1) if start <= dt.replace(tzinfo=None) < end else timedelta(0)

    def utcoffset(self, dt):
        return timedelta(hours=1) + self._dst(dt)

    def dst(self, dt):
        return self._dst(dt)

    def tzname(self, dt):
        return "CEST" if self._dst(dt) else "CET"


try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = ParisTZ()
GRACE = timedelta(minutes=int(os.environ.get("GRACE_MIN", "10")))   # on rattrape une sortie manquée de moins de N min
SENT_FILE = os.path.join(HERE, "deja_envoyes.json")


def load():
    with open(os.path.join(HERE, "animes.json"), encoding="utf-8") as f:
        return json.load(f)


def release_dt(a, day):
    """Heure de sortie (Paris) de l'anime `a` le jour `day` (date). None si pas de sortie ce jour-là."""
    if not a.get("p") or not a.get("f") or day.weekday() != a["d"]:
        return None
    first = datetime.strptime(a["f"], "%Y-%m-%d").date()
    if day < first:
        return None
    ep = (day - first).days // 7 + 1
    if a.get("tot") and ep > a["tot"]:
        return None
    h, m = map(int, a["p"].split(":"))
    minutes = h * 60 + m
    # Même règle que la page : -60 min du 25 octobre au 31 octobre 2026 (décalage des changements d'heure US/EU)
    if "2026-10-25" <= day.isoformat() < "2026-11-01":
        minutes -= 60
    minutes %= 1440
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=PARIS), ep


def releases_between(animes, start, end):
    out = []
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        for a in animes:
            r = release_dt(a, day)
            if r and start < r[0] <= end:
                out.append((r[0], r[1], a))
        day += timedelta(days=1)
    return sorted(out, key=lambda x: x[0])


def followed(a):
    s = os.environ.get("SUIVIS", "").strip()
    if not s:
        return True
    return any(x.strip().lower() in a["t"].lower() for x in s.split(",") if x.strip())


def post(content, embed=None):
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        sys.exit("Variable DISCORD_WEBHOOK manquante (voir l'aide en haut du fichier).")
    body = {"content": content}
    if embed:
        body["embeds"] = [embed]
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "User-Agent": "anime-notifier/1.0"})
    for _ in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)))
                continue
            print("Erreur Discord", e.code, e.read()[:200])
            return False
        except Exception as e:
            print("Erreur réseau :", e)
            time.sleep(2)
    return False


def notify(when, ep, a):
    tot = a.get("tot")
    mention = os.environ.get("DISCORD_MENTION", "")
    embed = {
        "title": f"🎬 {a['t']}",
        "description": f"**Épisode {ep}{' / ' + str(tot) if tot else ''}** est disponible sur Crunchyroll\nVO japonaise + sous-titres français",
        "color": 0xE8590C,
        "footer": {"text": f"{a['j']} · prévu à {when:%H:%M} (Paris)"},
    }
    if a.get("cr"):
        embed["url"] = a["cr"]
    return post((mention + " " if mention else "") + f"Nouvel épisode : {a['t']}", embed)


def load_sent():
    try:
        with open(SENT_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(s):
    keep = sorted(s)[-500:]
    with open(SENT_FILE, "w") as f:
        json.dump(keep, f)


def check(animes, sent):
    now = datetime.now(PARIS)
    n = 0
    for when, ep, a in releases_between(animes, now - GRACE, now):
        key = f"{when.isoformat()}|{a['t']}"
        if key in sent or not followed(a):
            continue
        if notify(when, ep, a):
            sent.add(key)
            n += 1
            print(f"{now:%d/%m %H:%M} envoyé : {a['t']} ép. {ep}")
    if n:
        save_sent(sent)


def main():
    animes = load()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--test":
        ok = post("✅ Test : le bot d'animes est bien connecté à ce salon.")
        print("Message de test envoyé." if ok else "Échec de l'envoi.")
    elif arg == "--list":
        now = datetime.now(PARIS)
        for when, ep, a in [r for r in releases_between(animes, now, now + timedelta(days=8)) if followed(r[2])][:15]:
            print(f"{when:%a %d/%m %H:%M}  ép. {ep}  {a['t']}")
    elif arg == "--once":
        check(animes, load_sent())
    else:
        print("Bot lancé. Ctrl+C pour arrêter. Vérification toutes les 30 s.")
        sent = load_sent()
        while True:
            check(animes, sent)
            time.sleep(30)


if __name__ == "__main__":
    main()
