#!/usr/bin/env python3
"""Build sense-grounded translation clusters for the de/en/sv P5137 pilot.

A *cluster* is anchored on a single pivot sense ``(lexentry, sense_num)`` taken
from one pilot language, plus that sense's direct translations into the other
pilot languages. Grouping on ``(lexentry, sense_num)`` — a structural key, NOT
the gloss text — is what prevents polysemy stitching: two edges that merely
share a lemma string (e.g. de ``Bank`` -> en ``bank`` financial vs. sediment)
are kept apart because they come from different pivot senses.

License note: we read only ``lexentry``, ``sense_num`` and the lemma strings
from ``infer.sqlite3``. The CC-BY-SA gloss text (``all_trans.sense``) is never
copied or persisted — only the (lexentry, sense_num) sense *identity* is used.

Output: ``data/clusters.sqlite3``. Run from this dir:
    .venv/bin/python build_clusters.py
"""

import os
import sqlite3

import config

PILOT = ("de", "en", "sv")


def extract_edges(conn):
    """Pull the de/en/sv pilot subset of all_trans into a small indexed table."""
    conn.execute("ATTACH DATABASE ? AS infer", [config.INFER])
    conn.executescript(
        """
        DROP TABLE IF EXISTS edge;
        CREATE TABLE edge(
            from_lang TEXT, to_lang TEXT,
            lexentry TEXT, sense_num TEXT,
            from_lemma TEXT, to_lemma TEXT
        );
        """
    )
    placeholders = ",".join("?" for _ in PILOT)
    conn.execute(
        f"""
        INSERT INTO edge
        SELECT from_lang, to_lang, lexentry, sense_num, from_vocable, to_vocable
        FROM infer.all_trans
        WHERE from_lang IN ({placeholders})
          AND to_lang   IN ({placeholders})
          AND from_lang <> to_lang
          AND sense_num IS NOT NULL
        """,
        [*PILOT, *PILOT],
    )
    conn.execute("DETACH DATABASE infer")
    conn.executescript(
        """
        CREATE INDEX edge_pivot ON edge(lexentry, sense_num);
        CREATE INDEX edge_to ON edge(to_lang, to_lemma);
        CREATE INDEX edge_from ON edge(from_lang, from_lemma);
        """
    )
    return conn.execute("SELECT count(*) FROM edge").fetchone()[0]


def load_nouns(conn):
    """Build noun(lang, lemma): common nouns only (properNoun excluded)."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS noun;
        CREATE TABLE noun(lang TEXT, lemma TEXT, PRIMARY KEY (lang, lemma)) WITHOUT ROWID;
        """
    )
    for lang in PILOT:
        conn.execute("ATTACH DATABASE ? AS p", [config.PROCESSED % lang])
        conn.execute(
            """
            INSERT OR IGNORE INTO noun
            SELECT ?, written_rep FROM p.entry WHERE part_of_speech = 'noun'
            """,
            [lang],
        )
        conn.execute("DETACH DATABASE p")
    return conn.execute("SELECT lang, count(*) FROM noun GROUP BY lang").fetchall()


def build_clusters(conn):
    """One cluster per pivot sense; members = pivot lemma + its translations."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS cluster_member;
        CREATE TABLE cluster_member(
            lexentry TEXT, sense_num TEXT,
            lang TEXT, lemma TEXT, role TEXT  -- 'pivot' | 'trans'
        );

        -- the pivot member (the source word/sense itself)
        INSERT INTO cluster_member
        SELECT DISTINCT lexentry, sense_num, from_lang, from_lemma, 'pivot' FROM edge;

        -- the translated members
        INSERT INTO cluster_member
        SELECT DISTINCT lexentry, sense_num, to_lang, to_lemma, 'trans' FROM edge;

        CREATE INDEX cm_pivot ON cluster_member(lexentry, sense_num);

        -- per-pivot summary: which pilot languages the sense reaches
        DROP TABLE IF EXISTS cluster;
        CREATE TABLE cluster AS
        SELECT lexentry, sense_num,
               max(CASE WHEN role = 'pivot' THEN lang END) AS pivot_lang,
               max(CASE WHEN role = 'pivot' THEN lemma END) AS pivot_lemma,
               count(DISTINCT lang) AS n_langs,
               count(*) AS n_members
        FROM cluster_member
        GROUP BY lexentry, sense_num;
        CREATE INDEX cluster_pivot ON cluster(lexentry, sense_num);
        """
    )


def measure_stitching(conn):
    """Quantify polysemy stitching avoided, on the de->{en,sv} pivot direction.

    naive  = distinct (de,en,sv) lemma triples where *some* de->en edge and
             *some* de->sv edge exist for the German lemma (sense ignored).
    shared = distinct (de,en,sv) lemma triples where the de->en and de->sv
             edges come from the *same* German pivot sense.
    The difference is the spurious "stitched" triples the gate removes.
    """
    naive = conn.execute(
        """
        WITH de_en AS (SELECT DISTINCT from_lemma de, to_lemma en
                       FROM edge WHERE from_lang='de' AND to_lang='en'),
             de_sv AS (SELECT DISTINCT from_lemma de, to_lemma sv
                       FROM edge WHERE from_lang='de' AND to_lang='sv')
        SELECT count(*) FROM (
            SELECT DISTINCT de_en.de, en, sv
            FROM de_en JOIN de_sv USING (de))
        """
    ).fetchone()[0]
    shared = conn.execute(
        """
        WITH de_en AS (SELECT DISTINCT lexentry, sense_num, from_lemma de, to_lemma en
                       FROM edge WHERE from_lang='de' AND to_lang='en'),
             de_sv AS (SELECT DISTINCT lexentry, sense_num, from_lemma de, to_lemma sv
                       FROM edge WHERE from_lang='de' AND to_lang='sv')
        SELECT count(*) FROM (
            SELECT DISTINCT de_en.de, en, sv
            FROM de_en JOIN de_sv USING (lexentry, sense_num, de))
        """
    ).fetchone()[0]
    return naive, shared


def main():
    os.makedirs(config.DATA, exist_ok=True)
    if os.path.exists(config.CLUSTERS):
        os.remove(config.CLUSTERS)
    conn = sqlite3.connect(config.CLUSTERS)
    conn.isolation_level = None

    n_edges = extract_edges(conn)
    print(f"edges (de/en/sv pilot subset): {n_edges:,}")

    for lang, n in load_nouns(conn):
        print(f"  nouns[{lang}]: {n:,}")

    build_clusters(conn)
    total, three, noun3 = conn.execute(
        """
        SELECT
          count(*),
          sum(n_langs = 3),
          sum(n_langs = 3 AND EXISTS (
              SELECT 1 FROM noun WHERE noun.lang = cluster.pivot_lang
                                   AND noun.lemma = cluster.pivot_lemma))
        FROM cluster
        """
    ).fetchone()
    print(f"clusters (pivot senses): {total:,}")
    print(f"  spanning all 3 langs: {three:,}")
    print(f"  ...with a noun pivot: {noun3:,}")

    naive, shared = measure_stitching(conn)
    stitched = naive - shared
    pct = 100.0 * stitched / naive if naive else 0.0
    print(f"de->(en,sv) lemma triples — naive: {naive:,}  shared-sense: {shared:,}")
    print(f"  spurious stitched triples removed by the gate: {stitched:,} ({pct:.1f}%)")

    conn.close()


if __name__ == "__main__":
    main()
