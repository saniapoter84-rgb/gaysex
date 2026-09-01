#!/usr/bin/env python3
"""Build rezka_database.db (SQLite) from rezka_database.json.

    python3 json_to_sqlite.py rezka_database.json rezka_database.db

rezka_database.json stays the scraper's plain-text, git-diffable output
format; the addon itself only reads the compiled .db at runtime, and
build.sh regenerates it into the packaged zip on every release.
"""
import json
import os
import sqlite3
import sys

SCHEMA = """
CREATE TABLE items (
    id      INTEGER PRIMARY KEY,
    title   TEXT NOT NULL,
    type    TEXT NOT NULL,
    url     TEXT NOT NULL,
    source  TEXT
);
CREATE INDEX idx_items_type ON items(type);
CREATE INDEX idx_items_title ON items(title);

CREATE TABLE translators (
    id       INTEGER PRIMARY KEY,
    item_id  INTEGER NOT NULL REFERENCES items(id),
    position INTEGER NOT NULL,
    name     TEXT NOT NULL
);
CREATE INDEX idx_translators_item ON translators(item_id);

-- Only populated for legacy entries scraped with static per-quality URLs
-- baked in (item["translators"] was a {name: {quality: url}} dict).
CREATE TABLE translator_urls (
    translator_id INTEGER NOT NULL REFERENCES translators(id),
    quality       TEXT NOT NULL,
    url           TEXT NOT NULL,
    PRIMARY KEY (translator_id, quality)
);

CREATE TABLE seasons (
    item_id       INTEGER NOT NULL REFERENCES items(id),
    season        INTEGER NOT NULL,
    episode_count INTEGER NOT NULL,
    PRIMARY KEY (item_id, season)
);

-- Cached resolved HLS links per episode, if the scraper pre-fetched them.
CREATE TABLE hls_episodes (
    item_id INTEGER NOT NULL REFERENCES items(id),
    season  INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    url     TEXT NOT NULL,
    PRIMARY KEY (item_id, season, episode)
);
"""


def build(json_path, db_path):
    with open(json_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    for item in items:
        cur = conn.execute(
            "INSERT INTO items (title, type, url, source) VALUES (?, ?, ?, ?)",
            (item.get("title", ""), item.get("type", ""), item.get("url", ""), item.get("source")),
        )
        item_id = cur.lastrowid

        translators = item.get("translators", {})
        # New format: plain list of names. Old format: {name: {quality: url}}.
        names = translators if isinstance(translators, list) else list(translators.keys())
        for pos, name in enumerate(names):
            t_cur = conn.execute(
                "INSERT INTO translators (item_id, position, name) VALUES (?, ?, ?)",
                (item_id, pos, name),
            )
            if isinstance(translators, dict):
                translator_id = t_cur.lastrowid
                for quality, url in translators.get(name, {}).items():
                    conn.execute(
                        "INSERT INTO translator_urls (translator_id, quality, url) VALUES (?, ?, ?)",
                        (translator_id, quality, url),
                    )

        for season, ep_count in item.get("seasons", {}).items():
            conn.execute(
                "INSERT INTO seasons (item_id, season, episode_count) VALUES (?, ?, ?)",
                (item_id, int(season), int(ep_count)),
            )

        for season, episodes in item.get("hls_episodes", {}).items():
            for ep, url in episodes.items():
                conn.execute(
                    "INSERT INTO hls_episodes (item_id, season, episode, url) VALUES (?, ?, ?, ?)",
                    (item_id, int(season), int(ep), url),
                )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "rezka_database.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "rezka_database.db"
    build(src, dst)
    print(f"==> built {dst} from {src}")
