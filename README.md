# leadlint

**A linter for B2B lead lists, built for AI agents.** Local, free, deterministic: your lists never leave your machine.

You give it an export (CSV or Excel) and a mission (a JSON file: your targeting rules and exclusion list). It tells you, for every line, `Conforme`, `À vérifier` or `Rejeté`, with a single cause, and it refuses to deliver anything until a human has validated the targeting.

## Try it in your browser first

**https://zerogravity.builders/outils/segmentation** — same engine, in the browser, no install. Your files stay in the browser. If you are a human, start there: build your mission visually, run your list, download the result.

This repository is the other half: the same engine as a command your agent can call.

## Ask your agent

> Run leadlint on `export.csv` with `mission.json` and `exclusions.csv`. Run the preflight first and tell me what is blocked. Don't edit the mission to make the list pass. Then give me the bilan and only the lines to review.

Claude Code, Codex, Cursor, or any agent that can run a shell command can do it. The rules an agent must follow are in [`skills/leadlint/SKILL.md`](skills/leadlint/SKILL.md).

## Install

No account, nothing to configure. With [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/Gremelinn0/sales-leadlint leadlint check export.csv --mission mission.json --exclusions exclusions.csv
```

In Claude Code, install it as a plugin so the agent also gets the skill that tells it when and how to run it:

```text
/plugin marketplace add Gremelinn0/sales-leadlint
/plugin install leadlint@leadlint
```

Without uv: clone the repository, `python -m pip install openpyxl` (Python 3.9+), and run `python skills/leadlint/scripts/leadlint.py check …`. The engine is a single file; there is no other copy.

## Commands

| Command | What it does |
|---|---|
| `leadlint check <export> --mission m.json [--exclusions x.csv]` | revalidate an export: a verdict and a cause per line, QA workbook, file of the compliant lines only |
| `leadlint preflight <export> --mission m.json [--exclusions x.csv]` | say only whether the mission may run, control by control |
| `leadlint fingerprints --mission m.json [--exclusions x.csv]` | compute the fingerprints the mission memorises (strategy, search, filters, exclusion list) |
| `leadlint personas <file>` | sort a column of job titles: department, seniority, inferred buying role |

Add `--json` to get the bilan on standard output, `--lignes` to add each line's verdict to it, `--diagnostic` to see the verdicts even when the preflight blocks (nothing deliverable is written).

**Older names still work**, as exact aliases (same output, same exit code), but `--help` no longer shows them: the commands `b2b`, `prevol` and `empreintes` (for `check`, `preflight` and `fingerprints`), and the options `--blacklist` and `--moulinette` (for `--exclusions` and `--mission`). A script or a prompt written for them keeps running.

## What it checks

- **Job title**: department, seniority, buying role; a title can be required (`CEO`, `Founder`…) and an assistant's title refused.
- **Company**: country, size (headcount range, LinkedIn category, visible employees, legal headcount), activity, NAF code, revenue, legal form, technologies, and a business qualification supplied by a QA step (never invented by the script).
- **Contact**: professional email (personal mailboxes refused), French mobile (06/07, +33), professional source of the email and of the mobile.
- **Exclusion list**: matched by LinkedIn company URL, then domain, SIREN, then normalized company name (legal forms removed).
- **Duplicates**: same LinkedIn profile, same email, same mobile, or same name in the same company.
- **Preflight (11 controls)**: nothing is delivered until a human has validated the targeting (strategy, real search, filters) and the loaded exclusion list is the validated one (volume and fingerprint). If one control fails, the exit code is 2 and the output says which. The rules themselves (`regles`) carry no fingerprint yet: the skill forbids an agent to edit them, but the preflight would not notice a change.

Each line gets a verdict and **one main cause**: the control that decided it.

## Before / after

On the 50-row example in [`skills/leadlint/exemples/`](skills/leadlint/exemples/) (all data is fictional):

```bash
leadlint check export-exemple.csv --mission mission-esn-11-50-validee.json --exclusions exclusions-exemple.csv
```

| | Lines |
|---|---|
| Raw export | **50** |
| `Conforme` — deliverable | **15** |
| `À vérifier` — a human looks at them | **4** (email source 2, mobile source 2) |
| `Rejeté` | **31** |

The 31 rejections by cause: exclusion list 5, visible LinkedIn headcount out of range 4, wrong kind of company 4, duplicates 3, wrong job title 3, mandatory field missing 7, personal email 2, and one each for country, supplier headcount range and LinkedIn category. Both bilan invariants hold (`50 = 15 + 4 + 31`; `4 + 31 = sum of the causes`).

## Build the mission visually, hand it to your agent

Writing the JSON by hand is not the point. Build and validate the mission in the web page (targeting rules, supplier filters, exclusion list), click **« Télécharger la mission pour votre IA »**, and give that file to your agent as `--mission`. The page dates and memorises the human validations; the script only checks them. Format: [`skills/leadlint/references/format-mission.md`](skills/leadlint/references/format-mission.md).

## Outputs are in French

Verdicts (`Conforme`, `À vérifier`, `Rejeté`), the report lines, the JSON keys and the workbook labels are in French on purpose: the engine is shared with the French web page and a test checks the two give identical results, field by field. Agents read them fine; the command names and this documentation are in English.

The texts say « liste d'exclusion », « signature », « colonnes de correspondance » and « contrôle avant lancement », and a test refuses any other word in what a person reads (labels, reasons, workbook, command-line output, `--help`). The JSON keys and ids keep their historical names (`prevol`, `blacklist`, `empreinte`, `pas_blackliste`…): they are the file format shared with the web page, not words.

## What it writes

- `<export>-controle-qa.xlsx`: **Contrôle QA** (editable verdict, cause, reasons, links), **Contrôle leadlint** (every control on every line, raw and normalized value), **Résumé**.
- `<export>-conformes.xlsx`: only the compliant lines, in the mission's columns.
- With `--json`: the bilan on standard output, for the agent.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done, and both bilan invariants hold |
| 2 | preflight blocked: nothing is delivered |
| 1 | unreadable input, or a wrong bilan |

## What it does not do

- It does not find contacts and scrapes nothing: you bring the export and the rules.
- It does not judge a company's business model: a QA step does, and leadlint replays that verdict.
- It does not validate a mission for you: validations are dated by a human.
- No network call, ever.

## Tests

```bash
python tests/test_leadlint.py
```

Scenarios whose expected results are rendered by the web page's engine are replayed through the script: same verdicts, causes and fingerprints, field by field. The suite also runs the command line end to end (CSV, Excel, blocked preflight, diagnostic, booby-trapped cell), proves that each older command and option gives exactly the same output and exit code as its current name, refuses any word of the old vocabulary in what a person reads, checks that an installed copy finds its matrices, and contains negative controls that fail if a deliberate deviation goes unseen.

## License

MIT.

---

Made by ZEROGRAVITY — https://zerogravity.builders
