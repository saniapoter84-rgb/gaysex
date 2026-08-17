#!/usr/bin/env python3
"""
add_to_db.py — добавляет запись в rezka_database.json

Поддерживаемые сервисы (определяются автоматически по URL):
  • rezka.ag     — парсит переводы и сезоны с сайта
  • kinogo.online — переводы и сезоны динамические (из cinemar.cc при воспроизведении)

Использование:
  python3 add_to_db.py <URL> [опции]

Опции:
  --translators "Имя1,Имя2"   (только для rezka) оставить только эти переводы
  --all                        (только для rezka) взять все переводы без вопросов
  --title "Название"           переопределить название (если парсер ошибся)
  --yes                        не спрашивать подтверждения перед записью
  --dry-run                    показать что будет добавлено, но не писать в файл

Примеры:
  python3 add_to_db.py https://rezka.ag/cartoons/comedy/2136-rik-i-morti-2013.html --translators "Сыендук"
  python3 add_to_db.py https://kinogo.online/serialy/12345-название-сериала.html
  python3 add_to_db.py https://kinogo.online/filmy/67890-film.html --yes
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

_UA_REZKA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_UA_KINOGO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_REZKA_HEADERS = {
    "User-Agent": _UA_REZKA,
    "Referer": "https://rezka.ag/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

_KINOGO_HEADERS = {
    "User-Agent": _UA_KINOGO,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
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
    req = Request(submit_url, headers=_REZKA_HEADERS)
    return _read_response(_opener.open(req, timeout=20))


def fetch_rezka(url):
    req = Request(url, headers=_REZKA_HEADERS)
    html = _read_response(_opener.open(req, timeout=15))
    if "anubis_challenge" in html:
        real = _solve_anubis(html, url)
        if real:
            html = real
    return html


def fetch_kinogo(url):
    req = Request(url, headers=_KINOGO_HEADERS)
    return _read_response(_opener.open(req, timeout=20))


# ── Service detection ─────────────────────────────────────────────────────────

# Известные домены каждого сервиса
_REZKA_DOMAINS = {"rezka.ag", "hdrezka.ag", "hdrezka.me", "rezka.me"}
_KINOGO_DOMAINS = {
    "kinogo.online", "kinogo.biz", "kinogo.fun", "kinogo.cc",
    "kinogo.me", "kinogo.club", "kinogo.tv",
}


def detect_service(url):
    """Вернуть 'rezka' или 'kinogo' по домену URL. Поднять ValueError если неизвестный домен."""
    m = re.search(r'https?://(?:www\.)?([^/]+)', url)
    if not m:
        raise ValueError(f"Не удалось разобрать URL: {url}")
    domain = m.group(1).lower()
    if domain in _REZKA_DOMAINS:
        return "rezka"
    if domain in _KINOGO_DOMAINS:
        return "kinogo"
    # Попытка по ключевым словам в домене
    if "rezka" in domain:
        return "rezka"
    if "kinogo" in domain:
        return "kinogo"
    raise ValueError(
        f"Неизвестный домен: {domain}\n"
        f"Поддерживаемые: rezka.ag, kinogo.online (и зеркала)"
    )


# ── URL normalization ─────────────────────────────────────────────────────────

def normalize_url(url, service):
    url = url.strip()
    if service == "rezka":
        url = re.sub(r'(https://(?:hd)?rezka\.[a-z]+)/ua/', r'\1/', url)
    # Убираем лишние query-параметры и якоря
    url = re.sub(r'[?#].*$', '', url)
    return url


# ── Rezka parsers ─────────────────────────────────────────────────────────────

def detect_type_rezka(url, html):
    """Вернуть: фильмы / сериалы / мультфильмы / аниме"""
    path = re.sub(r'https?://[^/]+/', '', url).split("/")[0]
    mapping = {
        "films": "фильмы",
        "series": "сериалы",
        "cartoons": "мультфильмы",
        "animation": "аниме",
    }
    t = mapping.get(path)
    if t:
        return t
    if re.search(r'initCDNMoviesEvents', html):
        return "фильмы"
    if re.search(r'initCDNSeriesEvents', html):
        return "сериалы"
    return "фильмы"


def parse_title_rezka(html):
    for pat in (
        r'<h1[^>]*itemprop="name"[^>]*>\s*<span[^>]*>([^<]+)</span>',
        r'<h1[^>]*>\s*<span[^>]*>([^<]+)</span>',
        r'<h1[^>]*>([^<]+)</h1>',
        r'<title>([^<|]+)',
    ):
        m = re.search(pat, html)
        if m:
            title = m.group(1).strip()
            title = re.sub(r'\s*[-–|].*?(смотреть|онлайн|бесплатно).*$', '', title, flags=re.IGNORECASE).strip()
            return title
    return ""


def parse_translators(html):
    result = {}

    def add(name, tid):
        name = name.strip().replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if name and name not in result:
            result[name] = tid

    for m in re.finditer(r'<li[^>]+title="([^"]+)"[^>]+data-translator_id="(\d+)"', html):
        add(m.group(1), m.group(2))
    for m in re.finditer(r'<li[^>]+data-translator_id="(\d+)"[^>]+title="([^"]+)"', html):
        add(m.group(2), m.group(1))
    for m in re.finditer(r'<a[^>]+title="([^"]+)"[^>]+data-translator_id="(\d+)"', html):
        add(m.group(1), m.group(2))
    for m in re.finditer(r'<a[^>]+data-translator_id="(\d+)"[^>]+title="([^"]+)"', html):
        add(m.group(2), m.group(1))
    if not result:
        for m in re.finditer(r'id="translator-\d+-(\d+)"[^>]*>\s*<b>([^<]+)</b>', html):
            add(m.group(2), m.group(1))

    return list(result.keys())


def parse_seasons_episodes(html, entry_type):
    if entry_type == "фильмы" or re.search(r'initCDNMoviesEvents', html):
        return {}

    ep_per_season = {}

    for m in re.finditer(r'data-season_id=["\'](\d+)["\'][^>]*data-episode_id=["\'](\d+)["\']', html):
        sn, ep = m.group(1), int(m.group(2))
        ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)
    for m in re.finditer(r'data-episode_id=["\'](\d+)["\'][^>]*data-season_id=["\'](\d+)["\']', html):
        ep, sn = int(m.group(1)), m.group(2)
        ep_per_season[sn] = max(ep_per_season.get(sn, 0), ep)

    if ep_per_season:
        return {k: v for k, v in sorted(ep_per_season.items(), key=lambda x: int(x[0]))}

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


# ── KinoGo parsers ────────────────────────────────────────────────────────────

# Маппинг сегментов URL-пути на тип контента
_KINOGO_PATH_MAP = {
    # Фильмы
    "filmy": "фильмы", "films": "фильмы", "film": "фильмы",
    "kino": "фильмы", "movie": "фильмы", "movies": "фильмы",
    # Сериалы
    "serialy": "сериалы", "series": "сериалы", "serial": "сериалы",
    "serials": "сериалы", "tvseries": "сериалы", "tv-series": "сериалы",
    # Аниме
    "anime": "аниме", "animes": "аниме", "anime-serialy": "аниме",
    "anime-filmy": "аниме", "animefilm": "аниме", "animeserial": "аниме",
    "аниме": "аниме",
    # Мультфильмы
    "multfilmy": "мультфильмы", "multserialy": "мультфильмы",
    "multiki": "мультфильмы", "multfilm": "мультфильмы",
    "cartoons": "мультфильмы", "cartoon": "мультфильмы",
    "animation": "мультфильмы", "animated": "мультфильмы",
    "multserial": "мультфильмы",
}


def detect_type_kinogo(url, html):
    """Определить тип контента по URL-пути и HTML kinogo."""
    path_seg = re.sub(r'https?://[^/]+/', '', url).split("/")[0].lower()
    t = _KINOGO_PATH_MAP.get(path_seg)
    if t:
        return t

    # Фолбек: ищем в хлебных крошках / категориях внутри HTML
    breadcrumb = re.search(
        r'(?:breadcrumb|category)[^>]*>.*?</(?:ul|nav|div)',
        html, re.IGNORECASE | re.DOTALL,
    )
    if breadcrumb:
        bc = breadcrumb.group(0).lower()
        if "аниме" in bc or "anime" in bc:
            return "аниме"
        if "мультф" in bc or "cartoon" in bc or "animation" in bc:
            return "мультфильмы"
        if "сериал" in bc or "series" in bc:
            return "сериалы"
        if "фильм" in bc or "film" in bc or "movie" in bc:
            return "фильмы"

    # Фолбек: og:type
    m = re.search(r'property=["\']og:type["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        og = m.group(1).lower()
        if "video.tv" in og or "series" in og:
            return "сериалы"

    # Фолбек: слово в URL слаге
    slug = url.lower()
    if "anime" in slug or "аниме" in slug:
        return "аниме"
    if "mult" in slug or "cartoon" in slug:
        return "мультфильмы"
    if "serial" in slug or "series" in slug:
        return "сериалы"

    return "фильмы"


def parse_title_kinogo(html):
    """Распарсить название с kinogo (DLE CMS)."""
    for pat in (
        # DLE: <h1 class="page-title">Название</h1>
        r'<h1[^>]*class=["\'][^"\']*page-title[^"\']*["\'][^>]*>([^<]+)</h1>',
        r'<h1[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>([^<]+)</h1>',
        r'<h1[^>]*itemprop=["\']name["\'][^>]*>([^<]+)</h1>',
        r'<h1[^>]*>([^<]{3,100})</h1>',
        # og:title
        r'property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<title>([^<|–\-]+)',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            # Убираем суффиксы вида " - KinoGo", " | смотреть онлайн" и т.п.
            title = re.sub(
                r'\s*[-–|]\s*(kinogo|смотреть|онлайн|бесплатно|скачать|hd).*$',
                '', title, flags=re.IGNORECASE,
            ).strip()
            if len(title) >= 2:
                return title
    return ""


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
    needles = [s.strip().lower() for s in filter_str.split(",") if s.strip()]
    return [t for t in all_translators if any(n in t.lower() for n in needles)]


def main():
    parser = argparse.ArgumentParser(
        description="Добавить запись в rezka_database.json (rezka.ag или kinogo.online)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="Ссылка на страницу (rezka.ag или kinogo.online)")
    parser.add_argument(
        "--translators", "-t",
        metavar="ИМЕНА",
        help='(rezka) Только эти переводы, через запятую. Пример: "Сыендук,AniDUB"',
    )
    parser.add_argument("--all", "-a", action="store_true", help="(rezka) Взять все переводы без вопросов")
    parser.add_argument("--title", metavar="ТЕКСТ", help="Переопределить название (если парсер ошибся)")
    parser.add_argument("--yes", "-y", action="store_true", help="Не спрашивать подтверждения")
    parser.add_argument("--dry-run", action="store_true", help="Показать результат, не писать в файл")
    args = parser.parse_args()

    # ── Определяем сервис ──────────────────────────────────────────────────────
    try:
        service = detect_service(args.url)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    url = normalize_url(args.url, service)
    print(f"Сервис   : {service}")
    print(f"Загружаю : {url}")

    # ── Загружаем страницу ─────────────────────────────────────────────────────
    try:
        if service == "rezka":
            html = fetch_rezka(url)
        else:
            html = fetch_kinogo(url)
    except Exception as e:
        print(f"Ошибка загрузки: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Парсим метаданные ──────────────────────────────────────────────────────
    if service == "rezka":
        entry_type = detect_type_rezka(url, html)
        title = args.title or parse_title_rezka(html)
        all_translators = parse_translators(html)
        seasons = parse_seasons_episodes(html, entry_type)
    else:
        entry_type = detect_type_kinogo(url, html)
        title = args.title or parse_title_kinogo(html)
        all_translators = []
        seasons = None  # динамически из cinemar.cc

    if not title:
        print("Не удалось определить название.", file=sys.stderr)
        if sys.stdin.isatty():
            title = input("Введи название вручную: ").strip()
        if not title:
            sys.exit(1)

    print(f"\nНазвание : {title}")
    print(f"Тип      : {entry_type}")

    if service == "rezka":
        print(f"Переводы : {all_translators}")
        if seasons:
            print(f"Сезоны   : {seasons}")
    else:
        print("Переводы : (динамические — загружаются при воспроизведении)")

    # ── Выбор переводов (только rezka) ────────────────────────────────────────
    chosen = []
    if service == "rezka":
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

    # ── Формируем запись ───────────────────────────────────────────────────────
    if service == "rezka":
        entry = {
            "title": title,
            "type": entry_type,
            "url": url,
            "translators": chosen,
        }
        if seasons:
            entry["seasons"] = seasons
    else:
        entry = {
            "title": title,
            "type": entry_type,
            "url": url,
            "source": "kinogo",
        }

    print(f"\nЗапись для добавления:")
    print(json.dumps(entry, ensure_ascii=False, indent=2))

    # ── Дубликаты ──────────────────────────────────────────────────────────────
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
