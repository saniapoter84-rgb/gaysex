#!/usr/bin/env python3
"""
add_to_db.py — добавляет запись с rezka.ag в rezka_database.json

Использование:
  python3 add_to_db.py <URL> [опции]

Опции:
  --translators "Имя1,Имя2"   оставить только эти переводы (подстрока, без учёта регистра)
  --all                        взять все доступные переводы без вопросов
  --yes                        не спрашивать подтверждения перед записью
  --dry-run                    показать что будет добавлено, но не писать в файл

Примеры:
  python3 add_to_db.py https://rezka.ag/cartoons/comedy/2136-rik-i-morti-2013.html --translators "Сыендук"
  python3 add_to_db.py https://rezka.ag/films/action/44774-operaciya-fortuna-2023.html --all --yes
"""

import argparse
import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rezka_database.json")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BASE_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://rezka.ag/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# ── Network ───────────────────────────────────────────────────────────────────

_cookie_jar = http.cookiejar.CookieJar()
_opener = build_opener(HTTPCookieProcessor(_cookie_jar))


def _read_response(resp):
    with resp as r:
        raw = r.read()
        enc = r.headers.get("Content-Encoding", "")
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def _solve_anubis(html, url):
    m = re.search(r'id="anubis_challenge"[^>]*>(\{.*?\})\s*</script>', html, re.DOTALL)
    if not m:
        return None
    wrap = json.loads(m.group(1))
    ch = wrap["challenge"]
    m2 = re.search(r'id="anubis_base_prefix"[^>]*>"([^"]*)"', html)
    base_prefix = m2.group(1) if m2 else ""
    difficulty = ch["difficulty"]
    p, u = difficulty // 2, difficulty % 2 != 0
    random_data = ch["randomData"]
    nonce = 0
    while True:
        digest = hashlib.sha256((random_data + str(nonce)).encode()).digest()
        ok = all(digest[i] == 0 for i in range(p))
        if ok and u and (digest[p] >> 4) != 0:
            ok = False
        if ok:
            break
        nonce += 1
    params = urlencode({
        "id": ch["id"],
        "response": digest.hex(),
        "nonce": str(nonce),
        "redir": url,
        "elapsedTime": "1337",
    })
    submit_url = f"https://rezka.ag{base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge?{params}"
    req = Request(submit_url, headers=_BASE_HEADERS)
    return _read_response(_opener.open(req, timeout=20))


def fetch(url):
    req = Request(url, headers=_BASE_HEADERS)
    html = _read_response(_opener.open(req, timeout=15))
    if "anubis_challenge" in html:
        real = _solve_anubis(html, url)
        if real:
            html = real
    return html


# ── Parsers ───────────────────────────────────────────────────────────────────

def normalize_url(url):
    """Clean up URL: strip whitespace and remove /ua/ locale prefix if present."""
    url = url.strip()
    # Remove /ua/ locale variant: rezka.ag/ua/films/... -> rezka.ag/films/...
    url = re.sub(r'(https://rezka\.ag)/ua/', r'\1/', url)
    return url


def detect_type(url, html):
    """Return one of: фильмы / сериалы / мультфильмы / аниме"""
    path = url.split("rezka.ag/")[-1].split("/")[0]
    mapping = {
        "films": "фильмы",
        "series": "сериалы",
        "cartoons": "мультфильмы",
        "animation": "аниме",
    }
    t = mapping.get(path)
    if t:
        return t
    # Fallback via JS init call
    if re.search(r'initCDNMoviesEvents', html):
        return "фильмы"
    if re.search(r'initCDNSeriesEvents', html):
        return "сериалы"
    return "фильмы"


def parse_title(html):
    for pat in (
        r'<h1[^>]*itemprop="name"[^>]*>\s*<span[^>]*>([^<]+)</span>',
        r'<h1[^>]*>\s*<span[^>]*>([^<]+)</span>',
        r'<h1[^>]*>([^<]+)</h1>',
        r'<title>([^<|]+)',
    ):
        m = re.search(pat, html)
        if m:
            title = m.group(1).strip()
            # Strip rezka.ag boilerplate from <title>
            title = re.sub(r'\s*[-–|].*?(смотреть|онлайн|бесплатно).*$', '', title, flags=re.IGNORECASE).strip()
            return title
    return ""


def parse_translators(html):
    """Return ordered list of translator display names."""
    result = {}

    def add(name, tid):
        name = name.strip().replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if name and name not in result:
            result[name] = tid

    # Series/anime: <li title="Name" data-translator_id="ID">
    for m in re.finditer(r'<li[^>]+title="([^"]+)"[^>]+data-translator_id="(\d+)"', html):
        add(m.group(1), m.group(2))
    for m in re.finditer(r'<li[^>]+data-translator_id="(\d+)"[^>]+title="([^"]+)"', html):
        add(m.group(2), m.group(1))
    # Films: <a title="Name" class="b-translator__items" data-translator_id="ID">
    for m in re.finditer(r'<a[^>]+title="([^"]+)"[^>]+data-translator_id="(\d+)"', html):
        add(m.group(1), m.group(2))
    for m in re.finditer(r'<a[^>]+data-translator_id="(\d+)"[^>]+title="([^"]+)"', html):
        add(m.group(2), m.group(1))
    # Fallback: older markup with data-translator-id (dash)
    if not result:
        for m in re.finditer(r'id="translator-\d+-(\d+)"[^>]*>\s*<b>([^<]+)</b>', html):
            add(m.group(2), m.group(1))

    return list(result.keys())


