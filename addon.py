import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

addon = xbmcaddon.Addon()
ADDON_PATH = addon.getAddonInfo("path")
JSON_PATH = os.path.join(ADDON_PATH, "rezka_database.json")

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


# ── Network / rezka helpers ───────────────────────────────────────────────────

_BASE_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://rezka.ag/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
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
    # opener follows the redirect and sets the cookie; response is the real page
    return _read_response(_opener.open(req, timeout=20))


def _fetch(url):
    req = Request(url, headers=_BASE_HEADERS)
    html = _read_response(_opener.open(req, timeout=15))
    if "anubis_challenge" in html:
        real = _solve_anubis(html, url)
        if real:
            html = real
    return html


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
    """Return {display_name: translator_id} from the page."""
    result = {}
    # Primary: <li title="Name" ... data-translator_id="ID"> (underscore — current rezka.ag)
    for m in re.finditer(r'<li[^>]+title="([^"]+)"[^>]+data-translator_id="(\d+)"', html):
        result[m.group(1).strip()] = m.group(2)
    for m in re.finditer(r'<li[^>]+data-translator_id="(\d+)"[^>]+title="([^"]+)"', html):
        name = m.group(2).strip()
        if name and name not in result:
            result[name] = m.group(1)
    # Fallback: data-translator-id (dash — older markup)
    for m in re.finditer(r'id="translator-\d+-(\d+)"[^>]*>\s*<b>([^<]+)</b>', html):
        name = m.group(2).strip()
        if name and name not in result:
            result[name] = m.group(1)
    for m in re.finditer(r'data-translator-id="(\d+)"[^>]*>\s*(?:<[a-z][^>]*>)?\s*([^<]{2,80})', html):
        name = m.group(2).strip()
        if name and name not in result:
            result[name] = m.group(1)
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


def _parse_cdn_url(raw):
    """
    Parse CDN API response `url` field into {quality: stream_url}.
    Handles plain and trash-encoded strings.
    Decoded format: [360p]url_mp4 or url_hls[/360p],[720p]...[/720p],...
    """
    if not isinstance(raw, str) or not raw:
        return {}
    decoded = raw if "[" in raw else _trash_decode(raw)

    result = {}
    for m in re.finditer(r"\[(\d+p)\](.*?)\[/\1\]", decoded, re.DOTALL):
        quality = m.group(1)
        parts = [p.strip() for p in m.group(2).split(" or ")]
        # Prefer HLS / m3u8 variant
        url = next((p for p in reversed(parts) if "m3u8" in p or "hls" in p), parts[-1])
        if url.startswith("//"):
            url = "https:" + url
        result[quality] = url

    # Fallback if no quality tags found
    if not result:
        url = decoded.strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http"):
            result["auto"] = url

    return result


def _call_cdn_api(content_id, translator_id, action, season=None, episode=None):
    """POST to /ajax/get_cdn_series/ and return {quality: url}."""
    data = {
        "id": content_id,
        "translator_id": translator_id,
        "is_cam": "0",
        "is_ads": "0",
        "action": action,
    }
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
            "Referer": "https://rezka.ag/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    raw_body = _read_response(_opener.open(req, timeout=15))
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
    Fetch fresh stream URLs from rezka for a new-format entry.
    Returns {quality: url} or raises RuntimeError / URLError.
    """
    page_url = item["url"]

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
    tid = id_map.get(translator_name)

    if not tid:
        low = translator_name.lower()
        for name, t in id_map.items():
            if low in name.lower() or name.lower() in low:
                tid = t
                break

    if not tid:
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

    action = "get_stream" if season is not None else "get_movie"
    return _call_cdn_api(content_id, tid, action, season, episode)


# ── Kodi helpers ──────────────────────────────────────────────────────────────

def _url(**kwargs):
    return f"{BASE_URL}?{urlencode(kwargs)}"


def _notify_error(msg):
    xbmc.log(f"RezkaLocal ERROR: {msg}", xbmc.LOGERROR)
    xbmcgui.Dialog().notification("RezkaLocal", msg, xbmcgui.NOTIFICATION_ERROR, 6000)


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


def show_items(category):
    for item in _load_db():
        if item.get("type") != category:
            continue
        title = item.get("title", "Без названия")
        is_series = "seasons" in item
        li = xbmcgui.ListItem(label=title)
        li.setInfo("video", {"title": title, "mediatype": "tvshow" if is_series else "movie"})
        xbmcplugin.addDirectoryItem(HANDLE, _url(action="list_translators", title=title), li, True)
    xbmcplugin.endOfDirectory(HANDLE)


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
    li = xbmcgui.ListItem(path=video_url)
    xbmcplugin.setResolvedUrl(HANDLE, True, listitem=li)


# ── Router ────────────────────────────────────────────────────────────────────

def router(paramstring):
    p = dict(parse_qsl(paramstring))
    action = p.get("action")

    if not action:
        show_categories()
    elif action == "list_items":
        show_items(p["category"])
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
