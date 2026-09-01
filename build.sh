#!/usr/bin/env bash
# Packages both add-ons and regenerates everything that has to stay in sync
# with them: the repository index, its checksum, and the browsable listing.
#
#   ./build.sh
#
# To ship an update, bump the version in the add-on's own addon.xml and run
# this — Kodi only notices a new release when the version in repo/addons.xml
# changes and a matching zip exists under its id.
set -euo pipefail

cd "$(dirname "$0")"

PLUGIN_ID="plugin.video.rezka_local"
REPO_ID="repository.rezka_local"

version_of() {
    python3 - "$1" <<'PY'
import sys, xml.etree.ElementTree as E
print(E.parse(sys.argv[1]).getroot().get("version"))
PY
}

PLUGIN_VER=$(version_of addon.xml)
REPO_VER=$(version_of "$REPO_ID/addon.xml")

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Kodi requires the add-on id as a top-level directory inside the archive,
# so stage the files under it rather than zipping them loose.
pack() {
    local id="$1" out="$2"; shift 2
    mkdir -p "$STAGE/$id"
    cp "$@" "$STAGE/$id/"
    (cd "$STAGE" && zip -rq "$out" "$id")
    if ! unzip -l "$STAGE/$out" | grep -q "$id/addon.xml"; then
        echo "!! $out is missing $id/addon.xml — aborting" >&2
        exit 1
    fi
}

# rezka_database.json is the scraper's plain-text, git-diffable output;
# the addon itself only reads the compiled SQLite .db at runtime, rebuilt
# fresh on every release so it never drifts from the JSON source.
echo "==> building rezka_database.db from rezka_database.json"
python3 json_to_sqlite.py rezka_database.json "$STAGE/rezka_database.db"

echo "==> $PLUGIN_ID $PLUGIN_VER"
pack "$PLUGIN_ID" "$PLUGIN_ID-$PLUGIN_VER.zip" addon.py addon.xml "$STAGE/rezka_database.db"
rm -f "$PLUGIN_ID"/*.zip
cp "$STAGE/$PLUGIN_ID-$PLUGIN_VER.zip" "$PLUGIN_ID/"

echo "==> $REPO_ID $REPO_VER"
pack "$REPO_ID" "$REPO_ID-$REPO_VER.zip" "$REPO_ID/addon.xml"
rm -f "$REPO_ID"/*.zip r/*.zip
cp "$STAGE/$REPO_ID-$REPO_VER.zip" "$REPO_ID/"
cp "$STAGE/$REPO_ID-$REPO_VER.zip" "r/"

# The index lists the repository alongside the plugin so that future
# repository fixes reach Kodi as an update instead of a reinstall.
python3 - addon.xml "$REPO_ID/addon.xml" > repo/addons.xml <<'PY'
import sys, xml.etree.ElementTree as E

out = ['<?xml version="1.0" encoding="UTF-8"?>', '<addons>']
for path in sys.argv[1:]:
    body = E.tostring(E.parse(path).getroot(), encoding="unicode").strip()
    out.append(body)
out.append('</addons>')
print("\n".join(out))
PY

md5sum repo/addons.xml | cut -d' ' -f1 | tr -d '\n' > repo/addons.xml.md5

# r/ is served by GitHub Pages as the source Kodi browses; it reads the file
# name from the link and the size and date from the columns after it.
SIZE=$(stat -c%s "r/$REPO_ID-$REPO_VER.zip")
cat > r/index.html <<EOF
<html>
<head><title>Index of /r/</title></head>
<body>
<h1>Index of /r/</h1><hr><pre><a href="../">../</a>
<a href="$REPO_ID-$REPO_VER.zip">$REPO_ID-$REPO_VER.zip</a>   $(date -u +'%d-%b-%Y %H:%M')   $(printf '%18s' "$SIZE")
</pre><hr></body>
</html>
EOF

echo "==> repo/addons.xml.md5 = $(cat repo/addons.xml.md5)"
python3 - <<'PY'
import xml.etree.ElementTree as E
for a in E.parse("repo/addons.xml").getroot():
    print(f"    indexed: {a.get('id')} {a.get('version')}")
PY
echo "==> done; commit and push"
