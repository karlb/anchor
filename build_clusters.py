#!/usr/bin/env python3
"""Build sense-grounded translation clusters for P5137 propagation.

A *cluster* is anchored on a single pivot sense ``(lexentry, sense_num)`` taken
from one WikDict language, plus that sense's direct translations into every other
WikDict language. All ~26 languages in ``infer.all_trans`` are used as input —
richer, sense-grounded evidence than a pilot subset: more support per candidate,
and non-pilot pivots (e.g. es ``banco``) bridge pilot members that no pilot pivot
connects. The pilot restriction is *not* applied here; it lives only at the review
stage (``config.REVIEW_LANGS``, enforced by load_lexemes.py + match.py), so
widening the input never floods the queue with unreviewed languages.

Grouping on ``(lexentry, sense_num)`` — a structural key, NOT the gloss text —
is what prevents polysemy stitching: two edges that merely share a lemma string
(e.g. de ``Bank`` -> en ``bank`` financial vs. sediment) are kept apart because
they come from different pivot senses.

License note: we read only ``lexentry``, ``sense_num`` and the lemma strings
from ``infer.sqlite3``. The CC-BY-SA gloss text (``all_trans.sense``) is never
copied or persisted — only the (lexentry, sense_num) sense *identity* is used.
``sense_num`` is a factual index (the entry's own sense ordinal), not creative
text; it is used solely to keep one sense's translations together and is never
written to Wikidata — the output P5137 link re-derives its concept from
Wikidata's own CC0 data, so no sense number or partition leaves DBnary.

Output: ``data/clusters.sqlite3``. Run from this dir:
    uv run build_clusters.py
"""

import os
import sqlite3

import config


def extract_edges(conn):
    """Pull every all-language translation edge into a small indexed table."""
    conn.execute("ATTACH DATABASE ? AS infer", [config.INFER])
    conn.executescript(
        """
        DROP TABLE IF EXISTS edge;
        CREATE TABLE edge(
            from_lang TEXT, to_lang TEXT,
            lexentry TEXT, sense_num TEXT,
            from_lemma TEXT, to_lemma TEXT
        );
        INSERT INTO edge
        SELECT from_lang, to_lang, lexentry, sense_num, from_vocable, to_vocable
        FROM infer.all_trans
        WHERE from_lang <> to_lang
          AND sense_num IS NOT NULL;
        """
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

        -- per-pivot summary: how many languages the sense reaches
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

    The de->en and de->sv slices are materialised into indexed temp tables first,
    then self-joined. Joining them as inline CTEs instead makes SQLite nest-loop
    two unindexed derived tables over the full all-language edge table, which runs
    for minutes; the indexed temp tables keep it ~2s (the de/en/sv slice, hence
    this number, is unchanged by the all-language widening).
    """
    conn.executescript(
        """
        DROP TABLE IF EXISTS de_en;
        DROP TABLE IF EXISTS de_sv;
        CREATE TEMP TABLE de_en AS SELECT DISTINCT lexentry, sense_num, from_lemma de, to_lemma en
          FROM edge WHERE from_lang='de' AND to_lang='en';
        CREATE TEMP TABLE de_sv AS SELECT DISTINCT lexentry, sense_num, from_lemma de, to_lemma sv
          FROM edge WHERE from_lang='de' AND to_lang='sv';
        CREATE INDEX de_en_de  ON de_en(de);
        CREATE INDEX de_sv_de  ON de_sv(de);
        CREATE INDEX de_en_key ON de_en(lexentry, sense_num, de);
        CREATE INDEX de_sv_key ON de_sv(lexentry, sense_num, de);
        """
    )
    naive = conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT de_en.de, en, sv "
        "FROM de_en JOIN de_sv USING (de))"
    ).fetchone()[0]
    shared = conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT de_en.de, en, sv "
        "FROM de_en JOIN de_sv USING (lexentry, sense_num, de))"
    ).fetchone()[0]
    return naive, shared


def main():
    os.makedirs(config.DATA, exist_ok=True)
    if os.path.exists(config.CLUSTERS):
        os.remove(config.CLUSTERS)
    conn = sqlite3.connect(config.CLUSTERS)
    conn.isolation_level = None

    n_edges = extract_edges(conn)
    print(f"edges (all WikDict languages): {n_edges:,}")

    build_clusters(conn)
    # A cluster needs >=2 review languages among its members to yield a candidate
    # (an anchor member + a target member); match.py restricts targets to
    # config.REVIEW_LANGS, so that reach is what actually produces reviewable pairs.
    review = ",".join(f"'{c}'" for c in config.REVIEW_LANGS)
    total, review_reach = conn.execute(
        f"""
        SELECT count(*),
               sum((SELECT count(DISTINCT lang) FROM cluster_member cm
                    WHERE cm.lexentry = cluster.lexentry
                      AND cm.sense_num = cluster.sense_num
                      AND cm.lang IN ({review})) >= 2)
        FROM cluster
        """
    ).fetchone()
    print(f"clusters (pivot senses): {total:,}")
    print(f"  reaching >=2 review languages ({'/'.join(config.REVIEW_LANGS)}): {review_reach:,}")

    naive, shared = measure_stitching(conn)
    stitched = naive - shared
    pct = 100.0 * stitched / naive if naive else 0.0
    print(f"de->(en,sv) lemma triples — naive: {naive:,}  shared-sense: {shared:,}")
    print(f"  spurious stitched triples removed by the gate: {stitched:,} ({pct:.1f}%)")

    conn.close()


if __name__ == "__main__":
    main()
