import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, HTTPSHandler

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

addon = xbmcaddon.Addon()
ADDON_PATH = addon.getAddonInfo("path")
JSON_PATH = os.path.join(ADDON_PATH, "rezka_database.json")
CACHE_PATH = os.path.join(ADDON_PATH, "rezka_url_cache.json")
PROXY_CACHE_PATH = os.path.join(ADDON_PATH, "rezka_proxy.txt")

_STATIC_PROXIES = [
    "http://47.237.92.86:3129",
    "http://47.238.134.126:82",
]
_PROXY_LIST_URL = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"

BASE_URL = sys.argv[0]
HANDLE = int(sys.argv[1])

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_QUALITY_ORDER = ["1080p", "720p", "480p", "360p", "auto"]

_cookie_jar = http.cookiejar.CookieJar()
_opener = build_opener(HTTPCookieProcessor(_cookie_jar))

# ── Proxy support ─────────────────────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _make_proxy_opener(proxy_url):
    return build_opener(
        HTTPCookieProcessor(_cookie_jar),
        ProxyHandler({"http": proxy_url, "https": proxy_url}),
        HTTPSHandler(context=_SSL_CTX),
    )


def _test_proxy(proxy_url):
    try:
        opener = _make_proxy_opener(proxy_url)
        req = Request("https://rezka.ag/", headers={"User-Agent": _UA, "Accept-Encoding": "identity"})
        with opener.open(req, timeout=6) as r:
            return r.getcode() == 200
    except Exception:
        return False


def _fetch_proxy_list():
    try:
        req = Request(_PROXY_LIST_URL, headers={"User-Agent": _UA})
        with _opener.open(req, timeout=10) as r:
            text = r.read().decode("utf-8", errors="replace")
        return ["http://" + line.strip() for line in text.splitlines() if line.strip() and ":" in line]
    except Exception:
        return []


def _find_working_proxy():
    for p in _STATIC_PROXIES:
        xbmc.log(f"RezkaLocal: тест прокси {p}", xbmc.LOGDEBUG)
        if _test_proxy(p):
            return p
    xbmc.log("RezkaLocal: статичные прокси мертвы, тяну список с GitHub", xbmc.LOGINFO)
    for p in _fetch_proxy_list()[:30]:
        if _test_proxy(p):
            return p
    return None


def _get_opener():
    """Return opener with working proxy (cached to disk). Falls back to direct."""
    # Read cached proxy
    cached = None
    try:
        if os.path.exists(PROXY_CACHE_PATH):
            cached = open(PROXY_CACHE_PATH).read().strip() or None
    except Exception:
        pass

    if cached and _test_proxy(cached):
        return _make_proxy_opener(cached)

    # Find new proxy
    proxy = _find_working_proxy()
    try:
        with open(PROXY_CACHE_PATH, "w") as f:
            f.write(proxy or "")
    except Exception:
        pass

    if proxy:
        xbmc.log(f"RezkaLocal: используется прокси {proxy}", xbmc.LOGINFO)
        return _make_proxy_opener(proxy)

    xbmc.log("RezkaLocal: прокси не найден, работаем напрямую", xbmc.LOGWARNING)
    return _opener


# ── Network / rezka helpers ───────────────────────────────────────────────────

_BASE_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://rezka.ag/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}


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
    """
    Solve Anubis PoW challenge and return the real page content.
    Algorithm 'fast': find nonce where sha256(randomData + str(nonce))
    has the first floor(difficulty/2) bytes == 0x00, and if difficulty is odd
    the high nibble of byte floor(difficulty/2) must also be 0.
    """
    m = re.search(r'id="anubis_challenge"[^>]*>(\{.*?\})\s*</script>', html, re.DOTALL)
    if not m:
        return None
    wrap = json.loads(m.group(1))
    ch = wrap["challenge"]

    m2 = re.search(r'id="anubis_base_prefix"[^>]*>"([^"]*)"', html)
    base_prefix = m2.group(1) if m2 else ""

    difficulty = ch["difficulty"]
    p = difficulty // 2
    u = difficulty % 2 != 0
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
    return _read_response(_get_opener().open(req, timeout=20))


