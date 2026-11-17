---
name: leadlint-setup
description: >-
  When someone wants their own cleaning script for B2B contact lists, built from a few questions about their targets,
  job titles and sectors: interview them, write their answers, generate a local script (moulinette.py) and a plain-language
  page of their rules (regles.html), have them read and validate, then run exports through it. Also when they want to see
  or change their rules afterwards. Runs on the leadlint engine: no account, no network, nothing leaves the machine.
  Triggers: set up leadlint, build my own leadlint script, create my lead cleaning script, change my LeadLint rules,
  show me my rules, install my moulinette, installe ma moulinette, crée mon script de nettoyage de contacts,
  change mes règles LeadLint, montre-moi mes règles, configure leadlint pour moi, fais-moi ma moulinette.
---

# leadlint-setup

Turns a short conversation into **the person's own cleaning script for contact lists**, written on their machine. Speak the person's language throughout (the files it writes are in French; explain them in the person's language). The engine and its rules are in [`../leadlint/SKILL.md`](../leadlint/SKILL.md); this skill only builds what sits on top.

## What it produces

A folder (default name `ma-moulinette`, in the person's workspace) that works on its own, wherever it is moved:

| File | What it is |
|---|---|
| `regles.html` | The person's rules in plain language, to read in a browser. Shows whether they are validated and the last pass. |
| `Ouvrir la moulinette.bat`, `ouvrir-la-moulinette.command` | The double-click launchers (Windows, Mac/Linux): they open the drop page |
| `moulinette.py` | Their script: no argument opens the drop page, `python moulinette.py export.csv` runs from the command line |
| `mission.json`, `matrice.json` | The rules and the targets, read by the engine |
| `reponses.json` | Their answers: **the one file to edit to change a rule** |
| `leadlint.py`, `leadlint_setup.py` | A copy of the engine, so the folder needs nothing else but Python and `openpyxl` |
| `exclusions.csv` | Their copy of the list of companies never to contact, if they have one |
| `resultats/`, `dernier-passage.json` | Written by each pass: the cleaned file with a `Cible` column, the line-by-line control, the bilan |

## The interview

Ask **one question at a time**, in plain words, with an example and a default. Five to seven questions are enough; never ask the person to write JSON.

1. **For whom?** A name for the mission and who it is for (the person or their company). Default: « Ma prospection ».
2. **Which country or countries?** (« France ») Skip the rule if they sell everywhere.
3. **Their targets (1 to 5).** A target is a kind of buyer: « Dirigeants », « Direction commerciale ». For each, ask for the job titles they want, then **propose yourself the variants** they will not think of: French and English (CEO, Founder, Gérant, Directeur général), feminine forms (Directrice commerciale, Fondatrice), common synonyms (Head of Sales, VP Sales). Titles are compared as **whole words**, so a title that is not listed is not found: « Directeur commercial » does not match « Directrice commerciale ». Let them cut or add.
4. **Titles to refuse.** Propose a default list and let them edit it: stagiaire, stage, alternant, alternante, assistant, assistante, freelance, open to work. A contact whose title holds one of these is refused.
5. **Sectors.** The words that must appear in the company's activity (« ESN », « conseil informatique ») and, if any, the words that exclude (« SaaS »). Ask for **NAF codes** only if the person knows them; never explain NAF to someone who does not.
6. **Company size.** Employees visible on LinkedIn, a minimum and/or a maximum (« 11 to 200 »). Optional.
7. **What a contact must have.** A professional email (no Gmail or Outlook)? A French mobile? And: **do they have a list of companies to never contact** (clients, deals in progress)? If yes, ask for the file (.csv or .xlsx with a company name, website, LinkedIn page or SIREN column).

If they have an export at hand, **read its header line** and compare it with the field names below: when a header is not one the engine guesses (« Intitulé » for the job title), write it in `colonnes`. Otherwise the script stops at the first pass and names the missing column.

## The answers file (`reponses.json`)

Every key is optional except `cibles`. A key that does not exist is refused, so a typo cannot silently drop a rule. A complete example: [`exemples/reponses-exemple.json`](exemples/reponses-exemple.json) (with a fictional export and exclusion list next to it).

```json
{
  "nom": "Ma prospection",
  "pour": "Name of the person or their company",
  "pays": ["France"],
  "cibles": [
    { "nom": "Dirigeants", "intitules": ["CEO", "Fondateur", "Fondatrice", "Gérant", "Directeur général"] },
    { "nom": "Direction commerciale", "intitules": ["Directeur commercial", "Directrice commerciale", "Head of Sales"] }
  ],
  "postes_exclus": ["stagiaire", "assistant", "assistante", "freelance", "open to work"],
  "secteurs": { "mots_cles": ["ESN"], "mots_exclus": ["SaaS"], "codes_naf": ["62.01Z"], "codes_naf_exclus": [] },
  "taille": { "min": 11, "max": 200 },
  "email_pro_obligatoire": true,
  "mobile_obligatoire": false,
  "liste_exclusion": true,
  "champs_obligatoires": ["nomComplet", "entreprise", "poste"],
  "colonnes": { "poste": "Intitulé" }
}
```

Field names for `colonnes` and `champs_obligatoires`: `nomComplet`, `entreprise`, `poste`, `linkedinProfil`, `linkedinEntreprise`, `pays`, `activite`, `codeNaf`, `employesVisibles`, `siteEntreprise`, `siren`, `emailProfessionnel`, `mobile` (the engine knows more; see the list in `leadlint_setup.py`). If a target holds several titles, a title that fits two targets goes to the first target of the list.

## The steps

1. **Write `reponses.json`** from the answers (in the person's workspace).
2. **Create the folder:** `create reponses.json -o ma-moulinette [--exclusions liste.csv]`. Invalid answers stop with one clear sentence and write nothing: fix the answer and run it again.
3. **Have the person read their rules.** Open `regles.html` for them (or give them its path) and walk them through the four families (company, contact, exclusions, quality) and the targets. Ask what is wrong or missing; a change goes through `reponses.json` (see below).
4. **Validate, only on their explicit « yes ».** `validate ma-moulinette [--exclusions liste.csv]` records the date and the rules' signature. **It is the person's decision, never yours:** do not run it because the rules « look fine », because they were silent, or to get a list through. The script delivers nothing until this has been done.
5. **Run an export.** Tell the person to double-click `Ouvrir la moulinette` (Windows) or `ouvrir-la-moulinette.command` (Mac), then drop their export on the page: they get the bilan and the cleaned file there, **with no agent and no terminal from now on**. For your own run: `python ma-moulinette/moulinette.py export.csv` (add `--exclusions liste.csv` if the list is not in the folder). **Give the bilan as printed**: lines in, kept, to review, rejected, causes, kept per target, and any rule not applied for lack of a column. Point them to `resultats/…-nettoye.xlsx` (the kept contacts with their `Cible`) and to `regles.html`, which now shows the last pass.

## Changing a rule

Edit `reponses.json` (never `mission.json`), run `create reponses.json -o ma-moulinette --force`, have the person read the new `regles.html`, and validate again after their « yes ». **Never edit `mission.json` by hand to make a list pass**: the script notices (« Vos règles ont changé depuis la validation ») and delivers nothing. `--force` is needed once a mission is validated, and it erases the validation on purpose.

## The commands, by installation

The same three commands everywhere: `create`, `validate`, `page` (rewrites `regles.html`).

- **Claude Code plugin:** `python "${CLAUDE_PLUGIN_ROOT}/skills/leadlint/scripts/leadlint_setup.py" create reponses.json -o ma-moulinette`
- **uv, nothing to install first:** `uvx --from git+https://github.com/Gremelinn0/sales-leadlint leadlint-setup create reponses.json -o ma-moulinette`
- **Cloned repository:** `python skills/leadlint/scripts/leadlint_setup.py create reponses.json -o ma-moulinette`
- **Once the folder exists, use its own copy**, from any mode: `python ma-moulinette/leadlint_setup.py validate ma-moulinette`.

Add `--json` to `create` and `validate` to read their result as data. Requirements: Python 3.9+ and `openpyxl` (`python -m pip install openpyxl`; `uvx` installs it).

## What it does not do

It does not find contacts and scrapes nothing: the person brings the export. It makes no network call and needs no account. It does not judge what a company really does (the activity words are a filter, not a verdict), and it does not validate anything for the person. The detail of every rule, verdict and exit code (0 done, 2 blocked, 1 unreadable) is in [`../leadlint/SKILL.md`](../leadlint/SKILL.md).
