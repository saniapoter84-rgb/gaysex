import base64
import gzip
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

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


# ── Network / rezka helpers ───────────────────────────────────────────────────

def _fetch(url):
    req = Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://rezka.ag/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    with urlopen(req, timeout=15) as r:
        raw = r.read()
        enc = r.headers.get("Content-Encoding", "")
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def _content_id_from_url(page_url):
    """Fast extraction of content ID from rezka.ag URL without fetching the page.
    URL pattern: /category/genre/12345-slug-year.html
    """
    m = re.search(r'/(\d+)-[^/]+\.html', page_url)
    return m.group(1) if m else None


def _parse_content_id(html):
    for pat in (
        r"initCDNMoviesEvents\s*\(\s*(\d+)",
        r"initCDNSeriesEvents\s*\(\s*(\d+)",
        r'sof\.tv\.\w+Events\s*\(\s*(\d+)',
        r'"id"\s*:\s*(\d{4,})',
        r"data-id=[\"'](\d+)[\"']",
        r'id_content\s*=\s*(\d+)',
        r"banhammer_id\s*=\s*[\"']?(\d+)",
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _parse_translator_ids(html):
    """Return {display_name: translator_id} from the page."""
    result = {}
    # Pattern 1: <li id="translator-CONTENTID-TRANSID"><b>Name</b>
    for m in re.finditer(r'id="translator-\d+-(\d+)"[^>]*>\s*<b>([^<]+)</b>', html):
        result[m.group(2).strip()] = m.group(1)
    # Pattern 2: data-translator-id="ID" class="b-translator__item">Name
    for m in re.finditer(r'data-translator-id="(\d+)"[^>]*>\s*([^<\n]{2,60})', html):
        name = m.group(2).strip().rstrip('<').strip()
        if name and name not in result:
            result[name] = m.group(1)
    # Pattern 3: <option value="ID">Name</option> in translator select
    for m in re.finditer(r'<option[^>]+value="(\d+)"[^>]*>([^<]{2,60})</option>', html):
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
        },
    )
    with urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode("utf-8"))

    if not body.get("success"):
        raise RuntimeError(body.get("message", "CDN API вернул ошибку"))

    return _parse_cdn_url(body["url"])


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
        raise RuntimeError("Не удалось определить ID контента на странице")

    id_map = _parse_translator_ids(html)
    tid = id_map.get(translator_name)

    if not tid:
        # Fuzzy match when names differ slightly (e.g. trailing spaces, ™)
        low = translator_name.lower()
        for name, t in id_map.items():
            if low in name.lower() or name.lower() in low:
                tid = t
                break

    if not tid:
        # Last resort: use the first available translator
        if id_map:
            tid = next(iter(id_map.values()))
        else:
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
    if not item or "seasons" not in item:
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
