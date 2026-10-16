---
name: moulinette-b2b
description: "Quand une base de contacts ou un export de prospection B2B doit être requalifié ou revalidé avant usage → lancer le script de la moulinette : trier les intitulés de poste en personas, ou rejouer les règles d'une mission (cible entreprise, poste, email et mobile pro, liste d'exclusion, doublons), avec un contrôle avant lancement et un bilan chiffré. Tout tourne en local, aucune donnée ne sort. Triggers : moulinette, revalider un export, nettoyer une liste de prospection, requalifier des contacts, trier des intitulés de poste, personas, liste d'exclusion, blacklist, doublons, export Pharow, Sales Navigator, Pipecorn, contrôle QA leads."
---

# La moulinette B2B

Un script, `scripts/moulinette.py`, qui fait deux choses sur un fichier de contacts (.xlsx, .xlsm, .csv) :

1. **`personas`** : classe chaque intitulé de poste (département, séniorité, rôle d'achat déduit) selon une matrice de mots-clés.
2. **`b2b`** : revalide un export de prospection selon la **moulinette de la mission** (un fichier JSON) : chaque ligne sort `Conforme`, `À vérifier` ou `Rejeté`, avec une seule cause principale et le détail de chaque contrôle.

Python 3.9+ et `openpyxl` (`python -m pip install openpyxl`). Aucun appel réseau.

## Quand l'utiliser

- Un export vient d'un outil de recherche (Pharow, Sales Navigator, Pipecorn…) et doit être contrôlé avant enrichissement payant ou import en campagne.
- Une base achetée ou un CRM doit être trié par persona avant une campagne.
- Un agent (par exemple un agent de ciblage) doit livrer une liste : il passe par ce script, jamais par un tri « à l'œil ».

## Les commandes

```bash
python scripts/moulinette.py prevol  export.csv --moulinette mission.json --blacklist exclusions.csv
python scripts/moulinette.py b2b     export.csv --moulinette mission.json --blacklist exclusions.csv --json
python scripts/moulinette.py personas contacts.xlsx --json
python scripts/moulinette.py empreintes --moulinette mission.json --blacklist exclusions.csv
```

| Code de sortie | Sens |
|---|---|
| 0 | terminé ; les deux contrôles du bilan sont justes |
| 2 | prévol bloqué : rien n'est livré (le bilan dit quels contrôles lever) |
| 1 | entrée illisible ou bilan faux |

`--json` écrit le bilan sur la sortie standard, pour un agent. `--lignes` y ajoute le verdict de chaque ligne.

## La règle pour un agent

1. **Lancer `prevol` d'abord.** S'il bloque, ne pas contourner : rapporter les contrôles en échec à l'humain qui valide la mission. La stratégie de recherche, la recherche réelle et la liste d'exclusion se valident par un humain, jamais par l'agent.
2. **`--diagnostic`** rend les verdicts même quand le prévol bloque, pour comprendre une liste ; le fichier produit porte `-diagnostic-` dans son nom et **aucun fichier de conformes n'est écrit**. Un diagnostic ne se livre jamais.
3. **Après `b2b`, recopier le bilan tel quel** : total, conformes, à vérifier, rejetés, et chaque cause principale non nulle. Vérifier `invariants` : `total = conformes + à vérifier + rejetés` et `à vérifier + rejetés = somme des causes`.
4. **Seules les lignes `À vérifier` partent en revue humaine ou en QA métier**, avec leur cause. Les rejetées ne se repêchent pas en silence ; les conformes sont dans `<export>-conformes.xlsx`, aux colonnes et dans l'ordre de la mission.
5. Ne jamais modifier la mission pour faire passer une liste : changer une règle, un filtre ou la liste d'exclusion invalide leur empreinte, et le prévol le verra.

## Ce que rend `b2b`

- `<export>-controle-qa.xlsx` : onglet **Contrôle QA** (une ligne par contact, verdict modifiable en liste, type de problème, raisons, liens cliquables, colonne « Contrôlé le » à remplir), **Audit moulinette** (chaque contrôle de chaque ligne : valeur brute, valeur normalisée, colonne source, diagnostic) et **Résumé** (compteurs, règles appliquées, état du prévol).
- `<export>-conformes.xlsx` : les seules lignes conformes, prêtes à livrer.
- Toute cellule qui commencerait comme une formule de tableur (`=`, `+`, `-`, `@`) est neutralisée.

## Le fichier de la mission

Le même JSON que la page web de la moulinette enregistre : un setup (`client`, `mission`, `configuration`) ou une configuration seule. Il contient les règles (critère, opérateur, valeurs, importance, politique si la donnée manque), les champs obligatoires, les colonnes finales, les correspondances de colonnes, la référence de la liste d'exclusion, la stratégie de recherche et les filtres réellement utilisés. Il ne contient jamais l'export, les lignes d'exclusion ni un secret.

Exemples dans `exemples/` : `mission-esn-11-50-validee.json` (passe le prévol sur `export-exemple.csv`) et `modele-mission-esn-11-50.json` (à adapter ; bloque tant que rien n'est validé). Détail des critères, opérateurs et contrôles : [references/format-mission.md](references/format-mission.md).

## Comment il classe (b2b)

- Les colonnes sont devinées depuis les entêtes usuels (français et anglais), puis recouvertes par les correspondances mémorisées dans la mission quand la colonne existe.
- Les valeurs sont normalisées avant comparaison : accents et ponctuation, URL LinkedIn, domaine, SIREN, mobile français 06/07, email, nombres écrits à la française.
- Liste d'exclusion : croisement par URL LinkedIn entreprise, puis domaine, SIREN, nom normalisé (formes juridiques retirées).
- Doublons : même profil LinkedIn, même email, même mobile, ou même nom dans la même entreprise.
- Un mot-clé de trois lettres ou moins ne compte que comme mot entier (« IT » ne se trouve pas dans « efficacité »).

## Les preuves

`python tests/test_moulinette.py` (depuis la racine du dépôt) : parité champ par champ avec le moteur de la page web sur des scénarios rendus par ce moteur, la ligne de commande de bout en bout, et deux contrôles négatifs qui exigent que le banc voie un écart volontaire.
