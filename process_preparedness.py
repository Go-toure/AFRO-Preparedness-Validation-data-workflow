#!/usr/bin/env python3
"""
process_preparedness.py   (step 2 of run_pipeline.r)
Author: Gorgui Ba Toure

SIA Preparedness Validation checklist = a supervisor visits a place (national, region, district, health facility) before the round and answers
about 45 Yes / No / NA questions in four sections: microplan, cold chain and logistics, social mobilisation, training. One record = one visit.
This step puts the 38 country forms (several versions and languages) in ONE table.

What it does
  1  reads data/raw/<iso3>_<form_id>.parquet of every enabled row of config/forms.csv
  2  gives each question one standard name (the same question is spelled differently in some forms)
  3  cleans the date, the level of the place, the geography, the supervisor designation, the round (Rnd1, Rnd2 ...) and the campaign name
     (the campaign lookup is the authority; this checklist is filled BEFORE the round, so a missing campaign is found from the NEXT round that starts)
  4  keeps every visit (only exact duplicates are removed) and marks the last visit of each place and round (is_latest_of_place)
  5  computes the readiness of each visit: share of Yes among the answered questions (a Yes on "vials at discard point" counts as NOT ready), per section and overall
  6  writes the repository, the summary by country x campaign x round x level, the quality summary and the dictionary

Outputs (data/final/*repository*.csv stays out of git)
  data/final/afro_preparedness_repository.csv      one row per visit
  data/final/afro_preparedness_country_round.csv   country x campaign x round x level: visits, places, readiness, % Yes per question (last visit of each place)
  data/final/afro_preparedness_repository_removed_duplicates.csv
  data/metadata/AFRO_preparedness_dictionary.xlsx, reports/qc_summary.json

Usage:  python process_preparedness.py        (run from the project folder)
"""
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
FINAL = HERE / "data" / "final"
META = HERE / "data" / "metadata"
REP = HERE / "reports"
LOOKUP_CANDIDATES = [HERE / "data" / "lookup" / "campaign_lookup.xlsx", HERE / "data" / "lookup" / "lookup.xlsx"]

MAX_NUMBER = 5_000_000                     # a count of vials / children above this is a typing error
DOSES_OK = {1, 2, 5, 10, 20, 25, 50, 100}
LEVEL_MAP = {"national": "National", "country": "National", "regional": "Regional", "region": "Regional", "state": "Regional", "province": "Regional",
             "district": "District", "county": "District", "sub-district": "Facility", "subdistrict": "Facility", "sub-county": "Facility",
             "subcounty": "Facility", "ward": "Facility", "facility": "Facility", "health facility": "Facility"}
LEVEL_ORDER = ["National", "Regional", "District", "Facility"]


