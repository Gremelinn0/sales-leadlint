# moulinette-b2b

**La moulinette B2B : vous déposez une base de contacts, vous la récupérez requalifiée et
revalidée selon vos règles.** Un script Python, une compétence pour Claude Code, et rien qui
quitte votre machine.

Deux usages :

| Vous avez… | Commande | Vous obtenez |
|---|---|---|
| une base de contacts (CRM, fichier acheté, export Sales Navigator) | `personas` | chaque intitulé de poste classé : département, séniorité, rôle d'achat |
| un export de prospection à contrôler avant de payer l'enrichissement ou d'importer en campagne | `b2b` | chaque ligne `Conforme`, `À vérifier` ou `Rejeté`, avec sa cause, un classeur de contrôle et le fichier des seuls conformes |

---

## Le problème

Une liste de prospection arrive toujours plus sale qu'on ne le croit : des entreprises hors cible
qu'un filtre trop large a laissées passer, des contacts déjà dans votre CRM ou chez un client, des
doublons, des emails personnels, des postes d'assistant au lieu du dirigeant. Les trier à l'œil
coûte des heures et laisse passer la moitié des erreurs ; les laisser passer coûte des crédits
d'enrichissement, des envois brûlés et une réputation d'expéditeur.

La moulinette rejoue vos règles sur chaque ligne, dit pourquoi elle accepte ou refuse, et refuse
de livrer tant que la recherche n'a pas été validée.

## Installer

```bash
python -m pip install openpyxl
```

Python 3.9 ou plus. Pour Claude Code, ajoutez le dépôt comme marketplace de plugins :

```text
/plugin marketplace add Gremelinn0/sales-moulinette-b2b
/plugin install moulinette-b2b@moulinette-b2b
```

La compétence `moulinette-b2b` explique alors à Claude, ou à n'importe quel agent, quand et comment
lancer le script.

## Essayer en une minute

Depuis `skills/moulinette-b2b/` :

```bash
python scripts/moulinette.py b2b exemples/export-exemple.csv --moulinette exemples/mission-esn-11-50-validee.json --blacklist exemples/exclusions-exemple.csv
```

```text
Moulinette « Client fictif / ESN 11-50 (exemple validé) » — export-exemple.csv
Prévol : autorisé
Lignes 50 · conformes 15 · à vérifier 4 · rejetées 31
Rejetées, par cause : Blacklist 5 · Employés visibles LinkedIn 4 · Mauvaise entreprise 4 · …
À vérifier, par cause : Provenance de l'email 2 · Provenance professionnelle du mobile 2
Contrôles du bilan : justes
Écrit : export-exemple-controle-qa.xlsx · export-exemple-conformes.xlsx
```

Toutes les données d'exemple sont fictives.

## Comment ça marche

### `personas` : trier des intitulés de poste

```bash
python scripts/moulinette.py personas contacts.xlsx
python scripts/moulinette.py personas contacts.csv -m matrices/matrice-energie.json --multi
```

La colonne des intitulés est devinée (ou donnée par `-c`). Chaque intitulé, normalisé (minuscules,
sans accents), est comparé à tous les mots-clés de la matrice ; la catégorie de plus basse priorité
l'emporte. *« Directeur RSE »* contient `directeur` (priorité 5) et `rse` (priorité 1) : il part en
**RSE**. Un mot-clé de trois lettres ou moins ne compte que comme mot entier : « IT » ne se trouve
pas dans « efficacité ». Ce qui n'est pas classé est listé, au lieu d'être caché.

### `b2b` : revalider un export selon la mission

La **mission** est un fichier JSON : les règles (pays, activité, tranche d'effectif, catégorie et
employés LinkedIn, poste du décideur, email et mobile professionnels, provenance des données…),
les champs obligatoires, les colonnes à livrer, la liste d'exclusion attendue et la stratégie de
recherche validée. Format complet : [`skills/moulinette-b2b/references/format-mission.md`](skills/moulinette-b2b/references/format-mission.md).

Pour chaque ligne :

- les valeurs sont normalisées (URL LinkedIn, domaine, SIREN, mobile 06/07, email, nombres
  écrits à la française) ;
- la liste d'exclusion est croisée par URL LinkedIn entreprise, domaine, SIREN puis nom
  (formes juridiques retirées) ;
- les doublons sont repérés par profil LinkedIn, email, mobile, ou nom dans la même entreprise ;
- chaque règle rend un résultat, et la ligne reçoit un verdict et **une seule cause principale** :
  le contrôle qui a décidé.

### Le prévol : rien n'est livré sans validation

Avant de livrer, onze contrôles vérifient que la stratégie et la recherche réelle ont été validées
et n'ont pas bougé depuis (empreintes), que la liste d'exclusion chargée est bien celle attendue
(volume et empreinte), que chaque filtre utilisé chez le fournisseur est rejoué par une règle, et
que l'export chargé est celui de la recherche validée. S'il en manque un, le script s'arrête
(code 2) et dit lequel. `--diagnostic` rend quand même les verdicts pour comprendre une liste, sans
rien produire de livrable.

### Ce qui sort

- `<export>-controle-qa.xlsx` : **Contrôle QA** (verdict modifiable, cause, raisons, liens),
  **Audit moulinette** (chaque contrôle de chaque ligne, valeur brute et normalisée), **Résumé**.
- `<export>-conformes.xlsx` : les seuls conformes, aux colonnes de la mission.
- `--json` : le bilan sur la sortie standard, pour un agent.

## Pour un agent

```bash
python scripts/moulinette.py prevol export.csv --moulinette mission.json --blacklist exclusions.csv --json
python scripts/moulinette.py b2b    export.csv --moulinette mission.json --blacklist exclusions.csv --json --lignes
```

Le bilan JSON porte `prevol` (chaque contrôle), `total`, `conformes`, `a_verifier`, `rejetes`,
`causes` et deux `invariants` à vérifier avant de rapporter quoi que ce soit. Les règles de
conduite d'un agent sont dans [`skills/moulinette-b2b/SKILL.md`](skills/moulinette-b2b/SKILL.md).

## Ce que la moulinette ne fait pas

- Elle ne cherche pas de contacts et ne scrape rien : vous apportez l'export et les règles.
- Elle ne juge pas le modèle économique d'une entreprise : une QA humaine ou un agent le fait, et la
  moulinette rejoue ensuite son verdict (`qualification_activite`).
- Elle ne valide pas une mission à votre place : les validations sont datées par un humain.

## Les preuves

```bash
python tests/test_moulinette.py
```

Le banc rejoue des scénarios dont les résultats attendus sont rendus par le moteur de la page web
de la moulinette ([zerogravity.builders/outils/segmentation](https://zerogravity.builders/outils/segmentation)) :
mêmes verdicts, mêmes causes, mêmes empreintes, champ par champ. Il lance aussi le script de bout
en bout (CSV, Excel, prévol bloqué, diagnostic, cellule piégée) et contient deux contrôles
négatifs qui exigent qu'un écart volontaire soit vu.

## Licence

MIT. Fait par [ZEROGRAVITY](https://zerogravity.builders).