def parse_seasons_episodes(html, entry_type):
    """
    Return {season_num: episode_count} for series/anime/cartoons.
    Return {} for films.
    """
    if entry_type == "фильмы" or re.search(r'initCDNMoviesEvents', html):
        return {}

    ep_per_season = {}

    # Primary: <li data-season_id="S" data-episode_id="E">
    for m in re.finditer(r'data-season_id=["\'](\d+)["\'][^>]*data-episode_id=["\'](\d+)["\']', html):
        sn, ep = m.group(1), int(m.group(2))
        ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)
    for m in re.finditer(r'data-episode_id=["\'](\d+)["\'][^>]*data-season_id=["\'](\d+)["\']', html):
        ep, sn = int(m.group(1)), m.group(2)
        ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)

    if ep_per_season:
        return {k: v for k, v in sorted(ep_per_season.items(), key=lambda x: int(x[0]))}

    # Fallback: <option value="EP" data-season="S">
    for m in re.finditer(r'<option[^>]+data-season=["\'](\d+)["\'][^>]*value=["\'](\d+)["\']', html):
        sn, ep = m.group(1), int(m.group(2))
        if ep < 1000:
            ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)
    for m in re.finditer(r'<option[^>]+value=["\'](\d+)["\'][^>]*data-season=["\'](\d+)["\']', html):
        ep, sn = int(m.group(1)), m.group(2)
        if ep < 1000:
            ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)

    if ep_per_season:
        return {k: v for k, v in sorted(ep_per_season.items(), key=lambda x: int(x[0]))}

    return {"1": 0}


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_db():
    if not os.path.exists(DB_PATH):
        return []
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=4)


# ── Main ──────────────────────────────────────────────────────────────────────

def filter_translators(all_translators, filter_str):
    """Keep translators whose name contains any of the comma-separated substrings (case-insensitive)."""
    needles = [s.strip().lower() for s in filter_str.split(",") if s.strip()]
    return [t for t in all_translators if any(n in t.lower() for n in needles)]


def main():
    parser = argparse.ArgumentParser(
        description="Добавить запись с rezka.ag в rezka_database.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="Ссылка на страницу rezka.ag")
    parser.add_argument(
        "--translators", "-t",
        metavar="ИМЕНА",
        help='Только эти переводы, через запятую (подстрока). Пример: "Сыендук,AniDUB"',
    )
    parser.add_argument("--all", "-a", action="store_true", help="Взять все переводы без вопросов")
    parser.add_argument("--yes", "-y", action="store_true", help="Не спрашивать подтверждения")
    parser.add_argument("--dry-run", action="store_true", help="Показать результат, не писать в файл")
    args = parser.parse_args()

    url = normalize_url(args.url)
    print(f"Загружаю: {url}")

    try:
        html = fetch(url)
    except Exception as e:
        print(f"Ошибка загрузки: {e}", file=sys.stderr)
        sys.exit(1)

    entry_type = detect_type(url, html)
    title = parse_title(html)
    all_translators = parse_translators(html)
    seasons = parse_seasons_episodes(html, entry_type)

    if not title:
        print("Не удалось определить название. Проверь URL.", file=sys.stderr)
        sys.exit(1)

    print(f"\nНазвание : {title}")
    print(f"Тип      : {entry_type}")
    print(f"Переводы : {all_translators}")
    if seasons:
        print(f"Сезоны   : {seasons}")

    # Translator selection
    if args.translators:
        chosen = filter_translators(all_translators, args.translators)
        if not chosen:
            print(f"\nНичего не совпало с фильтром '{args.translators}'.", file=sys.stderr)
            print(f"Доступные: {all_translators}", file=sys.stderr)
            sys.exit(1)
        print(f"\nВыбраны переводы: {chosen}")
    elif args.all or not sys.stdin.isatty():
        chosen = all_translators
    else:
        print("\nКакие переводы добавить?")
        for i, t in enumerate(all_translators, 1):
            print(f"  {i}. {t}")
        print("  Введи номера через запятую, или Enter = все:")
        raw = input("> ").strip()
        if not raw:
            chosen = all_translators
        else:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
            chosen = [all_translators[i] for i in indices if 0 <= i < len(all_translators)]
            if not chosen:
                print("Не выбрано ни одного перевода.", file=sys.stderr)
                sys.exit(1)
        print(f"Выбраны: {chosen}")

    # Build entry
    entry = {
        "title": title,
        "type": entry_type,
        "url": url,
        "translators": chosen,
    }
    if seasons:
        entry["seasons"] = seasons

    print(f"\nЗапись для добавления:")
    print(json.dumps(entry, ensure_ascii=False, indent=2))

    # Check duplicates
    db = load_db()
    existing = [e for e in db if isinstance(e, dict) and e.get("url") == url]
    if existing:
        print(f"\nВНИМАНИЕ: URL уже есть в базе: «{existing[0].get('title')}»")
        if not (args.yes or args.dry_run):
            ans = input("Заменить? [y/N]: ").strip().lower()
            if ans != "y":
                print("Отменено.")
                sys.exit(0)
        db = [e for e in db if not (isinstance(e, dict) and e.get("url") == url)]

    if args.dry_run:
        print("\n[dry-run] Файл не изменён.")
        return

    if not args.yes and not existing and sys.stdin.isatty():
        ans = input("\nДобавить в базу? [Y/n]: ").strip().lower()
        if ans == "n":
            print("Отменено.")
            sys.exit(0)

    db.append(entry)
    save_db(db)
    print(f"\nГотово. Всего записей в базе: {len(db)}")


if __name__ == "__main__":
    main()
