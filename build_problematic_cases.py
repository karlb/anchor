#!/usr/bin/env python3
"""Build the problematic-cases reference set from existing caches (no LLM calls).

The GOLD is the hand-curated PROPOSED set, keyed by (lid, qid) so a curated case stays in the
gold no matter how the verifier later classifies it. Membership used to ride on the live
'suspect' (scope/coherence) filter, so verify-cache drift silently shrank the gold on a clean
rebuild — keying by (lid, qid) closes that latent data-loss bug. For each case we dump every
signal we have (gloss, full multilingual anchor, human log, per-arm action) so we can agree the
CORRECT output by hand, then score approaches against it. Pure reads: ab cache + verify cache +
decisions + items DB. The rebuild runs only as a script; importing the module is side-effect-free
(PROPOSED is importable data).
"""
import csv
import sqlite3
import collections
import config
import verify
import ab_concept as ab

ARMS = ["base", "+multi"]

# Proposed-correct verdicts, keyed by (lid, qid) — the hand-curated source of truth and the
# gold's membership. Each grounded in the gloss + full multilingual anchor, and for the
# lexeme-meaning-sensitive ones also Wiktionary. The {..} in the rationale = the set of outputs
# we'd accept (defer is a valid triage action, never scored wrong). Verdict = the single best
# output; pick a different one in the CSV to override. A curated case need not be a
# scope/coherence ('suspect') pair: a label-collision false-keep — an EQUAL homograph the
# verifier wrongly accepts, e.g. Achtzigstel (the fraction 1/80) vs the ordinal '80th' — belongs
# here too. Keying by (lid, qid) also keeps each curated verdict on the intended lexeme even when
# several lexemes share one anchor qid.
PROPOSED = {
    # --- EQUAL / NARROWER -> keep (tolerant) ---
    ("L37080", "Q182436"): ("keep", "librarian=librarian; gloss 'works at a library' = same role; {keep}"),
    ("L36115", "Q124378"): ("keep", "daggmask IS Swedish for earthworm; genus-vs-suborder rank split is pedantic; {keep,defer}"),
    ("L33343", "Q7958"): ("keep", "förklaring=explanation; gloss generic but the sense matches; {keep}"),
    ("L14671", "Q126553"): ("keep", "ark in context=Noah's Ark; 'ancient mythical boat' gloss points there; {keep,defer}"),
    ("L32714", "Q5716684"): ("keep", "dansös=female artistic dancer; gender recorded separately (gender rule); {keep}"),
    ("L594719", "Q5194627"): ("keep", "Lilie=genus Lilium; you previously said keep & defer both fine; {keep,defer}"),
    ("L1459837", "Q15098051"): ("keep", "Nasenbeutler=order Peramelemorphia=bandicoots; rank match; {keep,defer}"),
    ("L588188", "Q200569"): ("keep", "holländare colloquially=Dutch/Netherlands in Swedish (demonym); {keep}"),
    ("L52374", "Q18973"): ("defer", "georgier=person from Georgia; anchor=Georgians ETHNIC group (de Volk/sv folkgrupp); demonym split: ethnic anchor -> not a keep; {defer,reject}"),
    ("L1505829", "Q10294"): ("keep", "armod=severe poverty/destitution (Wikt 'svår fattigdom'); anchor=poverty — NARROWER; {keep,defer}"),
    ("L1494890", "Q83267"): ("keep", "förbrytelse=(serious) crime; anchor=crime — EQUAL/narrower; {keep,defer}"),
    ("L44498", "Q105196"): ("keep", "transplantation=transplantation; gloss vague but matches the medical organ-transplant anchor; {keep,defer}"),
    ("L7743", "Q106575204"): ("keep", "sheet=piece of paper; anchor=sheet (paper, esp. for maps) — same core concept; {keep,defer}"),
    ("L809398", "Q13382566"): ("keep", "Kanute=canoeist; anchor=canoeist (sport) — EQUAL/slightly broader; {keep,defer}"),
    ("L1500783", "Q886518"): ("keep", "blood count=(complete) blood count test; EQUAL/near; {keep,defer}"),
    ("L319856", "Q154547"): ("keep", "dukedom=duchy (territory/rank of a duke); EQUAL; {keep,defer}"),
    ("L321955", "Q843281"): ("keep", "hobo=hobo; gloss 'homeless person'~anchor 'migratory worker/homeless vagabond'; EQUAL; {keep,defer}"),
    ("L35966", "Q1714828"): ("keep", "huntsman (hunts with weapons per gloss)=hunter; NARROWER/EQUAL; {keep,defer}"),
    ("L41973", "Q1429491"): ("keep", "riverbed=bed of a river; anchor=stream bed (general channel bottom) — NARROWER/EQUAL; {keep,defer}"),
    ("L823400", "Q846664"): ("keep", "Habicht=goshawk/large Accipiter (per gloss); anchor=hawk (family Accipitridae) — NARROWER; {keep,defer}"),
    # --- Type-1 'label-artifact BROADER': base flags BROADER off ONE narrow label, but the
    # concept read across its multilingual descriptions is really EQUAL to the lexeme. These
    # are the cases +multi should correct (BROADER->keep); human approved each. ---
    ("L490934", "Q1622272"): ("keep", "högskolelärare=higher-ed teacher; de Hochschullehrer/en 'university or college'/no 'universitet eller høgskole'/nl/eo all broad higher-ed = lexeme; only sv label 'universitetslärare' is narrow — EQUAL; {keep,defer}"),
    ("L37817", "Q1554231"): ("keep", "resurs=usable asset; en/da 'source/supply from which benefit is produced' = lexeme; de/sv 'economic' is the narrow label only — EQUAL; {keep,defer}"),
    ("L35402", "Q1762621"): ("keep", "leverantör=supplier; de 'Lieferant: jemand der Waren/Dienstleistungen liefert' = lexeme (anyone who delivers); en/sv frame commercial vendor — EQUAL; {keep,defer}"),
    ("L41355", "Q1762621"): ("keep", "leverantör=supplier; de 'Lieferant: jemand der Waren/Dienstleistungen liefert' = lexeme (anyone who delivers); en/sv frame commercial vendor — EQUAL; {keep,defer}"),
    ("L1449683", "Q11199149"): ("keep", "blygsamhet=unassuming modesty; de/nl 'Bescheidenheit/bescheidenheid' = general modesty = lexeme; en 'dress modesty' is the narrow outlier that drove BROADER — EQUAL; {keep,defer}"),
    # --- BROADER / NARROWER / LATERAL / contested -> defer (send to human) ---
    ("L13580", "Q44619"): ("defer", "dread=intense/anticipatory fear — narrower; {defer,keep}"),
    ("L242024", "Q21004260"): ("defer", "efterforskning (find facts/persons) narrower than inquiry (knowledge process); {defer}"),
    ("L242407", "Q290691"): ("defer", "30-lang anchor consensus=FICTION villain; gloss leans real wrongdoer — judgment call; {defer,reject}"),
    ("L1524564", "Q50803918"): ("defer", "all 3 langs: anchor=generic any-cause large wave; tidal wave is NARROWER instance — scope mismatch; {defer,reject}"),
    ("L1405801", "Q2684232"): ("defer", "de/it descs reveal anchor=waste-MANAGEMENT umbrella (~EQUAL to avfallshantering); en label misleads; human reject too harsh; {keep,defer}"),
    ("L317571", "Q1369832"): ("defer", "all langs: anchor=ACT of purchasing; 'a buy'=advantageous result/instance — lateral/narrower; {defer,reject}"),
    ("L1524836", "Q41374"): ("defer", "gloss=general embodiment of the abstract (broad); anchor=divine/religious incarnation (de 'Menschwerdung eines Gottes') — lexeme BROADER; {defer,keep}"),
    ("L326739", "Q1749879"): ("defer", "Wikt: retiree broader than pensioner (retirement status vs pension receipt) — lateral/near; {defer,reject}"),
    ("L35857", "Q21849766"): ("defer", "arvtagerska=woman who inherits (general); anchor=heiress narrowly 'in noble context' — BROADER; {defer,reject}"),
    ("L36983", "Q79915"): ("defer", "kingfisher=general bird (family Alcedinidae); anchor=Common Kingfisher (ONE species) — lexeme BROADER; {defer,reject}"),
    ("L808291", "Q3699044"): ("defer", "gloss: Mammut=genus Mammuthus (Gattung); anchor=woolly mammoth (one species) — lexeme BROADER; {defer,reject}"),
    ("L1368369", "Q1192553"): ("defer", "facial recognition (the process) vs facial recognition SYSTEM (the application) — LATERAL process-vs-system; {defer,reject}"),
    ("L1406336", "Q6301005"): ("defer", "Wikt: wise guy=sarcastic joker (primary); anchor=know-it-all — adjacent distinct tropes; {defer,reject}"),
    ("L1413190", "Q1367353"): ("defer", "sv.Wikt: bekvämlighet=amenity/convenience or laziness, NOT 'comfort the feeling'; anchor spans feeling(en) & Komfort/amenity(de) — contested; {defer}"),
    ("L20954", "Q168983"): ("defer", "blaze=bright fire (gloss 'burn brightly'); anchor=conflagration (large destructive fire) — broader/lateral not equal; {defer,reject}"),
    ("L246621", "Q4070300"): ("defer", "balettdansös=any female ballet dancer; anchor=ballerina (PRINCIPAL dancer per en desc) — lexeme BROADER (colloquially equal); {defer,keep}"),
    ("L30751", "Q155953"): ("defer", "competitiveness (general quality) vs anchor=ECONOMIC competitiveness (en/es/it/fi/ko) — lexeme BROADER; {defer,reject}"),
    ("L31295", "Q278138"): ("defer", "Wikt: dairyman=BOTH dairy worker AND milkman; gloss leans worker but overlaps anchor (milk deliverer); {defer,reject}"),
    ("L29089", "Q127076"): ("defer", "gloss=the substance ('food brought back up'); anchor=vomiting (the act) — LATERAL substance-vs-act; barf also=to vomit; {keep,defer}"),
    ("L316756", "Q127076"): ("defer", "gloss=the substance ('food brought back up'); anchor=vomiting (the act) — LATERAL substance-vs-act; barf also=to vomit; {keep,defer}"),
    ("L319342", "Q1747066"): ("defer", "desperation=urgency born of despair; anchor=despair (the feeling) — adjacent/lateral; {defer,reject}"),
    ("L32555", "Q47054"): ("defer", "enigma (mystery/puzzle, broad) vs riddle (a posed word-puzzle) — enigma broader/lateral; {keep,defer}"),
    ("L326986", "Q13370881"): ("defer", "ruling=judicial/official decision (gloss broad: regulation, law); anchor=verdict (jury/judge trial decision) — broader/lateral; {defer,reject}"),
    ("L892979", "Q385586"): ("defer", "Apostel=apostle (the 12 sent by Jesus, per gloss); anchor=disciple (general follower) — Apostel NARROWER; {defer,reject}"),
    ("L792107", "Q1055701"): ("defer", "Computerhersteller=makes whole computers; anchor desc says 'computer PARTS'(en)/'equipment'(sv) — label/desc scope clash; {defer}"),
    # --- DIFFERENT -> reject ---
    ("L14596", "Q201650"): ("reject", "hypocrite (person) vs hypocrisy (abstract); also incoherent target; {reject,defer}"),
    ("L1437703", "Q102014"): ("reject", "Querverweis=intra-document cross-reference; anchor=hyperlink (clickable digital link) — different; {reject,defer}"),
    ("L306888", "Q429795"): ("reject", "Abart=general deviation from norm; anchor=mineral variety — different; {reject}"),
    ("L312612", "Q998"): ("reject", "kiddo=child of any age; anchor=infant (baby under one year) — different; {reject}"),
    ("L409010", "Q1113939"): ("reject", "Rückkehr=general return to a place; anchor=comeback (public-figure return) — different; {reject}"),
    ("L479561", "Q36993249"): ("reject", "arrangemang=result/event; anchor 'arranging'=the act — lateral; {reject,defer}"),
    ("L1183032", "Q1552579"): ("reject", "hantlangare=unskilled helper, not building-maintenance handyman (Wikt; corrected in log); {reject}"),
    ("L14911", "Q15942027"): ("reject", "Wikt: hog=generic adult swine, NOT female; anchor=female pig (sow) — wrong referent; uniform false-keep; {reject}"),
    ("L3245", "Q218005"): ("reject", "amount=general quantity; anchor=sum (mathematical addition of a sequence) — different; {reject,defer}"),
    ("L59818", "Q218005"): ("reject", "amount=general quantity; anchor=sum (mathematical addition of a sequence) — different; {reject,defer}"),
    ("L610533", "Q28469790"): ("reject", "Achtzigstel=the fraction 1/80; anchor=ordinal '80th' (every lang desc 'ordinal number for 80'); label 'eightieth' coincides but kind differs (fraction vs ordinal) — homograph; {reject,defer}"),
    # --- 56 newly-surfaced cases curated 2026-06-26 (split demonym convention: nationality
    # anchor -> keep, ethnic-group/folk anchor -> defer/reject). Grounded in gloss + the
    # curated multilingual anchor descriptions. ---
    ("L1234846", "Q2941309"): ("keep", "Aalbeere=schwarze Johannisbeere=blackcurrant (Ribes nigrum); exact; {keep}"),
    ("L941741", "Q99907279"): ("keep", "Bosnier=person from Bosnia; anchor=citizens/residents of B&H (nationality, gender aside); demonym split nationality->keep; {keep,defer}"),
    ("L316539", "Q1936750"): ("defer", "atrocity=a terrible act; cruelty=pleasure/indifference in inflicting suffering — adjacent/different; {defer,reject}"),
    ("L253643", "Q166231"): ("defer", "contagion=disease transmission/spread; infection=pathogen invasion in host — lateral; {defer,reject}"),
    ("L586350", "Q166231"): ("defer", "contagion=disease transmission/spread; infection=pathogen invasion in host — lateral; {defer,reject}"),
    ("L1402975", "Q29485"): ("reject", "false (response of untruth/lie) vs error (inadvertent incorrectness) — different; {reject,defer}"),
    ("L322593", "Q21168708"): ("reject", "insistence=pressing for an outcome; determination=perseverance through obstacles — lateral/different; {reject,defer}"),
    ("L7184", "Q9332"): ("defer", "manner=mode of action; behavior=the actions themselves — lateral, not a kind of; {defer,reject}"),
    ("L4336", "Q18574339"): ("defer", "owner (legal title) vs possessor (possession/control) — adjacent legal concepts; {defer,reject}"),
    ("L253306", "Q247792"): ("defer", "progression=general forward movement; concept=historiographic 'progress' — broader/different; {defer,reject}"),
    ("L12984", "Q189053"): ("defer", "prompt=the ready-indicator; CLI=the whole interface — lateral part-vs-whole; {defer,reject}"),
    ("L291058", "Q19029"): ("reject", "pudding rice=the raw ingredient; rice pudding=the dish made from it — lateral ingredient-vs-dish; {reject,defer}"),
    ("L42154", "Q2334804"): ("defer", "shipment=the goods being shipped; delivery=the transport process — lateral object-vs-process; {defer,reject}"),
    ("L241919", "Q1422583"): ("defer", "angelägenhet=an important matter/affair; importance=the abstract quality — lateral; {defer,reject}"),
    ("L479556", "Q1357284"): ("reject", "anseende=reputation/standing (can be negative); popularity=being liked — different facets; {reject,defer}"),
    ("L242795", "Q28008277"): ("reject", "bekantskap=the state of being acquainted; concept=the PERSON one knows — lateral state-vs-person; {reject,defer}"),
    ("L47375", "Q133255"): ("defer", "bulgar=person from Bulgaria (nationality); anchor=Bulgarians ETHNIC group; demonym split ethnic->not keep; {defer,reject}"),
    ("L245965", "Q2396923"): ("defer", "dagdrivare=idler without fixed occupation; good-for-nothing=burden on others — broader/near; {defer,reject}"),
    ("L55012", "Q11326182"): ("keep", "dagning=daybreak/early morning=dawn; {keep,defer}"),
    ("L663898", "Q1341477"): ("keep", "energiförsörjning=energy provision/access=energy supply; {keep,defer}"),
    ("L1097215", "Q189829"): ("defer", "enstöring=loner avoiding contact; hermit=lives in seclusion — near/contested rank; {defer,keep}"),
    ("L1380689", "Q3683504"): ("keep", "erövrare=any conqueror; concept desc narrows to 'famous empire-extender' — lexeme broader, dominant referent; {keep,defer}"),
    ("L35507", "Q15951126"): ("reject", "fegis=coward; sissy/Tunte=effeminate/gender-nonconforming man — different concept; {reject,defer}"),
    ("L1418111", "Q181391"): ("keep", "flint=bald patch/spot on crown; concept=bald head/baldness — near/narrower; {keep,defer}"),
    ("L52376", "Q121842"): ("defer", "fransman=person from France; anchor mixed (en 'ethnic group'/de 'citizens'); demonym split mixed->defer; {defer,keep}"),
    ("L251553", "Q2095"): ("keep", "föda=food/sustenance=food; {keep,defer}"),
    ("L1003470", "Q25223"): ("reject", "förbundspresident=any federal president; concept=president of GERMANY specifically — broader/wrong-instance; {reject,defer}"),
    ("L1208527", "Q104776274"): ("keep", "förvåning=surprise; astonishment=very great surprise — lexeme slightly broader; {keep,defer}"),
    ("L35690", "Q1400189"): ("keep", "handläggare=case officer=Sachbearbeiter (de label exact); en 'official in charge' loose; EQUAL; {keep,defer}"),
    ("L1551883", "Q256401"): ("reject", "irritationsmoment=the cause of irritation; annoyance=the resulting feeling — lateral cause-vs-effect; {reject,defer}"),
    ("L54862", "Q273610"): ("defer", "kioskvältare=blockbuster (book/film/product); bestseller=specifically books — lexeme broader; {defer,keep}"),
    ("L32358", "Q192425"): ("defer", "kort=any card (paper/plastic); concept=postcard specifically — lexeme broader; {defer,reject}"),
    ("L588200", "Q167420"): ("defer", "kroat=person from Croatia (nationality); concept=Croats ETHNIC group; demonym split ethnic->not keep; {defer,reject}"),
    ("L32308", "Q1126160"): ("keep", "kännare=connoisseur/expert (sv label konnässör); EQUAL translation; {keep,defer}"),
    ("L43328", "Q3499072"): ("keep", "köksmästare=head of the kitchen=chef/Chefkoch; EQUAL/near; {keep,defer}"),
    ("L496793", "Q4175034"): ("keep", "lagstiftare=person or BODY that legislates; concept=individual member — lexeme broader (incl. bodies); {keep,defer}"),
    ("L247856", "Q11451"): ("keep", "lantbruk=farming incl. animal husbandry=agriculture (plants+animals per desc); EQUAL; {keep,defer}"),
    ("L1188185", "Q186192"): ("defer", "litauer=person from Lithuania; anchor MIXED (en 'ethnic group; and citizens/residents'); demonym split mixed->defer; {defer,keep}"),
    ("L1415069", "Q1092641"): ("keep", "luxemburgare=person from Luxembourg; anchor=citizens/residents (nationality, not ethnic); demonym split nationality->keep; {keep,defer}"),
    ("L1504463", "Q231043"): ("reject", "medhåll=general agreement/support; consent=granting permission (legal) — different; {reject,defer}"),
    ("L32830", "Q168473"): ("defer", "mes=tit (Paridae); gloss 'fågelart' suggests species, anchor=family — rank mismatch/contested; {defer,reject}"),
    ("L1551075", "Q1415280"): ("defer", "metrev=angling line (specific type); fishing line=general — narrower/specific; {defer,reject}"),
    ("L1504468", "Q19977811"): ("defer", "motorstopp=engine stall; breakdown=any machine/system failure — narrower/specific; {defer,reject}"),
    ("L242066", "Q188779"): ("defer", "norrman=person from Norway (nationality); concept=Norwegians ETHNIC group; demonym split ethnic->not keep; {defer,reject}"),
    ("L54454", "Q4919820"): ("defer", "nyckelord=central/important word; concept=statistical keyword (corpus) — different sense/broader; {defer,keep}"),
    ("L59175", "Q13649246"): ("keep", "osäkerhet=insecurity+uncertainty; concept=epistemic uncertainty — lexeme broader (incl. emotional); {keep,defer}"),
    ("L32757", "Q2083967"): ("reject", "plantering=the planted area/bed; concept=the act of planting — lateral place-vs-act; {reject,defer}"),
    ("L56812", "Q2105114"): ("defer", "portion of food (measured); concept=measured part/unit (general) — narrower; {defer,reject}"),
    ("L483676", "Q21196"): ("reject", "påskhelg=Easter holiday/weekend; Easter=the religious feast — lateral period-vs-feast; {reject,defer}"),
    ("L1521495", "Q738538"): ("defer", "ryckning=any involuntary muscle twitch; tic=specifically repetitive/clinical — lexeme broader; {defer,reject}"),
    ("L58886", "Q133279"): ("defer", "rygg=back of anything or anyone; concept=the body part — lexeme broader; {defer,reject}"),
    ("L245401", "Q133279"): ("defer", "rygg=back of anything or anyone; concept=the body part — lexeme broader; {defer,reject}"),
    ("L32650", "Q49542"): ("defer", "ryss=person from Russia (nationality); concept=Russians EAST SLAVIC ethnic group; demonym split ethnic->not keep; {defer,reject}"),
    ("L33626", "Q10843402"): ("defer", "simmerska=woman who swims (any); concept=competitive swimmer — lexeme broader (gender aside); {defer,keep}"),
    ("L1414147", "Q10737"): ("reject", "självmördare=the person; suicide=the act — lateral person-vs-act; {reject,defer}"),
    ("L57278", "Q82592"): ("keep", "sjöfågel=waterbird; concept sv label 'Sjöfåglar' exact; EQUAL; {keep,defer}"),
    ("L1508132", "Q1255864"): ("keep", "skoj=fun/amusement (also prank); concept=fun (enjoyment) — core EQUAL; {keep,defer}"),
    ("L1525043", "Q997267"): ("keep", "skärmytsling=skirmish (sv label exact); minor military conflict; EQUAL; {keep,defer}"),
}