def log(msg):
    print(f"[process_preparedness] {msg}", flush=True)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def key(s):
    """name for matching: no accents, lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower()) if isinstance(s, str) else ""


def clean_name(s):
    """a place name the way the admin data writes it: capitals, single spaces, no underscores."""
    if not isinstance(s, str):
        return ""
    t = re.sub(r"[_\s]+", " ", s.strip())
    return t.upper()


def parse_dates(s, utc=False):
    """dates in ONA come in mixed forms (2021-05-06, 2021-05-06T11:26:22, with or without a zone, or 06/05/2021): read each one on its own."""
    t = pd.Series(s).astype(str).str.strip().replace({"nan": "", "None": "", "NaT": ""})
    a = pd.to_datetime(t, format="ISO8601", errors="coerce", utc=utc)
    miss = a.isna() & (t != "")
    if miss.any():
        b = pd.to_datetime(t[miss], format="mixed", dayfirst=True, errors="coerce", utc=utc)
        a = a.fillna(b)
    return a


def short_columns(df):
    """one column per question name (the last part of 'group/question', repeat indices removed); the first non-empty value wins."""
    groups = {}
    for c in df.columns:
        groups.setdefault(re.sub(r"\[\d+\]", "", str(c)).split("/")[-1], []).append(c)
    out = {}
    for name, cols in groups.items():
        s = df[cols[0]]
        for c in cols[1:]:
            s = s.where(s.astype(str).str.strip() != "", df[c])
        out[name] = s
    return pd.DataFrame(out)


def first_text(df, names):
    s = pd.Series("", index=df.index, dtype=object)
    for n in names:
        if n in df.columns:
            v = df[n].astype(str).str.strip().replace({"nan": "", "None": ""})
            s = s.where(s != "", v)
    return s


def first_number(df, names):
    s = pd.Series(np.nan, index=df.index, dtype=float)
    for n in names:
        if n in df.columns:
            s = s.fillna(pd.to_numeric(df[n].astype(str).str.replace(",", ".").str.strip(), errors="coerce"))
    return s


def norm_round(x):
    """Rnd1, RND 1, 'round 1', '1' -> Rnd1; anything else (a campaign name typed in the round question) -> ''."""
    if not isinstance(x, str):
        return ""
    t = x.strip()
    m = re.fullmatch(r"(?i)(?:rnd|round|rd|r|passage|tour)?\s*[-_]?\s*(\d{1,2})", t)
    return "Rnd%d" % int(m.group(1)) if m else ""


COUNTRY_ALIAS = {"tanzania": "unitedrepublicoftanzania", "unitedrepublicoftanzania": "unitedrepublicoftanzania", "drc": "democraticrepublicofthecongo"}


def ckey(s):
    k = key(s)
    return COUNTRY_ALIAS.get(k, k)


def read_aliases():
    """config/campaign_aliases.csv (iso3, typed_name, lookup_name): a typed campaign name that is not in the lookup, mapped by hand to the lookup's OBR Name."""
    p = HERE / "config" / "campaign_aliases.csv"
    if not p.exists():
        return {}
    a = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
    return {(r.iso3.strip().upper(), key(r.typed_name)): r.lookup_name.strip() for r in a.itertuples() if r.typed_name.strip() and r.lookup_name.strip()}


def apply_aliases(df):
    al = read_aliases()
    if not al:
        return
    m = pd.Series([al.get((i, key(r))) for i, r in zip(df["iso3"], df["response"])], index=df.index)
    df.loc[m.notna(), "response"] = m[m.notna()]
    log(f"campaign aliases applied: {int(m.notna().sum()):,} rows")


def read_lookup():
    for p in LOOKUP_CANDIDATES:
        if p.exists():
            L = pd.read_excel(p)
            L = L[L["OBR Name"].notna()].copy()
            L["_ck"] = L["Country"].map(ckey)
            L["_rk"] = L["OBR Name"].map(key)
            L["_d"] = pd.to_datetime(L["Round Start Date"], errors="coerce")
            L["_rn"] = L["Round Number"].map(lambda x: int(re.search(r"(\d+)", str(x)).group(1)) if re.search(r"(\d+)", str(x)) else np.nan)
            log(f"lookup: {p.name} ({len(L)} rounds)")
            return L
    log("lookup not found in data/lookup/: campaign names are only cleaned, not matched")
    return None



# ------------------------------------------------------------------------------------------------ the checklist
# standard question -> (section, standard name). Names that differ between forms are listed together.
SECTIONS = {
    "Microplan": ["microplan_done", "address_H2R", "microplan_ontime", "sketch_maps", "MP_budget", "spreadsheet_ontiime", "fund_ontime", "admin_commitment",
                  "activity_plan_available", "rh_available", "Itinari_available"],
    "ColdChain_Logistics": ["VMA_available", "refrigerator_maintain", "adequate_freeziing_capacity", "VVM_for_discard", "supply_ontime", "adequate_vaccine_droppers",
                            "adequate_Fmarker", "adequate_chalk", "adequate_tallysheet", "adequate_carrier", "adequate_masks_sanitizers", "adequate_transport",
                            "vaccine_dist_plan", "VitA_Quantity", "Albendazole_qty", "VitA_pouchi", "VitAScissors"],
    "SocialMobilization": ["functional_SM", "SM_activityPlan", "Campaign_announcement_started", "SpecialStrategies_H2R", "KeyFigure_informed", "PlansPerSchedure",
                           "SocMob_Funds", "AdvocacyMeet", "Xbordermeet", "CommunityMeet"],
    "Training": ["SupervisorsTeam_TrainedSeperately", "Socmob_TrainedSeperately", "Small_group_training", "Agenda_rationale", "Agenda_Mapping", "Agenda_marking",
                 "Agenda_recording", "Agenda_keyIECmessages", "Agenda_VaccineRetrival", "Agenda_afpcase", "Agenda_demonstrations", "Training_completed", "printed_material"],
}
ITEM_SECTION = {q: s for s, qs in SECTIONS.items() for q in qs}
ITEMS = [q for qs in SECTIONS.values() for q in qs]
SYNONYM = {"County_commitment": "admin_commitment", "district_commitment": "admin_commitment", "adeduate_carrier": "adequate_carrier",
           "adequate_Carriers": "adequate_carrier", "adequate_mOPV2": "adequate_vaccine_droppers", "adequate_bOPV2": "adequate_vaccine_droppers",
           "adequate_bOPV": "adequate_vaccine_droppers", "adequate_nOPV2": "adequate_vaccine_droppers"}
