#!/usr/bin/env bash
# Builds the Kodi repository zip and refreshes everything that must stay in
# sync with it: the browsable listing under r/ and the index checksum.
#
#   ./build.sh
#
# Version is read from repository.rezka_local/addon.xml, so bump it there.
set -euo pipefail

cd "$(dirname "$0")"

ADDON_ID="repository.rezka_local"
SRC="$ADDON_ID/addon.xml"

VERSION=$(python3 - "$SRC" <<'PY'
import sys, xml.etree.ElementTree as E
print(E.parse(sys.argv[1]).getroot().get("version"))
PY
)
ZIP="$ADDON_ID-$VERSION.zip"

echo "==> building $ZIP"

# Kodi requires the addon id as a top-level directory inside the archive,
# so stage it rather than zipping the file on its own.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$ADDON_ID"
cp "$SRC" "$STAGE/$ADDON_ID/addon.xml"
(cd "$STAGE" && zip -rq "$ZIP" "$ADDON_ID")

# Refuse to ship an archive Kodi would silently reject.
if ! unzip -l "$STAGE/$ZIP" | grep -q "$ADDON_ID/addon.xml"; then
    echo "!! $ZIP is missing $ADDON_ID/addon.xml — aborting" >&2
    exit 1
fi

rm -f "$ADDON_ID"/*.zip r/*.zip
cp "$STAGE/$ZIP" "$ADDON_ID/$ZIP"
cp "$STAGE/$ZIP" "r/$ZIP"

# r/ is served by GitHub Pages; Kodi parses this listing to find the zip,
# and reads the size and date columns from it.
SIZE=$(stat -c%s "r/$ZIP")
DATE=$(date -u +'%d-%b-%Y %H:%M')
cat > r/index.html <<EOF
<html>
<head><title>Index of /r/</title></head>
<body>
<h1>Index of /r/</h1><hr><pre><a href="../">../</a>
<a href="$ZIP">$ZIP</a>   $DATE   $(printf '%18s' "$SIZE")
</pre><hr></body>
</html>
EOF

# The repository serves this index; Kodi rejects it when the checksum drifts.
md5sum repo/addons.xml | cut -d' ' -f1 | tr -d '\n' > repo/addons.xml.md5

echo "==> $ZIP ($SIZE bytes)"
unzip -l "r/$ZIP" | sed 's/^/    /'
echo "==> repo/addons.xml.md5 = $(cat repo/addons.xml.md5)"
echo "==> done; commit and push"
