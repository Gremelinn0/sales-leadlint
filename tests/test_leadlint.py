# -*- coding: utf-8 -*-
"""Banc de leadlint : parité avec le moteur de la page web, puis la ligne de commande de bout en bout.

    python tests/test_leadlint.py

Les attendus de `tests/parite/` sont rendus par le moteur de la page web (oracle
`scripts/segmentation/oracle_parite.ts` du dépôt de la Marketplace). Le banc rejoue les
mêmes entrées dans le script et exige le même résultat, champ par champ. Le pont de bout
en bout : la mission que la page fait télécharger (`mission-telechargee.json`) passe le
contrôle avant lancement du script et en rend les signatures. Des contrôles sont négatifs :
un banc incapable de dire non ne vaut pas son oui.
"""

from __future__ import annotations

import copy
import contextlib
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
PARITE = RACINE / "tests" / "parite"
EXEMPLES = RACINE / "skills" / "leadlint" / "exemples"
SCRIPT = RACINE / "skills" / "leadlint" / "scripts" / "leadlint.py"
sys.path.insert(0, str(SCRIPT.parent))

import leadlint as m  # noqa: E402


def lire(nom: str):
    return json.loads((PARITE / nom).read_text(encoding="utf-8"))


def rendu_b2b(entree: dict) -> dict:
    """Ce que le script rend pour une entrée de parité, au format de l'oracle."""
    configuration = m.migrer_configuration_b2b(copy.deepcopy(entree["configuration"]))
    candidats, blacklist = entree["candidats"], entree["blacklist"]
    empreinte_bl = m.empreinte_blacklist(blacklist, configuration["mappingBlacklist"]) if blacklist else None
    controles = m.controler_prevol(configuration, len(candidats), len(blacklist), empreinte_bl)
    lignes = m.valider_export_b2b(candidats, blacklist, configuration)
    return {
        "empreintes": {
            "strategie": m.empreinte_strategie(configuration["strategie"]), "recherche": m.empreinte_recherche(configuration),
            "filtres": m.empreinte_filtres(configuration["pharow"]["filtres"]), "blacklist": empreinte_bl,
        },
        "prevol": {"autorise": m.prevol_autorise(controles), "controles": controles},
        "lignes": [{
            "numeroLigne": l["numeroLigne"], "statut": l["statut"], "cause": l["causePrincipale"]["critereId"] if l["causePrincipale"] else None,
            "typeProbleme": m.type_probleme(l), "raisons": l["raisons"], "valeurs": l["valeurs"], "evaluations": l["evaluations"],
        } for l in lignes],
    }


def ecarts(attendu, obtenu, chemin="") -> list[str]:
    """Toutes les différences, avec leur chemin ; un nombre vaut l'autre s'il est égal (20 == 20.0)."""
    if isinstance(attendu, dict) and isinstance(obtenu, dict):
        sortie = []
        for cle in sorted(set(attendu) | set(obtenu)):
            if cle not in obtenu or cle not in attendu:
                sortie.append(f"{chemin}.{cle} : présent d'un seul côté")
            else:
                sortie += ecarts(attendu[cle], obtenu[cle], f"{chemin}.{cle}")
        return sortie
    if isinstance(attendu, list) and isinstance(obtenu, list):
        if len(attendu) != len(obtenu):
            return [f"{chemin} : {len(attendu)} éléments attendus, {len(obtenu)} obtenus"]
        return [e for i, (a, o) in enumerate(zip(attendu, obtenu)) for e in ecarts(a, o, f"{chemin}[{i}]")]
    if m.est_nombre(attendu) and m.est_nombre(obtenu):
        return [] if attendu == obtenu else [f"{chemin} : {attendu!r} attendu, {obtenu!r} obtenu"]
    return [] if attendu == obtenu and type(attendu) is type(obtenu) else [f"{chemin} : {attendu!r} attendu, {obtenu!r} obtenu"]


SCENARIOS_B2B = ["esn-validee", "esn-non-validee", "esn-filtre-modifie", "regles-variees"]


class Parite(unittest.TestCase):
    """Le script rend exactement ce que rend la page web."""

    def test_scenarios_b2b(self):
        for nom in SCENARIOS_B2B:
            with self.subTest(scenario=nom):
                differences = ecarts(lire(f"b2b-{nom}.attendu.json"), rendu_b2b(lire(f"b2b-{nom}.entree.json")))
                self.assertEqual(differences, [], "\n".join(differences[:15]))

    def test_mission_validee_passe_le_prevol_apres_rechargement(self):
        self.assertTrue(lire("b2b-esn-validee.attendu.json")["prevol"]["autorise"])
        self.assertTrue(rendu_b2b(lire("b2b-esn-validee.entree.json"))["prevol"]["autorise"])

    def test_ancienne_mission(self):
        entree, attendu = lire("b2b-ancienne.entree.json"), lire("b2b-ancienne.attendu.json")
        configuration = m.migrer_configuration_b2b(entree["configuration"])
        regles = [{k: r[k] for k in ("critereId", "operateur", "valeur", "importance", "siAbsent")} for r in configuration["regles"]]
        self.assertEqual(regles, attendu["regles"])
        self.assertEqual([l["statut"] for l in m.valider_export_b2b(entree["candidats"], [], configuration)], attendu["statuts"])

    def test_normaliseurs(self):
        fonctions = {
            "texte": m.normaliser_texte, "nomEntreprise": m.normaliser_nom_entreprise, "linkedin": m.normaliser_url_linkedin,
            "domaine": m.normaliser_domaine, "siren": m.normaliser_siren, "categorie": m.normaliser_categorie_linkedin,
            "nombre": m.lire_nombre, "email": m.normaliser_email, "emailPro": m.email_est_professionnel,
            "mobile": m.normaliser_mobile_francais, "intitule": m.normaliser_intitule,
        }
        for cas in lire("normaliseurs.attendu.json"):
            for cle, fonction in fonctions.items():
                with self.subTest(brut=cas["brut"], fonction=cle):
                    self.assertEqual(ecarts(cas[cle], fonction(cas["brut"])), [])

    def test_mappings(self):
        for cas in lire("mappings.attendu.json"):
            self.assertEqual(m.deviner_mapping(cas["entetes"], m.ENTETES_CANDIDATS), cas["candidats"])
            self.assertEqual(m.deviner_mapping(cas["entetes"], m.ENTETES_BLACKLIST), cas["blacklist"])

    def test_strategies(self):
        for cas in lire("strategies.attendu.json"):
            strategie = m.migrer_strategie(cas["strategie"])
            self.assertEqual(m.erreurs_strategie(strategie), cas["erreurs"])
            self.assertEqual(m.empreinte_strategie(strategie), cas["empreinte"])

    def test_empreintes(self):
        for cas in lire("empreintes.attendu.json"):
            self.assertEqual(m.empreinte_texte(cas["valeur"]), cas["empreinte"], cas["valeur"])

    def test_personas(self):
        attendu = lire("personas.attendu.json")
        for nom in ("defaut", "generique"):
            cas = attendu[nom]
            self.assertEqual(m.segmenter(cas["intitules"], cas["matrice"], False)["lignes"], cas["strict"], nom)
            self.assertEqual(m.segmenter(cas["intitules"], cas["matrice"], True)["lignes"], cas["multi"], nom)
            self.assertEqual([m.classer(i, cas["matrice"]["axes"][0]) for i in cas["intitules"]], cas["axe0"], nom)
        for cas in attendu["colonnes"]:
            self.assertEqual(m.deviner_colonne_poste(cas["entetes"]), cas["index"], cas["entetes"])


