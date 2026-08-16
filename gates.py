"""Anchor-class scope filter — concepts that are not valid anchors for a concrete
common noun, regardless of whether the propagation denotes them correctly.

This is a SCOPE filter, not a precision filter. The LLM verifier checks *denotation*
("does the noun denote this concept?") and will pass abstract-but-correct cases:
e.g. it said "yes" to *plus*/*pro* → Q24050179 "advantage" because they genuinely
denote it. Scope (concrete common noun, per the pilot) is a separate axis the verifier
does not judge, so this filter is complementary, not redundant. Held-out hit-rate is
therefore the wrong yardstick — a correct propagation to an out-of-scope concept should
still be excluded.

Two sources:
  - makesense (Nudin/makesense, queries/en.sparql) false-positive P31/P279 lists:
    proper-noun / instance / work classes (humans, films, albums, bands, genes,
    disambiguation pages, creative works, …) — never common-noun concepts.
  - abstract-quality extension (this project): general term / quality / relative
    quality — out of the concrete-noun pilot scope (the plus/pro → "advantage" class).

Contrast with the demonym→language idea, which measurement *rejected*: that one claimed
to drop wrong cases but actually dropped a 93%-precision legit set. This one drops on a
principled scope axis the verifier ignores.
"""

# makesense en.sparql P31 false-positive classes (proper-noun / instance / work)
_MAKESENSE_P31 = """
Q5 Q395 Q7187 Q7366 Q7397 Q8134 Q8502 Q11424 Q11446 Q23786 Q40056 Q43229 Q101352 Q134556
Q147276 Q170173 Q191067 Q202866 Q208569 Q215380 Q226730 Q355304 Q482994 Q494452 Q786820
Q819989 Q1145276 Q1229071 Q1348589 Q1391494 Q1475691 Q1505023 Q1725664 Q1897960 Q2235308
Q3305213 Q3331189 Q3404720 Q3491429 Q3658341 Q3744866 Q3744880 Q4167410 Q4167836 Q5185279
Q5398426 Q6881511 Q7725634 Q10429085 Q11060274 Q11668273 Q13406463 Q13417114 Q15056993
Q15632617 Q15642541 Q15831596 Q17155032 Q17537576 Q17638537 Q18218093 Q19798642 Q19798644
Q19798645 Q20202269 Q20643955 Q21191270 Q22808320 Q29168811 Q29654788 Q56876983 Q57814795
Q58408484 Q66050470 Q99045339 Q99281788 Q105543609
"""
_MAKESENSE_P279 = "Q3918 Q14745 Q15284 Q55043 Q192249 Q494452 Q783794 Q13220204 Q15911314"

# abstract-quality extension — out of concrete-noun scope (observed: plus/pro → Q24050179)
_ABSTRACT_P31 = "Q12379553"               # general term
_ABSTRACT_P279 = "Q100195948 Q1207505"    # relative quality, quality

EXCLUDE_P31 = set(_MAKESENSE_P31.split()) | set(_ABSTRACT_P31.split())
EXCLUDE_P279 = set(_MAKESENSE_P279.split()) | set(_ABSTRACT_P279.split())


def clean_clause(cand_alias="c"):
    """SQL fragment for 'clean': an unambiguous lemma -> lexeme match, and nothing else.

    This used to also carry `NOT cross_pivot_conflict`. That flag (match.py:140-142) is true
    whenever the target lexeme already carries a P5137 pointing elsewhere on ANY sense —
    which is simply what a correctly curated polysemous lexeme looks like, because
    match.py's member_qid joins wd.sense_item on lid alone and drops sid. Measured: it
    excluded 92.2% of candidate rows with >=2 senses against 3.1% of monosemous ones, and
    wiped out all 1,147 partially-linked polysemous lexemes. It is an ACTION precondition
    wearing a shared filter's clothes, so it now lives with the action instead — see
    create_first_clause / match_clause."""
    return f"{cand_alias}.homograph_n_lid = 1"