NEGATIVE = {"VVM_for_discard"}               # "Are there vials with VVM at discard point?": Yes = a problem
YES, NO, NA = "Yes", "No", "NA"
ANSWER = {"yes": YES, "oui": YES, "sim": YES, "no": NO, "non": NO, "nao": NO, "não": NO, "na": NA, "n/a": NA, "nan": ""}
GEO_REGION = ["Region", "State"]
GEO_DISTRICT = ["District", "district", "County"]
GEO_FACILITY = ["facility", "Facility", "nameofhf", "Sub_District", "sub_district", "Ward", "ward", "other_facility", "Other_facility", "facility_other", "OTH_Facility"]
VALIDATE_LEVEL = {"national": "National", "regional": "Regional", "district": "District", "health_area": "Facility", "woreda": "Facility"}
LEVEL_ORDER = ["National", "Regional", "District", "Facility"]


def designation(code):
    """who made the visit, in 6 groups, from the code of the question 'select your designation'."""
    c = str(code or "").strip().lower()
    if not c:
        return ""
    if any(t in c for t in ("who", "unicef", "cdc", "afinet", "iqvia", "gpei", "king", "partner", "bmgf", "usaid", "rotary", "msf")):
        return "Partner"
    if "national" in c:
        return "National"
    if any(t in c for t in ("region", "regsup", "ras", "zone", "provinc")):
        return "Regional"
    if any(t in c for t in ("district", "distcord", "wored", "woreda", "chief")):
        return "District"
    if any(t in c for t in ("subdistrict", "healtharea", "health_area", "responsavel", "sub_district")):
        return "Sub-district / health area"
    if any(t in c for t in ("team", "equipe", "stop")):
        return "Team supervisor"
    return "Other"


def answers(d, name):
    s = d[name].astype(str).str.strip().str.lower().map(lambda v: ANSWER.get(v, v))
    return s.where(s.isin([YES, NO, NA]), "")


def load_form(row):
    pq = RAW / f"{row['iso3']}_{row['form_id']}.parquet"
    if not pq.exists():
        return None
    d = short_columns(pd.read_parquet(pq))
    out = pd.DataFrame(index=d.index)
    out["record_uuid"] = d["_uuid"] if "_uuid" in d.columns else (d["uuid"] if "uuid" in d.columns else "")
    out["country"], out["iso3"], out["form_id"], out["language"] = row["country"], row["iso3"], int(row["form_id"]), row.get("language", "")
    out["form_name"] = row["name"]
    out["submission_time"] = parse_dates(d["_submission_time"], utc=True) if "_submission_time" in d.columns else pd.NaT
    out["_today"] = parse_dates(first_text(d, ["today"]))
    out["_form_date"] = parse_dates(first_text(d, ["Date_of_Reporting_Updates"]))
    out["date_typed"] = out["_form_date"].fillna(out["_today"])
    out["response_reported"] = first_text(d, ["Response", "response_ID"]).str.replace(r"\s+", " ", regex=True).str.strip()
    out["round_raw"] = first_text(d, ["roundNumber", "Camp_Rnd"])
    out["region_raw"], out["district_raw"], out["facility_raw"] = first_text(d, GEO_REGION), first_text(d, GEO_DISTRICT), first_text(d, GEO_FACILITY)
    out["settlement"] = first_text(d, ["SettlementName"]).map(clean_name)
    out["validate_loc"] = first_text(d, ["Validate_LOC"])
    out["monitor_code"] = first_text(d, ["Monitored_Level"])
    out["monitor_group"] = out["monitor_code"].map(designation)
    gps = first_text(d, ["GIS_Location"]).str.extract(r"^\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")
    out["latitude"], out["longitude"] = pd.to_numeric(gps[0], errors="coerce"), pd.to_numeric(gps[1], errors="coerce")
    out["days_supplies_before_campaign"] = first_number(d, ["Days2Campaign"])
    out["comments"] = first_text(d, ["comments"])
    out["min_date"] = pd.to_datetime(row.get("min_date") or "2019-01-01", errors="coerce")
    # the checklist: one standard column per question, Yes / No / NA / empty
    found = {}
    for c in d.columns:
        std = SYNONYM.get(c, c)
        if std in ITEM_SECTION:
            a = answers(d, c)
            found[std] = a.where(a != "", found[std]) if std in found else a
    for q in ITEMS:
        out[q] = found.get(q, pd.Series("", index=d.index))
    return out