class LeBancSaitDireNon(unittest.TestCase):
    """Contrôles négatifs : un écart volontaire doit être vu."""

    def test_un_verdict_modifie_est_vu(self):
        attendu = lire("b2b-esn-validee.attendu.json")
        sabote = copy.deepcopy(attendu)
        sabote["lignes"][0]["statut"] = "Rejeté"
        self.assertTrue(ecarts(attendu, sabote))
        self.assertTrue(ecarts({"n": 20}, {"n": "20"}), "une chaîne ne vaut pas un nombre")

    def test_un_moteur_sabote_est_vu(self):
        original = m.email_est_professionnel
        m.email_est_professionnel = lambda valeur: True
        try:
            differences = ecarts(lire("b2b-esn-validee.attendu.json"), rendu_b2b(lire("b2b-esn-validee.entree.json")))
        finally:
            m.email_est_professionnel = original
        self.assertTrue(differences, "le banc n'a pas vu un moteur qui accepte tous les emails")


def ecrire_csv(chemin: Path, lignes: list[dict], separateur: str = ";") -> None:
    entetes = list(lignes[0]) if lignes else []
    with chemin.open("w", encoding="utf-8-sig", newline="") as fichier:
        ecrivain = csv.writer(fichier, delimiter=separateur, lineterminator="\n")
        ecrivain.writerow(entetes)
        for ligne in lignes:
            ecrivain.writerow(["" if ligne.get(e) is None else m.texte_js(ligne.get(e)) for e in entetes])


def lancer(*arguments: str) -> tuple[int, str]:
    sortie = io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(io.StringIO()):
        code = m.main(list(arguments))
    return code, sortie.getvalue()


def preparer_mission(scenario: str) -> tuple[Path, Path, Path, Path]:
    """Un dossier temporaire avec l'export, la liste d'exclusion et la mission d'un scénario de parité."""
    dossier = Path(tempfile.mkdtemp(prefix="leadlint-"))
    entree = lire(f"b2b-{scenario}.entree.json")
    export, exclusions, mission = dossier / "export.csv", dossier / "exclusions.csv", dossier / "mission.json"
    ecrire_csv(export, entree["candidats"])
    ecrire_csv(exclusions, entree["blacklist"], ",")
    mission.write_text(json.dumps({"client": "Client fictif", "mission": "ESN 11-50", "configuration": entree["configuration"]}, ensure_ascii=False), encoding="utf-8")
    return dossier, export, exclusions, mission


