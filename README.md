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