def next_round(g, rd, before=21, after=120):
    """round of the lookup table g (sorted by start) that this visit prepares: the first one starting from `before` days before the visit date."""
    starts = g["_d"].values.astype("datetime64[ns]")
    rdv = rd.values.astype("datetime64[ns]")
    pos = np.searchsorted(starts, rdv - np.timedelta64(before, "D"), side="left")
    pos_c = np.clip(pos, 0, len(g) - 1)
    gap = (starts[pos_c] - rdv) / np.timedelta64(1, "D")
    return pos_c, (pos < len(g)) & (gap <= after)


def clean(df, lk):
    F = ["implausible_date", "date_from_submission", "no_geography", "round_not_a_round", "campaign_not_in_lookup", "campaign_from_lookup_date",
         "few_answers", "all_yes"]
    flags = pd.DataFrame(False, index=df.index, columns=F)
    sub_d = df["submission_time"].dt.tz_convert(None).dt.normalize()
    cand = df["date_typed"]
    ok = cand.notna() & (cand >= df["min_date"]) & (cand <= sub_d + pd.Timedelta(days=1)) & (cand >= sub_d - pd.Timedelta(days=366))
    df["report_date"] = cand.where(ok, sub_d)
    df["date_source"] = np.where(ok, "form", "submission")
    flags["date_from_submission"] = ~ok
    df["report_year"] = df["report_date"].dt.year
    # place and level
    for c, src in (("region", "region_raw"), ("district", "district_raw"), ("facility", "facility_raw")):
        df[c] = df[src].map(clean_name)
    lv = df["validate_loc"].str.strip().str.lower().map(VALIDATE_LEVEL)
    guess = np.where(df["facility"] != "", "Facility", np.where(df["district"] != "", "District", np.where(df["region"] != "", "Regional", "National")))
    df["level"] = lv.where(lv.notna(), pd.Series(guess, index=df.index))
    df["level_source"] = np.where(lv.notna(), "form", "geography")
    flags["no_geography"] = ((df["level"] == "Regional") & (df["region"] == "")) | ((df["level"] == "District") & (df["district"] == "")) | \
                            ((df["level"] == "Facility") & (df["facility"] == "") & (df["settlement"] == ""))
    # round and campaign
    df["round"] = df["round_raw"].map(norm_round)
    flags["round_not_a_round"] = (df["round_raw"] != "") & (df["round"] == "")
    df["response"] = df["response_reported"]
    df["campaign_source"] = np.where(df["response"] != "", "reported", "")
    df["campaign_start"] = pd.NaT
    if lk is not None:
        L = lk.copy()
        names = {k: g.iloc[0]["OBR Name"] for k, g in L.groupby(["_ck", "_rk"])}
        apply_aliases(df)
        ck = df["country"].map(ckey)
        rk = df["response"].map(key)
        std = pd.Series([names.get((c, r), "") for c, r in zip(ck, rk)], index=df.index)
        in_lk = std != ""
        df.loc[in_lk, "response"] = std[in_lk]
        df.loc[in_lk, "campaign_source"] = "lookup_name"
        for (c, r), idx in df[in_lk].groupby([ck[in_lk], rk[in_lk]]).groups.items():
            g = L[(L["_ck"] == c) & (L["_rk"] == r)].sort_values("_d").reset_index(drop=True)
            rows = df.loc[idx]
            pos, use = next_round(g, rows["report_date"], 21, 150)
            rnd = np.where(use, g["_rn"].values[pos], np.nan)
            rep = rows["round"].map(lambda x: int(x[3:]) if x else np.nan).values
            sel = np.where(np.isin(rep, g["_rn"].values), rep, rnd)           # a typed round that exists in the campaign wins over the date
            starts = g.set_index("_rn")["_d"].to_dict()
            df.loc[idx, "round"] = ["Rnd%d" % int(x) if x == x else "" for x in sel]
            df.loc[idx, "campaign_start"] = pd.Series([starts.get(x, pd.NaT) if x == x else pd.NaT for x in sel], index=idx)
        pattern = df["response"].str.match(r"^[A-Za-z]{3}-\d{4}-\d{2}-\d{2}")
        todo = (~in_lk) & ((df["response"] == "") | ~pattern)
        for c, idx in df[todo].groupby(df.loc[todo, "country"].map(ckey)).groups.items():
            g = L[L["_ck"] == c].sort_values("_d").reset_index(drop=True)
            if g.empty:
                continue
            pos, use = next_round(g, df.loc[idx, "report_date"], 21, 120)
            if not use.any():
                continue
            sel = g.iloc[pos[use]]
            ii = idx[use]
            df.loc[ii, "response"] = sel["OBR Name"].values
            df.loc[ii, "round"] = ["Rnd%d" % int(x) if x == x else "" for x in sel["_rn"].values]
            df.loc[ii, "campaign_start"] = sel["_d"].values
            df.loc[ii, "campaign_source"] = "lookup_date"
            flags.loc[ii, "campaign_from_lookup_date"] = True
        flags["campaign_not_in_lookup"] = (df["response"] != "") & (df["campaign_source"] == "reported")
    df["campaign_start"] = pd.to_datetime(df["campaign_start"], errors="coerce")
    # readiness
    ready = pd.DataFrame(np.nan, index=df.index, columns=ITEMS)
    for q in ITEMS:
        a = df[q]
        r = np.where(a == YES, 0.0 if q in NEGATIVE else 1.0, np.where(a == NO, 1.0 if q in NEGATIVE else 0.0, np.nan))
        ready[q] = r
    df["items_answered"] = ready.notna().sum(axis=1)
    df["items_ready"] = ready.sum(axis=1)
    df["readiness_pct"] = (df["items_ready"] / df["items_answered"] * 100).where(df["items_answered"] > 0).round(1)
    for s, qs in SECTIONS.items():
        cols = [q for q in qs]
        n = ready[cols].notna().sum(axis=1)
        df["ready_" + s] = (ready[cols].sum(axis=1) / n * 100).where(n > 0).round(1)
    flags["few_answers"] = df["items_answered"] < 10
    flags["all_yes"] = (df["items_answered"] >= 20) & (df["readiness_pct"] == 100)
    df["qc_flags"] = flags.apply(lambda r: ";".join(c for c in flags.columns if r[c]), axis=1)
    return df, flags, ready


