---
name: leadlint
description: "When a B2B contact list or prospecting export must be cleaned, requalified or revalidated before use, run the leadlint script: sort job titles into personas, or replay a mission's rules (company target, job title, professional email and mobile, exclusion list, duplicates) with a preflight and a numbered bilan. Everything runs locally, no data leaves the machine. Triggers: lead list, clean leads, ICP check, exclusion list, blacklist, dedupe, duplicates, Sales Navigator export, lead QA, personas, job titles, leadlint; moulinette, revalider un export, nettoyer une liste de prospection, requalifier des contacts, trier des intitulés de poste, liste d'exclusion, doublons, export Pharow, Pipecorn, contrôle QA leads."
---

# leadlint

A linter for B2B lead lists, built for AI agents. A local Python script that does two things on a contacts file (Excel or CSV), without any data leaving the machine. It lives in `scripts/leadlint.py`:

1. **`personas`**: classifies each job title (department, seniority, inferred buying role) with a keyword matrix.
2. **`check`** (French alias `b2b`): revalidates a prospecting export against the **mission** (a JSON file): every line comes out `Conforme`, `À vérifier` or `Rejeté`, with one main cause and the detail of every control.

Python 3.9+ and `openpyxl`. No network call. Verdicts, report lines, JSON keys and workbook labels are in French on purpose (the engine is shared with the French web page); read them as they are.

## When to use it

- An export comes from a search tool (Sales Navigator, Pharow, Pipecorn…) and must be checked before paid enrichment or import into a campaign.
- A purchased list or a CRM must be sorted by persona before a campaign.
- An agent must deliver a list: it goes through this script, never through an eyeballed sort.

A human who prefers a screen can use the same engine in the browser, at https://zerogravity.builders/outils/segmentation. The page also builds and validates the mission and downloads it with the button « Télécharger la mission pour votre IA »; that file is what `--mission` reads.

## The commands

From the skill folder:

```bash
python scripts/leadlint.py preflight     export.csv --mission mission.json --blacklist exclusions.csv
python scripts/leadlint.py check         export.csv --mission mission.json --blacklist exclusions.csv --json
python scripts/leadlint.py personas      contacts.xlsx --json
python scripts/leadlint.py fingerprints  --mission mission.json --blacklist exclusions.csv
```

Installed anywhere, no account needed: `uvx --from git+https://github.com/Gremelinn0/sales-leadlint leadlint check export.csv --mission mission.json --blacklist exclusions.csv`.

The French names are exact aliases (same result, same exit code): `b2b` = `check`, `prevol` = `preflight`, `empreintes` = `fingerprints`; `--moulinette` = `--mission`.

| Exit code | Meaning |
|---|---|
| 0 | done; both bilan invariants hold |
| 2 | preflight blocked: nothing is delivered (the bilan says which controls to lift) |
| 1 | unreadable input, or a wrong bilan |

`--json` writes the bilan to standard output, for an agent. `--lignes` adds each line's verdict to it.

## Rules for an agent

1. **Run `preflight` first.** If it blocks, do not work around it: report the failing controls to the human who validates the mission. The search strategy, the real search and the exclusion list are validated by a human, never by the agent: the validation date and the fingerprint (`leadlint fingerprints`) are written into the mission only on their explicit agreement.
2. **`--diagnostic`** returns the verdicts even when the preflight blocks, to understand a list; the file it produces carries `-diagnostic-` in its name and **no compliant file is written**. A diagnostic is never delivered.
3. **After `check`, copy the bilan as is**: total, compliant, to review, rejected, and every non-zero main cause. Verify `invariants`: `total = compliant + to review + rejected` and `to review + rejected = sum of the causes`.
4. **Only the `À vérifier` lines go to human review or to business QA**, with their cause. Rejected lines are not silently rescued; compliant lines are in `<export>-conformes.xlsx`, with the mission's columns and in its order.
5. **Never edit the mission to make a list pass**: changing a rule, a filter or the exclusion list invalidates their fingerprint, and the preflight will see it.

## What `check` returns

- `<export>-controle-qa.xlsx`: sheet **Contrôle QA** (one row per contact, a verdict editable as a dropdown, problem type, reasons, clickable links, a « Contrôlé le » column to fill), **Audit moulinette** (every control on every line: raw value, normalized value, source column, diagnostic) and **Résumé** (counters, rules applied, preflight state).
- `<export>-conformes.xlsx`: only the compliant lines, ready to deliver.
- Any cell that would start like a spreadsheet formula (`=`, `+`, `-`, `@`) is neutralized.

## The mission file

The same JSON the web page saves: a setup (`client`, `mission`, `configuration`) or a configuration alone. It holds the rules (criterion, operator, values, importance, policy when the data is missing), the mandatory fields, the final columns, the column mappings, the exclusion list reference, the search strategy and the filters really used. It never contains the export, the exclusion lines or a secret.

Examples in `exemples/`: `mission-esn-11-50-validee.json` (passes the preflight on `export-exemple.csv`) and `modele-mission-esn-11-50.json` (a template to adapt; it blocks until something is validated). Detail of the criteria, operators and controls: [references/format-mission.md](references/format-mission.md).

## How it classifies (`check`)

- Columns are guessed from the usual headers (French and English), then overridden by the mappings memorised in the mission when the column exists.
- Values are normalized before comparison: accents and punctuation, LinkedIn URL, domain, SIREN, French mobile 06/07, email, numbers written the French way.
- Exclusion list: matched by company LinkedIn URL, then domain, SIREN, normalized name (legal forms removed).
- Duplicates: same LinkedIn profile, same email, same mobile, or same name in the same company.
- A keyword of three letters or fewer counts only as a whole word (« IT » is not found in « efficacité »).

## Proof

`python tests/test_leadlint.py` (from the repository root): field-by-field parity with the web page's engine on scenarios rendered by that engine, the command line end to end, each English command equal to its French alias, and negative controls that require the suite to see a deliberate deviation.
