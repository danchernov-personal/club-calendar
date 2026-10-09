#!/usr/bin/env python3
"""Генерирует .ics со всеми матчами клубов: календарь sports.ru (все турниры) + API ФНЛ для матчей ФНЛ."""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

# Ключ — слаг клуба на sports.ru (sports.ru/football/club/<слаг>/).
# fnl — матчи этого турнира берём из API ФНЛ: там окна дат для неназначенных туров, трансляции, билеты, судьи.
CLUBS = {
    "ural": {"fnl": {"league": 100, "team": "ural", "sportsru_tournament": "1liga"}},
    "zenit": {},
}
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "calendars"

SPORTSRU = "https://www.sports.ru"
FNL_API = "https://fnl-app.fnl.pro/api/v1"
MATCH_DURATION = timedelta(hours=2)
MSK = timezone(timedelta(hours=3))
PLACEHOLDER_TIME = "03:00"
UNKNOWN_PLACE = (None, "", "Неизвестно", "Sports.ru")
FNL_PAGE = 15
BROADCASTS = {
    "match-tv": "Матч ТВ",
    "match-premier": "Матч Премьер",
    "kinopoisk": "Кинопоиск",
    "vk": "VK Видео",
    "tricolor": "Триколор",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (club-calendar)", "Accept-Language": "ru"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def text(fragment):
    fragment = re.sub(r"<!‐‐.*?‐‐>|<[^>]+>", " ", fragment)
    return " ".join(html.unescape(fragment).split())


def first(pattern, s, default=None):
    m = re.search(pattern, s, re.S)
    return html.unescape(m.group(1)).strip() if m else default


def place(*parts):
    return ", ".join(p for p in parts if p not in UNKNOWN_PLACE)


def title(home, guest, score):
    return f"{home} {score[0]}:{score[1]} {guest}" if score else f"{home} – {guest}"


def timed(start):
    return [f"DTSTART:{utc(start)}", f"DTEND:{utc(start + MATCH_DURATION)}"]


def all_day(d_from, d_to):
    return [f"DTSTART;VALUE=DATE:{d_from:%Y%m%d}", f"DTEND;VALUE=DATE:{d_to + timedelta(days=1):%Y%m%d}", "TRANSP:TRANSPARENT"]


# --- sports.ru -------------------------------------------------------------


def sportsru_events(club):
    page = fetch(f"{SPORTSRU}/football/club/{club}/calendar/")
    club_name = first(r'<meta property="og:title" content="([^"]+?)(?: [-–—:|].*)?"', page) or club
    start = page.find('<table class="stat-table"')
    if start < 0:
        raise ValueError("sports.ru: calendar table not found")
    table = page[start:page.find("</table>", start)]

    events, seen = [], Counter()
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        venue = first(r'class="alRight padR20">([^<]*)<', row)
        if len(cells) < 3 or venue is None:
            continue
        d, _, t = text(cells[0]).replace(" ", "").partition("|")
        d = datetime.strptime(d, "%d.%m.%Y").date()
        score_text = text(first(r'class="score-td">(.*?)</td>', row, ""))
        score = re.fullmatch(r"(\d+) : (\d+)", score_text)
        opponent = first(r'<a [^>]*title="([^"]+)"', cells[2], "")
        is_home = venue == "Дома"
        tournament = first(r'title="([^"]+)"', cells[1], "")
        tournament_slug = first(r"/tournament/([^/]+)/", cells[1], "")
        link = first(r'class="score"[^>]*href="([^"]+)"', row)
        url = SPORTSRU + link if link and link.startswith("/") else link

        home, guest = (club_name, opponent) if is_home else (opponent, club_name)
        summary = title(home, guest, score.groups() if score else None)
        desc = [tournament]
        if not score and score_text not in ("превью", ""):
            desc.append(f"Статус: {score_text}")
        if t and t != PLACEHOLDER_TIME:
            timing = timed(datetime.combine(d, datetime.strptime(t, "%H:%M").time(), MSK))
        else:
            timing = all_day(d, d)
            summary = f"⏳ {summary}"
            desc.append("Время, а возможно и дата, ещё не назначены: день ориентировочный.")
        if url:
            desc.append(url)

        season_id = first(r"[?]s=(\d+)", cells[1], "")
        opponent_slug = first(r"/club/([^/]+)/", cells[2], "")
        key = f"{season_id}-{tournament_slug}-{opponent_slug}-{'h' if is_home else 'a'}"
        seen[key] += 1
        events.append(
            {
                "uid": f"sportsru-{key}-{seen[key]}",
                "sort": d.isoformat(),
                "tournament_slug": tournament_slug,
                "timing": timing,
                "summary": summary,
                "location": place(
                    first(r'itemprop="location".*?itemprop="name" content="([^"]*)"', row),
                    first(r'itemprop="location".*?itemprop="address" content="([^"]*)"', row),
                ),
                "desc": desc,
                "url": url,
            }
        )
    if not events:
        raise ValueError("sports.ru: no matches parsed")
    return club_name, events


# --- ФНЛ -------------------------------------------------------------------


def fnl_get(path, **query):
    return json.loads(fetch(f"{FNL_API}{path}?{urllib.parse.urlencode(query)}"))


def fnl_matches(path, **query):
    matches, offset = [], 0
    while True:
        data = fnl_get(path, limit=FNL_PAGE, offset=offset, **query)
        matches += data.get("matches") or []
        offset += FNL_PAGE
        if offset >= (data.get("location") or {}).get("count", 0):
            return matches


def fnl_events(league, team_slug):
    team = fnl_get("/team/info", leagueId=league, teamSlug=team_slug)
    season = fnl_get("/info/activeSeason", leagueId=league)
    query = dict(leagueId=league, teamId=team["teamId"], seasonId=season["seasonId"])
    matches = {m["matchId"]: m for m in fnl_matches("/team/matchesResult", **query) + fnl_matches("/team/calendar", **query)}
    if not matches:
        raise ValueError("fnl: no matches received")

    events = []
    for m in matches.values():
        finished = m["status"] == "END"
        score = (m["home"]["score"], m["guest"]["score"]) if finished else None
        summary = title(m["home"]["name"], m["guest"]["name"], score)
        desc = [f"ФНЛ, {m.get('tour') or ''} (сезон {m.get('season') or season['title']})"]

        raw = m.get("date")
        if raw and " " in raw:
            timing = timed(datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=MSK))
        elif raw:
            timing = all_day(date.fromisoformat(raw), date.fromisoformat(raw))
            summary = f"⏳ {summary}"
            desc.append("Время начала ещё не объявлено.")
        else:
            d_from = date.fromisoformat(m["estimatedDate"])
            d_to = date.fromisoformat(m.get("estimatedDateTo") or m["estimatedDate"])
            timing = all_day(d_from, d_to)
            summary = f"⏳ {summary}"
            desc.append(f"Дата ещё не назначена: матч пройдёт между {d_from:%d.%m} и {d_to:%d.%m}.")

        if (m.get("judges") or {}).get("name"):
            desc.append(f"Судья: {m['judges']['name'].strip()}")
        if not finished:
            tv = [f"{BROADCASTS.get(l.get('typeLink'), l.get('typeLink') or 'Трансляция')}: {l['url']}" for l in m.get("links") or [] if l.get("url")]
            if tv:
                desc.append("Где смотреть:\n" + "\n".join(tv))
            if m.get("ticketUrl"):
                desc.append(f"Билеты: {m['ticketUrl']}")
        url = f"https://fnl.pro/pari/matches/{m['matchId']}"
        desc.append(url)

        stadium = m.get("stadium") or {}
        events.append(
            {
                "uid": f"fnl-{league}-match-{m['matchId']}",
                "sort": raw or m.get("estimatedDate") or "",
                "timing": timing,
                "summary": summary,
                "location": place(stadium.get("title"), stadium.get("city")),
                "desc": desc,
                "url": url,
            }
        )
    return events