class LigneDeCommande(unittest.TestCase):
    """Le script tel qu'un humain ou un agent le lance : fichiers réels, codes de sortie, classeur."""

    def preparer(self, scenario: str) -> tuple[Path, Path, Path, Path]:
        return preparer_mission(scenario)

    def test_mission_validee_rend_les_verdicts_de_la_page(self):
        dossier, export, exclusions, mission = self.preparer("esn-validee")
        code, sortie = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json", "--lignes")
        self.assertEqual(code, 0, sortie)
        bilan = json.loads(sortie)
        attendu = lire("b2b-esn-validee.attendu.json")
        self.assertTrue(bilan["prevol"]["autorise"])
        self.assertEqual([l["statut"] for l in bilan["lignes"]], [l["statut"] for l in attendu["lignes"]])
        self.assertEqual([l["cause"] for l in bilan["lignes"]], [l["typeProbleme"] for l in attendu["lignes"]])
        self.assertEqual(bilan["total"], bilan["conformes"] + bilan["a_verifier"] + bilan["rejetes"])
        self.assertTrue(all(bilan["invariants"].values()))
        self.assertEqual(set(bilan["fichiers"]), {"controle_qa", "conformes"})

        import openpyxl
        classeur = openpyxl.load_workbook(bilan["fichiers"]["controle_qa"])
        self.assertEqual(classeur.sheetnames, ["Contrôle QA", "Contrôle leadlint", "Résumé"])
        self.assertEqual(classeur.properties.creator, "leadlint")
        self.assertEqual([c.value for c in classeur["Contrôle leadlint"][1]], [
            "N° ligne source", "Statut leadlint", "Portée", "Critère", "Importance",
            "Résultat critère", "Colonne source", "Valeur brute", "Valeur normalisée", "Diagnostic",
        ])
        self.assertIn("ControleLeadlint", classeur["Contrôle leadlint"].tables)
        self.assertIn("ResumeLeadlint", classeur["Résumé"].tables)
        resume = {ligne[0].value: ligne[1].value for ligne in classeur["Résumé"].iter_rows(min_row=2)}
        self.assertEqual(resume["Conformes leadlint"], bilan["conformes"])
        self.assertEqual(resume["Signature de la liste d'exclusion"], attendu["empreintes"]["blacklist"])
        self.assertEqual(resume["Contrôle avant lancement"], "autorisé")
        controle = classeur["Contrôle QA"]
        self.assertEqual([c.value for c in controle[1]], m.ENTETES_CONTROLE_QA)
        self.assertEqual(controle.freeze_panes, "A2")
        self.assertIn("ControleQA", controle.tables)
        self.assertEqual(controle.max_row, len(attendu["lignes"]) + 1)
        self.assertTrue(controle.data_validations.dataValidation)
        conformes = openpyxl.load_workbook(bilan["fichiers"]["conformes"])["Conformes"]
        self.assertEqual(conformes.max_row - 1, bilan["conformes"])

    def test_excel_en_entree_donne_les_memes_verdicts(self):
        dossier, export, exclusions, mission = self.preparer("esn-validee")
        import openpyxl
        entree = lire("b2b-esn-validee.entree.json")
        classeur = openpyxl.Workbook()
        entetes = list(entree["candidats"][0])
        classeur.active.append(entetes)
        for ligne in entree["candidats"]:
            classeur.active.append([ligne.get(e) for e in entetes])
        xlsx = dossier / "export.xlsx"
        classeur.save(xlsx)
        code, sortie = lancer("check", str(xlsx), "--mission", str(mission), "--exclusions", str(exclusions), "--json", "--lignes")
        self.assertEqual(code, 0, sortie)
        self.assertEqual([l["statut"] for l in json.loads(sortie)["lignes"]], [l["statut"] for l in lire("b2b-esn-validee.attendu.json")["lignes"]])

    def test_prevol_bloque_ne_livre_rien(self):
        dossier, export, exclusions, mission = self.preparer("esn-non-validee")
        code, sortie = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(sortie)["livre"])
        self.assertEqual(sorted(p.name for p in dossier.glob("*.xlsx")), [])

    def test_diagnostic_rend_les_verdicts_sans_rien_de_livrable(self):
        dossier, export, exclusions, mission = self.preparer("esn-non-validee")
        code, sortie = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json", "--diagnostic")
        self.assertEqual(code, 0, sortie)
        bilan = json.loads(sortie)
        self.assertTrue(bilan["diagnostic"])
        self.assertEqual(list(bilan["fichiers"]), ["controle_qa"])
        self.assertIn("-diagnostic-", bilan["fichiers"]["controle_qa"])

    def test_commandes_preflight_et_fingerprints(self):
        dossier, export, exclusions, mission = self.preparer("esn-validee")
        self.assertEqual(lancer("preflight", str(export), "--mission", str(mission), "--exclusions", str(exclusions))[0], 0)
        code, sortie = lancer("fingerprints", "--mission", str(mission), "--exclusions", str(exclusions), "--json")
        self.assertEqual(code, 0)
        attendu = lire("b2b-esn-validee.attendu.json")["empreintes"]
        obtenu = json.loads(sortie)
        self.assertEqual({k: obtenu[k] for k in ("strategie", "recherche", "filtres", "blacklist")}, attendu)

    def test_une_formule_injectee_est_neutralisee(self):
        dossier, export, exclusions, mission = self.preparer("esn-validee")
        entree = lire("b2b-esn-validee.entree.json")
        entree["candidats"][0]["Nom complet"] = '=HYPERLINK("https://piege.example")'
        ecrire_csv(export, entree["candidats"])
        code, sortie = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json", "--diagnostic")
        self.assertEqual(code, 0, sortie)
        import openpyxl
        cellule = openpyxl.load_workbook(json.loads(sortie)["fichiers"]["controle_qa"])["Contrôle QA"]["B2"]
        self.assertTrue(str(cellule.value).startswith("'="), cellule.value)

    def test_entree_illisible_sort_proprement(self):
        code, _ = lancer("check", "introuvable.csv", "--mission", "introuvable.json")
        self.assertEqual(code, 1)

    def test_personas(self):
        dossier = Path(tempfile.mkdtemp(prefix="leadlint-"))
        fichier = dossier / "contacts.csv"
        ecrire_csv(fichier, [{"Nom": "A", "Job title": "Directeur RSE"}, {"Nom": "B", "Job title": "Chargé IT"}, {"Nom": "C", "Job title": "Office manager"}], ",")
        code, sortie = lancer("personas", str(fichier), "--json")
        self.assertEqual(code, 0, sortie)
        bilan = json.loads(sortie)
        self.assertEqual(bilan["colonne"], "Job title")
        self.assertEqual(bilan["total"], 3)
        self.assertTrue(Path(bilan["fichier"]).exists())


