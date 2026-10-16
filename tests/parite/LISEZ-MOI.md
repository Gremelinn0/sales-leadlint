# Les attendus de parité

Chaque `*.attendu.json` est rendu par le moteur de la page web de la moulinette, à partir de
l'entrée `*.entree.json` du même nom (ou de valeurs écrites dans l'oracle). Le banc
`tests/test_moulinette.py` rejoue ces entrées dans le script et exige le même résultat, champ par
champ : verdicts, causes, raisons, valeurs normalisées, contrôles du prévol et empreintes.

L'oracle vit avec le moteur web (`marketplace/scripts/segmentation/oracle_parite.ts`, dépôt de la
Marketplace ZEROGRAVITY). Il se relance après toute modification de l'un ou l'autre moteur :

    ./node_modules/.bin/vite-node scripts/segmentation/oracle_parite.ts -- <ce dossier>

Toutes les données sont fictives.
