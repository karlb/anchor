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


def high_tier_where(cand_alias="c", it_alias="it", n_senses=1):
    """The canonical High-tier predicate as one SQL fragment, so verify / review /
    export / write all agree on what "High tier" means (they used to diverge — the
    scope exclusion was applied in some consumers but not others).

    = clean (unambiguous match ∩ no cross-pivot conflict) ∩ A (>=2 agreeing anchors)
      ∩ B (target sense count == n_senses) ∩ D (concrete anchor with a P31) ∩ in-scope.
    `n_senses` selects the tier's target: 1 = monosemous (the pilot write stream, add
    P5137 to the one existing sense); 0 = senseless (phase-2 sense-creation lane). ONE
    definition, parameterized, so the two tiers can't silently drift (that bug once
    existed — a hard-coded `= 1` here blocked senseless rows from every consumer).
    Assumes candidate alias `cand_alias` and the items DB attached as `it_alias`."""
    c, it = cand_alias, it_alias
    return (
        f"{c}.homograph_n_lid = 1 AND NOT {c}.cross_pivot_conflict "
        f"AND {c}.support_clusters >= 2 AND {c}.target_n_senses = {int(n_senses)} "
        f"AND " + concrete_anchor_clause(c, it)
    )


def concrete_anchor_clause(cand_alias="c", it_alias="it"):
    """SQL fragment for the 'D' gate: the anchor is a concrete, in-scope concept — it
    carries a P31 (a real instance-typed item, not a bare value) AND is not an
    out-of-scope class. Shared by high_tier_where and verify.candidates so the
    concrete-noun tier has ONE definition and they can't silently drift apart."""
    c, it = cand_alias, it_alias
    return (f"EXISTS(SELECT 1 FROM {it}.item_type _t "
            f"WHERE _t.qid = {c}.anchor_qid AND _t.prop = 'P31') "
            f"AND NOT (" + exclusion_clause(c, it) + ")")


def exclusion_clause(cand_alias="c", it_alias="it"):
    """SQL fragment (True when the candidate's anchor is an out-of-scope class).

    Assumes the items DB is attached as `it_alias` (table item_type(qid,prop,value)).
    Use as: ``... AND NOT (`` + exclusion_clause() + ``)``."""
    p31 = ",".join(f"'{q}'" for q in sorted(EXCLUDE_P31))
    p279 = ",".join(f"'{q}'" for q in sorted(EXCLUDE_P279))
    return (f"EXISTS(SELECT 1 FROM {it_alias}.item_type _x "
            f"WHERE _x.qid={cand_alias}.anchor_qid AND ("
            f"(_x.prop='P31' AND _x.value IN ({p31})) OR "
            f"(_x.prop='P279' AND _x.value IN ({p279}))))")