class MissionTelechargee(unittest.TestCase):
    """Le pont web -> script : le fichier que le bouton « Télécharger la mission pour votre IA » fait télécharger passe le script.

    `mission-telechargee.json` est la sortie de `missionPourFichier` (oracle, scénario 6), validée dans la page sur l'export et la
    liste d'exclusion d'exemple de `skills/leadlint/exemples/` ; `mission-telechargee-attendu.json` porte les signatures que la page
    calcule pour elle. Si la page change ce qu'elle écrit, ou le script ce qu'il lit, l'un de ces trois tests tombe.
    """

    EXPORT = EXEMPLES / "export-exemple.csv"
    EXCLUSIONS = EXEMPLES / "exclusions-exemple.csv"
    MISSION = PARITE / "mission-telechargee.json"

    def prevol(self, mission: Path) -> tuple[int, dict]:
        code, sortie = lancer("preflight", str(self.EXPORT), "--mission", str(mission), "--exclusions", str(self.EXCLUSIONS), "--json")
        return code, json.loads(sortie)["prevol"]

    def test_le_controle_avant_lancement_laisse_passer_la_mission_telechargee(self):
        code, prevol = self.prevol(self.MISSION)
        controles = prevol["controles"]
        self.assertEqual(code, 0, [c["id"] for c in controles if not c["valide"]])
        self.assertTrue(prevol["autorise"])
        self.assertEqual((sum(c["valide"] for c in controles), len(controles)), (11, 11))

    def test_les_signatures_du_script_sont_celles_de_la_page(self):
        attendu = lire("mission-telechargee-attendu.json")
        self.assertEqual(set(attendu), {"strategie", "recherche", "filtres", "blacklist", "blacklist_volume"}, "un attendu amputé ne prouverait rien")
        code, sortie = lancer("fingerprints", "--mission", str(self.MISSION), "--exclusions", str(self.EXCLUSIONS), "--json")
        self.assertEqual(code, 0, sortie)
        obtenu = json.loads(sortie)
        self.assertEqual(ecarts(attendu, {champ: obtenu[champ] for champ in attendu if champ in obtenu}), [])

    def test_une_valeur_de_la_strategie_modifiee_apres_validation_bloque_le_controle_avant_lancement(self):
        """Contrôle négatif : la même mission, une seule valeur de sa stratégie changée en mémoire, n'est plus laissée passer."""
        self.assertEqual(self.prevol(self.MISSION)[0], 0, "le témoin positif : la mission d'origine passe, avec les mêmes fichiers")
        mission = json.loads(self.MISSION.read_text(encoding="utf-8"))
        mission["configuration"]["strategie"]["lignes"][0]["parametrage"] += " OU 50-99"
        with tempfile.TemporaryDirectory(prefix="leadlint-") as dossier:
            modifiee = Path(dossier) / "mission-modifiee.json"
            modifiee.write_text(json.dumps(mission, ensure_ascii=False), encoding="utf-8")
            code, prevol = self.prevol(modifiee)
        refuses = {c["id"] for c in prevol["controles"] if c["bloquant"] and not c["valide"]}
        self.assertEqual(code, 2)
        self.assertFalse(prevol["autorise"])
        self.assertIn("strategie", refuses)
        self.assertLessEqual(refuses, {"strategie", "recherche_validee"}, "seul le verrou de la stratégie (et la recherche qui l'embarque) doit tomber")


def cellules_du_classeur(chemin: str) -> dict:
    """Toutes les cellules d'un classeur, sauf l'horodatage du contrôle (la seule valeur qui change d'un passage à l'autre)."""
    import openpyxl
    rendu = {}
    for feuille in openpyxl.load_workbook(chemin).worksheets:
        for ligne in feuille.iter_rows():
            for cellule in ligne:
                if feuille.title == "Résumé" and cellule.column == 2 and feuille.cell(cellule.row, 1).value == "Contrôle effectué le":
                    continue
                rendu[(feuille.title, cellule.coordinate)] = cellule.value
    return rendu