# --- iCalendar -------------------------------------------------------------


def escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    if len(line.encode("utf-8")) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render(club, club_name, events, sources):
    now = utc(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//club-calendar//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(club_name)} — матчи",
        "X-WR-CALDESC:" + escape(f"Все матчи {club_name} во всех турнирах. Источники: {sources}"),
        "X-WR-TIMEZONE:Europe/Moscow",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for e in sorted(events, key=lambda e: e["sort"]):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}@{club}.club-calendar",
            f"DTSTAMP:{now}",
            *e["timing"],
            f"SUMMARY:{escape(e['summary'])}",
            *([f"LOCATION:{escape(e['location'])}"] if e["location"] else []),
            f"DESCRIPTION:{escape(chr(10).join(e['desc']))}",
            *([f"URL:{e['url']}"] if e["url"] else []),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def build(club, config):
    """Возвращает (имя клуба, события, источники, частичный сбой)."""
    club_name, events = sportsru_events(club)
    fnl = config.get("fnl")
    if not fnl:
        return club_name, events, "sports.ru", False
    try:
        league_events = fnl_events(fnl["league"], fnl["team"])
    except Exception as e:
        print(f"{club}: fnl failed, using sports.ru for the league: {e}", file=sys.stderr)
        return club_name, events, "sports.ru", True
    rest = [e for e in events if e["tournament_slug"] != fnl["sportsru_tournament"]]
    return club_name, rest + league_events, "fnl.pro, sports.ru", False


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    failed = False
    for club, config in CLUBS.items():
        try:
            club_name, events, sources, partial = build(club, config)
        except Exception as e:
            print(f"{club}: failed, keeping previous file: {e}", file=sys.stderr)
            failed = True
            continue
        failed |= partial
        path = os.path.join(OUT_DIR, f"{club}.ics")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(render(club, club_name, events, sources))
        print(f"{club} ({club_name}): {len(events)} matches from {sources} -> {path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
