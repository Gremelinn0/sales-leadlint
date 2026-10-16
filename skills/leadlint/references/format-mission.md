# The mission file

A mission = one JSON file. The JSON keys and the criterion ids (`regles`, `critereId`, `bloquant`, `pas_blackliste`…) are French on purpose: they are the schema shared with the web page, which writes the same file, so they are not translated below. A few keep a historical name (`blacklist`, `mappingBlacklist`, `empreinte`, `pas_blackliste`): they are the file format, not words. In the texts a person reads, the same things are called the exclusion list, the column matching and the signature (fingerprint).

The script accepts three shapes: a full setup (`{ "client", "mission", "configuration": {…} }`), a database row (same shape), or the `configuration` alone. An old configuration (before schema 2, with a `criteres` block) is migrated when read, as in the web page.

## The B2B configuration (`mode: "revalidation_b2b"`, `schemaVersion: 2`)

| Key | Role |
|---|---|
| `regles` | the controls applied to every line (see below) |
| `champsObligatoires` | fields that must be filled in, otherwise the line is rejected |
| `colonnesFinales` | `[{ "champ", "libelle" }]`: columns and order of the compliant-lines file |
| `mappingCandidats` | column matching of the export: `{ field: "Export header" }`; whatever is missing is guessed from the headers |
| `mappingBlacklist` | column matching of the exclusion list (`entreprise`, `linkedinEntreprise`, `siteEntreprise`, `siren`) |
| `blacklist` | reference of the exclusion list: `obligatoire`, `nom`, `volumeAttendu`, `empreinte`, `miseAJourLe` |
| `strategie` | the validated search strategy: `lignes`, `limites`, `valideeLe`, `empreinteValidee` |
| `pharow` | the real search at the data supplier: names, filters, proofs and dates (see the preflight) |

### Recognised fields

`nomComplet`, `entreprise`, `poste`, `linkedinProfil`, `linkedinEntreprise`, `categorieLinkedIn`, `employesVisibles`, `pays`, `activite`, `tagsFournisseur`, `trancheEffectifFournisseur`, `qualificationActivite`, `siteEntreprise`, `siren`, `emailProfessionnel`, `sourceEmail`, `mobile`, `sourceMobile`, `effectifLegal`, `chiffreAffaires`, `codeNaf`, `statutJuridique`, `technologies`, `departement`, `niveauHierarchique`, `anciennetePoste`.

### A rule

```json
{ "id": "r-pays", "critereId": "pays", "operateur": "est_dans", "valeur": ["France"],
  "importance": "bloquant", "siAbsent": "rejeter", "active": true }
```

| Criterion (`critereId`) | Field | Operators |
|---|---|---|
| `pays` | pays | `est_dans` |
| `activite` | activite | `contient_un`, `ne_contient_aucun`, `est_dans` |
| `tags_source` | tagsFournisseur | `est_dans`, `contient_un` |
| `tranche_effectif_source` | trancheEffectifFournisseur | `est_dans` |
| `qualification_activite` | qualificationActivite | `est_dans` (the verdict of a QA step, never invented by the script) |
| `categorie_linkedin` | categorieLinkedIn | `est_dans` (« 11-50 employees » becomes `11-50`) |
| `employes_visibles`, `effectif_legal`, `chiffre_affaires` | numeric | `entre` (`{ "min", "max" }`) |
| `code_naf`, `statut_juridique` | same | `est_dans` |
| `technologies` | technologies | `contient_un` |
| `poste` | poste | `est_dans` (accepts « CEO & Founder » for `CEO`; refuses an assistant's title), `contient_un` |
| `departement`, `niveau_hierarchique` | same | `est_dans` |
| `email_pro` | emailProfessionnel | `email_professionnel` (personal mailboxes refused) |
| `source_email` | sourceEmail | `present` |
| `mobile_fr` | mobile | `mobile_francais` (06/07, +33 accepted) |
| `source_mobile` | sourceMobile | `provenance_professionnelle` (« pro », « work »… without « perso ») |
| `blacklist` | computed | `pas_blackliste` |
| `doublon` | computed | `unique` |

- **`importance`**: `bloquant` (a failure rejects), `verification` (a failure puts the line to review), `informatif` (does not change the verdict).
- **`siAbsent`**: `rejeter`, `verifier` or `ignorer` when the value is missing from the export.
- An unknown criterion is never ignored: it returns « Non évalué » and the line cannot be compliant.
- A line's **main cause** is the control that decided its verdict: the first one that rejects, otherwise the first one that puts it to review.

## The preflight: eleven controls before any delivery

| Control | What it requires |
|---|---|
| `strategie` | a strategy with no error, dated, and unchanged since its validation (fingerprint) |
| `recherche_validee` | real search validated: names, filters, volume, 5 to 10 sample companies, screenshot, fingerprint |
| `configuration` | at least one active rule, known criteria, valid ranges and lists, final columns, every field used linked to a column |
| `blacklist` | exclusion list loaded and linked to its columns (blocking if the mission requires it) |
| `empreinte` | volume and fingerprint of the loaded list = the memorised ones |
| `liste_pharow` | name of the search and of the exclusion list filled in |
| `exclusion_pharow` | exclusion confirmed active at the supplier |
| `export_source` | export identified, and its number of lines = that of the loaded file |
| `filtres` | final filters copied after the extraction, confirmed, unchanged since |
| `couverture_filtres` | every mandatory filter linked to an active rule and to its column |
| `candidats` | a non-empty candidates file |

**Not covered yet**: the rules (`regles`), `champsObligatoires` and `colonnesFinales` carry no fingerprint. Widening a value list, disabling a rule or lowering its importance passes the preflight; such a change must go back to the human who validates the mission.

The validations are human: the web page dates them and memorises their fingerprints. `leadlint fingerprints --mission mission.json --blacklist exclusions.csv` computes those fingerprints, to check a mission; it validates nothing.

## The persona matrix (`mode: "persona"`)

```json
{ "axes": [ { "id": "categorie", "libelle": "Département",
              "regles": [ { "categorie": "Sales", "priorite": 1, "motsCles": ["sales", "commercial"] } ] } ],
  "derives": [ { "id": "buyer_role", "libelle": "Buyer Role", "depuis": "seniorite", "table": { "Founder": "Décideur" }, "defaut": "Utilisateur" } ] }
```

For each axis, the normalized job title (lower case, no accents) is compared with all the keywords; the category with the **lowest priority number** wins (`--multi` keeps them all). A `derives` column is deduced from an axis through a table. Matrices provided: `matrices/matrice-generique.json` (B2B departments, the default) and `matrices/matrice-energie.json`.