def dedupe(df):
    k = (df["iso3"] + "|" + df["response"].map(key) + "|" + df["round"] + "|" + df["level"] + "|" + df["region"].map(key) + "|" + df["district"].map(key) + "|" +
         df["facility"].map(key) + "|" + df["settlement"].map(key))
    ok = (df["response"] != "") & (df["round"] != "") & ~df["qc_flags"].str.contains("no_geography")
    k = k.where(ok, df["record_uuid"].astype(str))
    df = df.assign(_k=k).sort_values(["_k", "submission_time"])
    df["visits_of_this_place"] = df.groupby("_k")["_k"].transform("size")
    df["is_latest_of_place"] = ~df.duplicated("_k", keep="last")
    sig = df["_k"] + "|" + df["report_date"].astype(str) + "|" + df["monitor_code"] + "|" + df["latitude"].astype(str) + "|" + df["longitude"].astype(str) + "|" + df[ITEMS].agg("|".join, axis=1)
    dup = sig.duplicated(keep="last")
    removed = df[dup].copy()
    removed["kept_record_uuid"] = sig[dup].map(pd.Series(df.loc[~dup, "record_uuid"].values, index=sig[~dup].values))
    return df[~dup].drop(columns="_k").sort_values(["iso3", "report_date"]).reset_index(drop=True), removed.drop(columns="_k")


