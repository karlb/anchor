#!/usr/bin/env python3
"""Build the problematic-cases reference set from existing caches (no LLM calls).

A case is kept in the gold if it has a hand-curated PROPOSED verdict (so the locked set is
never silently dropped), OR it is newly 'problematic' — the base / +multi arms disagree on
the action, or an arm disagrees with the human log — so contested cases keep surfacing for
curation. For each we dump every signal we have so we can agree the CORRECT output by hand,
then score approaches against it. Pure reads: ab cache + verify cache + decisions + items DB.
"""
import csv
import sqlite3
import json
import collections
import config
import verify
import ab_concept as ab

ARMS = ["base", "+multi"]
SHORT = {"base": "base", "+multi": "multi"}

rows, truth = ab.load_eval_rows(suspect=True)
keys = {(r[0], r[3]): r for r in rows}

# per-arm majority-vote action from the ab cache
votes = collections.defaultdict(collections.Counter)
for line in open("data/ab_concept_cache.jsonl"):
    e = json.loads(line)
    k = (e["lid"], e["qid"])
    if k in keys and e["arm"] in ARMS:
        votes[(e["arm"], k)][ab.predicted_action(e["verdict"], e["reason"])] += 1
pred = collections.defaultdict(dict)
for (arm, k), c in votes.items():
    pred[arm][k] = c.most_common(1)[0][0]

it = sqlite3.connect(config.ITEMS)
gmap = verify.load_glosses([k[0] for k in keys])

# full multilingual anchor evidence — every language's label+desc from the unified
# all-language CC0 cache (item_lang, fetch_anchor_langs.py).
PRIORITY = ["en", "de", "sv", "fr", "es", "it", "nl", "pt", "pl", "ru", "ja", "zh"]
wide = collections.defaultdict(dict)
for qid, lang, label, desc in it.execute(
        "SELECT qid, lang, label, desc FROM item_lang"):
    wide[qid][lang] = (label, desc)


def anchor_evidence(qid):
    """All languages that carry a description, quality-priority first, then alphabetical."""
    ent = wide.get(qid, {})
    langs = [l for l in ent if ent[l][1]]               # has a desc
    langs.sort(key=lambda l: (PRIORITY.index(l) if l in PRIORITY else 99, l))
    return " | ".join(f"{l}: {ent[l][0] or '?'} :: {ent[l][1]}" for l in langs)


def all_glosses(lid):
    """Every language's gloss we have for the lexeme (lexeme-side multilingual evidence)."""
    return " | ".join(f"{lg}: {g}" for lg, g in sorted(gmap.get(lid, {}).items()))