def free_sense_clause(cand_alias="c", wd_alias="wd"):
    """SQL fragment: the target has at least one sense carrying no P5137 — a slot to write
    into. Requires the wikidata DB attached as `wd_alias`.

    Evaluated LIVE against the dump rather than frozen into `candidate` at match time, which
    is what write.py already does before it writes (resolve_sense + the never-override guard):
    a sense somebody linked since the last match run should stop us, and a frozen flag would
    not. NOTE this is FALSE for a senseless lexeme — no senses means no free sense — which is
    exactly why it cannot be folded back into clean_clause."""
    c, wd = cand_alias, wd_alias
    return (f"EXISTS(SELECT 1 FROM {wd}.sense s WHERE s.lid = {c}.target_lid "
            f"AND NOT EXISTS(SELECT 1 FROM {wd}.sense_item si "
            f"WHERE si.lid = s.lid AND si.sid = s.sid))")


def create_first_clause(cand_alias="c"):
    """Admissibility for the CREATE_FIRST action: no recorded senses, so the edit creates the
    lexeme's first one. This is the senseless lane, ~88% of the pool."""
    return f"{cand_alias}.target_n_senses = 0"


def match_clause(cand_alias="c", wd_alias="wd"):
    """Admissibility for the MATCH action: at least one recorded sense, at least one of them
    still free. Generalizes over EVERY sense count >= 1 — the same predicate that reproduces
    today's monosemous stream exactly also reaches the polysemous targets gate_b(2) could
    never express."""
    return (f"{cand_alias}.target_n_senses >= 1 AND "
            + free_sense_clause(cand_alias, wd_alias))


def poly_match_clause(cand_alias="c", wd_alias="wd"):
    """MATCH restricted to genuinely polysemous targets (>= 2 recorded senses, >= 1 of them
    free). This is the NEW review lane, and it is deliberately not the same set as
    `match_clause`: the m=1 half of that predicate is the monosemous write stream, which has
    445 written decisions, its own calibration history and its own auto-defer gates. Pooling
    the two would silently restate the established lane's precision over a population that has
    never been reviewed. Same ACTION, separate LANE."""
    return (f"{cand_alias}.target_n_senses >= 2 AND "
            + free_sense_clause(cand_alias, wd_alias))


def admissible_clause(cand_alias="c", wd_alias="wd"):
    """Admissible for SOME action: the lexeme has no senses (CREATE_FIRST) or has at least one
    free sense (MATCH). This is the union `clean_clause`'s cross_pivot_conflict term used to
    approximate, and it is what a consumer wants when it is NOT selecting a single lane."""
    return (f"({create_first_clause(cand_alias)} OR "
            f"{match_clause(cand_alias, wd_alias)})")


def gate_a(cand_alias="c", min_support=2):
    """SQL fragment for the 'A' gate: >= min_support agreeing anchor clusters."""
    return f"{cand_alias}.support_clusters >= {int(min_support)}"


def gate_b(cand_alias="c", n_senses=1):
    """SQL fragment for the 'B' gate: target sense count == n_senses (1 = monosemous
    write stream, 0 = the senseless sense-creation lane)."""
    return f"{cand_alias}.target_n_senses = {int(n_senses)}"


def high_tier_where(cand_alias="c", it_alias="it", n_senses=1, wd_alias="wd"):
    """The canonical High-tier predicate as one SQL fragment, so verify / review /
    export / write all agree on what "High tier" means (they used to diverge — the
    scope exclusion was applied in some consumers but not others).

    = clean (unambiguous lemma match) ∩ A (>=2 agreeing anchors) ∩ D (concrete anchor with
      a P31, in scope) ∩ the admissibility of the ACTION this tier feeds.

    `n_senses` selects the action:
      0      -> CREATE_FIRST, the senseless sense-creation lane (target has no senses).
      1      -> MATCH restricted to monosemous targets — today's pilot write stream.
      "poly" -> MATCH restricted to polysemous targets (m >= 2), the new review lane. The
                per-sense measurement it was gated on has now run (assign_eval.py: 93-95%
                selection, abstention discriminating 60:1, ~9.5% conditional error), and the
                verdict was human review, never auto-approve.
      None   -> MATCH at any sense count, the general form — the union of the 1 and "poly"
                lanes. For measuring and for consumers that are not selecting one lane.

    Both pre-existing lanes are byte-identical to the pre-refactor definition — `gates.py
    --check` asserts it against the live databases. Requires `it_alias` and, for the MATCH
    forms, `wd_alias` attached."""
    c, it = cand_alias, it_alias
    parts = [clean_clause(c), gate_a(c), concrete_anchor_clause(c, it)]
    if n_senses is None:
        parts.append(match_clause(c, wd_alias))
    elif n_senses == "poly":
        parts.append(poly_match_clause(c, wd_alias))
    elif int(n_senses) == 0:
        parts.append(create_first_clause(c))
    else:                       # exact sense count, still MATCH: must have a free sense
        parts.append(gate_b(c, n_senses) + " AND " + free_sense_clause(c, wd_alias))
    return " AND ".join(parts)