REPO_COLS = (["record_uuid", "iso3", "country", "form_id", "language", "response", "response_reported", "round", "campaign_start", "campaign_source", "report_date",
              "date_typed", "report_year", "date_source", "submission_time", "level", "level_source", "region", "district", "facility", "settlement", "latitude", "longitude",
              "monitor_group", "monitor_code", "days_supplies_before_campaign"] + ITEMS +
             ["items_answered", "items_ready", "readiness_pct"] + ["ready_" + s for s in SECTIONS] +
             ["visits_of_this_place", "is_latest_of_place", "qc_flags", "comments"])


def summaries(df):
    base = df[(df["response"] != "") & (df["round"] != "") & df["is_latest_of_place"]]
    by = ["iso3", "country", "response", "round", "level"]
    allv = df[(df["response"] != "") & (df["round"] != "")].groupby(by).size().rename("visits_all").reset_index()
    a = base.groupby(by).agg(places_assessed=("record_uuid", "size"), first_visit=("report_date", "min"), last_visit=("report_date", "max"),
                             readiness_pct=("readiness_pct", "mean"), **{"ready_" + s: ("ready_" + s, "mean") for s in SECTIONS}).reset_index()
    yes = base.groupby(by)[ITEMS].agg(lambda s: (s == YES).sum() / max(1, (s.isin([YES, NO])).sum()) * 100 if s.isin([YES, NO]).any() else np.nan).add_prefix("pct_yes_").reset_index()
    a = a.merge(yes, on=by).merge(allv, on=by, how="left")
    for c in ["first_visit", "last_visit"]:
        a[c] = a[c].dt.strftime("%Y-%m-%d")
    for c in a.columns:
        if c.startswith(("readiness", "ready_", "pct_yes_")):
            a[c] = a[c].round(1)
    a["level"] = pd.Categorical(a["level"], LEVEL_ORDER, ordered=True)
    return a.sort_values(["iso3", "response", "round", "level"]).reset_index(drop=True)