# proposed-correct verdicts. Each grounded in the gloss + full multilingual anchor, and for
# the lexeme-meaning-sensitive ones also Wiktionary. The {..} in the rationale = the set of
# outputs we'd accept (defer is a valid triage action, never scored wrong). Verdict column
# holds the single best output; pick a different one in the CSV to override.
PROPOSED = {
    # --- EQUAL / NARROWER -> keep (tolerant) ---
    "Q182436": ("keep", "librarian=librarian; gloss 'works at a library' = same role; {keep}"),
    "Q124378": ("keep", "daggmask IS Swedish for earthworm; genus-vs-suborder rank split is pedantic; {keep,defer}"),
    "Q7958": ("keep", "förklaring=explanation; gloss generic but the sense matches; {keep}"),
    "Q126553": ("keep", "ark in context=Noah's Ark; 'ancient mythical boat' gloss points there; {keep,defer}"),
    "Q5716684": ("keep", "dansös=female artistic dancer; gender recorded separately (gender rule); {keep}"),
    "Q5194627": ("keep", "Lilie=genus Lilium; you previously said keep & defer both fine; {keep,defer}"),
    "Q15098051": ("keep", "Nasenbeutler=order Peramelemorphia=bandicoots; rank match; {keep,defer}"),
    "Q200569": ("keep", "holländare colloquially=Dutch/Netherlands in Swedish (demonym); {keep}"),
    "Q18973": ("keep", "georgier=Georgian (demonym, person from Georgia); anchor=Georgians ethnic group — demonym link; {keep}"),
    "Q10294": ("keep", "armod=severe poverty/destitution (Wikt 'svår fattigdom'); anchor=poverty — NARROWER; {keep,defer}"),
    "Q83267": ("keep", "förbrytelse=(serious) crime; anchor=crime — EQUAL/narrower; {keep,defer}"),
    "Q105196": ("keep", "transplantation=transplantation; gloss vague but matches the medical organ-transplant anchor; {keep,defer}"),
    "Q106575204": ("keep", "sheet=piece of paper; anchor=sheet (paper, esp. for maps) — same core concept; {keep,defer}"),
    "Q13382566": ("keep", "Kanute=canoeist; anchor=canoeist (sport) — EQUAL/slightly broader; {keep,defer}"),
    "Q886518": ("keep", "blood count=(complete) blood count test; EQUAL/near; {keep,defer}"),
    "Q154547": ("keep", "dukedom=duchy (territory/rank of a duke); EQUAL; {keep,defer}"),
    "Q843281": ("keep", "hobo=hobo; gloss 'homeless person'~anchor 'migratory worker/homeless vagabond'; EQUAL; {keep,defer}"),
    "Q1714828": ("keep", "huntsman (hunts with weapons per gloss)=hunter; NARROWER/EQUAL; {keep,defer}"),
    "Q1429491": ("keep", "riverbed=bed of a river; anchor=stream bed (general channel bottom) — NARROWER/EQUAL; {keep,defer}"),
    "Q846664": ("keep", "Habicht=goshawk/large Accipiter (per gloss); anchor=hawk (family Accipitridae) — NARROWER; {keep,defer}"),
    # --- Type-1 'label-artifact BROADER': base flags BROADER off ONE narrow label, but the
    # concept read across its multilingual descriptions is really EQUAL to the lexeme. These
    # are the cases +multi should correct (BROADER->keep); human approved each. ---
    "Q1622272": ("keep", "högskolelärare=higher-ed teacher; de Hochschullehrer/en 'university or college'/no 'universitet eller høgskole'/nl/eo all broad higher-ed = lexeme; only sv label 'universitetslärare' is narrow — EQUAL; {keep,defer}"),
    "Q1554231": ("keep", "resurs=usable asset; en/da 'source/supply from which benefit is produced' = lexeme; de/sv 'economic' is the narrow label only — EQUAL; {keep,defer}"),
    "Q1762621": ("keep", "leverantör=supplier; de 'Lieferant: jemand der Waren/Dienstleistungen liefert' = lexeme (anyone who delivers); en/sv frame commercial vendor — EQUAL; {keep,defer}"),
    "Q11199149": ("keep", "blygsamhet=unassuming modesty; de/nl 'Bescheidenheit/bescheidenheid' = general modesty = lexeme; en 'dress modesty' is the narrow outlier that drove BROADER — EQUAL; {keep,defer}"),
    # --- BROADER / NARROWER / LATERAL / contested -> defer (send to human) ---
    "Q44619": ("defer", "dread=intense/anticipatory fear — narrower; {defer,keep}"),
    "Q21004260": ("defer", "efterforskning (find facts/persons) narrower than inquiry (knowledge process); {defer}"),
    "Q290691": ("defer", "30-lang anchor consensus=FICTION villain; gloss leans real wrongdoer — judgment call; {defer,reject}"),
    "Q50803918": ("defer", "all 3 langs: anchor=generic any-cause large wave; tidal wave is NARROWER instance — scope mismatch; {defer,reject}"),
    "Q2684232": ("defer", "de/it descs reveal anchor=waste-MANAGEMENT umbrella (~EQUAL to avfallshantering); en label misleads; human reject too harsh; {keep,defer}"),
    "Q1369832": ("defer", "all langs: anchor=ACT of purchasing; 'a buy'=advantageous result/instance — lateral/narrower; {defer,reject}"),
    "Q41374": ("defer", "gloss=general embodiment of the abstract (broad); anchor=divine/religious incarnation (de 'Menschwerdung eines Gottes') — lexeme BROADER; {defer,keep}"),
    "Q1749879": ("defer", "Wikt: retiree broader than pensioner (retirement status vs pension receipt) — lateral/near; {defer,reject}"),
    "Q21849766": ("defer", "arvtagerska=woman who inherits (general); anchor=heiress narrowly 'in noble context' — BROADER; {defer,reject}"),
    "Q79915": ("defer", "kingfisher=general bird (family Alcedinidae); anchor=Common Kingfisher (ONE species) — lexeme BROADER; {defer,reject}"),
    "Q3699044": ("defer", "gloss: Mammut=genus Mammuthus (Gattung); anchor=woolly mammoth (one species) — lexeme BROADER; {defer,reject}"),
    "Q1192553": ("defer", "facial recognition (the process) vs facial recognition SYSTEM (the application) — LATERAL process-vs-system; {defer,reject}"),
    "Q6301005": ("defer", "Wikt: wise guy=sarcastic joker (primary); anchor=know-it-all — adjacent distinct tropes; {defer,reject}"),
    "Q1367353": ("defer", "sv.Wikt: bekvämlighet=amenity/convenience or laziness, NOT 'comfort the feeling'; anchor spans feeling(en) & Komfort/amenity(de) — contested; {defer}"),
    "Q168983": ("defer", "blaze=bright fire (gloss 'burn brightly'); anchor=conflagration (large destructive fire) — broader/lateral not equal; {defer,reject}"),
    "Q4070300": ("defer", "balettdansös=any female ballet dancer; anchor=ballerina (PRINCIPAL dancer per en desc) — lexeme BROADER (colloquially equal); {defer,keep}"),
    "Q155953": ("defer", "competitiveness (general quality) vs anchor=ECONOMIC competitiveness (en/es/it/fi/ko) — lexeme BROADER; {defer,reject}"),
    "Q278138": ("defer", "Wikt: dairyman=BOTH dairy worker AND milkman; gloss leans worker but overlaps anchor (milk deliverer); {defer,reject}"),
    "Q127076": ("defer", "gloss=the substance ('food brought back up'); anchor=vomiting (the act) — LATERAL substance-vs-act; barf also=to vomit; {keep,defer}"),
    "Q1747066": ("defer", "desperation=urgency born of despair; anchor=despair (the feeling) — adjacent/lateral; {defer,reject}"),
    "Q47054": ("defer", "enigma (mystery/puzzle, broad) vs riddle (a posed word-puzzle) — enigma broader/lateral; {keep,defer}"),
    "Q13370881": ("defer", "ruling=judicial/official decision (gloss broad: regulation, law); anchor=verdict (jury/judge trial decision) — broader/lateral; {defer,reject}"),
    "Q385586": ("defer", "Apostel=apostle (the 12 sent by Jesus, per gloss); anchor=disciple (general follower) — Apostel NARROWER; {defer,reject}"),
    "Q1055701": ("defer", "Computerhersteller=makes whole computers; anchor desc says 'computer PARTS'(en)/'equipment'(sv) — label/desc scope clash; {defer}"),
    # --- DIFFERENT -> reject ---
    "Q201650": ("reject", "hypocrite (person) vs hypocrisy (abstract); also incoherent target; {reject,defer}"),
    "Q102014": ("reject", "Querverweis=intra-document cross-reference; anchor=hyperlink (clickable digital link) — different; {reject,defer}"),
    "Q429795": ("reject", "Abart=general deviation from norm; anchor=mineral variety — different; {reject}"),
    "Q998": ("reject", "kiddo=child of any age; anchor=infant (baby under one year) — different; {reject}"),
    "Q1113939": ("reject", "Rückkehr=general return to a place; anchor=comeback (public-figure return) — different; {reject}"),
    "Q36993249": ("reject", "arrangemang=result/event; anchor 'arranging'=the act — lateral; {reject,defer}"),
    "Q1552579": ("reject", "hantlangare=unskilled helper, not building-maintenance handyman (Wikt; corrected in log); {reject}"),
    "Q15942027": ("reject", "Wikt: hog=generic adult swine, NOT female; anchor=female pig (sow) — wrong referent; uniform false-keep; {reject}"),
    "Q218005": ("reject", "amount=general quantity; anchor=sum (mathematical addition of a sequence) — different; {reject,defer}"),
}