def main():
    # All curated pairs come from the FULL eval set — NOT the 'suspect' scope/coherence subset, which
    # rides on the live verify cache and drifts. Membership is exactly PROPOSED, so the gold is a
    # stable, fully-labelled reference no matter how the verifier later classifies a pair.
    rows_full, truth = ab.load_eval_rows(suspect=False)
    full_by_key = {(r[0], r[3]): r for r in rows_full}
    gold_keys = list(PROPOSED)
    missing = [k for k in gold_keys if k not in full_by_key]
    assert not missing, f"curated pairs absent from the eval set: {missing}"

    # 'to_curate' still scans the suspect set for newly-contested pairs that have no curated verdict.
    susp_keys = {(r[0], r[3]) for r in ab.load_eval_rows(suspect=True)[0]}

    # per-arm majority-vote action from the ab cache, for every pair we emit or merely list.
    # Vote from the DEDUPED cache (last event per (arm, sample, lid, qid) wins, matching
    # ab_concept.load_cache): a resumed run re-appends whole batches to the jsonl, and
    # counting raw lines double-counts those pairs — enough to flip a majority.
    watch = set(gold_keys) | susp_keys
    gmap = verify.load_glosses({lid for lid, _ in watch})
    votes = collections.defaultdict(collections.Counter)
    for (arm, _s, lid, qid), (verdict, reason) in ab.load_cache().items():
        k = (lid, qid)
        if k not in watch or arm not in ARMS:
            continue
        # same inputs as ab_concept.run_arm: the gloss + de/en/sv anchor descs enable the
        # demonym→ethnic-group nudge — called gloss-blind, demonym pairs record 'keep'
        # where the real pipeline defers
        row = full_by_key.get(k)
        gloss = verify.pick_gloss(gmap, lid, row[1]) if row else None
        anchor_desc = " | ".join(d for d in row[8:11] if d) if row else None
        votes[(arm, k)][ab.predicted_action(verdict, reason, gloss, anchor_desc)] += 1
    pred = collections.defaultdict(dict)
    for (arm, k), c in votes.items():
        pred[arm][k] = c.most_common(1)[0][0]

    it = sqlite3.connect(config.ITEMS)

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


    # emit the gold, iterating in eval-candidate order (deterministic + stable across rebuilds)
    out = []
    gold_set = set(gold_keys)
    for row in rows_full:
        k = (row[0], row[3])
        if k not in gold_set:
            continue
        lid, qid = k
        actions = {a: pred[a].get(k) for a in ARMS}
        prop, rat = PROPOSED[k]
        out.append({
            "lid": lid, "qid": qid, "lang": row[1], "lemma": row[2],
            "glosses_all": all_glosses(lid) or verify.pick_gloss(gmap, lid, row[1]),
            "anchor_langs": anchor_evidence(qid),
            "human": truth[k], "base": actions["base"] or "-", "multi": actions["+multi"] or "-",
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
    print(f"wrote notes/problematic_cases.csv — {len(out)} curated gold cases keyed by (lid, qid), "
          f"full multilingual anchor evidence inline")

    # candidates to curate next: suspect pairs not yet in the gold whose arms disagree, or an arm
    # disagrees with the human log — so contested cases keep surfacing for curation (NOT in the gold).
    to_curate = []
    for k in sorted(susp_keys - set(gold_keys)):
        row = full_by_key.get(k)
        if not row:
            continue
        actions = {a: pred[a].get(k) for a in ARMS}
        vals = [v for v in actions.values() if v]
        if len(set(vals)) > 1 or any(v != truth[k] for v in vals):
            to_curate.append((row[1], row[2], k[0], k[1], truth[k], actions))
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


if __name__ == "__main__":
    main()
