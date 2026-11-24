# AFRO-Preparedness-Validation-data-workflow

Author: Gorgui Ba Toure (WHO AFRO GIS Centre)

Pipeline for the **SIA Preparedness Validation** ONA forms (readiness of districts / health facilities before a campaign: "Preparedness Validation", "Liste de vérification pour la préparation du AVS", "Validação da preparação", "SIA Preparedness Checklist"), built like the team supervision / LQAS / IM pipelines: Python fetch, schema report, R processing,
quality control, push to the SharePoint *Data Repository* folder, data dictionary. **Config-driven**: one row per ONA form in
`config/forms.csv`, so a country or a new version of a form is added by editing that file.

## Status

| Step | State |
|---|---|
| `config/forms.csv` (every Preparedness Validation form the token can see, one row each) | done |
| `fetch_ona_data.py` (submissions + the questions of each form) | done |
| `tools/inspect_forms.py` (which questions each country's form has, share filled, same-template groups) | done |
| processing, clean / standard geography, QC, dictionary, SharePoint push | built after the schema report has been read (the forms are in several languages and versions) |

## Run

```
pip install requests pandas pyarrow
python fetch_ona_data.py --definitions-only      # quick: only the questions of each form
python fetch_ona_data.py                         # submissions of every enabled form
python tools/inspect_forms.py                    # writes reports/form_templates.txt, form_schema_matrix.csv ...
Rscript run_pipeline.r                           # the same steps in one go
Rscript run_pipeline.r --countries ETH,TZA       # only some countries
```

## Processing (`process_preparedness.py`)

One record = one supervision visit of one place, answered with about 45 Yes / No / NA questions in four sections (microplan, cold chain and logistics, social mobilisation, training). The 38 country forms (two main templates and four special ones) become one table.

- Questions spelled differently in some forms get one standard name; Yes / No / NA are standardised (English, French, Portuguese).
- Campaign and round follow `data/lookup/campaign_lookup.xlsx` (copy of the admin data lookup). The checklist is filled before the round, so a missing or invalid campaign is the next round of the country starting in the lookup. A typed name that should map to a lookup campaign can be added by hand in `config/campaign_aliases.csv` (iso3, typed_name, lookup_name).
- Level of the place: from the form's "level validated" question when it exists, else the deepest place name filled (`level_source` says which).
- Every visit is kept; only exact duplicates are removed (listed in `data/final/afro_preparedness_repository_removed_duplicates.csv`). `is_latest_of_place` marks the last visit of each place and round; the summary table uses these.
- Readiness = share of "ready" answers among Yes/No answers, per section and overall ("vials with VVM at discard point" is ready when the answer is No).
- Names of monitors are not kept, only the group (national, regional, district, sub-district, team supervisor, partner).

Outputs: `data/final/afro_preparedness_repository.csv` (not in git), `afro_preparedness_country_round.csv` (country x campaign x round x level: places, readiness, % Yes per question), `data/metadata/AFRO_preparedness_dictionary.xlsx`, `reports/qc_summary.json`.

Run: `python process_preparedness.py` (about 2 minutes) or `Rscript run_pipeline.r --skip-fetch`.

## Tokens (never mixed)

| Country | Variable in `config/secrets.env` |
|---|---|
| Nigeria | `ONA_API_TOKEN_NIE` (no Nigeria Preparedness Validation form was found with either token; add a row when its form id is known) |
| every other country | `ONA_API_TOKEN` |

Each row names its token in `token_env`; there is no fallback from one token to the other. Copy `config/secrets.env.example`
to `config/secrets.env` and fill it in (git-ignored; on Windows check it is not saved as `secrets.env.txt`).

## `config/forms.csv`

Columns: `form_id, country, iso3, name, variant, language, token_env, min_date, enabled, submissions_on_ona, last_submission, note`.
Built from the list of forms visible to `ONA_API_TOKEN` (`python tools/list_forms.py --token-env ONA_API_TOKEN`). `enabled` is
FALSE for forms with no submission. Several rows can share a country (a form per vaccine, an old and a new version, a
language): `variant` tells them apart. `note` flags what to confirm (CAE read as Cameroon, MAL read as Mali).

## Add or change a form

1. Add / edit a row in `config/forms.csv` (use `ONA_API_TOKEN` for any country other than Nigeria).
2. `python fetch_ona_data.py --form-ids <id>` then `python tools/inspect_forms.py`.
