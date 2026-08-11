import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

addon = xbmcaddon.Addon()
ADDON_PATH = addon.getAddonInfo("path")
JSON_PATH = os.path.join(ADDON_PATH, "rezka_database.json")
CACHE_PATH = os.path.join(ADDON_PATH, "rezka_url_cache.json")

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


# ── KinoGo / cinemar.cc helpers ──────────────────────────────────────────────

KINOGO_COOKIE_PATH = os.path.join(ADDON_PATH, "kinogo_cookies.lwp")
KINOGO_CACHE_PATH = os.path.join(ADDON_PATH, "kinogo_cache.json")
KINOGO_CACHE_TTL = 1800  # 30 minutes

_kinogo_jar = http.cookiejar.LWPCookieJar(KINOGO_COOKIE_PATH)
try:
    _kinogo_jar.load(ignore_discard=True, ignore_expires=True)
except Exception:
    pass
_kinogo_opener = build_opener(HTTPCookieProcessor(_kinogo_jar))

_KINOGO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_kinogo(url):
    """Fetch a kinogo page using saved CF cookies when available."""
    req = Request(url, headers=_KINOGO_HEADERS)
    try:
        html = _read_response(_kinogo_opener.open(req, timeout=20))
        try:
            _kinogo_jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
        return html
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"Не удалось загрузить kinogo ({e})")


