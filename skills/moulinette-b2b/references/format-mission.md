# Le fichier d'une mission

Une mission = un JSON. Le script accepte trois formes : un setup complet (`{ "client", "mission", "configuration": {…} }`), une ligne de base de données (même forme), ou la `configuration` seule. Une ancienne configuration (avant le schéma 2, bloc `criteres`) est migrée à la lecture, comme dans la page web.

## La configuration B2B (`mode: "revalidation_b2b"`, `schemaVersion: 2`)

| Clé | Rôle |
|---|---|
| `regles` | les contrôles appliqués à chaque ligne (voir plus bas) |
| `champsObligatoires` | champs qui doivent être renseignés, sinon la ligne est rejetée |
| `colonnesFinales` | `[{ "champ", "libelle" }]` : colonnes et ordre du fichier des conformes |
| `mappingCandidats` | `{ champ: "Entête de l'export" }` ; ce qui manque est deviné depuis les entêtes |
| `mappingBlacklist` | idem pour la liste d'exclusion (`entreprise`, `linkedinEntreprise`, `siteEntreprise`, `siren`) |
| `blacklist` | référence de la liste d'exclusion : `obligatoire`, `nom`, `volumeAttendu`, `empreinte`, `miseAJourLe` |
| `strategie` | la stratégie de recherche validée : `lignes`, `limites`, `valideeLe`, `empreinteValidee` |
| `pharow` | la recherche réelle chez le fournisseur : noms, filtres, preuves et dates (voir prévol) |

### Champs reconnus

`nomComplet`, `entreprise`, `poste`, `linkedinProfil`, `linkedinEntreprise`, `categorieLinkedIn`, `employesVisibles`, `pays`, `activite`, `tagsFournisseur`, `trancheEffectifFournisseur`, `qualificationActivite`, `siteEntreprise`, `siren`, `emailProfessionnel`, `sourceEmail`, `mobile`, `sourceMobile`, `effectifLegal`, `chiffreAffaires`, `codeNaf`, `statutJuridique`, `technologies`, `departement`, `niveauHierarchique`, `anciennetePoste`.

### Une règle

```json
{ "id": "r-pays", "critereId": "pays", "operateur": "est_dans", "valeur": ["France"],
  "importance": "bloquant", "siAbsent": "rejeter", "active": true }
```

| Critère (`critereId`) | Champ | Opérateurs |
|---|---|---|
| `pays` | pays | `est_dans` |
| `activite` | activite | `contient_un`, `ne_contient_aucun`, `est_dans` |
| `tags_source` | tagsFournisseur | `est_dans`, `contient_un` |
| `tranche_effectif_source` | trancheEffectifFournisseur | `est_dans` |
| `qualification_activite` | qualificationActivite | `est_dans` (verdict d'une QA, jamais inventé par le script) |
| `categorie_linkedin` | categorieLinkedIn | `est_dans` (« 11-50 employees » devient `11-50`) |
| `employes_visibles`, `effectif_legal`, `chiffre_affaires` | numériques | `entre` (`{ "min", "max" }`) |
| `code_naf`, `statut_juridique` | idem | `est_dans` |
| `technologies` | technologies | `contient_un` |
| `poste` | poste | `est_dans` (accepte « CEO & Founder » pour `CEO` ; refuse un poste d'assistant), `contient_un` |
| `departement`, `niveau_hierarchique` | idem | `est_dans` |
| `email_pro` | emailProfessionnel | `email_professionnel` (boîtes personnelles refusées) |
| `source_email` | sourceEmail | `present` |
| `mobile_fr` | mobile | `mobile_francais` (06/07, +33 accepté) |
| `source_mobile` | sourceMobile | `provenance_professionnelle` (« pro », « work »… sans « perso ») |
| `blacklist` | calculé | `pas_blackliste` |
| `doublon` | calculé | `unique` |

- **`importance`** : `bloquant` (un échec rejette), `verification` (un échec met à vérifier), `informatif` (ne change pas le verdict).
- **`siAbsent`** : `rejeter`, `verifier` ou `ignorer` quand la valeur manque dans l'export.
- Un critère inconnu n'est jamais ignoré : il rend « Non évalué » et la ligne ne peut pas être conforme.
- La **cause principale** d'une ligne est le contrôle qui a décidé de son verdict : le premier qui rejette, sinon le premier qui met à vérifier.

## Le prévol : onze contrôles avant toute livraison

| Contrôle | Ce qu'il exige |
|---|---|
| `strategie` | stratégie sans erreur, datée, et inchangée depuis sa validation (empreinte) |
| `recherche_validee` | recherche réelle validée : noms, filtres, volume, 5 à 10 entreprises témoins, capture, empreinte |
| `configuration` | au moins une règle active, critères connus, intervalles et listes valides, colonnes finales, chaque champ utilisé relié à une colonne |
| `blacklist` | liste d'exclusion chargée et reliée à ses colonnes (bloquant si la mission l'exige) |
| `empreinte` | volume et empreinte de la liste chargée = ceux mémorisés |
| `liste_pharow` | nom de la recherche et de la liste d'exclusion renseignés |
| `exclusion_pharow` | exclusion confirmée active chez le fournisseur |
| `export_source` | export identifié, et son nombre de lignes = celui du fichier chargé |
| `filtres` | filtres définitifs recopiés après l'extraction, confirmés, inchangés depuis |
| `couverture_filtres` | chaque filtre obligatoire relié à une règle active et à sa colonne |
| `candidats` | un fichier de candidats non vide |

Les validations sont humaines : la page web les date et mémorise leurs empreintes. `python moulinette.py empreintes --moulinette mission.json --blacklist exclusions.csv` calcule ces empreintes, pour vérifier une mission ; il ne valide rien.

## La matrice de personas (`mode: "persona"`)

```json
{ "axes": [ { "id": "categorie", "libelle": "Département",
              "regles": [ { "categorie": "Sales", "priorite": 1, "motsCles": ["sales", "commercial"] } ] } ],
  "derives": [ { "id": "buyer_role", "libelle": "Buyer Role", "depuis": "seniorite", "table": { "Founder": "Décideur" }, "defaut": "Utilisateur" } ] }
```

Pour chaque axe, l'intitulé normalisé (minuscules, sans accents) est comparé à tous les mots-clés ; la catégorie de **plus basse priorité** l'emporte (`--multi` les garde toutes). Une colonne `derives` se déduit d'un axe par une table. Matrices fournies : `matrices/matrice-generique.json` (départements B2B, par défaut) et `matrices/matrice-energie.json`.