# The GOLD is exactly the hand-curated set (cases with a PROPOSED verdict) — so it stays a
# stable, fully-labelled reference that scores cleanly. Cases that newly look problematic
# (arms disagree / disagree with human) but have NO curated verdict are merely LISTED as
# candidates to curate next, not written into the gold with an empty acceptable-set.
out, to_curate = [], []
for k, row in keys.items():
    lid, qid = k
    actions = {a: pred[a].get(k) for a in ARMS}
    vals = [v for v in actions.values() if v]
    h = truth[k]
    lemma, lang = row[2], row[1]
    if qid not in PROPOSED:
        if len(set(vals)) > 1 or any(v != h for v in vals):
            to_curate.append((lang, lemma, lid, qid, h, actions))
        continue
    prop, rat = PROPOSED[qid]
    out.append({
        "lid": lid, "qid": qid, "lang": lang, "lemma": lemma,
        "glosses_all": all_glosses(lid) or verify.pick_gloss(gmap, lid, lang),
        "anchor_langs": anchor_evidence(qid),
        "human": h, "base": actions["base"] or "-", "multi": actions["+multi"] or "-",
        "proposed_correct": prop, "rationale": rat,
    })

# disagreement breadth first (most contested at top)
out.sort(key=lambda r: -len({r["human"], r["base"], r["multi"]}))

cols = ["lid", "qid", "lang", "lemma", "glosses_all", "anchor_langs",
        "human", "base", "multi", "proposed_correct", "rationale"]
with open("notes/problematic_cases.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(out)
print(f"wrote notes/problematic_cases.csv — {len(out)} curated gold cases "
      f"(of {len(keys)} suspect pairs), full multilingual anchor evidence inline")
if to_curate:
    print(f"\n{len(to_curate)} NEW problematic cases without a curated verdict "
          f"(candidates to add to PROPOSED — NOT in the gold):")
    for lang, lemma, lid, qid, h, actions in to_curate:
        print(f"  {lang} {lemma:<22} {lid} {qid}  human={h}  "
              f"base={actions['base'] or '-'} multi={actions['+multi'] or '-'}")

# full multilingual dump of the cases I flagged as debatable, so we can settle them now
DEBATABLE = ["Q50803918", "Q2684232", "Q290691", "Q1369832"]
byqid = {r["qid"]: r for r in out}
for qid in DEBATABLE:
    r = byqid.get(qid)
    if not r:
        continue
    print("\n" + "=" * 78)
    print(f"{r['lang']}:{r['lemma']}  ({r['lid']} / {qid})   human={r['human']}  "
          f"arms: base={r['base']} multi={r['multi']}")
    print(f"  GLOSS(es): {r['glosses_all']}")
    print(f"  proposed: {r['proposed_correct']}  ({r['rationale']})")
    print("  ANCHOR across languages:")
    for part in r["anchor_langs"].split(" | "):
        print(f"    {part}")