def _fetch(url):
    for attempt in range(2):
        try:
            req = Request(url, headers=_BASE_HEADERS)
            html = _read_response(_get_opener().open(req, timeout=15))
            if "anubis_challenge" in html:
                real = _solve_anubis(html, url)
                if real:
                    html = real
            return html
        except Exception:
            if attempt == 0:
                # proxy might have died — clear cache and retry with fresh one
                try:
                    open(PROXY_CACHE_PATH, "w").close()
                except Exception:
                    pass
            else:
                raise


def _content_id_from_url(page_url):
    """Fast extraction of content ID from rezka.ag URL without fetching the page.
    URL pattern: /category/genre/12345-slug-year.html
    """
    m = re.search(r'/(\d+)-[^/]+\.html', page_url)
    return m.group(1) if m else None


def _parse_content_id(html):
    for pat in (
        # JS player init calls
        r"initCDNMoviesEvents\s*\(\s*(\d+)",
        r"initCDNSeriesEvents\s*\(\s*(\d+)",
        r'sof\.tv\.\w+Events\s*\(\s*(\d+)',
        # Canonical / og:url — most stable: ID is in the page URL itself
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'][^"\']*?/(\d+)-',
        r'property=["\']og:url["\'][^>]+content=["\'][^"\']*?/(\d+)-',
        r'content=["\'][^"\']*?/(\d+)-[^/]+\.html["\'][^>]*property=["\']og:url["\']',
        # HTML data attributes
        r"data-id=[\"'](\d+)[\"']",
        r'data-content-id=["\'](\d+)["\']',
        # JS / JSON properties
        r'"id"\s*:\s*(\d{4,})',
        r'"content_id"\s*:\s*(\d+)',
        r'id_content\s*=\s*(\d+)',
        r"banhammer_id\s*=\s*[\"']?(\d+)",
        # Numeric ID embedded anywhere in a script src / URL param
        r'player\.php[?][^"\']*id=(\d+)',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _parse_translator_ids(html):
    """Return {display_name: (translator_id, is_director, is_cam, is_ads)} from the page."""
    result = {}

    def add(name, tid, director="0", cam="0", ads="0"):
        name = name.strip().replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if name and name not in result:
            result[name] = (tid, director, cam, ads)

    def _flags(tag):
        d = re.search(r'data-director=["\'](\d+)["\']', tag)
        c = re.search(r'data-camrip=["\'](\d+)["\']', tag)
        a = re.search(r'data-ads=["\'](\d+)["\']', tag)
        return (d.group(1) if d else "0"), (c.group(1) if c else "0"), (a.group(1) if a else "0")

    # Series/anime use <li>, films use <a> — capture whole tag body then pick attrs
    for tag_re in (r'<li\b([^>]*)>', r'<a\b([^>]*)>'):
        for m in re.finditer(tag_re, html):
            tag = m.group(1)
            tid_m = re.search(r'data-translator_id=["\'](\d+)["\']', tag)
            if not tid_m:
                continue
            title_m = re.search(r'\btitle="([^"]+)"', tag)
            if not title_m:
                continue
            add(title_m.group(1), tid_m.group(1), *_flags(tag))

    # Fallback: data-translator-id (dash — older markup)
    if not result:
        for m in re.finditer(r'id="translator-\d+-(\d+)"[^>]*>\s*<b>([^<]+)</b>', html):
            add(m.group(2), m.group(1))
        for m in re.finditer(r'data-translator-id="(\d+)"[^>]*>\s*(?:<[a-z][^>]*>)?\s*([^<]{2,80})', html):
            add(m.group(2), m.group(1))
    return result


def _trash_decode(s):
    """Decode rezka 'trash' codec: reverse char substitutions, then base64."""
    s = s.replace("#", "=").replace("@", "/").replace("$", "+")
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except Exception:
        return s


def _pick_url(parts_str):
    """From 'url1 or url2' pick the HLS/m3u8 variant, else last."""
    parts = [p.strip() for p in parts_str.split(" or ") if p.strip()]
    url = next((p for p in reversed(parts) if "m3u8" in p or "hls" in p), parts[-1])
    return ("https:" + url) if url.startswith("//") else url


def _parse_cdn_url(raw):
    """
    Parse CDN API response `url` field into {quality: stream_url}.
    Handles trash-encoded strings and two plain-text formats:
      - with closing tags:    [720p]url or url2[/720p],[480p]...[/480p]
      - without closing tags: [720p]url or url2,[480p]...
    """
    if not isinstance(raw, str) or not raw:
        return {}

    decoded = raw if "[" in raw else _trash_decode(raw)

    result = {}

    # Format 1: [quality]...[/quality]
    for m in re.finditer(r"\[(\d+p)\](.*?)\[/\1\]", decoded, re.DOTALL):
        result[m.group(1)] = _pick_url(m.group(2))

    # Format 2: [quality]url or url2, (no closing tag, comma-separated)
    if not result:
        for m in re.finditer(r"\[(\d+p)\]([^\[]+)", decoded):
            body = m.group(2).rstrip(", ")
            result[m.group(1)] = _pick_url(body)

    # Fallback: single plain URL
    if not result:
        url = decoded.strip().rstrip(",")
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http"):
            result["auto"] = url

    return result


def _call_cdn_api(content_id, translator_id, action, season=None, episode=None, page_url=None,
                  is_director="0", is_cam="0", is_ads="0"):
    """POST to /ajax/get_cdn_series/ and return {quality: url}."""
    data = {
        "id": content_id,
        "translator_id": translator_id,
        "is_cam": is_cam,
        "is_ads": is_ads,
        "action": action,
    }
    if is_director != "0":
        data["is_director"] = is_director
    if season is not None:
        data["season"] = str(season)
    if episode is not None:
        data["episode"] = str(episode)

    req = Request(
        "https://rezka.ag/ajax/get_cdn_series/",
        data=urlencode(data).encode(),
        headers={
            "User-Agent": _UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": page_url or "https://rezka.ag/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    raw_body = _read_response(_get_opener().open(req, timeout=15))
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise RuntimeError("CDN API вернул не JSON (возможно, антибот-страница)")

    if not body.get("success"):
        raise RuntimeError(body.get("message", "CDN API вернул ошибку"))

    qualities = _parse_cdn_url(body.get("url"))
    if not qualities:
        raise RuntimeError(
            f"CDN API не вернул ссылки. url={body.get('url')!r}, "
            f"premium={body.get('premium_content')}"
        )
    return qualities


def _fetch_qualities(item, translator_name, season=None, episode=None):
    """
    Fetch stream URLs for a new-format entry.
    Checks disk cache first; only hits rezka if cache is missing or stale.
    Returns {quality: url} or raises RuntimeError / URLError.
    """
    page_url = item["url"]
    title = item.get("title", "")

    key = _cache_key(title, translator_name, season, episode)
    cache = _load_cache()
    if key in cache:
        cached = cache[key]
        probe = next(iter(cached.values()), None)
        if probe and _probe_url(probe):
            xbmc.log(f"RezkaLocal: кэш жив [{key}]", xbmc.LOGDEBUG)
            return cached
        xbmc.log(f"RezkaLocal: кэш устарел [{key}], обновляем", xbmc.LOGDEBUG)
        del cache[key]
        _save_cache(cache)

    # Extract content_id from the URL itself — no network needed
    content_id = _content_id_from_url(page_url)

    # Fetch the page (needed for translator IDs; also fallback for content_id)
    html = _fetch(page_url)

    if not content_id:
        content_id = _parse_content_id(html)
    if not content_id:
        xbmcgui.Dialog().ok(
            "RezkaLocal — ID не найден",
            f"URL: {page_url}\n\nHTML начало:\n{html[:400]}",
        )
        raise RuntimeError("Не удалось определить ID контента на странице")

    id_map = _parse_translator_ids(html)
    entry = id_map.get(translator_name)

    if not entry:
        low = translator_name.lower()
        for name, e in id_map.items():
            if low in name.lower() or name.lower() in low:
                entry = e
                break

    if not entry:
        if id_map:
            found = ", ".join(id_map.keys())
            xbmcgui.Dialog().ok(
                "RezkaLocal — озвучка не найдена",
                f"Искали: «{translator_name}»\n\nНайдено на странице:\n{found}",
            )
            raise RuntimeError(f"Озвучка «{translator_name}» не найдена. На странице: {found}")
        else:
            xbmcgui.Dialog().ok(
                "RezkaLocal — ошибка",
                f"Озвучки не найдены вообще.\n\nURL: {page_url}\n\nHTML начало:\n{html[:300]}",
            )
            raise RuntimeError(f"Озвучка «{translator_name}» не найдена на странице")

    if isinstance(entry, tuple):
        tid, is_director, is_cam, is_ads = entry
    else:
        tid, is_director, is_cam, is_ads = entry, "0", "0", "0"

    action = "get_stream" if season is not None else "get_movie"
    qualities = _call_cdn_api(content_id, tid, action, season, episode, page_url=page_url,
                              is_director=is_director, is_cam=is_cam, is_ads=is_ads)
    cache = _load_cache()
    cache[key] = qualities
    _save_cache(cache)
    return qualities


# ── Kodi helpers ──────────────────────────────────────────────────────────────

def _url(**kwargs):
    return f"{BASE_URL}?{urlencode(kwargs)}"


def _notify_error(msg):
    xbmc.log(f"RezkaLocal ERROR: {msg}", xbmc.LOGERROR)
    xbmcgui.Dialog().notification("RezkaLocal", msg, xbmcgui.NOTIFICATION_ERROR, 6000)


def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        xbmc.log(f"RezkaLocal: ошибка сохранения кэша: {e}", xbmc.LOGWARNING)


def _cache_key(title, translator, season, episode):
    return "|".join([title, translator, str(season) if season is not None else "", str(episode) if episode is not None else ""])


def _probe_url(url):
    """Check if a cached CDN URL is still alive via a 1-byte range request."""
    try:
        req = Request(url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
        with _get_opener().open(req, timeout=5) as r:
            return r.getcode() in (200, 206)
    except Exception:
        return False


def _load_db():
    if not os.path.exists(JSON_PATH):
        xbmc.log(f"RezkaLocal: база не найдена: {JSON_PATH}", xbmc.LOGERROR)
        return []
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        xbmc.log(f"RezkaLocal: ошибка загрузки JSON: {e}", xbmc.LOGERROR)
        return []


def _find_item(title):
    return next((i for i in _load_db() if i.get("title") == title), None)


def _render_qualities(title, qualities):
    """Render quality → play items. qualities = {quality_str: stream_url}."""
    ordered = sorted(
        qualities.items(),
        key=lambda kv: _QUALITY_ORDER.index(kv[0]) if kv[0] in _QUALITY_ORDER else 99,
    )
    for quality, stream_url in ordered:
        li = xbmcgui.ListItem(label=f"Качество: {quality}")
        li.setInfo("video", {"title": f"{title} [{quality}]"})
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(HANDLE, _url(action="play", video_url=stream_url), li, False)
    xbmcplugin.endOfDirectory(HANDLE)


# ── Navigation handlers ───────────────────────────────────────────────────────

def show_categories():
    for cat in ("фильмы", "сериалы", "мультфильмы", "аниме"):
        li = xbmcgui.ListItem(label=cat.capitalize())
        li.setInfo("video", {"title": cat.capitalize()})
        xbmcplugin.addDirectoryItem(HANDLE, _url(action="list_items", category=cat), li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_items(category, query=""):
    # Search entry at the top
    search_li = xbmcgui.ListItem(label="[Поиск...]")
    search_li.setInfo("video", {"title": "Поиск"})
    xbmcplugin.addDirectoryItem(HANDLE, _url(action="search", category=category), search_li, True)

    needle = query.lower().strip()
    for item in _load_db():
        if item.get("type") != category:
            continue
        title = item.get("title", "Без названия")
        if needle and needle not in title.lower():
            continue
        is_series = "seasons" in item
        li = xbmcgui.ListItem(label=title)
        li.setInfo("video", {"title": title, "mediatype": "tvshow" if is_series else "movie"})
        xbmcplugin.addDirectoryItem(HANDLE, _url(action="list_translators", title=title), li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_search(category):
    query = xbmcgui.Dialog().input("Поиск", type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    show_items(category, query=query)


def show_translators(title):
    item = _find_item(title)
    if not item:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    translators = item.get("translators", {})
    # New format: list of names. Old format: dict {name: {quality: url}}.
    names = translators if isinstance(translators, list) else list(translators.keys())
    is_series = "seasons" in item
    next_action = "list_seasons" if is_series else "list_qualities"

    for name in names:
        li = xbmcgui.ListItem(label=f"Озвучка: {name}")
        xbmcplugin.addDirectoryItem(HANDLE, _url(action=next_action, title=title, translator=name), li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_qualities(title, translator):
    """
    Movies/cartoons quality menu.
    Old format: static URLs from database.
    New format: fetch from rezka CDN API.
    """
    item = _find_item(title)
    if not item:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    translators = item.get("translators", {})

    if isinstance(translators, dict):
        # Old format — use stored static stream URLs
        qualities = translators.get(translator, {})
        _render_qualities(title, qualities)
    else:
        # New format — fetch live from rezka
        try:
            qualities = _fetch_qualities(item, translator)
        except (URLError, HTTPError) as e:
            _notify_error(f"Сетевая ошибка: {e}")
            xbmcplugin.endOfDirectory(HANDLE)
            return
        except RuntimeError as e:
            _notify_error(str(e))
            xbmcplugin.endOfDirectory(HANDLE)
            return
        _render_qualities(title, qualities)


def show_seasons(title, translator):
    item = _find_item(title)
    if not item:
        xbmcgui.Dialog().ok("RezkaLocal", f"Сериал не найден в базе:\n{title}")
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if "seasons" not in item:
        xbmcgui.Dialog().ok("RezkaLocal", f"У записи «{title}» нет поля seasons.\nПроверь формат JSON.")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    for s in sorted(item["seasons"].keys(), key=int):
        li = xbmcgui.ListItem(label=f"Сезон {s}")
        li.setInfo("video", {"title": f"Сезон {s}", "season": int(s)})
        xbmcplugin.addDirectoryItem(
            HANDLE, _url(action="list_episodes", title=title, translator=translator, season=s), li, True
        )
    xbmcplugin.endOfDirectory(HANDLE)


def show_episodes(title, translator, season):
    item = _find_item(title)
    if not item:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    ep_count = int(item.get("seasons", {}).get(str(season), 0))
    for ep in range(1, ep_count + 1):
        label = f"Серия {ep}"
        li = xbmcgui.ListItem(label=label)
        li.setInfo("video", {"title": f"{title} С{season}Е{ep:02d}", "episode": ep, "season": int(season)})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            _url(action="list_episode_qualities", title=title, translator=translator,
                 season=season, episode=ep),
            li,
            True,
        )
    xbmcplugin.endOfDirectory(HANDLE)


def show_episode_qualities(title, translator, season, episode):
    """Fetch stream URLs for one episode and show quality menu."""
    item = _find_item(title)
    if not item:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    try:
        qualities = _fetch_qualities(item, translator, season=int(season), episode=int(episode))
    except (URLError, HTTPError) as e:
        _notify_error(f"Сетевая ошибка: {e}")
        xbmcplugin.endOfDirectory(HANDLE)
        return
    except RuntimeError as e:
        _notify_error(str(e))
        xbmcplugin.endOfDirectory(HANDLE)
        return

    _render_qualities(f"{title} С{season}Е{int(episode):02d}", qualities)


def play_video(video_url):
    headers = urlencode({"User-Agent": _UA, "Referer": "https://rezka.ag/"})
    li = xbmcgui.ListItem(path=f"{video_url}|{headers}")
    xbmcplugin.setResolvedUrl(HANDLE, True, listitem=li)


# ── Router ────────────────────────────────────────────────────────────────────

def router(paramstring):
    p = dict(parse_qsl(paramstring))
    action = p.get("action")

    if not action:
        show_categories()
    elif action == "list_items":
        show_items(p["category"])
    elif action == "search":
        show_search(p["category"])
    elif action == "list_translators":
        show_translators(p["title"])
    elif action == "list_qualities":
        show_qualities(p["title"], p["translator"])
    elif action == "list_seasons":
        show_seasons(p["title"], p["translator"])
    elif action == "list_episodes":
        show_episodes(p["title"], p["translator"], p["season"])
    elif action == "list_episode_qualities":
        show_episode_qualities(p["title"], p["translator"], p["season"], p["episode"])
    elif action == "play":
        play_video(p["video_url"])


if __name__ == "__main__":
    router(sys.argv[2][1:])