def _extract_cinemar_url(html):
    """Extract cinemar.cc embed URL from kinogo page HTML."""
    for pat in (
        r'<iframe[^>]+src=["\']([^"\']*cinemar\.cc/embed/[^"\']+)["\']',
        r'data-src=["\']([^"\']*cinemar\.cc/embed/[^"\']+)["\']',
        r'"src"\s*:\s*"([^"]*cinemar\.cc/embed/[^"]+)"',
        r"'src'\s*:\s*'([^']*cinemar\.cc/embed/[^']+)'",
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            u = m.group(1)
            return u if u.startswith("http") else "https:" + u
    return None


def _fetch_cinemar_embed(embed_url, page_referer=None):
    """
    Fetch cinemar.cc embed page.
    page_referer must be the kinogo page URL that contains the iframe —
    cinemar.cc checks it and returns an empty/blocked page without it.
    """
    m_domain = re.match(r'(https?://[^/]+)', page_referer or "")
    origin = m_domain.group(1) if m_domain else "https://kinogo.online"
    referer = page_referer or "https://kinogo.online/"
    req = Request(embed_url, headers={
        "User-Agent": _UA,
        "Referer": referer,
        "Origin": origin,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return _read_response(_opener.open(req, timeout=15))


def _extract_cinemar_encoded(html):
    """Extract encoded playlist string (#236z, #237T, or any #2NN<letter>) from cinemar embed HTML."""
    _ENC = r'#2\d{2}[a-zA-Z][^"\'<>\s\\]{10,}'
    for pat in (
        rf'file\s*:\s*["\']?({_ENC})',
        rf'(?:var|let|const)\s+\w+\s*=\s*["\']?({_ENC})',
        rf'data-(?:file|src|playlist)\s*=\s*["\']?({_ENC})',
        rf'({_ENC})',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    xbmc.log(f"RezkaLocal: encoded playlist не найден. Начало embed HTML: {html[:1200]}", xbmc.LOGWARNING)
    return None


def _decode_cinemar_236z(encoded):
    """Decode legacy #236z (Playerjs) playlist."""
    if encoded.startswith("#236z"):
        encoded = encoded[5:]
    parts = []
    for seg in encoded.split("$"):
        if not seg:
            continue
        idx = ord(seg[-1]) - 48
        body = seg[:-1]
        if 0 < idx <= len(body):
            body = body[idx:] + body[:idx]
        parts.append(body)
    joined = "".join(parts)
    padding = (4 - len(joined) % 4) % 4
    raw = base64.b64decode(joined + "=" * padding)
    latin = raw.decode("latin-1")
    pct = "".join(f"%{ord(c):02X}" if ord(c) > 127 else c for c in latin)
    return json.loads(unquote(pct))


def _decode_cinemar_237T(encoded):
    """
    Decode Cinemar player #237T playlist.
    Format: "#2" + 2-digit delimiter code + data split by that delimiter char.
    Each segment longer than 32 chars is unshuffled:
      result = seg[2t : len-t-1] + seg[0:t]   where t = int(last_char)
    Then base64-decode → latin-1 → percent-encode high bytes → unquote → JSON.
    """
    e = encoded[2:]          # strip "#2" → "37TA0M2V0..."
    try:
        dm = int(e[:2])      # "37" → 37
    except ValueError:
        dm = 36              # fallback
    delimiter = chr(dm)      # chr(37) = '%'
    body = e[2:]             # "TA0M2V0..."
    _ml = 32

    parts = []
    for seg in body.split(delimiter):
        if not seg:
            continue
        if len(seg) > _ml:
            try:
                t = int(seg[-1])
            except ValueError:
                parts.append(seg)
                continue
            # JS: seg.substr(2*t, seg.length-3*t-1) + seg.substr(0, t)
            # Python equiv (substr second arg is LENGTH):
            unshuffled = seg[2 * t: len(seg) - t - 1] + seg[:t]
            parts.append(unshuffled)
        else:
            parts.append(seg)

    joined = "".join(parts)
    padding = (4 - len(joined) % 4) % 4
    raw = base64.b64decode(joined + "=" * padding)
    latin = raw.decode("latin-1")
    pct = "".join(f"%{ord(c):02X}" if ord(c) > 127 else c for c in latin)
    return json.loads(unquote(pct))


def _decode_cinemar_playlist(encoded):
    """Dispatch to the right decoder based on the encoded string prefix."""
    if encoded.startswith("#237"):
        return _decode_cinemar_237T(encoded)
    return _decode_cinemar_236z(encoded)


def _node_file(node):
    """Return the raw file/dlink string from a playlist leaf node, or ''."""
    return node.get("file") or node.get("dlink") or ""


def _kinogo_get_translators(playlist):
    """Return unique translator names (leaf nodes with 'file' or 'dlink') from any playlist structure."""
    names = []
    seen = set()

    def walk(node):
        if _node_file(node) and "title" in node:
            n = node["title"]
            if n not in seen:
                seen.add(n)
                names.append(n)
        for child in node.get("folder", []):
            walk(child)

    for item in playlist:
        walk(item)
    return names


def _playlist_is_series(playlist):
    """True if the playlist has at least two levels of folders (season/episode/voice)."""
    if not playlist:
        return False
    sub = playlist[0].get("folder", [])
    return bool(sub) and "folder" in sub[0]


def _pick_hls(file_str):
    """Pick best HLS URL from a 'url1 or url2' file string."""
    parts = [p.strip() for p in file_str.split(" or ") if p.strip()]
    if not parts:
        return ""
    url = next((p for p in reversed(parts) if "m3u8" in p or "hls" in p), parts[-1])
    return ("https:" + url) if url.startswith("//") else url


def _load_kinogo_cache():
    try:
        with open(KINOGO_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_kinogo_cache(cache):
    try:
        with open(KINOGO_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        xbmc.log(f"RezkaLocal: kinogo cache save failed: {e}", xbmc.LOGWARNING)


def _get_kinogo_playlist(page_url):
    """
    Return decoded cinemar.cc playlist for a kinogo URL, cached for KINOGO_CACHE_TTL seconds.
    Raises RuntimeError on network / parse failure.
    """
    cache = _load_kinogo_cache()
    entry = cache.get(page_url, {})
    now = int(time.time())

    if entry and now - entry.get("fetched_at", 0) < KINOGO_CACHE_TTL:
        playlist = entry.get("playlist")
        if playlist:
            xbmc.log("RezkaLocal: kinogo cache hit", xbmc.LOGDEBUG)
            return playlist

    html = _fetch_kinogo(page_url)

    embed_url = _extract_cinemar_url(html)
    if not embed_url:
        raise RuntimeError(
            "cinemar.cc embed не найден на странице kinogo. "
            "Возможно, Cloudflare блокирует запрос — попробуй с домашнего IP."
        )

    embed_html = _fetch_cinemar_embed(embed_url, page_referer=page_url)
    encoded = _extract_cinemar_encoded(embed_html)
    if not encoded:
        raise RuntimeError("Зашифрованный плейлист не найден на странице cinemar.cc")

    xbmc.log(f"RezkaLocal: cinemar encoded prefix: {encoded[:10]}", xbmc.LOGDEBUG)
    playlist = _decode_cinemar_playlist(encoded)
    xbmc.log(f"RezkaLocal: cinemar playlist sample: {json.dumps(playlist[:1], ensure_ascii=False)[:400]}", xbmc.LOGDEBUG)

    cache[page_url] = {"playlist": playlist, "fetched_at": now}
    _save_kinogo_cache(cache)
    return playlist


def _show_kinogo_translators(item):
    title = item["title"]
    try:
        playlist = _get_kinogo_playlist(item["url"])
    except RuntimeError as e:
        _notify_error(str(e))
        xbmcplugin.endOfDirectory(HANDLE)
        return

    translators = _kinogo_get_translators(playlist)
    if not translators:
        _notify_error("Переводы не найдены в плейлисте cinemar.cc")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    is_series = "seasons" in item or _playlist_is_series(playlist)
    next_action = "list_seasons" if is_series else "list_qualities"

    for name in translators:
        li = xbmcgui.ListItem(label=f"Озвучка: {name}")
        xbmcplugin.addDirectoryItem(HANDLE, _url(action=next_action, title=title, translator=name), li, True)
    xbmcplugin.endOfDirectory(HANDLE)


def _show_kinogo_seasons(item, translator):
    title = item["title"]
    try:
        playlist = _get_kinogo_playlist(item["url"])
    except RuntimeError as e:
        _notify_error(str(e))
        xbmcplugin.endOfDirectory(HANDLE)
        return

    for s_idx, season_item in enumerate(playlist, 1):
        label = season_item.get("title", f"Сезон {s_idx}")
        li = xbmcgui.ListItem(label=label)
        li.setInfo("video", {"title": label, "season": s_idx})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            _url(action="list_episodes", title=title, translator=translator, season=str(s_idx)),
            li,
            True,
        )
    xbmcplugin.endOfDirectory(HANDLE)


def _show_kinogo_episodes(item, translator, season):
    title = item["title"]
    try:
        playlist = _get_kinogo_playlist(item["url"])
    except RuntimeError as e:
        _notify_error(str(e))
        xbmcplugin.endOfDirectory(HANDLE)
        return

    s_idx = int(season)
    if s_idx < 1 or s_idx > len(playlist):
        _notify_error(f"Сезон {season} не найден (доступно: {len(playlist)})")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    season_folder = playlist[s_idx - 1].get("folder", [])

    for ep_idx, ep_item in enumerate(season_folder, 1):
        ep_label = ep_item.get("title", f"Серия {ep_idx}")
        li = xbmcgui.ListItem(label=ep_label)
        li.setInfo("video", {"title": f"{title} {ep_label}", "episode": ep_idx, "season": s_idx})

        hls_url = None
        for voice in ep_item.get("folder", []):
            if voice.get("title") == translator:
                f = _node_file(voice)
                hls_url = _pick_hls(f) if f else None
                break

        if hls_url:
            li.setProperty("IsPlayable", "true")
            xbmcplugin.addDirectoryItem(HANDLE, _url(action="play", video_url=hls_url), li, False)
        else:
            li.setLabel(f"{ep_label} (нет: {translator})")
            xbmcplugin.addDirectoryItem(HANDLE, "#", li, False)

    xbmcplugin.endOfDirectory(HANDLE)


def _show_kinogo_movie_qualities(item, translator):
    title = item["title"]
    try:
        playlist = _get_kinogo_playlist(item["url"])
    except RuntimeError as e:
        _notify_error(str(e))
        xbmcplugin.endOfDirectory(HANDLE)
        return

    hls_url = None
    for node in playlist:
        f = _node_file(node)
        if node.get("title") == translator and f:
            hls_url = _pick_hls(f)
            break
        for sub in node.get("folder", []):
            sf = _node_file(sub)
            if sub.get("title") == translator and sf:
                hls_url = _pick_hls(sf)
                break
        if hls_url:
            break

    if hls_url:
        li = xbmcgui.ListItem(label=f"Смотреть [{translator}]")
        li.setInfo("video", {"title": f"{title} [{translator}]"})
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(HANDLE, _url(action="play", video_url=hls_url), li, False)
    else:
        _notify_error(f"URL не найден для перевода: {translator}")

    xbmcplugin.endOfDirectory(HANDLE)


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
        with _opener.open(req, timeout=5) as r:
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

    if item.get("source") == "kinogo":
        _show_kinogo_translators(item)
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
    New format: fetch from rezka CDN API or kinogo cinemar playlist.
    """
    item = _find_item(title)
    if not item:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    if item.get("source") == "kinogo":
        _show_kinogo_movie_qualities(item, translator)
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

    if item.get("source") == "kinogo":
        _show_kinogo_seasons(item, translator)
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

    if item.get("source") == "kinogo":
        _show_kinogo_episodes(item, translator, season)
        return

    hls_season = item.get("hls_episodes", {}).get(str(season), {})

    ep_count = int(item.get("seasons", {}).get(str(season), 0))
    for ep in range(1, ep_count + 1):
        label = f"Серия {ep}"
        li = xbmcgui.ListItem(label=label)
        li.setInfo("video", {"title": f"{title} С{season}Е{ep:02d}", "episode": ep, "season": int(season)})
        hls_url = hls_season.get(str(ep))
        if hls_url:
            li.setProperty("IsPlayable", "true")
            xbmcplugin.addDirectoryItem(HANDLE, _url(action="play", video_url=hls_url), li, False)
        else:
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
