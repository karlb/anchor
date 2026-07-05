#!/usr/bin/env python3
"""Match sense-grounded clusters to Wikidata lexemes and emit P5137 candidates.

Pipeline (all CC0 / structural — no gloss text):
  1. Match each cluster member (lemma, lang) to Wikidata noun lexemes (Q1084).
     Exact, case- & diacritic-sensitive; all representations of a lexeme count,
     so de-ch spellings match automatically. Homographs keep every L-id (flagged).
  2. Anchor: among the cluster's matched lexemes, an "anchor" is a member lexeme
     whose P5137 senses agree on a single Q-item (monosemous anchor). A cluster
     has a usable anchor only if all such anchors agree on ONE Q (single dominant
     anchor); otherwise abstain. Anchors may be in ANY loaded language — clusters
     are built from all WikDict languages, so a non-pilot member can supply the
     anchor concept.
  3. Targets: matched member lexemes not yet grounded to the anchor Q, restricted
     to config.REVIEW_LANGS — the languages we actually ground/review/write. This
     is the one place the pilot limit is applied to the output. A target that
     already carries a *different* P5137 is a cross-pivot conflict -> abstain
     (never override an existing P5137).

Outputs data/candidates.sqlite3 (candidate + cluster_abstain tables).
Run after build_clusters.py and load_lexemes.py:
    uv run match.py
"""

import argparse
import os
import sqlite3

import config

NOUN_QID = "Q1084"