def dictionary(forms, n):
    d = {"record_uuid": "ONA unique id of the submission (one visit)", "iso3": "Country ISO3", "country": "Country", "form_id": "ONA form id", "language": "Language of the form",
         "response": "Campaign name: the OBR Name of the lookup (matched on the typed name, or from the country and the NEXT round starting after the visit date when the typed name is empty or not a campaign name)",
         "response_reported": "Campaign name as typed", "round": "Round (Rnd1, Rnd2 ...): the typed round when it is one of the campaign's rounds in the lookup, else the round the visit prepares",
         "campaign_start": "Start date of that round (lookup)", "campaign_source": "reported / lookup_name / lookup_date", "report_date": "Date of the visit: the date typed in the form if plausible, else the submission date",
         "date_typed": "The date typed in the form as it was", "report_year": "Year of report_date", "date_source": "form or submission", "submission_time": "Time the submission reached ONA (UTC)",
         "level": "Level of the place assessed: National, Regional, District, Facility (from the form's validation level, else from the deepest place name filled)", "level_source": "form (the form asks which level is validated) or geography (deepest place name filled)", "region": "Region / province / state",
         "district": "District / county", "facility": "Health facility, health area, sub-district, ward", "settlement": "Settlement (some forms)", "latitude": "GPS latitude", "longitude": "GPS longitude",
         "monitor_group": "Who made the visit: National, Regional, District, Sub-district / health area, Team supervisor, Partner (WHO, UNICEF, CDC ...), Other. Names of monitors are not kept",
         "monitor_code": "Code of the designation as in the form", "days_supplies_before_campaign": "Days before the campaign the supplies arrived (some forms)",
         "items_answered": "Number of questions answered Yes or No", "items_ready": "Of these, how many are 'ready' (Yes; for 'vials with VVM at discard point' it is No)", "readiness_pct": "items_ready / items_answered x 100",
         "visits_of_this_place": "How many visits the same place, campaign, round and level had", "is_latest_of_place": "True for the last visit of that place and round (the summary table uses these)",
         "qc_flags": "Semicolon list of checks that failed", "comments": "Free-text comment of the supervisor"}
    for s in SECTIONS:
        d["ready_" + s] = "Readiness % of the section " + s
    lab = {"microplan_done": "Microplan done and available", "address_H2R": "Microplan covers hard-to-reach areas and special populations", "microplan_ontime": "Microplanning finished on time",
           "sketch_maps": "Appropriate sketch maps", "MP_budget": "Microplan has a budget", "spreadsheet_ontiime": "Tally / allocation sheet arrived on time", "fund_ontime": "Funds arrived on time",
           "admin_commitment": "Support or commitment of the administration and partners", "activity_plan_available": "Implementation plan of the activities exists", "rh_available": "List of actors available",
           "Itinari_available": "Itineraries available for each actor", "VMA_available": "Vaccine vial monitor recruited, trained, deployed", "refrigerator_maintain": "Refrigerator kept between +2 and +8 C",
           "adequate_freeziing_capacity": "Freezing capacity and frozen ice packs adequate", "VVM_for_discard": "Vials with VVM at discard point (Yes = a problem)", "supply_ontime": "Supplies arrived on time",
           "adequate_vaccine_droppers": "Adequate vaccine and droppers", "adequate_Fmarker": "Adequate markers and plastic bags", "adequate_chalk": "Adequate chalk", "adequate_tallysheet": "Adequate tally sheets",
           "adequate_carrier": "Adequate vaccine carriers", "adequate_masks_sanitizers": "Adequate masks and sanitizers", "adequate_transport": "Adequate transport", "vaccine_dist_plan": "Distribution plan available 3 days before",
           "VitA_Quantity": "Adequate vitamin A", "Albendazole_qty": "Adequate albendazole", "VitA_pouchi": "Adequate vitamin A sachets", "VitAScissors": "Adequate scissors and spoons",
           "functional_SM": "Functional social mobilisation committee", "SM_activityPlan": "Social mobilisation activity plan", "Campaign_announcement_started": "Campaign announcements started",
           "SpecialStrategies_H2R": "Hard-to-reach areas targeted with special strategies", "KeyFigure_informed": "Key stakeholders informed", "PlansPerSchedure": "Plans implemented on schedule",
           "SocMob_Funds": "Social mobilisation funds available", "AdvocacyMeet": "Advocacy meetings held", "Xbordermeet": "Cross-border meetings held", "CommunityMeet": "Community meetings planned or held",
           "SupervisorsTeam_TrainedSeperately": "Supervisors and teams trained separately", "Socmob_TrainedSeperately": "Social mobilisers trained separately", "Small_group_training": "Trainings in small groups",
           "Agenda_rationale": "Training agenda: rationale", "Agenda_Mapping": "Training agenda: mapping, cold chain, VVM", "Agenda_marking": "Training agenda: marking", "Agenda_recording": "Training agenda: recording and reporting",
           "Agenda_keyIECmessages": "Training agenda: key messages", "Agenda_VaccineRetrival": "Training agenda: vaccine retrieval", "Agenda_afpcase": "Training agenda: AFP sensitisation",
           "Agenda_demonstrations": "Training agenda: demonstrations", "Training_completed": "Recruitment and training of supervisors completed", "printed_material": "Guides, checklists and forms printed"}
    for q in ITEMS:
        d[q] = f"[{ITEM_SECTION[q]}] {lab.get(q, q)}: Yes / No / NA (empty = not asked in that form version)"
    cols = pd.DataFrame([{"Column": c, "Description": d.get(c, "")} for c in REPO_COLS])
    flags = pd.DataFrame([{"Flag": k, "Meaning": v} for k, v in {
        "date_from_submission": "The typed date is missing or impossible: submission date used", 
        "no_geography": "The level needs a place name that is empty", "round_not_a_round": "The round question holds something that is not a round number",
        "campaign_not_in_lookup": "A typed campaign name that is not in the lookup: kept as typed", "campaign_from_lookup_date": "The typed campaign was empty or not a campaign name: the next round of the country in the lookup",
        "few_answers": "Fewer than 10 questions answered", "all_yes": "20 or more questions answered and all of them ready (to check)"}.items()])
    items = pd.DataFrame([{"Question": q, "Section": ITEM_SECTION[q], "Meaning": lab.get(q, q), "Yes means": "a problem" if q in NEGATIVE else "ready"} for q in ITEMS])
    rules = pd.DataFrame({"Rule": [
        "One record = one supervision visit of one place. Several visits of the same place are normal (before and during preparation): all are kept.",
        "Only exact duplicates (same place, campaign, round, date, monitor designation, GPS point and every answer) are removed; they are listed in afro_preparedness_repository_removed_duplicates.csv.",
        "is_latest_of_place marks the last visit of each place and round. The summary table uses these: readiness and % Yes per question over places, not over visits.",
        "The checklist is filled before the round: a missing campaign is the next round starting in the lookup (from 21 days before the visit to 120 days after).",
        "Readiness = share of 'ready' answers among Yes/No answers (NA and empty are ignored). 'Vials with VVM at discard point' is ready when the answer is No.",
        "Names of monitors are not kept: only the group (monitor_group)."]})
    with pd.ExcelWriter(META / "AFRO_preparedness_dictionary.xlsx", engine="openpyxl") as xw:
        cols.to_excel(xw, sheet_name="Columns", index=False)
        items.to_excel(xw, sheet_name="Checklist", index=False)
        flags.to_excel(xw, sheet_name="QC_flags", index=False)
        forms[["form_id", "country", "iso3", "name", "variant", "language", "enabled", "submissions_on_ona", "last_submission"]].to_excel(xw, sheet_name="Forms", index=False)
        rules.to_excel(xw, sheet_name="Rules", index=False)
        pd.DataFrame({"Item": ["Author", "Built", "Visits"], "Value": ["Gorgui Ba Toure", datetime.now().strftime("%Y-%m-%d"), n]}).to_excel(xw, sheet_name="About", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(100, max(12, max(len(str(c.value or "")) for c in col[:60]) + 2))


def main():
    forms = pd.read_csv(HERE / "config" / "forms.csv", encoding="utf-8-sig", dtype=str).fillna("")
    use = forms[forms["enabled"].str.upper().isin(["TRUE", "T", "1", "YES"])]
    lk = read_lookup()
    parts = [x for x in (load_form(r) for _, r in use.iterrows()) if x is not None]
    if not parts:
        sys.exit("no raw data: run fetch_ona_data.py first")
    df = pd.concat(parts, ignore_index=True)
    n_raw = len(df)
    log(f"{n_raw:,} visits from {len(parts)} forms")
    df, flags, ready = clean(df, lk)
    df, removed = dedupe(df)
    log(f"{len(removed):,} exact duplicates removed -> {len(df):,} visits ({int(df['is_latest_of_place'].sum()):,} places and rounds)")
    for d_ in (FINAL, META, REP):
        d_.mkdir(parents=True, exist_ok=True)
    out = df[REPO_COLS].copy()
    for c in ("campaign_start", "report_date", "date_typed"):
        out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")
    out["submission_time"] = pd.to_datetime(out["submission_time"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(FINAL / "afro_preparedness_repository.csv", index=False, encoding="utf-8-sig")
    removed[["record_uuid", "kept_record_uuid", "iso3", "form_id", "response", "round", "level", "region", "district", "facility", "report_date", "submission_time"]].to_csv(
        FINAL / "afro_preparedness_repository_removed_duplicates.csv", index=False, encoding="utf-8-sig")
    country = summaries(df)
    country.to_csv(FINAL / "afro_preparedness_country_round.csv", index=False, encoding="utf-8-sig")
    dictionary(forms, len(df))
    fl = df["qc_flags"].str.split(";").explode()
    fl = fl[fl != ""].value_counts()
    unk = df[df["qc_flags"].str.contains("campaign_not_in_lookup")].groupby(["iso3", "response"]).size().rename("visits").reset_index()
    unk.to_csv(REP / "preparedness_unmatched_campaigns.csv", index=False, encoding="utf-8-sig")
    qc = {"built": datetime.now().isoformat(timespec="seconds"), "submissions_read": int(n_raw), "exact_duplicates_removed": int(len(removed)), "visits": int(len(df)),
          "places_and_rounds": int(df["is_latest_of_place"].sum()), "by_level": df["level"].value_counts().to_dict(), "by_monitor_group": df["monitor_group"].value_counts().to_dict(),
          "flags": {k: int(v) for k, v in fl.items()}, "visits_without_campaign": int((df["response"] == "").sum()), "visits_without_round": int((df["round"] == "").sum()),
          "countries": int(df["iso3"].nunique()), "mean_readiness_pct": round(float(df["readiness_pct"].mean()), 1)}
    (REP / "qc_summary.json").write_text(json.dumps(qc, indent=2), encoding="utf-8")
    log(json.dumps(qc))
    log(f"written: data/final/afro_preparedness_repository.csv ({len(out):,} rows), country_round ({len(country):,}), dictionary")


if __name__ == "__main__":
    main()
