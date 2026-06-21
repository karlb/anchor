#!/usr/bin/env python3
"""Export candidates to a browsable HTML table for human review.

Joins the candidate stream with the anchor Q's CC0 label/description (and any
verifier verdict we already have) and writes a self-contained HTML file you can
open in a browser. Defaults to the High tier (clean ∩ A: >=2 anchors ∩
B: monosemous target ∩ D: concrete anchor with P31) — the ~95.9%-precision slice.

    uv run export_review.py                 # High tier -> data/review.html
    uv run export_review.py --all-clean     # all clean candidates
    uv run export_review.py --out foo.html
"""

import argparse
import html
import sqlite3

import config
import gates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=config.DATA + "/review.html")
    ap.add_argument("--all-clean", action="store_true",
                    help="export all clean candidates, not just the High tier")
    args = ap.parse_args()

    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ? AS cn", [config.CANDIDATES])
    conn.execute("ATTACH DATABASE ? AS it", [config.ITEMS])
    conn.execute("ATTACH DATABASE ? AS vf", [config.VERIFY])

    gate = "" if args.all_clean else (
        " AND c.support_clusters>=2 AND c.target_n_senses=1"
        " AND EXISTS(SELECT 1 FROM it.item_type t WHERE t.qid=c.anchor_qid AND t.prop='P31')"
        " AND NOT " + gates.exclusion_clause("c", "it"))

    rows = conn.execute(f"""
        SELECT c.target_lang, c.target_lemma, c.target_lid, c.anchor_qid, c.support_clusters,
               c.target_n_senses,
               i.label_de, i.label_en, i.label_sv, i.desc_de, i.desc_en, i.desc_sv,
               v.verdict, v.reason
        FROM cn.candidate c
        LEFT JOIN it.item i ON i.qid = c.anchor_qid AND NOT i.missing
        LEFT JOIN vf.verdict v ON v.target_lid = c.target_lid AND v.anchor_qid = c.anchor_qid
        WHERE c.homograph_n_lid=1 AND NOT c.cross_pivot_conflict{gate}
        ORDER BY c.support_clusters DESC, c.target_lang, c.target_lemma
    """).fetchall()

    def pick(lang, vals):
        """First non-empty value, preferring the target language then en/de/sv.
        Returns (text, shown_lang) so a fallback language can be tagged."""
        order = [lang] + [x for x in ("en", "de", "sv") if x != lang]
        for code in order:
            if vals.get(code):
                return vals[code], code
        return "", None

    def cell(lang, labels, descs):
        l, llang = pick(lang, labels)
        d, dlang = pick(lang, descs)
        tag = lambda c: f" <span class=fl>[{c}]</span>" if c and c != lang else ""
        lab = f"<b>{html.escape(l)}</b>{tag(llang)}" if l else "<span class=d>(no label)</span>"
        dsc = f"<span class=d>{html.escape(d)}{tag(dlang)}</span>" if d else ""
        return f"{lab}<br>{dsc}"

    tr = []
    for (lang, lemma, lid, qid, support, nsenses,
         lde, len_, lsv, dde, den, dsv, verdict, reason) in rows:
        labels = {"de": lde, "en": len_, "sv": lsv}
        descs = {"de": dde, "en": den, "sv": dsv}
        anchor = cell(lang, labels, descs)
        vclass = {"yes": "yes", "no": "no", "uncertain": "unc"}.get(verdict, "")
        vcell = f"<span class='v {vclass}'>{html.escape(verdict)}</span>" if verdict else ""
        rtxt = html.escape(reason or "")
        lurl = f"https://www.wikidata.org/wiki/Lexeme:{lid}"
        qurl = f"https://www.wikidata.org/wiki/{qid}"
        tr.append(
            f"<tr><td class=lg>{html.escape(lang)}</td>"
            f"<td class=lm><a href='{lurl}' target=_blank>{html.escape(lemma)}</a>"
            f"<br><span class=d>{html.escape(lid)}</span></td>"
            f"<td><a href='{qurl}' target=_blank>{html.escape(qid)}</a></td>"
            f"<td>{anchor}</td>"
            f"<td class=n>{support}</td>"
            f"<td>{vcell}<br><span class=d>{rtxt}</span></td></tr>")

    title = ("High tier (clean ∩ ≥2 anchors ∩ monosemous ∩ concrete)"
             if not args.all_clean else "All clean candidates")
    doc = f"""<!doctype html><meta charset=utf-8>
<title>P5137 candidates — {title}</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;color:#222}}
 h1{{font-size:1.1rem}} .sub{{color:#666;margin-bottom:1rem}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border-bottom:1px solid #e3e3e3;padding:6px 9px;text-align:left;vertical-align:top}}
 th{{position:sticky;top:0;background:#fafafa;border-bottom:2px solid #ccc}}
 .lg{{color:#888;text-transform:uppercase;font-size:.8rem}}
 .lm{{font-weight:600}} .d{{color:#666;font-size:.86rem}} .n{{text-align:center}}
 .fl{{color:#aaa;font-size:.72rem;font-weight:400}}
 .v{{font-weight:700;font-size:.8rem;text-transform:uppercase}}
 .yes{{color:#197a19}} .no{{color:#b00}} .unc{{color:#b8860b}}
 a{{color:#3366cc;text-decoration:none}} tr:hover{{background:#f6f9ff}}
</style>
<h1>P5137 anchor-propagation candidates — {title}</h1>
<div class=sub>{len(rows):,} candidates. Anchor column shows the predicted Q's own CC0
label + description (the verification target — no DBnary gloss). Verdict column populated where
the verifier has been run.</div>
<table><thead><tr>
 <th>lang</th><th>lemma</th><th>→ Q</th><th>anchor concept (CC0 label / description)</th>
 <th>anchors</th><th>verifier</th></tr></thead>
<tbody>
{chr(10).join(tr)}
</tbody></table>
"""
    with open(args.out, "w") as f:
        f.write(doc)
    print(f"wrote {len(rows):,} candidates -> {args.out}")
    conn.close()


if __name__ == "__main__":
    main()