class AnciensNoms(unittest.TestCase):
    """Les noms d'avant restent acceptés, comme des alias exacts : même sortie, octet pour octet, même code de sortie.

    Les commandes `b2b`, `prevol` et `empreintes` valent `check`, `preflight` et `fingerprints` ; les options
    `--moulinette` et `--blacklist` valent `--mission` et `--exclusions`. Ils ne figurent plus dans l'aide.
    """

    def deux_passages(self, ancien: str, nouveau: str, scenario: str, *options: str, avec_export: bool = True):
        """Le même appel sous les anciens noms et sous les noms actuels, dans le même dossier (donc avec les mêmes chemins écrits)."""
        dossier, export, exclusions, mission = preparer_mission(scenario)
        entree = [str(export)] if avec_export else []
        return (
            lancer(ancien, *entree, "--blacklist", str(exclusions), *options, "--moulinette", str(mission)),
            lancer(nouveau, *entree, "--exclusions", str(exclusions), *options, "--mission", str(mission)),
        )

    def test_check_vaut_b2b(self):
        for scenario, options, code_attendu in (
            ("esn-validee", ("--json", "--lignes"), 0),
            ("esn-validee", (), 0),
            ("esn-non-validee", ("--json",), 2),
            ("esn-non-validee", (), 2),
            ("esn-non-validee", ("--json", "--diagnostic"), 0),
            ("esn-non-validee", ("--diagnostic",), 0),
        ):
            with self.subTest(scenario=scenario, options=options):
                (code_ancien, sortie_ancienne), (code_actuel, sortie_actuelle) = self.deux_passages("b2b", "check", scenario, *options)
                self.assertEqual((code_ancien, code_actuel), (code_attendu, code_attendu))
                self.assertEqual(sortie_ancienne, sortie_actuelle)
                self.assertTrue(sortie_ancienne)

    def test_check_ecrit_les_memes_classeurs_que_b2b(self):
        dossier, export, exclusions, mission = preparer_mission("esn-validee")
        code_ancien, sortie_ancienne = lancer("b2b", str(export), "--moulinette", str(mission), "--blacklist", str(exclusions), "--json")
        fichiers_anciens = {cle: cellules_du_classeur(chemin) for cle, chemin in json.loads(sortie_ancienne)["fichiers"].items()}
        code_actuel, sortie_actuelle = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json")
        fichiers_actuels = {cle: cellules_du_classeur(chemin) for cle, chemin in json.loads(sortie_actuelle)["fichiers"].items()}
        self.assertEqual((code_ancien, code_actuel), (0, 0))
        self.assertEqual(fichiers_anciens, fichiers_actuels)
        self.assertTrue(all(fichiers_actuels.values()))

    def test_preflight_vaut_prevol(self):
        for scenario, code_attendu in (("esn-validee", 0), ("esn-non-validee", 2)):
            for options in ((), ("--json",)):
                with self.subTest(scenario=scenario, options=options):
                    (code_ancien, sortie_ancienne), (code_actuel, sortie_actuelle) = self.deux_passages("prevol", "preflight", scenario, *options)
                    self.assertEqual((code_ancien, code_actuel), (code_attendu, code_attendu))
                    self.assertEqual(sortie_ancienne, sortie_actuelle)
                    self.assertTrue(sortie_ancienne)

    def test_fingerprints_vaut_empreintes(self):
        for options in ((), ("--json",)):
            with self.subTest(options=options):
                (code_ancien, sortie_ancienne), (code_actuel, sortie_actuelle) = self.deux_passages("empreintes", "fingerprints", "esn-validee", *options, avec_export=False)
                self.assertEqual((code_ancien, code_actuel), (0, 0))
                self.assertEqual(sortie_ancienne, sortie_actuelle)
                self.assertTrue(sortie_ancienne)

    def test_les_anciens_et_les_nouveaux_noms_se_melangent_sans_changer_le_resultat(self):
        """Chaque option ancienne ou actuelle, avec chaque nom de commande : toujours la même sortie."""
        dossier, export, exclusions, mission = preparer_mission("esn-validee")
        reference = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json", "--lignes")
        self.assertEqual(reference[0], 0)
        for commande in ("check", "b2b"):
            for drapeau_mission in ("--mission", "--moulinette"):
                for drapeau_exclusions in ("--exclusions", "--blacklist"):
                    with self.subTest(commande=commande, mission=drapeau_mission, exclusions=drapeau_exclusions):
                        obtenu = lancer(commande, str(export), drapeau_mission, str(mission), drapeau_exclusions, str(exclusions), "--json", "--lignes")
                        self.assertEqual(obtenu, reference)

    def test_sans_la_liste_d_exclusion_le_controle_avant_lancement_la_reclame(self):
        """Contrôle négatif : l'option est bien lue (sans elle, le contrôle avant lancement refuse), sous son nom actuel comme sous l'ancien."""
        dossier, export, exclusions, mission = preparer_mission("esn-validee")
        sans = lancer("preflight", str(export), "--mission", str(mission), "--json")
        self.assertEqual(sans[0], 2)
        refuses = {c["id"] for c in json.loads(sans[1])["prevol"]["controles"] if c["bloquant"] and not c["valide"]}
        self.assertLessEqual({"blacklist", "empreinte"}, refuses)
        for drapeau in ("--exclusions", "--blacklist"):
            with self.subTest(option=drapeau):
                self.assertEqual(lancer("preflight", str(export), "--mission", str(mission), drapeau, str(exclusions), "--json")[0], 0)

    def test_entree_illisible_sort_en_1_sous_tous_les_noms(self):
        self.assertEqual(lancer("b2b", "introuvable.csv", "--moulinette", "introuvable.json")[0], 1)
        self.assertEqual(lancer("check", "introuvable.csv", "--mission", "introuvable.json")[0], 1)

    def test_la_mission_reste_obligatoire_sous_tous_les_noms(self):
        for nom in ("check", "b2b", "preflight", "prevol"):
            with self.subTest(commande=nom), self.assertRaises(SystemExit) as arret:
                lancer(nom, "export.csv")
            self.assertEqual(arret.exception.code, 2)
        for nom in ("fingerprints", "empreintes"):
            with self.subTest(commande=nom), self.assertRaises(SystemExit) as arret:
                lancer(nom)
            self.assertEqual(arret.exception.code, 2)

    def test_l_outil_se_presente_sous_son_nouveau_nom(self):
        dossier, export, exclusions, mission = preparer_mission("esn-validee")
        contacts = dossier / "contacts.csv"
        ecrire_csv(contacts, [{"Nom": "A", "Job title": "Directeur RSE"}], ",")
        sorties = {
            "check": lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json")[1],
            "preflight": lancer("preflight", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json")[1],
            "fingerprints": lancer("fingerprints", "--mission", str(mission), "--json")[1],
            "personas": lancer("personas", str(contacts), "--json")[1],
        }
        for nom, sortie in sorties.items():
            with self.subTest(commande=nom):
                bilan = json.loads(sortie)
                self.assertEqual(bilan["outil"], "leadlint")
                self.assertEqual(bilan["version"], m.VERSION)
        resume = cellules_du_classeur(json.loads(sorties["check"])["fichiers"]["controle_qa"])
        lignes_outil = [v for (feuille, coordonnee), v in resume.items() if feuille == "Résumé" and coordonnee.startswith("B")]
        self.assertIn(f"leadlint {m.VERSION}", lignes_outil)
        self.assertNotIn(f"moulinette.py {m.VERSION}", lignes_outil)

    def test_l_aide_annonce_les_noms_actuels_et_aucun_ancien(self):
        aides = []
        for arguments in (["--help"], ["check", "--help"], ["preflight", "--help"], ["fingerprints", "--help"], ["personas", "--help"]):
            sortie = io.StringIO()
            with contextlib.redirect_stdout(sortie), self.assertRaises(SystemExit):
                m.main(arguments)
            aides.append(sortie.getvalue())
        tout = "\n".join(aides)
        for mot in ("personas", "check", "preflight", "fingerprints", "--mission", "--exclusions"):
            self.assertIn(mot, tout)
        for ancien in ("b2b", "prevol", "empreintes", "--moulinette", "--blacklist", "moulinette", "blacklist"):
            self.assertNotIn(ancien, tout.replace("B2B", ""), f"l'aide ne devrait plus annoncer « {ancien} »")


MOTS_INTERDITS = re.compile(r"blacklist|empreinte|mapping|pr[ée]vol|moulinette", re.IGNORECASE)


def lancer_tout(*arguments: str) -> tuple[int, str, str]:
    """Comme `lancer`, avec ce qui part sur la sortie d'erreur (les messages d'une entrée illisible)."""
    sortie, erreurs = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
        try:
            code = m.main(list(arguments))
        except SystemExit as arret:
            code = arret.code
    return code, sortie.getvalue(), erreurs.getvalue()


class Vocabulaire(unittest.TestCase):
    """Aucun texte qu'un humain lit ne parle de « blacklist », d'« empreinte », de « mapping », de « prévol » ni de « moulinette ».

    On dit « liste d'exclusion », « signature », « colonnes de correspondance », « contrôle avant lancement » et « leadlint ».
    Sont lus : les libellés des contrôles et des critères, les raisons des verdicts, les types de problème, les classeurs (auteur,
    feuilles, tableaux, titres de colonnes, résumé), l'aide et les sorties de la ligne de commande. Les identifiants gardent leur
    nom, ce sont des contrats et non des mots : clés du fichier de mission (`blacklist`, `mappingBlacklist`…), ids de critère et
    d'opérateur (`pas_blackliste`…), noms de fonctions, clés du JSON rendu. Jumeau de `vocabulaire.test.ts`, qui lit les mêmes
    textes du côté de la page web (dépôt de la Marketplace).
    """

    @staticmethod
    def recueillir_le_moteur() -> list[tuple[str, str]]:
        """Libellés, raisons et types de problème que le moteur rend sur les scénarios de parité."""
        textes: list[tuple[str, str]] = []
        for identifiant, (_, libelle, _) in m.DEFINITIONS.items():
            textes.append((f"critère {identifiant}", libelle))
        for operateur, libelle in m.LIBELLES_OPERATEURS.items():
            textes.append((f"opérateur {operateur}", libelle))
        for cle, etiquette in m.ETIQUETTES_SIGNATURES.items():
            textes.append((f"étiquette {cle}", etiquette))
        for scenario in ("esn-validee", "esn-non-validee"):
            rendu = rendu_b2b(lire(f"b2b-{scenario}.entree.json"))
            for controle in rendu["prevol"]["controles"]:
                textes.append((f"{scenario} : contrôle avant lancement « {controle['id']} »", controle["libelle"]))
            for ligne in rendu["lignes"]:
                numero = ligne["numeroLigne"]
                textes.append((f"{scenario}, ligne {numero} : type de problème", ligne["typeProbleme"]))
                textes += [(f"{scenario}, ligne {numero} : raison", raison) for raison in ligne["raisons"]]
                for evaluation in ligne["evaluations"]:
                    textes.append((f"{scenario}, ligne {numero} : libellé de {evaluation['critereId']}", evaluation["libelle"]))
                    textes.append((f"{scenario}, ligne {numero} : raison de {evaluation['critereId']}", evaluation["raison"]))
        return [(origine, texte) for origine, texte in textes if isinstance(texte, str) and texte]

    @staticmethod
    def recueillir_les_sorties() -> list[tuple[str, str]]:
        """Classeurs écrits, texte de la ligne de commande (une ligne = un texte), messages d'erreur et aide."""
        import openpyxl
        textes: list[tuple[str, str]] = []
        for scenario, options in (("esn-validee", ()), ("esn-non-validee", ("--diagnostic",))):
            dossier, export, exclusions, mission = preparer_mission(scenario)
            code, sortie = lancer("check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--json", *options)
            for cle, chemin in json.loads(sortie)["fichiers"].items():
                classeur = openpyxl.load_workbook(chemin)
                origine = f"{scenario} / {cle}"
                textes.append((f"{origine} : auteur", classeur.properties.creator or ""))
                for feuille in classeur.worksheets:
                    textes.append((f"{origine} : nom de feuille", feuille.title))
                    textes += [(f"{origine} / {feuille.title} : nom de tableau", nom) for nom in feuille.tables]
                    for ligne in feuille.iter_rows():
                        textes += [(f"{origine} / {feuille.title} {cellule.coordinate}", cellule.value) for cellule in ligne if isinstance(cellule.value, str)]
        for scenario in ("esn-validee", "esn-non-validee"):
            dossier, export, exclusions, mission = preparer_mission(scenario)
            appels = {
                "check": ["check", str(export), "--mission", str(mission), "--exclusions", str(exclusions)],
                "check --diagnostic": ["check", str(export), "--mission", str(mission), "--exclusions", str(exclusions), "--diagnostic"],
                "preflight": ["preflight", str(export), "--mission", str(mission), "--exclusions", str(exclusions)],
                "preflight sans liste": ["preflight", str(export), "--mission", str(mission)],
                "fingerprints": ["fingerprints", "--mission", str(mission), "--exclusions", str(exclusions)],
                "fingerprints sans liste": ["fingerprints", "--mission", str(mission)],
            }
            for nom, arguments in appels.items():
                code, sortie, erreurs = lancer_tout(*arguments)
                textes += [(f"{scenario} : {nom}, sortie", ligne) for ligne in sortie.splitlines() if ligne.strip()]
                textes += [(f"{scenario} : {nom}, erreur", ligne) for ligne in erreurs.splitlines() if ligne.strip()]
        matrice = RACINE / "skills" / "leadlint" / "matrices" / "matrice-generique.json"
        dossier, export, exclusions, mission = preparer_mission("esn-validee")
        for nom, arguments in {
            "check sur une matrice": ["check", str(export), "--mission", str(matrice)],
            "fingerprints sur une matrice": ["fingerprints", "--mission", str(matrice)],
            "personas sur une mission": ["personas", str(export), "--matrice", str(mission)],
            "check sans fichier": ["check", "introuvable.csv", "--mission", str(mission)],
            "check sans mission": ["check", str(export)],
            "check, option sans valeur": ["check", str(export), "--mission", str(mission), "--exclusions"],
            "fingerprints sans mission": ["fingerprints"],
            "commande inconnue": ["inconnue"],
            "aucune commande": [],
        }.items():
            code, sortie, erreurs = lancer_tout(*arguments)
            textes += [(f"{nom}, message", ligne) for ligne in (sortie + erreurs).splitlines() if ligne.strip()]
        for arguments in (["--help"], ["check", "--help"], ["preflight", "--help"], ["fingerprints", "--help"], ["personas", "--help"]):
            code, sortie, erreurs = lancer_tout(*arguments)
            textes += [(f"aide de leadlint {' '.join(arguments[:-1])}".strip(), ligne) for ligne in sortie.splitlines() if ligne.strip()]
        return textes

    @classmethod
    def setUpClass(cls):
        cls.moteur = cls.recueillir_le_moteur()
        cls.sorties = cls.recueillir_les_sorties()
        cls.textes = cls.moteur + cls.sorties

    def test_aucun_texte_humain_ne_parle_le_jargon(self):
        fautifs = [f"{origine} → {texte}" for origine, texte in self.textes if MOTS_INTERDITS.search(texte)]
        self.assertEqual(fautifs, [])

    def test_le_recueil_a_bien_lu_les_textes_attendus(self):
        """Témoin positif : un recueil qui raterait ces textes serait vert pour rien."""
        lus = [texte for _, texte in self.textes]
        self.assertGreater(len(lus), 500)
        for attendu in (
            "Entreprise absente de la liste d'exclusion", "Entreprise présente dans la liste d'exclusion (nom normalisé)", "Liste d'exclusion",
            "Liste d'exclusion chargée et colonnes de correspondance définies", "Volume et signature de la liste d'exclusion cohérents",
            "Règles, colonnes finales et correspondance des colonnes du fichier candidats complètes",
            "leadlint", "Contrôle QA", "Contrôle leadlint", "ControleLeadlint", "ResumeLeadlint", "Statut leadlint", "Conformes leadlint",
            "Signature de la liste d'exclusion", "Contrôle avant lancement", "Passage livrable (contrôle avant lancement passé)",
            "Diagnostic non livrable (contrôle avant lancement non passé)",
            "Contrôle avant lancement : autorisé", "Lignes de la liste d'exclusion : 5",
            "leadlint « Client fictif / ESN 11-50 » — export.csv",
            "leadlint : Ce fichier est une mission B2B : utilise la commande « check ».", "leadlint : Les signatures ne concernent qu'une mission B2B.",
            "leadlint check: error: the following arguments are required: --mission",
            "leadlint check: error: argument --exclusions: expected one argument",
            "leadlint: error: argument {personas,check,preflight,fingerprints}: invalid choice: 'inconnue' (choose from personas, check, preflight, fingerprints)",
        ):
            self.assertTrue(attendu in lus, f"texte jamais rencontré : {attendu}")
        self.assertTrue(any(texte.startswith("Contrôle avant lancement BLOQUÉ") for texte in lus), "aucune sortie bloquée n'a été lue")
        self.assertTrue(any(texte.startswith("Signature de la liste d'exclusion : fnv1a-") for texte in lus), "aucune signature n'a été lue")
        self.assertTrue(any(texte.startswith("BLOQUÉ — ") for texte in lus), "aucun résumé bloqué n'a été lu")

    def test_un_libelle_sabote_est_vu(self):
        """Contrôle négatif, en mémoire : un ancien libellé remis dans le moteur est trouvé par le recueil."""
        original = m.DEFINITIONS["blacklist"]
        m.DEFINITIONS["blacklist"] = (None, "Entreprise absente de la blacklist", "exclusion")
        try:
            fautifs = [origine for origine, texte in self.recueillir_le_moteur() if MOTS_INTERDITS.search(texte)]
        finally:
            m.DEFINITIONS["blacklist"] = original
        self.assertTrue(fautifs, "le recueil n'a pas vu un libellé qui parle de blacklist")
        self.assertEqual([origine for origine, texte in self.recueillir_le_moteur() if MOTS_INTERDITS.search(texte)], [], "l'original est bien remis")

    def test_un_message_d_usage_sabote_est_vu(self):
        """Contrôle négatif, en mémoire : sans le filtre des messages d'usage, argparse cite `--mission/--moulinette` et la règle le voit."""
        propre = lancer_tout("check", "export.csv")[2]
        self.assertNotRegex(propre, MOTS_INTERDITS, propre)
        original = m._Parseur.error
        m._Parseur.error = m.argparse.ArgumentParser.error
        try:
            brut = lancer_tout("check", "export.csv")[2]
        finally:
            m._Parseur.error = original
        self.assertRegex(brut, MOTS_INTERDITS, brut)
        self.assertEqual(lancer_tout("check", "export.csv")[2], propre, "le filtre est bien remis")

    def test_la_regle_sait_dire_non(self):
        for fautif in ("Entreprise absente de la blacklist", "Empreinte blacklist", "Audit moulinette", "Prévol", "prevol", "Règles et mapping", "pas_blackliste"):
            self.assertRegex(fautif, MOTS_INTERDITS, fautif)
        for correct in ("Entreprise absente de la liste d'exclusion", "Signature de la liste d'exclusion", "Contrôle leadlint", "Contrôle avant lancement", "Colonnes de correspondance"):
            self.assertNotRegex(correct, MOTS_INTERDITS, correct)


class Documentation(unittest.TestCase):
    """Les documents disent « liste d'exclusion » et `--exclusions`, et leurs exemples n'emploient plus les anciens noms."""

    DOCUMENTS = (RACINE / "README.md", RACINE / "skills" / "leadlint" / "SKILL.md")

    def test_les_exemples_de_commande_utilisent_les_noms_actuels(self):
        for chemin in self.DOCUMENTS:
            lignes = chemin.read_text(encoding="utf-8").splitlines()
            exemples = [ligne for ligne in lignes if re.search(r"leadlint(\.py)?\s+(check|preflight|fingerprints|personas|b2b|prevol|empreintes)\b", ligne)]
            with self.subTest(document=chemin.name):
                self.assertGreaterEqual(len(exemples), 3)
                for ligne in exemples:
                    self.assertNotRegex(ligne, r"--blacklist|--moulinette", ligne)
                self.assertTrue(any("--exclusions" in ligne for ligne in exemples), "aucun exemple n'emploie --exclusions")

    def test_les_noms_d_avant_sont_dits_une_fois_comme_encore_acceptes(self):
        for chemin in self.DOCUMENTS:
            texte = chemin.read_text(encoding="utf-8")
            with self.subTest(document=chemin.name):
                self.assertIn("--blacklist", texte)
                self.assertRegex(texte, r"(?i)older names|still (work|accepted)|are accepted")


class Exemples(unittest.TestCase):
    """Les fichiers d'exemple du dépôt sont ceux que la documentation annonce, et ceux que la page fait télécharger."""

    EXPORT = EXEMPLES / "export-exemple.csv"
    EXCLUSIONS = EXEMPLES / "exclusions-exemple.csv"
    VALIDEE = EXEMPLES / "mission-esn-11-50-validee.json"
    MODELE = EXEMPLES / "modele-mission-esn-11-50.json"

    def test_la_mission_d_exemple_est_la_mission_que_la_page_fait_telecharger(self):
        """À l'identité près (mission, description), l'exemple est le fichier de l'oracle : il ne reste pas en arrière de la page."""
        exemple = json.loads(self.VALIDEE.read_text(encoding="utf-8"))
        telechargee = json.loads((PARITE / "mission-telechargee.json").read_text(encoding="utf-8"))
        for identite in ("mission", "description"):
            exemple.pop(identite), telechargee.pop(identite)
        self.assertEqual(exemple, telechargee)

    def test_la_mission_d_exemple_passe_le_controle_avant_lancement_et_le_modele_le_refuse(self):
        code, sortie = lancer("preflight", str(self.EXPORT), "--mission", str(self.VALIDEE), "--exclusions", str(self.EXCLUSIONS), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(sortie)["prevol"]["autorise"])
        code, sortie = lancer("preflight", str(self.EXPORT), "--mission", str(self.MODELE), "--exclusions", str(self.EXCLUSIONS), "--json")
        self.assertEqual(code, 2)
        refuses = {c["id"] for c in json.loads(sortie)["prevol"]["controles"] if c["bloquant"] and not c["valide"]}
        self.assertLessEqual({"strategie", "recherche_validee", "empreinte"}, refuses)

    def test_les_chiffres_du_readme_sont_ceux_de_l_exemple(self):
        """Quinze conformes, quatre à vérifier, trente et un rejetés : ce que le README annonce sur les 50 lignes d'exemple."""
        with tempfile.TemporaryDirectory(prefix="leadlint-") as dossier:
            code, sortie = lancer("check", str(self.EXPORT), "--mission", str(self.VALIDEE), "--exclusions", str(self.EXCLUSIONS), "-o", dossier, "--json")
        bilan = json.loads(sortie)
        self.assertEqual(code, 0)
        self.assertEqual((bilan["total"], bilan["conformes"], bilan["a_verifier"], bilan["rejetes"]), (50, 15, 4, 31))
        self.assertEqual(bilan["causes"]["Rejeté"].get("Liste d'exclusion"), 5)
        readme = (RACINE / "README.md").read_text(encoding="utf-8")
        for annonce in ("| Raw export | **50** |", "| `Conforme` — deliverable | **15** |", "| `Rejeté` | **31** |", "exclusion list 5"):
            self.assertIn(annonce, readme)


class Paquet(unittest.TestCase):
    """Le moteur est une seule copie ; installé en paquet, il retrouve ses matrices à côté de lui."""

    def installer_comme_un_paquet(self, matrice_generique: dict | None = None) -> Path:
        """Reproduit la disposition de site-packages : le module et `leadlint_data/matrices/` côte à côte."""
        site = Path(tempfile.mkdtemp(prefix="leadlint-site-"))
        shutil.copy(SCRIPT, site / "leadlint.py")
        donnees = site / "leadlint_data" / "matrices"
        shutil.copytree(RACINE / "skills" / "leadlint" / "matrices", donnees)
        if matrice_generique is not None:
            (donnees / "matrice-generique.json").write_text(json.dumps(matrice_generique, ensure_ascii=False), encoding="utf-8")
        return site

    def personas(self, script: Path) -> dict:
        dossier = Path(tempfile.mkdtemp(prefix="leadlint-travail-"))
        contacts = dossier / "contacts.csv"
        ecrire_csv(contacts, [{"Nom": "A", "Job title": "Directeur RSE"}, {"Nom": "B", "Job title": "Chargé IT"}], ",")
        resultat = subprocess.run(
            [sys.executable, str(script), "personas", str(contacts), "-o", str(dossier / "qualifie.xlsx"), "--json"],
            capture_output=True, text=True, encoding="utf-8", cwd=dossier,
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        bilan = json.loads(resultat.stdout)
        bilan.pop("fichier")
        return bilan

    def test_matrice_par_defaut_trouvee_depuis_la_competence(self):
        self.assertEqual(m.MATRICE_DEFAUT, RACINE / "skills" / "leadlint" / "matrices" / "matrice-generique.json")
        self.assertTrue(m.MATRICE_DEFAUT.is_file())

    def test_matrice_par_defaut_trouvee_une_fois_installe(self):
        site = self.installer_comme_un_paquet()
        self.assertEqual(self.personas(site / "leadlint.py"), self.personas(SCRIPT))

    def test_la_matrice_livree_a_cote_du_module_est_bien_celle_lue(self):
        """Contrôle négatif : une matrice marquée, posée à côté du module installé, se retrouve dans le résultat."""
        marquee = json.loads((RACINE / "skills" / "leadlint" / "matrices" / "matrice-generique.json").read_text(encoding="utf-8"))
        marquee["axes"][0]["libelle"] = "AXE-MARQUE-PAR-LE-TEST"
        site = self.installer_comme_un_paquet(marquee)
        self.assertEqual(self.personas(site / "leadlint.py")["axes"][0]["libelle"], "AXE-MARQUE-PAR-LE-TEST")
        self.assertNotEqual(self.personas(SCRIPT)["axes"][0]["libelle"], "AXE-MARQUE-PAR-LE-TEST")

    def test_un_seul_moteur_dans_le_depot(self):
        moteurs = [p for p in RACINE.rglob("leadlint.py") if not {".git", "dist", "build", ".venv"} & set(p.parts)]
        self.assertEqual(moteurs, [SCRIPT])

    def test_versions_alignees(self):
        for nom in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            donnees = json.loads((RACINE / nom).read_text(encoding="utf-8"))
            self.assertEqual(donnees.get("version") or donnees["metadata"]["version"], m.VERSION, nom)

    def test_pyproject_declare_la_commande_et_la_source_unique(self):
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            self.skipTest("tomllib absent (Python < 3.11)")
        projet = tomllib.loads((RACINE / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(projet["project"]["name"], "leadlint")
        self.assertEqual(projet["project"]["scripts"], {"leadlint": "leadlint:main"})
        self.assertIn("version", projet["project"]["dynamic"])
        self.assertEqual(projet["tool"]["hatch"]["version"]["path"], "skills/leadlint/scripts/leadlint.py")
        livre = projet["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(livre["skills/leadlint/scripts/leadlint.py"], "leadlint.py")
        for source in livre:
            self.assertTrue((RACINE / source).exists(), source)


if __name__ == "__main__":
    unittest.main(verbosity=1)