BUILD = """
-- 1. member -> Wikidata noun lexeme matches (exact lemma, same language).
DROP TABLE IF EXISTS matched_member;
CREATE TABLE matched_member AS
SELECT cm.lexentry, cm.sense_num, cm.lang, cm.lemma, cm.role, lx.lid
FROM cl.cluster_member cm
JOIN wd.lexeme lx   ON lx.lang = cm.lang AND lx.lexcat = '%(noun)s'
JOIN wd.lemma  lm   ON lm.lid = lx.lid AND lm.lemma = cm.lemma;
CREATE INDEX mm_pivot ON matched_member(lexentry, sense_num);
CREATE INDEX mm_key   ON matched_member(lexentry, sense_num, lang, lemma);
CREATE INDEX mm_lid   ON matched_member(lid);

-- homograph flag: a member lemma matching >1 lexeme in its language
DROP TABLE IF EXISTS member_homograph;
CREATE TABLE member_homograph AS
SELECT lexentry, sense_num, lang, lemma, count(DISTINCT lid) AS n_lid
FROM matched_member GROUP BY lexentry, sense_num, lang, lemma;
CREATE INDEX mh_key ON member_homograph(lexentry, sense_num, lang, lemma);

-- 2. each matched lexeme's distinct P5137 targets within this cluster
DROP TABLE IF EXISTS member_qid;
CREATE TABLE member_qid AS
SELECT DISTINCT mm.lexentry, mm.sense_num, mm.lid, si.qid
FROM matched_member mm
JOIN wd.sense_item si ON si.lid = mm.lid;
CREATE INDEX mq_key ON member_qid(lid, lexentry, sense_num);

-- lexemes whose P5137 senses agree on exactly one Q = clean anchors
DROP TABLE IF EXISTS clean_anchor;
CREATE TABLE clean_anchor AS
SELECT lexentry, sense_num, lid, min(qid) AS qid
FROM member_qid
GROUP BY lexentry, sense_num, lid
HAVING count(DISTINCT qid) = 1;

-- a cluster has a single dominant anchor iff its clean anchors agree on one Q
DROP TABLE IF EXISTS cluster_anchor;
CREATE TABLE cluster_anchor AS
SELECT lexentry, sense_num, min(qid) AS anchor_qid
FROM clean_anchor
GROUP BY lexentry, sense_num
HAVING count(DISTINCT qid) = 1;
CREATE INDEX ca_pivot ON cluster_anchor(lexentry, sense_num);

-- 3a. per-cluster candidate rows: matched lexemes not yet grounded to the anchor Q.
-- Targets are restricted to REVIEW_LANGS (the review/write limit); anchor detection
-- above stays unrestricted, so an anchor may be in any loaded language.
DROP TABLE IF EXISTS candidate_raw;
CREATE TABLE candidate_raw AS
SELECT DISTINCT
  ca.lexentry, ca.sense_num, ca.anchor_qid,
  mm.lid          AS target_lid,
  mm.lang         AS target_lang,
  mm.lemma        AS target_lemma,
  (SELECT n_lid FROM member_homograph mh
     WHERE mh.lexentry=mm.lexentry AND mh.sense_num=mm.sense_num
       AND mh.lang=mm.lang AND mh.lemma=mm.lemma) AS homograph_n_lid,
  (SELECT count(*) FROM wd.sense s WHERE s.lid = mm.lid)            AS target_n_senses,
  EXISTS(SELECT 1 FROM member_qid mq                                -- already has SOME P5137...
         WHERE mq.lid=mm.lid AND mq.lexentry=ca.lexentry AND mq.sense_num=ca.sense_num
           AND mq.qid <> ca.anchor_qid)                            AS cross_pivot_conflict
FROM cluster_anchor ca
JOIN matched_member mm ON mm.lexentry=ca.lexentry AND mm.sense_num=ca.sense_num
WHERE mm.lang IN (%(review)s)                                       -- targets: review langs only
  AND NOT EXISTS (                                                  -- ...not already grounded to Q
  SELECT 1 FROM member_qid mq
  WHERE mq.lid=mm.lid AND mq.lexentry=ca.lexentry AND mq.sense_num=ca.sense_num
    AND mq.qid = ca.anchor_qid);

-- 3b. the writable unit: one row per (target lexeme, anchor Q), with support count.
-- support_clusters = how many independent sense-grounded clusters propose this
-- pair (a confidence signal). One new sense per pair would be written.
DROP TABLE IF EXISTS candidate;
CREATE TABLE candidate AS
SELECT target_lid, target_lang, target_lemma, anchor_qid,
       count(DISTINCT lexentry || '#' || sense_num) AS support_clusters,
       max(homograph_n_lid)    AS homograph_n_lid,
       target_n_senses,
       max(cross_pivot_conflict) AS cross_pivot_conflict
FROM candidate_raw
GROUP BY target_lid, anchor_qid;

-- abstain log: clusters that matched >=2 lexemes but produced no single anchor
DROP TABLE IF EXISTS cluster_abstain;
CREATE TABLE cluster_abstain AS
WITH matched_clusters AS (
  SELECT lexentry, sense_num, count(DISTINCT lid) AS n_matched
  FROM matched_member GROUP BY lexentry, sense_num),
 anchor_counts AS (
  SELECT lexentry, sense_num, count(DISTINCT qid) AS n_anchor_qids
  FROM clean_anchor GROUP BY lexentry, sense_num)
SELECT mc.lexentry, mc.sense_num, mc.n_matched,
  coalesce(ac.n_anchor_qids, 0) AS n_anchor_qids,
  CASE WHEN coalesce(ac.n_anchor_qids,0) = 0 THEN 'no_anchor'
       WHEN ac.n_anchor_qids > 1            THEN 'conflicting_anchors'
       ELSE 'ok' END AS reason
FROM matched_clusters mc
LEFT JOIN anchor_counts ac USING (lexentry, sense_num)
WHERE mc.n_matched >= 2
  AND NOT EXISTS (SELECT 1 FROM cluster_anchor ca
                  WHERE ca.lexentry=mc.lexentry AND ca.sense_num=mc.sense_num);
""" % {"noun": NOUN_QID, "review": ",".join(f"'{c}'" for c in config.REVIEW_LANGS)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clusters", default=config.CLUSTERS)
    ap.add_argument("--wikidata", default=config.WIKIDATA)
    ap.add_argument("--out", default=config.CANDIDATES)
    args = ap.parse_args()

    if os.path.exists(args.out):
        os.remove(args.out)
    conn = sqlite3.connect(args.out)
    conn.execute("ATTACH DATABASE ? AS cl", [args.clusters])
    conn.execute("ATTACH DATABASE ? AS wd", [args.wikidata])
    conn.executescript(BUILD)

    n_cand = conn.execute("SELECT count(*) FROM candidate").fetchone()[0]
    n_clean = conn.execute(
        "SELECT count(*) FROM candidate WHERE NOT cross_pivot_conflict AND homograph_n_lid = 1"
    ).fetchone()[0]
    n_conflict = conn.execute("SELECT count(*) FROM candidate WHERE cross_pivot_conflict").fetchone()[0]
    n_anchored = conn.execute("SELECT count(*) FROM cluster_anchor").fetchone()[0]
    print(f"clusters with a single dominant anchor: {n_anchored:,}")
    print(f"candidates (target lexemes to ground): {n_cand:,}")
    print(f"  clean (no cross-pivot conflict, unambiguous match): {n_clean:,}")
    print(f"  cross-pivot conflicts (abstain): {n_conflict:,}")
    for reason, n in conn.execute("SELECT reason, count(*) FROM cluster_abstain GROUP BY reason"):
        print(f"abstained clusters [{reason}]: {n:,}")

    print("\nsample candidates (target_lid, lemma@lang -> anchor_qid | support, n_senses, homographs, conflict):")
    for row in conn.execute(
        """SELECT target_lid, target_lemma, target_lang, anchor_qid, support_clusters,
                  target_n_senses, homograph_n_lid, cross_pivot_conflict
           FROM candidate ORDER BY support_clusters DESC LIMIT 20"""
    ):
        lid, lemma, lang, qid, sup, nsenses, homo, conflict = row
        print(f"   {lid:<8} {lemma}@{lang:<3} -> {qid:<8} | support={sup} senses={nsenses} homo={homo} conflict={conflict}")
    conn.close()


if __name__ == "__main__":
    main()
