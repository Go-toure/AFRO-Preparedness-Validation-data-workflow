#!/usr/bin/env python3
"""
inspect_forms.py   Author: Gorgui Ba Toure

Reads what fetch_ona_data.py downloaded (data/raw/*_form.json = the questions, data/raw/*.parquet = the answers) and writes
the report used to build the processing step: which questions each country's form has, how often they are filled, and which
forms are the same template.

    python tools/inspect_forms.py

Writes (reports/, git-ignored):
  form_schema_long.csv     one row per question of each form: iso3, form_id, question, type, label, group path
  form_columns_fill.csv    one row per column of the downloaded answers: iso3, form_id, column, question, filled share
  form_schema_matrix.csv   one row per question name, one column per form: share filled (blank = the form has no such question)
  form_templates.txt       forms grouped by template (same questions) with the questions that differ, and the most common questions
No answers are printed, only column names and counts.
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent
RAW = HERE / "data" / "raw"
REP = HERE / "reports"
STRUCT = {"group", "repeat", "survey", "begin group", "end group", "begin repeat", "end repeat"}


def short(col):
    """The question name of a column: the last part of the path, without repeat indices ([1])."""
    s = re.sub(r"\[\d+\]", "", str(col))
    return s.split("/")[-1]


def label_text(lab):
    if isinstance(lab, dict):
        for k in ("English", "english", "en", "French", "Portuguese"):
            if lab.get(k):
                return str(lab[k])
        for v in lab.values():
            if v:
                return str(v)
        return ""
    return "" if lab is None else str(lab)


def walk(node, path, out):
    for ch in node.get("children", []) or []:
        name, typ = ch.get("name", ""), ch.get("type", "")
        if typ in ("group", "repeat"):
            walk(ch, path + [name], out)
        elif name and typ not in STRUCT:
            out.append({"question": name, "type": typ, "label": label_text(ch.get("label"))[:160], "group": "/".join(path)})


def main():
    forms = sorted({p.name[: -len("_form.json")] for p in RAW.glob("*_form.json")} | {p.stem for p in RAW.glob("*.parquet")})
    if not forms:
        sys.exit("No downloaded form found in data/raw/: run  python fetch_ona_data.py  first.")
    REP.mkdir(exist_ok=True)
    long_rows, fill_rows, qsets = [], [], {}
    for stem in forms:
        iso3, _, fid = stem.partition("_")
        fj = RAW / f"{stem}_form.json"
        if fj.exists():
            try:
                d = json.load(open(fj, encoding="utf-8"))
                out = []
                walk(d if isinstance(d, dict) else {"children": d}, [], out)
                for q in out:
                    long_rows.append({"iso3": iso3, "form_id": fid, **q})
                qsets[stem] = {q["question"] for q in out}
            except (OSError, ValueError) as e:
                print(f"[WARN] {fj.name}: {e}")
        pq = RAW / f"{stem}.parquet"
        if pq.exists():
            df = pd.read_parquet(pq)
            n = len(df)
            for c in df.columns:
                filled = float((df[c].astype(str).str.strip() != "").mean()) if n else 0.0
                fill_rows.append({"iso3": iso3, "form_id": fid, "records": n, "column": c, "question": short(c), "filled": round(filled, 4)})
            qsets.setdefault(stem, set()).update(short(c) for c in df.columns if not str(c).startswith("_"))
    L = pd.DataFrame(long_rows)
    F = pd.DataFrame(fill_rows)
    L.to_csv(REP / "form_schema_long.csv", index=False, encoding="utf-8-sig")
    F.to_csv(REP / "form_columns_fill.csv", index=False, encoding="utf-8-sig")
    if len(F):
        F["form"] = F["iso3"] + "_" + F["form_id"].astype(str)
        M = F.groupby(["question", "form"])["filled"].max().unstack("form")
        M.insert(0, "forms_with_question", M.notna().sum(axis=1))
        M.sort_values("forms_with_question", ascending=False).to_csv(REP / "form_schema_matrix.csv", encoding="utf-8-sig")

    # templates: forms whose question sets are >= 80% alike (Jaccard) are the same template
    keys = sorted(qsets)
    groups = []
    for k in keys:
        for g in groups:
            ref = qsets[g[0]]
            if len(qsets[k] & ref) / max(1, len(qsets[k] | ref)) >= 0.8:
                g.append(k)
                break
        else:
            groups.append([k])
    lines = [f"{len(keys)} forms, {len(groups)} template group(s) (forms whose questions are at least 80% the same)", ""]
    for i, g in enumerate(groups, 1):
        common = set.intersection(*[qsets[k] for k in g])
        lines.append(f"Template {i}: {len(g)} form(s): {', '.join(g)}  | questions in all: {len(common)}")
        for k in g[1:]:
            only_k, only_ref = sorted(qsets[k] - qsets[g[0]]), sorted(qsets[g[0]] - qsets[k])
            if only_k or only_ref:
                lines.append(f"   {k} vs {g[0]}: +{len(only_k)} {only_k[:12]}  -{len(only_ref)} {only_ref[:12]}")
        lines.append("")
    if len(F):
        top = M.sort_values("forms_with_question", ascending=False).head(60)
        lines.append("Most common questions (name, forms having it):")
        lines += [f"   {q:<45} {int(r['forms_with_question'])}" for q, r in top.iterrows()]
    (REP / "form_templates.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:60]))
    print(f"\nReports written to {REP}")


if __name__ == "__main__":
    main()