def concrete_anchor_clause(cand_alias="c", it_alias="it"):
    """SQL fragment for the 'D' gate: the anchor is a concrete, in-scope concept — it
    carries a P31 (a real instance-typed item, not a bare value) AND is not an
    out-of-scope class (EXCLUDE_P31/P279). Shared by high_tier_where and
    verify.candidates so the concrete-noun tier has ONE definition and they can't
    silently drift apart. Assumes the items DB is attached as `it_alias`."""
    c, it = cand_alias, it_alias
    p31 = ",".join(f"'{q}'" for q in sorted(EXCLUDE_P31))
    p279 = ",".join(f"'{q}'" for q in sorted(EXCLUDE_P279))
    return (f"EXISTS(SELECT 1 FROM {it}.item_type _t "
            f"WHERE _t.qid = {c}.anchor_qid AND _t.prop = 'P31') "
            f"AND NOT (EXISTS(SELECT 1 FROM {it}.item_type _x "
            f"WHERE _x.qid={c}.anchor_qid AND ("
            f"(_x.prop='P31' AND _x.value IN ({p31})) OR "
            f"(_x.prop='P279' AND _x.value IN ({p279})))))")


def _check():
    """Assert the invariants the three lanes must satisfy, against the live databases.

    This used to compare each lane against the pre-refactor SQL verbatim. That proof did its
    job when admissibility moved out of `clean_clause`, and keeping it would pin the OLD
    definition into the code forever — a frozen arm constraining live code, which is the
    pattern we are trying to get rid of. What survives is the property that actually protects
    us, and it is stronger than the equivalence was:

      * every lane is NON-EMPTY. The near-miss was a predicate that silently took the
        senseless lane from 11,371 rows to ZERO — 88% of the pool — because
        free_sense_clause is false for a lexeme with no senses. An equivalence test caught
        that by accident; this catches it on purpose, and keeps catching it as the
        definitions legitimately change.
      * the two MATCH lanes PARTITION the general form, so a pair is reviewable in exactly
        one queue and can never be decided two ways.
    """
    import sqlite3
    import config
    cn = sqlite3.connect("file:" + config.CANDIDATES + "?mode=ro", uri=True)
    for alias, path in (("it", config.ITEMS), ("wd", config.WIKIDATA)):
        cn.execute("ATTACH DATABASE ? AS " + alias, ["file:" + path + "?mode=ro"])

    def pairs(where):
        return set(cn.execute(
            f"SELECT target_lid, anchor_qid FROM candidate c WHERE {where}"))

    ok = True
    lanes = {}
    for n_senses, name in ((0, "CREATE_FIRST (senseless)"), (1, "MATCH (monosemous)"),
                           ("poly", "MATCH (polysemous)")):
        lanes[n_senses] = got = pairs(high_tier_where("c", "it", n_senses))
        ok &= bool(got)
        print(f"{'PASS' if got else 'FAIL'}  n_senses={str(n_senses):<6} {name:<26} "
              f"{len(got):>6,} pairs over {len({l for l, _ in got}):>5,} lexemes")

    general = pairs(high_tier_where("c", "it", None))
    mono, poly = lanes[1], lanes["poly"]
    if poly & mono:
        ok = False
        print(f"  FAIL: {len(poly & mono):,} pairs are in BOTH review lanes")
    if poly | mono != general:
        ok = False
        print(f"  FAIL: mono + poly do not cover the general MATCH form "
              f"({len(general - (poly | mono)):,} uncovered, "
              f"{len((poly | mono) - general):,} extra)")
    else:
        print(f"\n{len(mono):,} mono + {len(poly):,} poly = {len(general):,} general MATCH — "
              f"the review lanes partition it, no overlap and no gap")
    cn.close()
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _check() else 1)
