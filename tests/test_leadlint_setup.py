# -*- coding: utf-8 -*-
"""Banc de leadlint-setup : des réponses jusqu'au fichier nettoyé, avec la page des règles.

    python tests/test_leadlint_setup.py

L'exemple de `skills/leadlint-setup/exemples/` contient 30 lignes fictives avec des cas plantés. Les chiffres attendus
sont comptés à la main, ligne par ligne, dans `LIGNES_ATTENDUES` : le banc ne les recopie pas d'une sortie. Plusieurs
contrôles sont négatifs : une règle changée après la validation, une liste d'exclusion changée, une validation
effacée doivent bloquer, et le même changement doit bien modifier les verdicts quand le contrôle est contourné.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import http.client
import io
import json
import os
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
SCRIPTS = RACINE / "skills" / "leadlint" / "scripts"
EXEMPLES = RACINE / "skills" / "leadlint-setup" / "exemples"
sys.path.insert(0, str(SCRIPTS))

import leadlint as m  # noqa: E402
import leadlint_setup as s  # noqa: E402

TIRET_CADRATIN = chr(0x2014)

# Compté à la main sur export-brut-exemple.csv : ligne -> (verdict, cause ou cible).
# TOUT COCHÉ : tous les filtres de l'exemple sont actifs (intitulés à écarter, doublons, liste d'exclusion, lignes incomplètes, pays, activité, taille).
LIGNES_TOUT = {
    1: ("Conforme", "Dirigeants"), 2: ("Conforme", "Direction commerciale"), 3: ("Conforme", "Dirigeants"),
    4: ("Conforme", "Direction commerciale"), 5: ("Conforme", "Dirigeants"), 6: ("Conforme", "Dirigeants"),
    7: ("Conforme", "Direction commerciale"), 8: ("Conforme", "Dirigeants"), 9: ("Conforme", "Direction commerciale"),
    10: ("Conforme", "Dirigeants"), 11: ("Rejeté", "Mauvais intitulé de poste"), 12: ("Rejeté", "Mauvais intitulé de poste"),
    13: ("Rejeté", "Mauvais intitulé de poste"), 14: ("Rejeté", "Mauvais intitulé de poste"), 15: ("Rejeté", "Doublon"),
    16: ("Rejeté", "Doublon"), 17: ("Rejeté", "Liste d'exclusion"), 18: ("Rejeté", "Liste d'exclusion"),
    19: ("Rejeté", "Mauvais intitulé de poste"), 20: ("Rejeté", "Mauvais intitulé de poste"), 21: ("Conforme", "Dirigeants"),
    22: ("Rejeté", "Pays"), 23: ("Rejeté", "Employés visibles LinkedIn"), 24: ("Rejeté", "Employés visibles LinkedIn"),
    25: ("Rejeté", "Mauvaise entreprise"), 26: ("Rejeté", "Mauvaise entreprise"), 27: ("Conforme", "Dirigeants"),
    28: ("Rejeté", "Champ obligatoire manquant : entreprise"), 29: ("Rejeté", "Mauvais intitulé de poste"), 30: ("Conforme", "Direction commerciale"),
}
CAUSES_TOUT = {
    "Mauvais intitulé de poste": 7, "Doublon": 2, "Liste d'exclusion": 2, "Employés visibles LinkedIn": 2,
    "Mauvaise entreprise": 2, "Pays": 1, "Champ obligatoire manquant " + TIRET_CADRATIN + " entreprise": 1,
}
PAR_CIBLE_TOUT = {"Dirigeants": 8, "Direction commerciale": 5}
CHIFFRES_TOUT = (30, 13, 0, 17)  # lignes, gardées, à vérifier, rejetées

# CASES PAR DÉFAUT : l'intitulé de poste, les intitulés à écarter et les doublons ; tout le reste décoché.
# Les lignes 17 et 18 (liste d'exclusion), 21 (Gmail), 22 (Belgique), 23 et 24 (taille), 25 et 26 (activité), 27 (sans email) et
# 28 (sans entreprise) passent : aucun de ces filtres n'est coché.
LIGNES_DEFAUT = {
    1: ("Conforme", "Dirigeants"), 2: ("Conforme", "Direction commerciale"), 3: ("Conforme", "Dirigeants"),
    4: ("Conforme", "Direction commerciale"), 5: ("Conforme", "Dirigeants"), 6: ("Conforme", "Dirigeants"),
    7: ("Conforme", "Direction commerciale"), 8: ("Conforme", "Dirigeants"), 9: ("Conforme", "Direction commerciale"),
    10: ("Conforme", "Dirigeants"), 11: ("Rejeté", "Mauvais intitulé de poste"), 12: ("Rejeté", "Mauvais intitulé de poste"),
    13: ("Rejeté", "Mauvais intitulé de poste"), 14: ("Rejeté", "Mauvais intitulé de poste"), 15: ("Rejeté", "Doublon"),
    16: ("Rejeté", "Doublon"), 17: ("Conforme", "Dirigeants"), 18: ("Conforme", "Direction commerciale"),
    19: ("Rejeté", "Mauvais intitulé de poste"), 20: ("Rejeté", "Mauvais intitulé de poste"), 21: ("Conforme", "Dirigeants"),
    22: ("Conforme", "Dirigeants"), 23: ("Conforme", "Dirigeants"), 24: ("Conforme", "Dirigeants"),
    25: ("Conforme", "Dirigeants"), 26: ("Conforme", "Dirigeants"), 27: ("Conforme", "Dirigeants"),
    28: ("Conforme", "Dirigeants"), 29: ("Rejeté", "Mauvais intitulé de poste"), 30: ("Conforme", "Direction commerciale"),
}
CAUSES_DEFAUT = {"Mauvais intitulé de poste": 7, "Doublon": 2}
PAR_CIBLE_DEFAUT = {"Dirigeants": 15, "Direction commerciale": 6}
CHIFFRES_DEFAUT = (30, 21, 0, 9)
CASES_DEFAUT = {"poste": True, "postes_exclus": True, "doublons": True, "liste_exclusion": False, "champs_obligatoires": False, "pays": False, "activite": False, "taille": False}
CASES_TOUT = {cle: True for cle in CASES_DEFAUT}
PHRASE_FILTRES_DEFAUT = ("Filtres actifs : Intitulé de poste dans mes cibles, Intitulés à écarter, Doublons"
                         " · désactivés : Liste d'exclusion, Lignes incomplètes, Pays, Activité, Taille")
SCEAU_ATTENTE = "Pas encore validées : rien n'est livré tant que vous n'avez pas relu"
MOTS_INTERDITS = re.compile(r"blacklist|empreinte|mapping|pr[ée]vol", re.IGNORECASE)


def lire_exemple() -> dict:
    return json.loads((EXEMPLES / "reponses-exemple.json").read_text(encoding="utf-8"))


def setup(*arguments: str) -> tuple:
    """leadlint-setup dans ce processus : (code, sortie, erreurs)."""
    sortie, erreurs = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
        try:
            code = s.main(list(arguments))
        except SystemExit as arret:
            code = arret.code
    return code, sortie.getvalue(), erreurs.getvalue()


def moulinette(dossier: Path, *arguments: str, cwd: Path = None) -> subprocess.CompletedProcess:
    """Le script de la personne, lancé comme elle le lance : un autre répertoire courant, sans le dépôt dans le chemin des modules."""
    environnement = {cle: valeur for cle, valeur in os.environ.items() if cle != "PYTHONPATH"}
    environnement["PYTHONIOENCODING"] = "utf-8"
    ailleurs = cwd or Path(tempfile.mkdtemp(prefix="leadlint-ailleurs-"))
    return subprocess.run(
        [sys.executable, str(dossier / "moulinette.py"), *arguments], capture_output=True, text=True, encoding="utf-8",
        cwd=ailleurs, env=environnement,
    )


def texte_visible(page: str) -> str:
    sans_style = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", sans_style))


class Banc(unittest.TestCase):
    def nouveau_dossier(self, prefixe: str = "leadlint-setup-") -> Path:
        dossier = Path(tempfile.mkdtemp(prefix=prefixe))
        self.addCleanup(shutil.rmtree, dossier, True)
        return dossier

    def monter(self, reponses: dict = None, exclusions: bool = True, valider: bool = True, coches: dict = None) -> Path:
        """Un dossier de travail créé (et, par défaut, validé) à partir des réponses."""
        travail = self.nouveau_dossier()
        fichier = travail / "reponses.json"
        fichier.write_text(json.dumps(reponses if reponses is not None else lire_exemple(), ensure_ascii=False), encoding="utf-8")
        dossier = travail / "ma-moulinette"
        arguments = ["create", str(fichier), "-o", str(dossier)]
        liste = EXEMPLES / "exclusions-exemple.csv"
        if exclusions:
            arguments += ["--exclusions", str(liste)]
        code, sortie, erreurs = setup(*arguments)
        self.assertEqual(code, 0, sortie + erreurs)
        if valider:
            code, sortie, erreurs = setup("validate", str(dossier), "--date", "2026-11-17")
            self.assertEqual(code, 0, sortie + erreurs)
        if coches:
            self.cocher(dossier, **coches)
        return dossier

    def cocher(self, dossier: Path, **cases: bool) -> None:
        """Pose des cases dans reglages.json (comme la page le fait) ; les autres gardent leur valeur."""
        _, configuration = m.charger_mission(dossier / "mission.json")
        filtres = s.etat_filtres(dossier, configuration)
        for filtre in filtres:
            if filtre["id"] in cases:
                filtre["actif"] = cases[filtre["id"]]
        s.ecrire_choix(dossier, filtres)

    def tout_cocher(self, dossier: Path) -> None:
        """Toutes les cases que la mission porte (y compris celles que l'exemple n'a pas toujours)."""
        _, configuration = m.charger_mission(dossier / "mission.json")
        self.cocher(dossier, **{f["id"]: True for f in s.filtres_de_la_mission(configuration)})

    def reglages(self, dossier: Path) -> dict:
        return json.loads((dossier / "reglages.json").read_text(encoding="utf-8"))["filtres"]

    def mission(self, dossier: Path) -> dict:
        return json.loads((dossier / "mission.json").read_text(encoding="utf-8"))

    def ecrire_mission(self, dossier: Path, donnees: dict) -> None:
        (dossier / "mission.json").write_text(json.dumps(donnees, ensure_ascii=False, indent=2), encoding="utf-8")

    def passage(self, dossier: Path) -> dict:
        return json.loads((dossier / "dernier-passage.json").read_text(encoding="utf-8"))

    def page(self, dossier: Path) -> str:
        return (dossier / "regles.html").read_text(encoding="utf-8")

    EXPORT = EXEMPLES / "export-brut-exemple.csv"


class Creation(Banc):
    def test_create_ecrit_tous_les_fichiers(self):
        dossier = self.monter(valider=False)
        for nom in ("mission.json", "matrice.json", "leadlint.py", "leadlint_setup.py", "moulinette.py", "regles.html", "reponses.json", "exclusions.csv"):
            self.assertTrue((dossier / nom).is_file(), nom)
        self.assertEqual((dossier / "leadlint.py").read_bytes(), (SCRIPTS / "leadlint.py").read_bytes(), "le moteur est copié tel quel")
        self.assertEqual((dossier / "leadlint_setup.py").read_bytes(), (SCRIPTS / "leadlint_setup.py").read_bytes())
        self.assertEqual((dossier / "reponses.json").read_bytes(), (dossier.parent / "reponses.json").read_bytes())
        self.assertEqual((dossier / "exclusions.csv").read_bytes(), (EXEMPLES / "exclusions-exemple.csv").read_bytes())
        self.assertFalse(list(dossier.glob("*.tmp")), "aucun fichier temporaire ne reste")

    def test_la_phrase_de_fin_et_le_json_pour_un_agent(self):
        travail = self.nouveau_dossier()
        code, sortie, _ = setup("create", str(EXEMPLES / "reponses-exemple.json"), "-o", str(travail / "a"), "--exclusions", str(EXEMPLES / "exclusions-exemple.csv"))
        self.assertEqual(code, 0)
        self.assertIn("Ouvrez regles.html, relisez vos règles, puis validez.", sortie.splitlines()[-1])
        code, sortie, _ = setup("create", str(EXEMPLES / "reponses-exemple.json"), "-o", str(travail / "b"), "--json")
        donnees = json.loads(sortie)
        self.assertEqual((code, donnees["validee"], donnees["liste_exclusion_requise"], donnees["liste_exclusion_fournie"]), (0, False, True, False))
        self.assertEqual(donnees["suite"], "Ouvrez regles.html, relisez vos règles, puis validez.")

    def test_la_mission_n_est_pas_validee_a_la_creation(self):
        dossier = self.monter(valider=False)
        mission = self.mission(dossier)
        origine = mission["configuration"]["origine"]
        self.assertEqual(origine["type"], "cible")
        self.assertEqual(origine["valideeLe"], "")
        self.assertEqual(origine["empreinteRegles"], "")
        self.assertEqual(origine["empreinteCible"], hashlib.sha256((dossier / "reponses.json").read_bytes()).hexdigest())
        self.assertEqual((mission["client"], mission["mission"], mission["mode"]), ("Camille Exemple", "Prospection ESN exemple", "revalidation_b2b"))
        _, configuration = m.charger_mission(dossier / "mission.json")
        self.assertFalse(m.cible_validee(configuration))

    def test_les_reponses_deviennent_les_regles_prevues(self):
        dossier = self.monter(valider=False)
        regles = {r["id"]: r for r in self.mission(dossier)["configuration"]["regles"]}
        self.assertEqual(list(regles), [
            "r-pays-est_dans", "r-poste-contient_un", "r-poste-ne_contient_aucun", "r-activite-contient_un", "r-activite-ne_contient_aucun",
            "r-employes_visibles-entre", "r-blacklist-pas_blackliste", "r-doublon-unique",
        ])
        voulu = regles["r-poste-contient_un"]
        self.assertEqual((voulu["importance"], voulu["siAbsent"], voulu.get("motEntier")), ("bloquant", "rejeter", True))
        self.assertEqual(voulu["valeur"], ["CEO", "Fondateur", "Fondatrice", "Founder", "Co-founder", "Co-fondateur", "Gérant", "Directeur général", "Président",
                                           "Directeur commercial", "Directrice commerciale", "Head of Sales", "VP Sales"])
        ecarte = regles["r-poste-ne_contient_aucun"]
        self.assertEqual((ecarte["siAbsent"], ecarte.get("motEntier")), ("ignorer", True))
        self.assertEqual(regles["r-pays-est_dans"]["siAbsent"], "ignorer")
        self.assertEqual(regles["r-employes_visibles-entre"]["valeur"], {"min": 11, "max": 200})
        self.assertNotIn("r-email_pro-email_professionnel", regles, "leadlint-setup ne contrôle ni les emails ni les mobiles")
        self.assertTrue(all(r["active"] is True for r in regles.values()), "mission.json porte toutes les règles, actives : les cases ne la touchent pas")
        self.assertNotIn("motEntier", regles["r-pays-est_dans"])
        configuration = self.mission(dossier)["configuration"]
        self.assertEqual(configuration["blacklist"], {"obligatoire": True, "nom": "Liste d'exclusion", "typeSource": "fichier"})
        self.assertEqual([c["champ"] for c in configuration["colonnesFinales"]], ["nomComplet", "entreprise", "poste", "linkedinProfil", "emailProfessionnel", "mobile"])
        self.assertEqual(configuration["champsObligatoires"], ["nomComplet", "entreprise", "poste"])
        self.assertNotIn("pharow", configuration)
        matrice = json.loads((dossier / "matrice.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["categorie"], r["priorite"]) for r in matrice["axes"][0]["regles"]], [("Dirigeants", 1), ("Direction commerciale", 2)])
        self.assertEqual(matrice["axes"][0]["id"], "cible")

    def test_aller_retour_par_le_moteur(self):
        dossier = self.monter(valider=False)
        identite, configuration = m.charger_mission(dossier / "mission.json")
        self.assertEqual((identite["client"], configuration["mode"]), ("Camille Exemple", "revalidation_b2b"))
        _, relue = m.charger_mission(dossier / "mission.json")
        controles = {c["id"]: c for c in s._controles_hors_export(relue, 3, "fnv1a-x")}
        self.assertTrue(controles["configuration"]["valide"])
        self.assertFalse(controles["cible_validee"]["valide"], "une mission neuve n'est pas validée")

    def test_sans_liste_d_exclusion_pas_de_regle_ni_de_blocage(self):
        reponses = lire_exemple()
        reponses["liste_exclusion"] = False
        dossier = self.monter(reponses, exclusions=False)
        configuration = self.mission(dossier)["configuration"]
        self.assertNotIn("pas_blackliste", [r["operateur"] for r in configuration["regles"]])
        self.assertFalse(configuration["blacklist"]["obligatoire"])
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        passage = self.passage(dossier)
        self.assertEqual((passage["total"], passage["conformes"], passage["rejetes"]), (30, 21, 9), "les deux lignes de la liste d'exclusion passent sans elle")
        self.assertNotIn("Liste d'exclusion", passage["causes"]["Rejeté"])
        self.assertNotIn("liste_exclusion", self.reglages(dossier), "pas de règle, pas de case")
        self.assertIn("Aucune liste d'exclusion.", texte_visible(self.page(dossier)))

    def test_refuse_d_ecraser_une_mission_validee_sans_force(self):
        dossier = self.monter()
        avant = (dossier / "mission.json").read_bytes()
        code, _, erreurs = setup("create", str(dossier / "reponses.json"), "-o", str(dossier))
        self.assertEqual(code, 1)
        self.assertIn("déjà validée", erreurs)
        self.assertEqual((dossier / "mission.json").read_bytes(), avant, "rien n'a été touché")
        code, _, erreurs = setup("create", str(dossier / "reponses.json"), "-o", str(dossier), "--force")
        self.assertEqual(code, 0, erreurs)
        self.assertEqual(self.mission(dossier)["configuration"]["origine"]["valideeLe"], "", "--force efface la validation")

    def test_create_deux_fois_sans_validation_est_permis(self):
        dossier = self.monter(valider=False)
        code, _, erreurs = setup("create", str(dossier / "reponses.json"), "-o", str(dossier))
        self.assertEqual(code, 0, erreurs)

    def test_entete_du_script_lisible_et_sans_injection(self):
        reponses = lire_exemple()
        reponses["nom"] = "Ma\nimport os; os.system('x')"
        reponses["cibles"][0]["nom"] = "Dirigeants\nimport sys; sys.exit(3)"
        dossier = self.monter(reponses)
        texte = (dossier / "moulinette.py").read_text(encoding="utf-8")
        self.assertIn("généré par leadlint-setup", texte)
        self.assertIn("ne dépend d'aucun compte", texte)
        self.assertEqual([l for l in texte.splitlines() if l.startswith("import os") or l.startswith("import sys; ")], [])
        py_compile.compile(str(dossier / "moulinette.py"), doraise=True)
        reponses_saines = lire_exemple()
        en_tete = (self.monter(reponses_saines, valider=False) / "moulinette.py").read_text(encoding="utf-8")
        self.assertIn("Moulinette : Prospection ESN exemple", en_tete)
        self.assertIn("Cibles : Dirigeants, Direction commerciale", en_tete)
        self.assertRegex(en_tete, r"Créée le \d{4}-\d{2}-\d{2}")

    def test_les_reponses_peuvent_vivre_dans_le_dossier_de_sortie(self):
        travail = self.nouveau_dossier()
        (travail / "reponses.json").write_text(json.dumps(lire_exemple(), ensure_ascii=False), encoding="utf-8")
        code, _, erreurs = setup("create", str(travail / "reponses.json"), "-o", str(travail))
        self.assertEqual(code, 0, erreurs)
        self.assertTrue((travail / "moulinette.py").is_file())

    def test_les_libelles_couvrent_tous_les_champs_du_moteur(self):
        self.assertEqual(set(s.LIBELLES_CHAMPS), set(m.ENTETES_CANDIDATS))


class ReponsesInvalides(Banc):
    def tenter(self, modifier, attendu: str) -> None:
        reponses = lire_exemple()
        modifier(reponses)
        travail = self.nouveau_dossier()
        fichier = travail / "reponses.json"
        fichier.write_text(json.dumps(reponses, ensure_ascii=False), encoding="utf-8")
        code, sortie, erreurs = setup("create", str(fichier), "-o", str(travail / "sortie"))
        self.assertEqual(code, 1, sortie)
        self.assertIn(attendu, erreurs)
        self.assertNotIn("Traceback", erreurs)
        self.assertFalse((travail / "sortie").exists(), "des réponses invalides n'écrivent rien")

    def test_cas_invalides(self):
        cas = [
            (lambda r: r.pop("cibles"), "au moins une cible"),
            (lambda r: r.update(cibles=[]), "au moins une cible"),
            (lambda r: r["cibles"][0].update(intitules=[]), "n'a aucun intitulé"),
            (lambda r: r["cibles"][0].pop("nom"), "n'a pas de nom"),
            (lambda r: r["cibles"][1].update(nom="dirigeants"), "Deux cibles portent le nom"),
            (lambda r: r.update(taille={"min": 300, "max": 20}), "plus grand"),
            (lambda r: r.update(taille={"min": "dix"}), "doit être un nombre"),
            (lambda r: r.update(colonnes={"poste_inconnu": "X"}), "n'est pas un champ connu"),
            (lambda r: r.update(champs_obligatoires=["nimporte"]), "n'est pas un champ connu"),
            (lambda r: r.update(cible="faute de frappe"), "Clé inconnue"),
            (lambda r: r["cibles"][0].update(intitules=["---"]), "ni lettre ni chiffre"),
            (lambda r: r.update(liste_exclusion="oui"), "true ou false"),
            (lambda r: r.update(pays=[3]), "que du texte"),
            (lambda r: r.update(secteurs={"autre": []}), "ne connaît que"),
            (lambda r: r.update(email_pro_obligatoire=True), "l'enrichissement vient après le nettoyage, leadlint-setup ne contrôle pas les emails ni les mobiles"),
            (lambda r: r.update(mobile_obligatoire=True), "l'enrichissement vient après le nettoyage, leadlint-setup ne contrôle pas les emails ni les mobiles"),
            (lambda r: r.update(email_pro_obligatoire=False), "n'existe plus"),
        ]
        for modifier, attendu in cas:
            with self.subTest(attendu=attendu):
                self.tenter(modifier, attendu)

    def test_fichier_illisible_ou_absent(self):
        travail = self.nouveau_dossier()
        (travail / "casse.json").write_text("{pas du json", encoding="utf-8")
        code, _, erreurs = setup("create", str(travail / "casse.json"), "-o", str(travail / "s"))
        self.assertEqual(code, 1)
        self.assertIn("illisibles", erreurs)
        code, _, erreurs = setup("create", str(travail / "absent.json"), "-o", str(travail / "s"))
        self.assertEqual(code, 1)
        self.assertIn("introuvables", erreurs)
        (travail / "liste.json").write_text("[]", encoding="utf-8")
        code, _, erreurs = setup("create", str(travail / "liste.json"), "-o", str(travail / "s"))
        self.assertEqual(code, 1)
        self.assertFalse((travail / "s").exists())

    def test_liste_d_exclusion_sans_colonne_reconnue(self):
        travail = self.nouveau_dossier()
        (travail / "liste.csv").write_text("Truc,Machin\na,b\n", encoding="utf-8")
        code, _, erreurs = setup("create", str(EXEMPLES / "reponses-exemple.json"), "-o", str(travail / "s"), "--exclusions", str(travail / "liste.csv"))
        self.assertEqual(code, 1)
        self.assertIn("Je ne reconnais aucune colonne", erreurs)
        self.assertFalse((travail / "s").exists())


class Validation(Banc):
    def test_valider_sans_liste_quand_elle_est_attendue(self):
        dossier = self.monter(exclusions=False, valider=False)
        code, _, erreurs = setup("validate", str(dossier))
        self.assertEqual(code, 1)
        self.assertIn("liste d'exclusion", erreurs)
        self.assertEqual(self.mission(dossier)["configuration"]["origine"]["valideeLe"], "", "rien n'a été validé")

    def test_valider_avec_la_liste_en_argument_l_inscrit(self):
        dossier = self.monter(exclusions=False, valider=False)
        code, sortie, erreurs = setup("validate", str(dossier), "--exclusions", str(EXEMPLES / "exclusions-exemple.csv"), "--date", "2026-11-17", "--json")
        self.assertEqual(code, 0, erreurs)
        donnees = json.loads(sortie)
        configuration = self.mission(dossier)["configuration"]
        self.assertEqual(configuration["origine"]["valideeLe"], "2026-11-17")
        self.assertEqual(configuration["origine"]["empreinteRegles"], donnees["signature_regles"])
        self.assertEqual(configuration["blacklist"]["volumeAttendu"], 3)
        self.assertEqual(configuration["blacklist"]["miseAJourLe"], "2026-11-17")
        self.assertTrue(configuration["blacklist"]["empreinte"].startswith("fnv1a-"))
        self.assertTrue(configuration["mappingBlacklist"])
        self.assertTrue((dossier / "exclusions.csv").is_file(), "la liste est gardée dans le dossier")
        _, relue = m.charger_mission(dossier / "mission.json")
        self.assertTrue(m.cible_validee(relue), "le moteur relit la validation comme valide")
        self.assertEqual(configuration["origine"]["empreinteRegles"], m.empreinte_regles(relue))

    def test_date_invalide(self):
        dossier = self.monter(valider=False)
        code, _, erreurs = setup("validate", str(dossier), "--date", "hier")
        self.assertEqual(code, 1)
        self.assertIn("Date illisible", erreurs)

    def test_l_aide_de_validate_dit_l_accord_explicite(self):
        code, sortie, _ = setup("validate", "--help")
        self.assertEqual(code, 0)
        self.assertIn("EXPLICITLY AGREED", sortie)


class Moulinette(Banc):
    def test_avant_validation_rien_n_est_livre(self):
        dossier = self.monter(valider=False)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
        self.assertIn("Vos règles ne sont pas encore validées", processus.stdout)
        self.assertIn("regles.html", processus.stdout)
        self.assertIn("validate", processus.stdout)
        self.assertIn("rien n'est livré", processus.stdout)
        self.assertEqual(list((dossier / "resultats").glob("*")) if (dossier / "resultats").exists() else [], [], "aucun fichier livré")
        self.assertFalse((dossier / "dernier-passage.json").exists())

    def test_apres_validation_les_chiffres_attendus(self):
        dossier = self.monter()
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        sortie = processus.stdout
        self.assertIn("Lignes 30 · gardées 21 · à vérifier 0 · rejetées 9", sortie)
        self.assertIn("Contrôles du bilan : justes", sortie)
        self.assertIn(PHRASE_FILTRES_DEFAUT, sortie)
        self.assertNotIn(TIRET_CADRATIN, sortie)
        passage = self.passage(dossier)
        self.assertEqual((passage["total"], passage["conformes"], passage["a_verifier"], passage["rejetes"]), CHIFFRES_DEFAUT)
        self.assertEqual({f["id"]: f["actif"] for f in passage["filtres"]}, CASES_DEFAUT, "le bilan dit quels filtres étaient actifs")
        self.assertEqual(passage["total"], passage["conformes"] + passage["a_verifier"] + passage["rejetes"], "premier invariant")
        self.assertEqual(passage["a_verifier"] + passage["rejetes"], sum(passage["causes"]["Rejeté"].values()) + sum(passage["causes"]["À vérifier"].values()), "second invariant")
        self.assertEqual(passage["causes"]["Rejeté"], CAUSES_DEFAUT)
        self.assertEqual(passage["causes"]["À vérifier"], {})
        self.assertEqual(passage["par_cible"], PAR_CIBLE_DEFAUT)
        self.assertEqual(passage["regles_non_appliquees"], [])
        self.assertEqual(passage["fichier"], "export-brut-exemple.csv")
        self.assertEqual(passage["moteur"], f"leadlint {m.VERSION}")
        for chemin in passage["fichiers"].values():
            self.assertTrue((dossier / chemin).is_file(), chemin)

    def test_verdict_ligne_par_ligne_cases_par_defaut(self):
        """Le contrôle ligne par ligne d'un passage, cases par défaut, donne les verdicts comptés à la main."""
        import openpyxl
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        feuille = openpyxl.load_workbook(dossier / "resultats" / "export-brut-exemple-controle-qa.xlsx")["Contrôle QA"]
        lignes = [[c.value for c in ligne] for ligne in feuille.iter_rows(min_row=2)]
        obtenu = {ligne[14] - 1: (ligne[0], (ligne[9] or "").replace(" " + TIRET_CADRATIN + " ", " : ")) for ligne in lignes}
        self.assertEqual(len(obtenu), 30)
        for numero, (statut, cause) in LIGNES_DEFAUT.items():
            with self.subTest(ligne=numero):
                self.assertEqual(obtenu[numero][0], statut)
                if statut == "Rejeté":
                    self.assertEqual(obtenu[numero][1], cause)
                else:
                    self.assertEqual(obtenu[numero][1], "")

    def test_verdict_ligne_par_ligne(self):
        """Les causes de l'exemple, tout coché, ligne par ligne, comptées à la main : le moteur de ce dossier (mission complète) donne les mêmes."""
        dossier = self.monter()
        entetes, lignes = m.lire_tableau(self.EXPORT)
        _, configuration = m.charger_mission(dossier / "mission.json")
        configuration["mappingCandidats"] = m.completer_mapping(configuration["mappingCandidats"], entetes, m.ENTETES_CANDIDATS)
        liste_entetes, liste_lignes = m.lire_tableau(dossier / "exclusions.csv")
        configuration["mappingBlacklist"] = m.completer_mapping(configuration["mappingBlacklist"], liste_entetes, m.ENTETES_BLACKLIST)
        verdicts = m.valider_export_b2b(m.vers_objets(entetes, lignes), m.vers_objets(liste_entetes, liste_lignes), configuration)
        obtenu = {v["numeroLigne"] - 1: (v["statut"], m.type_probleme(v).replace(" " + TIRET_CADRATIN + " ", " : ") or None) for v in verdicts}
        for ligne, (statut, cause) in LIGNES_TOUT.items():
            with self.subTest(ligne=ligne):
                if statut == "Conforme":
                    self.assertEqual(obtenu[ligne], ("Conforme", None))
                else:
                    self.assertEqual(obtenu[ligne], (statut, cause))

    def test_le_fichier_nettoye_garde_les_colonnes_d_origine_et_porte_la_cible(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        import openpyxl
        classeur = openpyxl.load_workbook(dossier / "resultats" / "export-brut-exemple-nettoye.xlsx")
        feuille = classeur.worksheets[0]
        lignes = [[c.value for c in ligne] for ligne in feuille.iter_rows()]
        entetes, source = m.lire_tableau(self.EXPORT)
        self.assertEqual(lignes[0], list(entetes) + ["Cible"], "toutes les colonnes de l'export, dans leur ordre, puis la cible")
        self.assertIn("Email professionnel", lignes[0])
        self.assertIn("Mobile", lignes[0])
        self.assertEqual(len(lignes) - 1, 21)
        self.assertEqual(Counter(l[-1] for l in lignes[1:]), Counter(PAR_CIBLE_DEFAUT))
        gardees = [numero for numero, (statut, _) in LIGNES_DEFAUT.items() if statut == "Conforme"]
        self.assertEqual([["" if v is None else v for v in l[:-1]] for l in lignes[1:]], [list(source[numero - 1]) for numero in gardees], "les lignes gardées, intactes et dans l'ordre")
        self.assertEqual([l[-1] for l in lignes[1:]], [LIGNES_DEFAUT[numero][1] for numero in gardees])
        # ce qui n'a pas de filtre n'est pas jugé : la ligne 21 (Gmail) et la ligne 27 (sans email) sont gardées avec leurs données telles quelles
        par_nom = {l[0]: l for l in lignes[1:]}
        self.assertEqual(par_nom["Camille Exemple 21"][entetes.index("Email professionnel")], "camille21@gmail.com")
        self.assertEqual(par_nom["Camille Exemple 27"][entetes.index("Email professionnel")] or "", "")
        for numero, (statut, _) in LIGNES_DEFAUT.items():
            nom = f"Camille Exemple {numero:02d}"
            if statut == "Rejeté" and numero not in (15, 16):  # deux doublons portent le nom d'une ligne gardée
                self.assertNotIn(nom, par_nom, nom)
        self.assertEqual(classeur.properties.creator, "leadlint")
        for nom in ("export-brut-exemple-controle-qa.xlsx", "export-brut-exemple-conformes.xlsx"):
            self.assertTrue((dossier / "resultats" / nom).is_file(), nom)

    def test_entetes_et_cellules_piegees_du_fichier_nettoye(self):
        self.assertEqual(s._entetes_uniques(["Poste", "poste", "", "Poste", "Cible"]), ["Poste", "poste (2)", "Colonne 3", "Poste (3)", "Cible"])
        self.assertEqual(s._cellule_nettoyee("=1+1"), "'=1+1")
        self.assertEqual(s._cellule_nettoyee("+33 6 00 00 00 01"), "+33 6 00 00 00 01", "un numéro n'est pas modifié")
        self.assertEqual(s._cellule_nettoyee("a\x01b"), "ab")
        self.assertEqual(s._cellule_nettoyee(45), 45)
        dossier = self.monter()
        export = self.nouveau_dossier() / "piege.csv"
        export.write_text("Nom complet,Poste,Entreprise,Notes,notes,\nAda,CEO,Atelier,=1+1,b,c\nGrace,Stagiaire,Autre,x,y,z\n", encoding="utf-8")
        self.assertEqual(moulinette(dossier, str(export)).returncode, 0)
        import openpyxl
        lignes = [[c.value for c in ligne] for ligne in openpyxl.load_workbook(dossier / "resultats" / "piege-nettoye.xlsx").worksheets[0].iter_rows()]
        self.assertEqual(lignes, [["Nom complet", "Poste", "Entreprise", "Notes", "notes (2)", "Colonne 6", "Cible"],
                                  ["Ada", "CEO", "Atelier", "'=1+1", "b", "c", "Dirigeants"]])

    def test_un_export_xlsx_donne_les_memes_chiffres(self):
        import openpyxl
        dossier = self.monter()
        entetes, lignes = m.lire_tableau(self.EXPORT)
        classeur = openpyxl.Workbook()
        classeur.active.append(entetes)
        for ligne in lignes:
            classeur.active.append([int(v) if re.fullmatch(r"\d+", str(v)) else v for v in ligne])
        fichier = self.nouveau_dossier() / "export.xlsx"
        classeur.save(fichier)
        processus = moulinette(dossier, str(fichier))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertEqual(self.passage(dossier)["causes"]["Rejeté"], CAUSES_DEFAUT)
        self.assertEqual(self.passage(dossier)["par_cible"], PAR_CIBLE_DEFAUT)

    def test_regle_modifiee_apres_validation_bloque(self):
        dossier = self.monter()
        mission = self.mission(dossier)
        pays = next(r for r in mission["configuration"]["regles"] if r["id"] == "r-pays-est_dans")
        pays["valeur"].append("Belgique")
        self.ecrire_mission(dossier, mission)
        self.tout_cocher(dossier)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
        self.assertIn("Vos règles ont changé depuis la validation", processus.stdout)
        self.assertFalse((dossier / "resultats").exists() and list((dossier / "resultats").glob("*")), "rien n'est livré")
        # Le contrôle négatif : sans ce garde-fou, le même changement ferait passer une ligne de plus (la ligne 22, en Belgique).
        def conformes(mission: Path) -> int:
            diagnostic = subprocess.run(
                [sys.executable, str(SCRIPTS / "leadlint.py"), "check", str(self.EXPORT), "--mission", str(mission),
                 "--exclusions", str(dossier / "exclusions.csv"), "--diagnostic", "--json", "-o", str(self.nouveau_dossier())],
                capture_output=True, text=True, encoding="utf-8")
            return json.loads(diagnostic.stdout)["conformes"]

        saine = self.nouveau_dossier() / "mission.json"
        pays["valeur"].remove("Belgique")
        saine.write_text(json.dumps(mission, ensure_ascii=False), encoding="utf-8")
        self.assertEqual((conformes(saine), conformes(dossier / "mission.json")), (13, 14), "le changement modifie bien les verdicts : le blocage n'est pas inutile")

    def test_une_valeur_retouchee_a_la_main_bloque_meme_si_son_filtre_est_decoche(self):
        """(a) Les cases ne valident rien : toute valeur de règle retouchée dans mission.json bloque, quelle que soit la case."""
        retouches = {
            "r-poste-contient_un": lambda r: r["valeur"].append("Directeur technique"),      # filtre verrouillé
            "r-poste-ne_contient_aucun": lambda r: r["valeur"].remove("stagiaire"),          # coché par défaut
            "r-pays-est_dans": lambda r: r["valeur"].append("Belgique"),                     # décoché par défaut
            "r-employes_visibles-entre": lambda r: r["valeur"].update(max=500),              # décoché par défaut
            "r-activite-ne_contient_aucun": lambda r: r["valeur"].clear(),                   # décoché par défaut
        }
        for identifiant, retoucher in retouches.items():
            for etat, coches in (("par défaut", None), ("tout coché", CASES_TOUT)):
                with self.subTest(regle=identifiant, cases=etat):
                    dossier = self.monter(coches=coches)
                    mission = self.mission(dossier)
                    retoucher(next(r for r in mission["configuration"]["regles"] if r["id"] == identifiant))
                    self.ecrire_mission(dossier, mission)
                    processus = moulinette(dossier, str(self.EXPORT))
                    self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
                    self.assertIn("Vos règles ont changé depuis la validation", processus.stdout)
                    self.assertFalse((dossier / "resultats").exists() and list((dossier / "resultats").glob("*")), "rien n'est livré")

    def test_une_regle_eteinte_a_la_main_bloque(self):
        """(d) Les cases se changent dans la page ou dans reglages.json, jamais dans mission.json : éteindre une règle là bloque."""
        dossier = self.monter()
        mission = self.mission(dossier)
        next(r for r in mission["configuration"]["regles"] if r["id"] == "r-doublon-unique")["active"] = False
        self.ecrire_mission(dossier, mission)
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 2)
        dossier = self.monter()
        mission = self.mission(dossier)
        mission["configuration"]["blacklist"]["obligatoire"] = False
        mission["configuration"]["champsObligatoires"] = []
        self.ecrire_mission(dossier, mission)
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 2)

    def test_signature_effacee_bloque(self):
        dossier = self.monter()
        mission = self.mission(dossier)
        mission["configuration"]["origine"]["empreinteRegles"] = ""
        self.ecrire_mission(dossier, mission)
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 2)

    def test_date_effacee_bloque(self):
        dossier = self.monter()
        mission = self.mission(dossier)
        mission["configuration"]["origine"]["valideeLe"] = ""
        self.ecrire_mission(dossier, mission)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2)
        self.assertIn("pas encore validées", processus.stdout)

    def test_champ_obligatoire_retire_apres_validation_bloque(self):
        dossier = self.monter()
        mission = self.mission(dossier)
        mission["configuration"]["champsObligatoires"].remove("poste")
        self.ecrire_mission(dossier, mission)
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 2)

    def test_liste_d_exclusion_changee_bloque(self):
        dossier = self.monter(coches={"liste_exclusion": True})
        liste = dossier / "exclusions.csv"
        original = liste.read_text(encoding="utf-8")
        # une entreprise de plus : le nombre de lignes change
        liste.write_text(original.rstrip("\n") + "\nZéphyr Conseil Exemple,zephyr-conseil.example\n", encoding="utf-8")
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
        self.assertIn("n'est plus celle que vous avez validée", processus.stdout)
        self.assertIn("3 lignes validées, 4 aujourd'hui", processus.stdout)
        # une entreprise remplacée : le nombre de lignes ne change pas, la signature si
        liste.write_text(original.replace("Comète Numérique Exemple,comete-numerique.example", "Autre Société Exemple,autre-societe.example"), encoding="utf-8")
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 2)
        liste.write_text(original, encoding="utf-8")
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0, "la liste d'origine rouvre le passage")

    def test_liste_d_exclusion_absente_bloque(self):
        dossier = self.monter(coches={"liste_exclusion": True})
        (dossier / "exclusions.csv").unlink()
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2)
        self.assertIn("Il manque la liste d'exclusion", processus.stdout)
        self.assertIn("décochez « Liste d'exclusion »", processus.stdout)
        # donnée à la main, elle suffit
        processus = moulinette(dossier, str(self.EXPORT), "--exclusions", str(EXEMPLES / "exclusions-exemple.csv"))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)

    def test_colonne_obligatoire_introuvable(self):
        dossier = self.monter()
        export = self.nouveau_dossier() / "export.csv"
        export.write_text("Nom complet;Entreprise;Intitulé\nCamille Exemple;Atelier;CEO\n", encoding="utf-8")
        processus = moulinette(dossier, str(export))
        self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
        self.assertIn("la colonne « Poste »", processus.stdout)
        self.assertIn("Colonnes lues : Nom complet, Entreprise, Intitulé", processus.stdout)
        self.assertFalse((dossier / "resultats").exists() and list((dossier / "resultats").glob("*")))

    def test_export_vide_ou_absent(self):
        dossier = self.monter()
        processus = moulinette(dossier, str(self.nouveau_dossier() / "introuvable.csv"))
        self.assertEqual(processus.returncode, 1)
        self.assertIn("introuvable", processus.stderr)
        self.assertNotIn("Traceback", processus.stderr)
        vide = self.nouveau_dossier() / "vide.csv"
        vide.write_text("Nom complet;Entreprise;Poste\n", encoding="utf-8")
        processus = moulinette(dossier, str(vide))
        self.assertEqual(processus.returncode, 2)
        self.assertIn("ne contient aucune ligne", processus.stdout)

    def test_regle_sans_colonne_dans_l_export_est_dite_non_appliquee(self):
        reponses = lire_exemple()
        reponses["secteurs"]["codes_naf"] = ["62.01Z"]
        dossier = self.monter(reponses)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertNotIn("Règles non appliquées", processus.stdout, "un filtre décoché ne s'applique pas : il n'a pas à se plaindre de sa colonne")
        self.assertEqual(self.passage(dossier)["regles_non_appliquees"], [])
        self.tout_cocher(dossier)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertIn("Règles non appliquées, faute de colonne dans votre export : Code NAF", processus.stdout)
        passage = self.passage(dossier)
        self.assertEqual(passage["regles_non_appliquees"], [{"regle": "r-code_naf-est_dans", "champ": "codeNaf", "colonne": "Code NAF"}])
        self.assertEqual(passage["conformes"], 13, "une règle sans colonne ne rejette personne (donnée absente : ignorée)")
        self.assertIn("Règles non appliquées : votre fichier n'a pas la colonne « Code NAF »", texte_visible(self.page(dossier)))

    def test_le_script_utilise_le_moteur_de_son_dossier(self):
        """Lancé d'ailleurs, sans le dépôt dans le chemin des modules : c'est la copie du dossier qui tourne."""
        dossier = self.monter()
        moteur = (dossier / "leadlint.py").read_text(encoding="utf-8")
        self.assertIn(f'VERSION = "{m.VERSION}"', moteur)
        (dossier / "leadlint.py").write_text(moteur.replace(f'VERSION = "{m.VERSION}"', 'VERSION = "9.9.9"'), encoding="utf-8")
        autre = self.nouveau_dossier("leadlint-autre-")
        processus = moulinette(dossier, str(self.EXPORT), cwd=autre)
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertEqual(self.passage(dossier)["moteur"], "leadlint 9.9.9")
        self.assertEqual(list(autre.iterdir()), [], "rien n'est écrit dans le répertoire courant")

    def test_le_dossier_se_deplace(self):
        dossier = self.monter()
        nouveau = self.nouveau_dossier("leadlint-deplace-") / "chez-moi"
        shutil.copytree(dossier, nouveau)
        shutil.rmtree(dossier)
        processus = moulinette(nouveau, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertEqual(self.passage(nouveau)["conformes"], 21)

    def test_deux_passages_de_suite(self):
        dossier = self.monter()
        premier = moulinette(dossier, str(self.EXPORT))
        second = moulinette(dossier, str(self.EXPORT))
        self.assertEqual((premier.returncode, second.returncode), (0, 0))
        self.assertEqual(self.passage(dossier)["conformes"], 21)


class PageDesRegles(Banc):
    def test_page_validee(self):
        dossier = self.monter()
        page = self.page(dossier)
        visible = texte_visible(page)
        self.assertIn("Prospection ESN exemple", visible)
        self.assertIn("pour Camille Exemple", visible)
        self.assertIn("Règles validées le 17/11/2026", visible)
        for cible in ("Dirigeants", "Direction commerciale"):
            self.assertIn(cible, visible)
        for pastille in ("CEO", "Gérant", "Head of Sales", "VP Sales"):
            self.assertIn(pastille, visible)
        for phrase in (
            "Le poste doit contenir au moins un de ces mots : CEO, Fondateur",
            "Écarté si le poste contient : stagiaire, assistant, assistante, freelance, open to work.",
            "Entre 11 et 200 employés visibles sur LinkedIn.",
            "Un même contact n'est gardé qu'une fois.",
            "L'entreprise doit être dans l'un de ces pays : France.",
            "Écarté si l'activité de l'entreprise contient : SaaS.",
            "Liste d'exclusion : 3 lignes validées.",
            "Une ligne est écartée s'il manque : Nom complet, Entreprise, Poste.",
            "Aucun fichier passé pour l'instant.",
            "python moulinette.py mon-export.csv",
            "change mes règles LeadLint",
            "Zero Gravity AI",
        ):
            self.assertIn(phrase, visible)
        familles = re.findall(r'<article class="carte"><h3>([^<]+)</h3>', page)
        self.assertEqual(familles, ["Contact", "Exclusions", "Qualité", "Entreprise (facultatif)"], "le contact d'abord")
        for titre, pastille in (("Poste voulu", "actif"), ("Postes écartés", "actif"), ("Doublons", "actif"), ("Liste d'exclusion", "désactivé"),
                                ("Lignes incomplètes", "désactivé"), ("Pays", "désactivé"), ("Activité voulue", "désactivé"), ("Activité écartée", "désactivé"),
                                ("Taille", "désactivé")):
            classe = "etat etat-actif" if pastille == "actif" else "etat"
            self.assertIn(f'<p class="regle-titre">{html.escape(titre)}<span class="{classe}">{pastille}</span></p>', page, titre)
        self.assertNotIn("Email professionnel", visible, "leadlint-setup ne contrôle ni les emails ni les mobiles")
        self.assertIn("Choisissez vos filtres dans cette même page", visible)
        self.assertIn("<details><summary>Détail</summary>", page)
        self.assertIn("mots entiers seulement", visible)
        self.assertIn("est compris entre", visible)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn('name="viewport"', page)

    def test_page_autonome_sans_reseau(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        page = self.page(dossier)
        adresses = set(re.findall(r"https?://[^\s\"'<>]+", page))
        self.assertEqual(adresses, {"https://github.com/Gremelinn0/sales-leadlint"})
        self.assertNotIn("<script", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("<link", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<iframe", page)

    def test_les_trois_sceaux(self):
        dossier = self.monter(valider=False)
        self.assertIn(SCEAU_ATTENTE, texte_visible(self.page(dossier)))
        self.assertIn("sceau-attente", self.page(dossier))
        self.assertEqual(setup("validate", str(dossier), "--date", "2026-11-17")[0], 0)
        self.assertIn("Règles validées le 17/11/2026", texte_visible(self.page(dossier)))
        self.assertIn("sceau-valide", self.page(dossier))
        mission = self.mission(dossier)
        mission["configuration"]["regles"][0]["valeur"].append("Belgique")
        self.ecrire_mission(dossier, mission)
        self.assertEqual(setup("page", str(dossier))[0], 0)
        visible = texte_visible(self.page(dossier))
        self.assertIn("Règles modifiées depuis la validation : à revalider", visible)
        self.assertNotIn("Règles validées le", visible)
        self.assertIn("sceau-modifie", self.page(dossier))

    def test_liste_modifiee_se_voit_sur_la_page(self):
        dossier = self.monter(coches={"liste_exclusion": True})
        (dossier / "exclusions.csv").write_text("Entreprise,Site\nAutre,autre.example\n", encoding="utf-8")
        self.assertEqual(setup("page", str(dossier))[0], 0)
        self.assertIn("Liste d'exclusion modifiée depuis la validation : à revalider", texte_visible(self.page(dossier)))
        self.cocher(dossier, liste_exclusion=False)
        self.assertEqual(setup("page", str(dossier))[0], 0)
        self.assertIn("Règles validées le 17/11/2026", texte_visible(self.page(dossier)), "une liste décochée ne change pas le sceau")

    def test_le_dernier_passage_se_lit_sur_la_page(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        visible = texte_visible(self.page(dossier))
        self.assertRegex(visible, r"21\s+Gardés\s+0\s+À vérifier\s+9\s+Rejetés")
        self.assertIn("sur 30 lignes", visible)
        self.assertIn(PHRASE_FILTRES_DEFAUT, re.sub(r"\s+", " ", visible), "la page dit quels filtres étaient actifs")
        self.assertIn("Mauvais intitulé de poste 7", re.sub(r"\s+", " ", visible))
        self.assertIn("Doublon 2", re.sub(r"\s+", " ", visible))
        self.assertNotIn("Champ obligatoire manquant", visible)
        self.assertIn("Dirigeants 15", re.sub(r"\s+", " ", visible))
        self.cocher(dossier, champs_obligatoires=True)
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        self.assertIn("Champ obligatoire manquant : entreprise", texte_visible(self.page(dossier)))
        self.assertNotIn("Aucun fichier passé", visible)
        self.assertNotIn("Ce passage date d'avant", visible)

    def test_un_passage_d_avant_la_modification_des_regles_est_signale(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        reponses = lire_exemple()
        reponses["taille"] = {"min": 1, "max": 500}
        (dossier / "reponses.json").write_text(json.dumps(reponses, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(setup("create", str(dossier / "reponses.json"), "-o", str(dossier), "--force")[0], 0)
        self.assertIn("Ce passage date d'avant votre dernière modification des règles.", texte_visible(self.page(dossier)))

    def test_ce_qui_vient_des_reponses_est_echappe(self):
        reponses = lire_exemple()
        reponses["nom"] = 'Ma <b>mission</b> & "co"'
        reponses["pour"] = "<img src=x onerror=alert(1)>"
        reponses["cibles"][0]["intitules"].append("<script>alert(1)</script>")
        reponses["cibles"][0]["nom"] = "<i>Dirigeants</i>"
        reponses["postes_exclus"].append("</details><script>x</script>")
        dossier = self.monter(reponses)
        page = self.page(dossier)
        self.assertNotIn("<script", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>mission", page)
        self.assertNotIn("<i>Dirigeants", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertEqual(page.count("<details>"), page.count("</details>"), "un texte piégé ne casse pas la structure")
        self.assertIn("<script>alert(1)</script>", texte_visible(page), "le texte reste lisible tel quel")

    def test_famille_vide_et_phrases_sans_tiret_cadratin(self):
        reponses = {"nom": "Minimale", "cibles": [{"nom": "Dirigeants", "intitules": ["CEO"]}]}
        dossier = self.monter(reponses, exclusions=False)
        visible = texte_visible(self.page(dossier))
        for vide in ("Aucune règle sur l'entreprise.", "Aucune liste d'exclusion."):
            self.assertIn(vide, visible)
        self.assertNotIn(TIRET_CADRATIN, self.page(dossier))
        self.assertNotIn("pour ", visible.split("Vos règles de ciblage")[1].split("Règles")[0], "pas de « pour » sans destinataire")

    def test_taille_a_une_seule_borne(self):
        for taille, phrase in (({"min": 50}, "Au moins 50 employés visibles sur LinkedIn."), ({"max": 20}, "Au plus 20 employés visibles sur LinkedIn.")):
            reponses = lire_exemple()
            reponses["taille"] = taille
            dossier = self.monter(reponses)
            self.assertIn(phrase, texte_visible(self.page(dossier)))


class Vocabulaire(Banc):
    def test_ce_que_la_personne_lit_n_emploie_pas_les_mots_du_metier(self):
        dossier = self.monter(valider=False)
        textes = []
        code, sortie, erreurs = setup("create", str(dossier / "reponses.json"), "-o", str(dossier), "--exclusions", str(EXEMPLES / "exclusions-exemple.csv"))
        textes += [("create", sortie)]
        textes += [("avant validation", moulinette(dossier, str(self.EXPORT)).stdout)]
        textes += [("validate", setup("validate", str(dossier))[1])]
        textes += [("passage", moulinette(dossier, str(self.EXPORT)).stdout)]
        textes += [("page", texte_visible(self.page(dossier)))]
        self.tout_cocher(dossier)
        textes += [("passage tout coché", moulinette(dossier, str(self.EXPORT)).stdout)]
        textes += [("page tout coché", texte_visible(self.page(dossier)))]
        (dossier / "exclusions.csv").unlink()
        textes += [("liste absente", moulinette(dossier, str(self.EXPORT)).stdout)]
        for origine, texte in textes:
            with self.subTest(origine=origine):
                self.assertIsNone(MOTS_INTERDITS.search(texte), MOTS_INTERDITS.search(texte) and MOTS_INTERDITS.search(texte).group(0))
                self.assertNotIn(TIRET_CADRATIN, texte)


class Lanceurs(Banc):
    def test_create_ecrit_les_deux_lanceurs(self):
        dossier = self.monter(valider=False)
        windows = (dossier / "Ouvrir la moulinette.bat").read_bytes()
        mac = (dossier / "ouvrir-la-moulinette.command").read_bytes()
        texte = windows.decode("ascii")  # aucune page de code ne peut le déformer
        self.assertNotIn(b"\r\r", windows)
        self.assertEqual(windows.count(b"\n"), windows.count(b"\r\n"), "fins de ligne Windows partout")
        for morceau in ('cd /d "%~dp0"', "py -3 moulinette.py --ouvrir", "python moulinette.py --ouvrir", "pause", "Python n'est pas installe", "python.org"):
            self.assertIn(morceau, texte)
        self.assertLess(texte.index("py -3 moulinette.py"), texte.index("python moulinette.py"), "essaie py, puis python")
        self.assertNotIn(b"\r", mac)
        texte_mac = mac.decode("utf-8")
        self.assertTrue(texte_mac.startswith("#!/bin/bash\n"))
        for morceau in ('cd "$(dirname "$0")"', "python3 moulinette.py --ouvrir", "read -r", "python.org"):
            self.assertIn(morceau, texte_mac)
        if os.name != "nt":
            self.assertTrue(os.stat(dossier / "ouvrir-la-moulinette.command").st_mode & 0o111, "bit exécutable posé")
        for nom in ("Ouvrir la moulinette.bat", "ouvrir-la-moulinette.command"):
            self.assertIn(nom, s.creer(dossier / "reponses.json", dossier)["fichiers"])

    def test_le_lanceur_windows_demarre_la_moulinette_et_dit_quand_python_manque(self):
        if os.name != "nt":
            self.skipTest("le lanceur Windows ne se lance que sous Windows")
        dossier = self.monter()
        environnement = {cle: valeur for cle, valeur in os.environ.items() if cle != "PYTHONPATH"}
        environnement.update({"LEADLINT_SANS_NAVIGATEUR": "1", "PYTHONIOENCODING": "utf-8"})
        lanceur = subprocess.Popen(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(dossier / "Ouvrir la moulinette.bat")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=tempfile.mkdtemp(prefix="leadlint-ailleurs-"), env=environnement,
        )
        self.addCleanup(lambda: (lanceur.poll() is None and subprocess.run(["taskkill", "/F", "/T", "/PID", str(lanceur.pid)], capture_output=True)))
        self.addCleanup(lanceur.stdout.close)
        ligne = ""
        while "http://127.0.0.1:" not in ligne:
            ligne = lanceur.stdout.readline()
            self.assertTrue(ligne, "le lanceur a rendu la main sans ouvrir la page")
        adresse = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=([\w-]+)", ligne)
        connexion = http.client.HTTPConnection("127.0.0.1", int(adresse.group(1)), timeout=10)
        connexion.request("POST", f"/fermer?t={adresse.group(2)}", b"")
        self.assertEqual(connexion.getresponse().status, 200)
        lanceur.wait(timeout=20)
        # Sans Python dans le chemin : un message clair, pas une fenêtre qui se ferme sans rien dire.
        vide = tempfile.mkdtemp(prefix="leadlint-sans-python-")
        self.addCleanup(shutil.rmtree, vide, True)
        environnement["PATH"] = vide
        sans = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", str(dossier / "Ouvrir la moulinette.bat")], capture_output=True, text=True,
                              encoding="cp850", errors="replace", stdin=subprocess.DEVNULL, env=environnement, timeout=60)
        self.assertIn("Python n'est pas installe sur cet ordinateur.", sans.stdout)
        self.assertIn("python.org", sans.stdout)


class Interface(Banc):
    """La page « Déposez votre export » : un vrai serveur lancé comme la personne le lance (un sous-processus), interrogé par de vraies requêtes."""

    def demarrer(self, dossier: Path, *arguments: str) -> tuple:
        environnement = {cle: valeur for cle, valeur in os.environ.items() if cle != "PYTHONPATH"}
        environnement.update({"LEADLINT_SANS_NAVIGATEUR": "1", "PYTHONIOENCODING": "utf-8"})
        processus = subprocess.Popen(
            [sys.executable, str(dossier / "moulinette.py"), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", cwd=tempfile.mkdtemp(prefix="leadlint-ailleurs-"), env=environnement,
        )
        self.addCleanup(self.arreter, processus)
        ligne = processus.stdout.readline()
        trouve = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=([\w-]+)\. Ctrl\+C pour fermer\.$", ligne.strip())
        self.assertIsNotNone(trouve, f"ligne d'ouverture inattendue : {ligne!r} {processus.poll()}")
        self.ligne_ouverture = ligne.strip()
        return processus, int(trouve.group(1)), trouve.group(2)

    @staticmethod
    def arreter(processus) -> None:
        if processus.poll() is None:
            processus.kill()
        processus.wait(timeout=20)
        processus.stdout.close()
        processus.stderr.close()

    def appeler(self, port: int, methode: str, chemin: str, corps: bytes = None, entetes: dict = None) -> tuple:
        """(code, entêtes, corps) : le chemin part tel quel, sans que le client le normalise."""
        connexion = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connexion.request(methode, chemin, body=corps, headers=entetes or {})
            reponse = connexion.getresponse()
            return reponse.status, {cle.lower(): valeur for cle, valeur in reponse.getheaders()}, reponse.read()
        finally:
            connexion.close()

    def deposer(self, port: int, jeton: str, nom: str, contenu: bytes = None, **entetes) -> tuple:
        contenu = self.EXPORT.read_bytes() if contenu is None else contenu
        code, tetes, corps = self.appeler(port, "POST", f"/passer?t={jeton}&nom={urllib.parse.quote(nom)}", contenu, entetes)
        return code, tetes, json.loads(corps.decode("utf-8"))

    def monter_et_demarrer(self, **options) -> tuple:
        dossier = self.monter(**options)
        processus, port, jeton = self.demarrer(dossier)
        return dossier, processus, port, jeton

    # --- la page

    def test_la_page_d_accueil_et_son_etat(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        code, tetes, corps = self.appeler(port, "GET", f"/?t={jeton}")
        self.assertEqual(code, 200)
        self.assertEqual(tetes["content-type"], "text/html; charset=utf-8")
        self.assertIn("default-src 'none'", tetes["content-security-policy"])
        self.assertEqual(tetes["cache-control"], "no-store")
        page = corps.decode("utf-8")
        visible = texte_visible(re.sub(r"<script.*?</script>", " ", page, flags=re.S))
        for phrase in (
            "Prospection ESN exemple", "pour Camille Exemple", "Règles validées le 17/11/2026",
            "Glissez votre export ici (.csv ou .xlsx) ou cliquez pour le choisir", "Liste d'exclusion : décochée (exclusions.csv n'est pas utilisée).",
            "Voir mes règles", "Fermer la moulinette", "Zero Gravity AI",
            "Ce que je filtre", "Contacts", "Entreprise (un autre traitement, facultatif)", "Intitulé de poste dans mes cibles", "Toujours actif.",
            "Cibles : Dirigeants, Direction commerciale.", "Intitulés à écarter", "Doublons", "Lignes incomplètes", "Pays", "Activité", "Taille",
            "Écarte un poste qui contient : stagiaire, assistant, assistante, freelance, open to work.", "Entre 11 et 200 employés visibles sur LinkedIn.",
        ):
            self.assertIn(phrase, visible)
        for identifiant, attendu in {"poste": " checked disabled data-verrou", "postes_exclus": " checked", "doublons": " checked", "liste_exclusion": "",
                                     "champs_obligatoires": "", "pays": "", "activite": "", "taille": ""}.items():
            self.assertIn(f'<input type="checkbox" data-filtre="{identifiant}"{attendu}>', page, identifiant)
        self.assertNotIn('data-filtre="naf"', page, "pas de règle NAF dans l'exemple : pas de case")
        self.assertRegex(page, r'<input type="file" id="fichier" accept="\.csv,\.xlsx,\.xlsm">')
        self.assertIn(f'href="/regles?t={jeton}"', page)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn('name="viewport"', page)
        self.assertEqual(set(re.findall(r"https?://[^\s\"'<>]+", re.sub(r"<script.*?</script>", " ", page, flags=re.S))), {"https://github.com/Gremelinn0/sales-leadlint"})
        sans_style = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
        self.assertIsNone(MOTS_INTERDITS.search(sans_style), MOTS_INTERDITS.search(sans_style) and MOTS_INTERDITS.search(sans_style).group(0))
        self.assertNotIn(TIRET_CADRATIN, page)
        script = re.search(r"<script>(.*?)</script>", page, flags=re.S).group(1)
        for interdit in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\""):
            self.assertNotIn(interdit, script, "tout texte reçu s'affiche avec textContent")
        for ressource in re.findall(r"""(?:src|href)=["']([^"']+)["']""", sans_style):
            self.assertTrue(ressource.startswith(("/regles?t=", "https://github.com/")), ressource)
        self.assertNotIn("<link", page)
        self.assertNotIn("@import", page)

    def test_la_liste_d_exclusion_dit_son_etat(self):
        reponses = lire_exemple()
        reponses["liste_exclusion"] = False
        dossier = self.monter(reponses, exclusions=False)
        processus, port, jeton = self.demarrer(dossier)
        page = self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8")
        self.assertIn("Liste d&#x27;exclusion : aucune.", page)
        self.assertNotIn('data-filtre="liste_exclusion"', page)
        # Une liste attendue mais absente, puis une liste changée depuis la validation (la case est cochée).
        dossier = self.monter(coches={"liste_exclusion": True})
        processus, port, jeton = self.demarrer(dossier)
        (dossier / "exclusions.csv").write_text("Entreprise\nAutre\n", encoding="utf-8")
        page = html.unescape(self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8"))
        self.assertIn("Liste d'exclusion : exclusions.csv a changé depuis la validation. Revalidez vos règles.", page)
        self.assertIn("Liste d'exclusion modifiée depuis la validation : à revalider", page)
        (dossier / "exclusions.csv").unlink()
        page = html.unescape(self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8"))
        self.assertIn("Liste d'exclusion : introuvable. Posez-la dans ce dossier sous le nom exclusions.csv.", page)
        self.assertIn('data-filtre="liste_exclusion" checked>', page, "cochée sans liste : la case reste visible pour pouvoir la décocher")

    def test_la_case_de_la_liste_n_est_proposee_que_si_une_liste_existe(self):
        dossier = self.monter(exclusions=False, valider=False)
        self.assertEqual(setup("validate", str(dossier), "--exclusions", str(EXEMPLES / "exclusions-exemple.csv"), "--date", "2026-11-17")[0], 0)
        _, port, jeton = self.demarrer(dossier)
        self.assertIn('data-filtre="liste_exclusion">', self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8"))
        (dossier / "exclusions.csv").unlink()
        page = html.unescape(self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8"))
        self.assertNotIn('data-filtre="liste_exclusion"', page)
        self.assertIn("Liste d'exclusion : décochée, et aucune liste dans ce dossier", page)

    def test_la_page_n_est_pas_validee_avant_la_validation(self):
        dossier = self.monter(valider=False, coches={"liste_exclusion": True})
        _, port, jeton = self.demarrer(dossier)
        page = html.unescape(self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8"))
        self.assertIn(SCEAU_ATTENTE, page)
        self.assertIn("Liste d'exclusion : exclusions.csv, pas encore validée.", page)

    def test_le_texte_de_la_personne_est_echappe(self):
        reponses = lire_exemple()
        reponses["nom"] = "Ma <img src=x onerror=alert(1)> prospection"
        reponses["pour"] = "Camille <script>alert(2)</script> & Cie"
        dossier = self.monter(reponses)
        _, port, jeton = self.demarrer(dossier)
        page = self.appeler(port, "GET", f"/?t={jeton}")[2].decode("utf-8")
        self.assertNotIn("<img", page)
        self.assertEqual(page.count("<script"), 1, "un seul script : le nôtre")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt; &amp; Cie", page)

    def test_la_page_des_regles_est_servie(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        code, tetes, corps = self.appeler(port, "GET", f"/regles?t={jeton}")
        self.assertEqual(code, 200)
        page = corps.decode("utf-8")
        self.assertIn("Règles validées le 17/11/2026", texte_visible(page))
        self.assertIn("Ouvrir la moulinette", texte_visible(page))
        self.assertNotIn("<script", page)

    # --- un dépôt

    def test_depot_de_l_export_d_exemple(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        code, tetes, donnees = self.deposer(port, jeton, "export-brut-exemple.csv")
        self.assertEqual(code, 200)
        self.assertEqual(tetes["content-type"], "application/json; charset=utf-8")
        self.assertEqual((donnees["ok"], donnees["bloque"], donnees["justes"]), (True, False, True))
        self.assertEqual((donnees["total"], donnees["conformes"], donnees["a_verifier"], donnees["rejetes"]), CHIFFRES_DEFAUT)
        self.assertEqual({cause: n for cause, n in donnees["causes"]}, CAUSES_DEFAUT)
        self.assertEqual(donnees["causes"], sorted(donnees["causes"], key=lambda ligne: -ligne[1]))
        self.assertEqual(dict(donnees["par_cible"]), PAR_CIBLE_DEFAUT)
        self.assertEqual(donnees["filtres_texte"], PHRASE_FILTRES_DEFAUT)
        self.assertEqual({f["id"]: f["actif"] for f in donnees["filtres"]}, CASES_DEFAUT)
        self.assertEqual((donnees["regles_non_appliquees"], donnees["avertissement"]), ([], None))
        self.assertEqual(donnees["mission"], "Camille Exemple / Prospection ESN exemple")
        self.assertNotIn(TIRET_CADRATIN, json.dumps(donnees, ensure_ascii=False))
        self.assertEqual((dossier / "depots" / "export-brut-exemple.csv").read_bytes(), self.EXPORT.read_bytes())
        liens = donnees["telechargements"]
        self.assertEqual((liens["nettoye"]["nom"], liens["controle"]["nom"]), ("export-brut-exemple-nettoye.xlsx", "export-brut-exemple-controle-qa.xlsx"))
        code, tetes, contenu = self.appeler(port, "GET", liens["nettoye"]["url"])
        self.assertEqual(code, 200)
        self.assertEqual(tetes["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("attachment", tetes["content-disposition"])
        self.assertIn("export-brut-exemple-nettoye.xlsx", tetes["content-disposition"])
        self.assertEqual(contenu, (dossier / "resultats" / "export-brut-exemple-nettoye.xlsx").read_bytes())
        import openpyxl
        lignes = [[c.value for c in ligne] for ligne in openpyxl.load_workbook(io.BytesIO(contenu)).worksheets[0].iter_rows()]
        self.assertEqual((len(lignes) - 1, lignes[0][-1]), (21, "Cible"))
        self.assertEqual(Counter(ligne[-1] for ligne in lignes[1:]), Counter(PAR_CIBLE_DEFAUT))
        code, _, controle = self.appeler(port, "GET", liens["controle"]["url"])
        self.assertEqual((code, controle), (200, (dossier / "resultats" / "export-brut-exemple-controle-qa.xlsx").read_bytes()))
        # le passage est aussi inscrit dans la page des règles
        self.assertEqual(self.passage(dossier)["conformes"], 21)
        regles = texte_visible(self.appeler(port, "GET", f"/regles?t={jeton}")[2].decode("utf-8"))
        self.assertIn("sur 30 lignes", regles)

    def test_la_page_donne_le_meme_bilan_que_la_ligne_de_commande(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        donnees = self.deposer(port, jeton, "export-brut-exemple.csv")[2]
        ligne = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(ligne.returncode, 0, ligne.stderr)
        self.assertEqual(ligne.stdout.splitlines()[1], "Lignes 30 · gardées 21 · à vérifier 0 · rejetées 9")
        self.assertEqual(ligne.stdout.splitlines()[2], "Rejetées, par cause : " + " · ".join(f"{cause} {n}" for cause, n in donnees["causes"]))
        self.assertEqual(ligne.stdout.splitlines()[3], "Gardées, par cible : " + " · ".join(f"{cible} {n}" for cible, n in donnees["par_cible"]))

    def test_la_ligne_de_commande_ne_change_pas(self):
        dossier = self.monter()
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 0, processus.stderr)
        lignes = processus.stdout.splitlines()
        self.assertEqual(lignes[:5], [
            "« Camille Exemple / Prospection ESN exemple » · export-brut-exemple.csv",
            "Lignes 30 · gardées 21 · à vérifier 0 · rejetées 9",
            "Rejetées, par cause : Mauvais intitulé de poste 7 · Doublon 2",
            "Gardées, par cible : Dirigeants 15 · Direction commerciale 6",
            "Contrôles du bilan : justes",
        ])
        self.assertEqual(lignes[5], PHRASE_FILTRES_DEFAUT)
        self.assertEqual(len(lignes), 9)
        for debut, fin in zip(lignes[6:], ("Contacts nettoyés : ", "Contrôle ligne par ligne : ", "Vos règles et ce passage : ")):
            self.assertTrue(debut.startswith(fin), debut)
        self.assertFalse((dossier / "depots").exists(), "la ligne de commande ne dépose rien")
        # un fichier ET --ouvrir : une phrase claire, rien ne démarre
        mixte = moulinette(dossier, str(self.EXPORT), "--ouvrir")
        self.assertEqual(mixte.returncode, 1)
        self.assertIn("Donnez un fichier, ou --ouvrir", mixte.stderr)
        liste = moulinette(dossier, "--exclusions", str(dossier / "exclusions.csv"))
        self.assertEqual(liste.returncode, 1)
        self.assertIn("exclusions.csv", liste.stderr)

    def test_avant_validation_la_page_bloque_et_ne_livre_rien(self):
        dossier = self.monter(valider=False)
        _, port, jeton = self.demarrer(dossier)
        code, _, donnees = self.deposer(port, jeton, "export-brut-exemple.csv")
        self.assertEqual(code, 200)
        self.assertEqual((donnees["ok"], donnees["bloque"]), (False, True))
        self.assertNotIn("telechargements", donnees)
        self.assertNotIn("conformes", donnees)
        self.assertIn("le contrôle avant lancement bloque, rien n'est livré.", donnees["message"])
        self.assertEqual(donnees["lignes"][0], "Vos règles ne sont pas encore validées.")
        self.assertFalse(list((dossier / "resultats").glob("*.xlsx")) if (dossier / "resultats").exists() else [], "rien n'est livré")
        self.assertFalse((dossier / "dernier-passage.json").exists())
        # les mêmes phrases qu'au terminal
        ligne = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(ligne.returncode, 2)
        self.assertEqual(ligne.stdout.splitlines()[2:], donnees["lignes"])
        self.assertEqual(ligne.stdout.splitlines()[1], "Le contrôle avant lancement bloque : rien n'est livré.")

    def test_les_autres_blocages_sont_dits_avec_ce_qu_il_faut_faire(self):
        dossier, _, port, jeton = self.monter_et_demarrer(coches={"liste_exclusion": True})
        # la liste d'exclusion n'est plus celle qui a été validée
        (dossier / "exclusions.csv").write_text("Entreprise\nAlpha\nBeta\n", encoding="utf-8")
        donnees = self.deposer(port, jeton, "export-brut-exemple.csv")[2]
        self.assertTrue(donnees["bloque"])
        self.assertIn("La liste d'exclusion n'est plus celle que vous avez validée (3 lignes validées, 2 aujourd'hui).", donnees["lignes"])
        shutil.copyfile(EXEMPLES / "exclusions-exemple.csv", dossier / "exclusions.csv")
        # les règles ont changé après la validation
        mission = self.mission(dossier)
        mission["configuration"]["regles"][0]["valeur"] = ["Belgique"]
        self.ecrire_mission(dossier, mission)
        donnees = self.deposer(port, jeton, "export-brut-exemple.csv")[2]
        self.assertTrue(donnees["bloque"])
        self.assertEqual(donnees["lignes"][0], "Vos règles ont changé depuis la validation (mission.json a été modifié après coup).")
        self.assertFalse((dossier / "resultats").exists() and list((dossier / "resultats").glob("*-nettoye.xlsx")))

    def test_une_colonne_obligatoire_introuvable_est_nommee(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        sans_poste = self.EXPORT.read_text(encoding="utf-8").replace("Poste", "Truc", 1)
        donnees = self.deposer(port, jeton, "sans-poste.csv", sans_poste.encode("utf-8"))[2]
        self.assertTrue(donnees["bloque"], donnees)
        self.assertTrue(any("Il manque dans votre export la colonne « Poste »." == ligne for ligne in donnees["lignes"]), donnees["lignes"])
        self.assertNotIn("telechargements", donnees)

    def test_une_regle_sans_colonne_est_dite(self):
        dossier, _, port, jeton = self.monter_et_demarrer(coches=CASES_TOUT)
        sans_pays = re.sub(r"\bPays\b", "Zone", self.EXPORT.read_text(encoding="utf-8"), count=1)
        donnees = self.deposer(port, jeton, "sans-pays.csv", sans_pays.encode("utf-8"))[2]
        self.assertTrue(donnees["ok"], donnees)
        self.assertEqual(donnees["regles_non_appliquees"], ["Pays"])
        self.assertEqual(donnees["avertissement"], "Règles non appliquées : votre fichier n'a pas la colonne « Pays ». Les lignes ne sont pas contrôlées sur ces points.")

    def test_un_fichier_illisible_recoit_une_phrase_claire(self):
        dossier, processus, port, jeton = self.monter_et_demarrer()
        code, _, donnees = self.deposer(port, jeton, "casse.xlsx", b"PK ceci n'est pas un classeur")
        self.assertEqual((code, donnees["ok"]), (422, False))
        self.assertEqual(donnees["erreur"], "Je n'arrive pas à lire ce fichier. Est-ce bien un export .csv ou .xlsx ?")
        code, _, donnees = self.deposer(port, jeton, "vide.csv", b"\n\n")
        self.assertEqual((code, donnees["ok"]), (422, False))
        self.assertTrue(donnees["erreur"])
        self.assertNotIn("Traceback", donnees["erreur"])
        self.assertEqual(self.appeler(port, "GET", f"/?t={jeton}")[0], 200, "la page continue de répondre")
        self.assertEqual(self.deposer(port, jeton, "export-brut-exemple.csv")[2]["conformes"], 21, "et de traiter")
        self.assertIsNone(processus.poll())

    # --- la sécurité

    def test_le_jeton_est_exige_partout(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        self.deposer(port, jeton, "export-brut-exemple.csv")
        contenu = self.EXPORT.read_bytes()
        faux = "x" * len(jeton)
        for essai in ("", "t=", f"t={faux}", f"t={jeton[:-1]}", f"t={jeton}x", f"T={jeton}", f"nom=a.csv&t={faux}"):
            with self.subTest(essai=essai):
                requete = f"?{essai}" if essai else ""
                self.assertEqual(self.appeler(port, "POST", f"/passer{requete}&nom=a.csv" if requete else "/passer?nom=a.csv", contenu)[0], 403)
                self.assertEqual(self.appeler(port, "GET", f"/{requete}")[0], 403)
                self.assertEqual(self.appeler(port, "GET", f"/regles{requete}")[0], 403)
                self.assertEqual(self.appeler(port, "GET", f"/resultats/export-brut-exemple-nettoye.xlsx{requete}")[0], 403)
                self.assertEqual(self.appeler(port, "POST", f"/fermer{requete}", b"")[0], 403)
                reglage = f"/reglage{requete}&id=doublons&actif=0" if requete else "/reglage?id=doublons&actif=0"
                self.assertEqual(self.appeler(port, "POST", reglage, b"")[0], 403)
        self.assertFalse((dossier / "depots" / "a.csv").exists(), "un dépôt refusé n'écrit rien")
        self.assertEqual(self.reglages(dossier), CASES_DEFAUT, "une case refusée n'écrit rien")
        # le bon jeton passe, et la moulinette tourne encore
        self.assertEqual(self.appeler(port, "GET", f"/resultats/export-brut-exemple-nettoye.xlsx?t={jeton}")[0], 200)

    def test_un_hote_etranger_est_refuse(self):
        _, _, port, jeton = self.monter_et_demarrer()
        for hote in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1", f"localhost.evil.example:{port}", f"127.0.0.1.evil.example:{port}", ""):
            with self.subTest(hote=hote):
                self.assertEqual(self.appeler(port, "GET", f"/?t={jeton}", entetes={"Host": hote})[0], 403)
                self.assertEqual(self.appeler(port, "POST", f"/passer?t={jeton}&nom=a.csv", b"x", {"Host": hote})[0], 403)
                self.assertEqual(self.appeler(port, "POST", f"/reglage?t={jeton}&id=doublons&actif=0", b"", {"Host": hote})[0], 403)
                self.assertEqual(self.appeler(port, "GET", f"/regles?t={jeton}", entetes={"Host": hote})[0], 403)
        for hote in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
            with self.subTest(hote=hote):
                self.assertEqual(self.appeler(port, "GET", f"/?t={jeton}", entetes={"Host": hote})[0], 200)

    def test_une_autre_origine_est_refusee(self):
        _, _, port, jeton = self.monter_et_demarrer()
        for origine in ("http://evil.example", "null", f"http://127.0.0.1:{port + 1}", "https://127.0.0.1:%d" % port):
            with self.subTest(origine=origine):
                code = self.appeler(port, "POST", f"/passer?t={jeton}&nom=a.csv", self.EXPORT.read_bytes(), {"Origin": origine})[0]
                self.assertEqual(code, 403)
                self.assertEqual(self.appeler(port, "POST", f"/reglage?t={jeton}&id=doublons&actif=0", b"", {"Origin": origine})[0], 403)
        for origine in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            with self.subTest(origine=origine):
                self.assertEqual(self.appeler(port, "POST", f"/passer?t={jeton}&nom=a.csv", self.EXPORT.read_bytes(), {"Origin": origine})[0], 200)

    def test_aucune_sortie_du_dossier_resultats(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        self.deposer(port, jeton, "export-brut-exemple.csv")
        secret = "Prospection ESN exemple"
        self.assertIn(secret, (dossier / "mission.json").read_text(encoding="utf-8"))
        chemins = [
            "/resultats/../mission.json", "/resultats/%2e%2e/mission.json", "/resultats/..%2fmission.json", "/resultats/%2e%2e%2fmission.json",
            "/resultats/..%5cmission.json", "/resultats/..\\mission.json", "/resultats/%2E%2E%2Fmission.json", "/resultats/..%252fmission.json",
            "/resultats/./../mission.json", "/resultats/sous/dossier.xlsx", "/resultats/", "/resultats", "/resultats//mission.json",
            "/resultats/....//mission.json", "/resultats/mission.json", "/resultats/%00mission.json", "/resultats/export-brut-exemple-nettoye.xlsx%00.json",
            "/resultats/C:/Windows/win.ini", "/resultats/export-brut-exemple-nettoye.xlsx::$DATA", "/resultats/export-brut-exemple-nettoye.xlsx.",
            "/mission.json", "/depots/export-brut-exemple.csv", "/leadlint.py", "//mission.json", "/resultats/nexiste-pas.xlsx",
        ]
        for chemin in chemins:
            with self.subTest(chemin=chemin):
                code, _, corps = self.appeler(port, "GET", f"{chemin}?t={jeton}")
                self.assertIn(code, (403, 404), chemin)
                self.assertNotIn(secret.encode(), corps)
                self.assertNotIn(b"PK", corps[:2])
        # le témoin positif : le même serveur sert bien le fichier légitime
        self.assertEqual(self.appeler(port, "GET", f"/resultats/export-brut-exemple-nettoye.xlsx?t={jeton}")[0], 200)

    def test_extensions_et_taille(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        for nom in ("virus.exe", "script.js", "export", "export.csv.exe", "export.txt", "export.html", ".csv", "export.xls", ""):
            with self.subTest(nom=nom):
                code, _, donnees = self.deposer(port, jeton, nom, b"a,b\n1,2\n")
                self.assertEqual((code, donnees["ok"]), (415, False))
                self.assertEqual(donnees["erreur"], "Seuls les fichiers .csv, .xlsx et .xlsm sont acceptés.")
        self.assertFalse((dossier / "depots").exists(), "un fichier refusé n'est pas gardé")
        code, _, donnees = self.deposer(port, jeton, "gros.csv", b"x" * (s.TAILLE_MAX_DEPOT + 1))
        self.assertEqual((code, donnees["ok"]), (413, False))
        self.assertIn("50 Mo", donnees["erreur"])
        self.assertFalse((dossier / "depots" / "gros.csv").exists())
        code, _, donnees = self.deposer(port, jeton, "vide.csv", b"")
        self.assertEqual((code, donnees["ok"]), (400, False))
        self.assertEqual(self.appeler(port, "POST", f"/passer?t={jeton}&nom=a.csv", None)[0], 400, "un corps vide")
        for longueur in ("", "Content-Length: abc\r\n", "Content-Length: -5\r\n"):  # pas de taille lisible, ou une taille négative
            with socket.create_connection(("127.0.0.1", port), timeout=10) as brut:
                brut.sendall(f"POST /passer?t={jeton}&nom=a.csv HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n{longueur}\r\n".encode("ascii"))
                reponse = b""
                while True:
                    morceau = brut.recv(4096)
                    if not morceau:
                        break
                    reponse += morceau
            self.assertRegex(reponse.split(b"\r\n")[0].decode("ascii"), r"HTTP/1\.\d (411|400)", longueur)
        for extension in ("csv", "XLSX", "xlsm"):
            self.assertEqual(s.assainir_nom_depot(f"a.{extension}"), f"a.{extension.lower()}")

    def test_les_noms_pieges_sont_assainis(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        cas = {
            "../../x.csv": "x.csv", "..\\..\\y.csv": "y.csv", "<script>.csv": "script.csv", 'a"b|c?.csv': "abc.csv", "C:\\Windows\\z.csv": "z.csv",
            "con.csv": "_con.csv", "sous/dossier/w.csv": "w.csv", "  espaces .csv": "espaces.csv", "a..b...csv": "a.b.csv", "é à ç.csv": "é à ç.csv",
        }
        for brut, assaini in cas.items():
            with self.subTest(brut=brut):
                self.assertEqual(s.assainir_nom_depot(brut), assaini)
                code, _, donnees = self.deposer(port, jeton, brut)
                self.assertEqual(code, 200)
                self.assertEqual(donnees["fichier"], assaini)
                self.assertNotIn("<", json.dumps(donnees))
        self.assertEqual({p.name for p in (dossier / "depots").iterdir()}, set(cas.values()))
        self.assertEqual({p.name for p in dossier.parent.iterdir()} & {"x.csv", "y.csv", "z.csv", "w.csv"}, set(), "rien n'est écrit hors du dossier de dépôt")
        self.assertEqual({p.name for p in dossier.iterdir()} & {"x.csv", "y.csv", "z.csv"}, set())
        for nom in ("script-nettoye.xlsx", "x-nettoye.xlsx"):
            self.assertTrue((dossier / "resultats" / nom).is_file(), nom)

    def test_le_serveur_n_ecoute_que_sur_127_0_0_1(self):
        _, _, port, jeton = self.monter_et_demarrer()
        try:
            autres = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not a[4][0].startswith("127.")}
        except OSError:
            autres = set()
        if not autres:
            self.skipTest("aucune autre adresse IPv4 sur cette machine")
        for adresse in autres:
            with self.subTest(adresse=adresse):
                with self.assertRaises(OSError):
                    socket.create_connection((adresse, port), timeout=3).close()
        self.assertEqual(self.appeler(port, "GET", f"/?t={jeton}")[0], 200)

    # --- la vie du serveur

    def test_fermer_arrete_le_serveur_et_la_sortie_reste_sobre(self):
        dossier, processus, port, jeton = self.monter_et_demarrer()
        self.assertEqual(self.appeler(port, "GET", f"/nexiste-pas?t={jeton}")[0], 404)
        self.assertEqual(self.appeler(port, "GET", "/")[0], 403)
        self.deposer(port, jeton, "export-brut-exemple.csv")
        code, _, corps = self.appeler(port, "POST", f"/fermer?t={jeton}", b"")
        self.assertEqual((code, json.loads(corps)), (200, {"ok": True}))
        self.assertEqual(processus.wait(timeout=20), 0)
        reste = processus.stdout.read().splitlines()
        self.assertEqual(reste, ["Passage : export-brut-exemple.csv · 30 lignes · gardées 21 · à vérifier 0 · rejetées 9.", "Moulinette fermée."])
        self.assertEqual(processus.stderr.read(), "")
        self.assertEqual(self.ligne_ouverture, f"Ouvrez cette adresse dans votre navigateur : http://127.0.0.1:{port}/?t={jeton}. Ctrl+C pour fermer.")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=3).close()

    def test_ouvrir_demarre_aussi_le_serveur_et_chaque_lancement_a_son_jeton(self):
        dossier = self.monter()
        premier, port1, jeton1 = self.demarrer(dossier, "--ouvrir")
        second, port2, jeton2 = self.demarrer(dossier)
        self.assertNotEqual(jeton1, jeton2)
        self.assertNotEqual(port1, port2)
        self.assertGreaterEqual(len(jeton1), 24)
        self.assertEqual(self.appeler(port2, "GET", f"/?t={jeton1}")[0], 403, "le jeton d'un lancement n'ouvre pas l'autre")
        self.assertEqual(self.appeler(port1, "GET", f"/?t={jeton1}")[0], 200)

    def test_sans_mission_la_page_ne_demarre_pas(self):
        dossier = self.monter()
        (dossier / "mission.json").unlink()
        processus = moulinette(dossier)
        self.assertEqual(processus.returncode, 1)
        self.assertIn("mission.json est introuvable", processus.stderr)
        self.assertNotIn("Traceback", processus.stderr)


class Filtres(Banc):
    """Les cases à cocher : retenues dans reglages.json, appliquées par la page comme par la ligne de commande, sans jamais toucher à la validation."""

    demarrer = Interface.demarrer
    arreter = staticmethod(Interface.arreter)
    appeler = Interface.appeler
    deposer = Interface.deposer
    monter_et_demarrer = Interface.monter_et_demarrer

    def regler(self, port: int, jeton: str, identifiant: str, actif: bool) -> tuple:
        code, _, corps = self.appeler(port, "POST", f"/reglage?t={jeton}&id={identifiant}&actif={1 if actif else 0}", b"")
        return code, json.loads(corps.decode("utf-8"))

    @staticmethod
    def causes(donnees: dict) -> dict:
        return {cause: n for cause, n in donnees["causes"]}

    # --- à la création

    def test_cases_par_defaut_a_la_creation(self):
        reponses = lire_exemple()
        reponses["secteurs"]["codes_naf"] = ["62.01Z"]
        travail = self.nouveau_dossier()
        (travail / "reponses.json").write_text(json.dumps(reponses, ensure_ascii=False), encoding="utf-8")
        code, sortie, erreurs = setup("create", str(travail / "reponses.json"), "-o", str(travail / "m"), "--json")
        self.assertEqual(code, 0, erreurs)
        dossier = travail / "m"
        donnees = json.loads(sortie)
        self.assertIn("reglages.json", donnees["fichiers"])
        self.assertEqual(self.reglages(dossier), {**CASES_DEFAUT, "naf": False}, "seuls les filtres définis ont une case ; seuls trois sont cochés")
        self.assertEqual([(f["id"], f["actif"]) for f in donnees["filtres"]], [
            ("poste", True), ("postes_exclus", True), ("doublons", True), ("liste_exclusion", False), ("champs_obligatoires", False),
            ("pays", False), ("activite", False), ("naf", False), ("taille", False)])
        self.assertEqual({f["id"] for f in donnees["filtres"] if f["actif"]}, {"poste", "postes_exclus", "doublons"})

    def test_sans_cible_entreprise_il_ne_reste_que_les_filtres_du_contact(self):
        reponses = {"nom": "Minimale", "cibles": [{"nom": "Dirigeants", "intitules": ["CEO"]}], "postes_exclus": ["stagiaire"]}
        dossier = self.monter(reponses, exclusions=False)
        self.assertEqual(self.reglages(dossier), {"poste": True, "postes_exclus": True, "doublons": True, "champs_obligatoires": False})

    def test_recreer_garde_les_cases_deja_posees(self):
        dossier = self.monter()
        self.cocher(dossier, pays=True, doublons=False)
        code, _, erreurs = setup("create", str(dossier / "reponses.json"), "-o", str(dossier), "--force")
        self.assertEqual(code, 0, erreurs)
        self.assertEqual(self.reglages(dossier), {**CASES_DEFAUT, "pays": True, "doublons": False})

    # --- par la page

    def test_changer_une_case_refait_le_passage_sur_le_meme_fichier(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        # Aucun export déposé : la case s'enregistre, rien d'autre ne se passe.
        code, reponse = self.regler(port, jeton, "taille", True)
        self.assertEqual((code, reponse["ok"], reponse["passage"]), (200, True, None))
        self.assertTrue(self.reglages(dossier)["taille"])
        self.assertEqual(self.regler(port, jeton, "taille", False)[0], 200)
        self.assertFalse((dossier / "dernier-passage.json").exists())
        # Un export déposé, cases par défaut.
        donnees = self.deposer(port, jeton, "export-brut-exemple.csv")[2]
        self.assertEqual((donnees["conformes"], donnees["rejetes"]), (21, 9))
        # Liste d'exclusion cochée : les lignes 17 et 18 sortent, sans redéposer le fichier.
        code, reponse = self.regler(port, jeton, "liste_exclusion", True)
        self.assertEqual((code, reponse["ok"]), (200, True))
        self.assertEqual({f["id"]: f["actif"] for f in reponse["filtres"]}, {**CASES_DEFAUT, "liste_exclusion": True})
        nouveau = reponse["passage"]
        self.assertEqual((nouveau["ok"], nouveau["fichier"], nouveau["total"], nouveau["conformes"], nouveau["rejetes"]), (True, "export-brut-exemple.csv", 30, 19, 11))
        self.assertEqual(self.causes(nouveau), {"Mauvais intitulé de poste": 7, "Doublon": 2, "Liste d'exclusion": 2})
        self.assertEqual(dict(nouveau["par_cible"]), {"Dirigeants": 14, "Direction commerciale": 5})
        self.assertIn("Liste d'exclusion", nouveau["filtres_texte"].split(" · désactivés")[0])
        self.assertTrue(self.reglages(dossier)["liste_exclusion"], "enregistré aussitôt")
        self.assertEqual(self.passage(dossier)["conformes"], 19, "le bilan du dossier suit")
        import openpyxl
        contenu = self.appeler(port, "GET", nouveau["telechargements"]["nettoye"]["url"])[2]
        self.assertEqual(len(list(openpyxl.load_workbook(io.BytesIO(contenu)).worksheets[0].iter_rows())) - 1, 19, "le téléchargement est à jour")
        # Les autres cases, une à une : chaque coche retire exactement les lignes comptées à la main.
        for identifiant, conformes in (("champs_obligatoires", 18), ("pays", 17), ("activite", 15), ("taille", 13)):
            with self.subTest(case=identifiant):
                reponse = self.regler(port, jeton, identifiant, True)[1]
                self.assertEqual(reponse["passage"]["conformes"], conformes)
        tout = reponse["passage"]
        self.assertEqual(self.causes(tout), {cause.replace(" " + TIRET_CADRATIN + " ", " : "): n for cause, n in CAUSES_TOUT.items()})
        self.assertEqual(dict(tout["par_cible"]), PAR_CIBLE_TOUT)
        self.assertEqual({f["id"]: f["actif"] for f in tout["filtres"]}, CASES_TOUT)
        # Décochée de nouveau : les deux lignes reviennent.
        reponse = self.regler(port, jeton, "liste_exclusion", False)[1]
        self.assertEqual((reponse["passage"]["conformes"], self.reglages(dossier)["liste_exclusion"]), (15, False))

    def test_la_page_des_regles_dit_quels_filtres_sont_actifs(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        self.deposer(port, jeton, "export-brut-exemple.csv")
        self.regler(port, jeton, "liste_exclusion", True)
        page = self.appeler(port, "GET", f"/regles?t={jeton}")[2].decode("utf-8")
        self.assertIn('<p class="regle-titre">Liste d&#x27;exclusion<span class="etat etat-actif">actif</span></p>', page)
        self.assertIn('<p class="regle-titre">Pays<span class="etat">désactivé</span></p>', page)
        self.assertIn("Filtres actifs : Intitulé de poste dans mes cibles, Intitulés à écarter, Doublons, Liste d'exclusion"
                      " · désactivés : Lignes incomplètes, Pays, Activité, Taille", re.sub(r"\s+", " ", texte_visible(page)))

    def test_les_cases_se_retiennent_d_un_lancement_a_l_autre(self):
        dossier, processus, port, jeton = self.monter_et_demarrer()
        self.assertEqual(self.regler(port, jeton, "pays", True)[0], 200)
        self.assertEqual(self.regler(port, jeton, "doublons", False)[0], 200)
        self.assertEqual(self.appeler(port, "POST", f"/fermer?t={jeton}", b"")[0], 200)
        self.assertEqual(processus.wait(timeout=20), 0)
        self.assertEqual(self.reglages(dossier), {**CASES_DEFAUT, "pays": True, "doublons": False})
        _, port2, jeton2 = self.demarrer(dossier)
        page = self.appeler(port2, "GET", f"/?t={jeton2}")[2].decode("utf-8")
        self.assertIn('data-filtre="pays" checked>', page)
        self.assertIn('data-filtre="doublons">', page)
        self.assertIn('data-filtre="postes_exclus" checked>', page)
        donnees = self.deposer(port2, jeton2, "export-brut-exemple.csv")[2]
        self.assertEqual((donnees["conformes"], donnees["rejetes"]), (22, 8), "pays coché : la ligne 22 sort ; doublons décoché : les lignes 15 et 16 restent")
        self.assertEqual(self.causes(donnees), {"Mauvais intitulé de poste": 7, "Pays": 1})
        # la ligne de commande applique les mêmes cases
        ligne = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(ligne.returncode, 0, ligne.stderr)
        self.assertIn("Lignes 30 · gardées 22 · à vérifier 0 · rejetées 8", ligne.stdout)
        self.assertIn("Filtres actifs : Intitulé de poste dans mes cibles, Intitulés à écarter, Pays · désactivés : Doublons, Liste d'exclusion, Lignes incomplètes, Activité, Taille", ligne.stdout)

    def test_la_ligne_de_commande_lit_reglages_json_ecrit_a_la_main(self):
        dossier = self.monter()
        (dossier / "reglages.json").write_text(json.dumps({"filtres": {"liste_exclusion": True}}), encoding="utf-8")
        ligne = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(ligne.returncode, 0, ligne.stdout + ligne.stderr)
        self.assertIn("Lignes 30 · gardées 19 · à vérifier 0 · rejetées 11", ligne.stdout)
        self.assertIn("Liste d'exclusion 2", ligne.stdout)
        (dossier / "reglages.json").unlink()
        self.assertIn("gardées 21", moulinette(dossier, str(self.EXPORT)).stdout, "sans fichier, les cases par défaut")
        (dossier / "reglages.json").write_text("{pas du json", encoding="utf-8")
        self.assertIn("gardées 21", moulinette(dossier, str(self.EXPORT)).stdout, "un fichier illisible ne casse rien : cases par défaut")

    # --- ce qui est refusé

    def test_la_case_de_l_intitule_est_verrouillee_cote_serveur(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        avant = (dossier / "reglages.json").read_bytes()
        code, reponse = self.regler(port, jeton, "poste", False)
        self.assertEqual((code, reponse["ok"]), (400, False))
        self.assertIn("toujours actif", reponse["erreur"])
        self.assertEqual((dossier / "reglages.json").read_bytes(), avant, "rien n'a été écrit")
        self.assertEqual(self.regler(port, jeton, "poste", True)[0], 200, "la cocher (elle l'est déjà) ne pose pas de problème")
        # un identifiant inconnu, un filtre que la mission n'a pas, une valeur de case illisible
        self.assertEqual(self.regler(port, jeton, "inconnu", True)[0], 400)
        self.assertEqual(self.regler(port, jeton, "naf", True)[0], 400, "pas de règle NAF dans cette mission")
        for requete in ("&actif=2", "&actif=", "&actif=oui", ""):
            code, _, corps = self.appeler(port, "POST", f"/reglage?t={jeton}&id=pays{requete}", b"")
            self.assertEqual(code, 400, requete)
            self.assertFalse(json.loads(corps)["ok"])
        code, _, corps = self.appeler(port, "POST", f"/reglage?t={jeton}&actif=1", b"")
        self.assertEqual(code, 400)
        self.assertEqual(self.reglages(dossier)["pays"], False)
        self.assertEqual(self.appeler(port, "GET", f"/reglage?t={jeton}&id=pays&actif=1")[0], 404, "seule la méthode POST écrit")
        self.assertFalse(self.reglages(dossier)["pays"])
        # la même chose écrite à la main dans reglages.json n'éteint pas l'intitulé non plus
        (dossier / "reglages.json").write_text(json.dumps({"filtres": {"poste": False, "doublons": "non", "pays": 1}}), encoding="utf-8")
        donnees = self.deposer(port, jeton, "export-brut-exemple.csv")[2]
        self.assertEqual({f["id"]: f["actif"] for f in donnees["filtres"]}, CASES_DEFAUT, "intitulé toujours actif, valeurs qui ne sont pas vrai/faux ignorées")
        self.assertEqual((donnees["conformes"], donnees["rejetes"]), (21, 9))

    def test_cocher_la_liste_sans_liste_est_refuse(self):
        dossier, _, port, jeton = self.monter_et_demarrer()
        (dossier / "exclusions.csv").unlink()
        code, reponse = self.regler(port, jeton, "liste_exclusion", True)
        self.assertEqual((code, reponse["ok"]), (400, False))
        self.assertIn("Posez d'abord votre liste", reponse["erreur"])
        self.assertFalse(self.reglages(dossier)["liste_exclusion"])

    def test_un_filtre_decoche_ne_bloque_jamais_le_controle_avant_lancement(self):
        """(b) Liste d'exclusion absente ou modifiée, champs obligatoires manquants : rien ne bloque tant que la case est décochée."""
        dossier = self.monter()
        (dossier / "exclusions.csv").unlink()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0, "liste absente, case décochée")
        (dossier / "exclusions.csv").write_text("Entreprise\nAlpha\n", encoding="utf-8")
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0, "liste modifiée, case décochée")
        self.assertEqual(self.passage(dossier)["conformes"], 21)
        court = self.nouveau_dossier() / "court.csv"
        court.write_text("Nom complet;Poste\nAda;CEO\nGrace;Stagiaire\n", encoding="utf-8")
        processus = moulinette(dossier, str(court))
        self.assertEqual(processus.returncode, 0, processus.stdout + processus.stderr)
        self.assertEqual(self.passage(dossier)["conformes"], 1, "sans colonne Entreprise : les champs obligatoires sont décochés")
        # cochés, les mêmes manques bloquent comme avant
        self.cocher(dossier, champs_obligatoires=True)
        processus = moulinette(dossier, str(court))
        self.assertEqual(processus.returncode, 2, processus.stdout)
        self.assertIn("la colonne « Entreprise »", processus.stdout)
        self.cocher(dossier, champs_obligatoires=False, liste_exclusion=True)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2, processus.stdout)
        self.assertIn("n'est plus celle que vous avez validée", processus.stdout)

    def test_la_validation_ne_depend_pas_des_cases(self):
        """Cocher et décocher n'écrit jamais dans mission.json et ne demande aucune nouvelle validation ; ni le fichier de mission ni sa signature ne bougent."""
        dossier, _, port, jeton = self.monter_et_demarrer()
        mission = (dossier / "mission.json").read_bytes()
        self.deposer(port, jeton, "export-brut-exemple.csv")
        for identifiant in ("liste_exclusion", "champs_obligatoires", "pays", "activite", "taille", "doublons", "postes_exclus"):
            self.assertEqual(self.regler(port, jeton, identifiant, identifiant not in ("doublons", "postes_exclus"))[0], 200)
        self.assertEqual((dossier / "mission.json").read_bytes(), mission, "mission.json n'a pas bougé d'un octet")
        _, configuration = m.charger_mission(dossier / "mission.json")
        self.assertTrue(m.cible_validee(configuration), "la validation tient")
        self.assertTrue(all(r["active"] is True for r in configuration["regles"]))
        self.assertEqual(configuration["champsObligatoires"], ["nomComplet", "entreprise", "poste"])
        self.assertTrue(configuration["blacklist"]["obligatoire"])
        self.assertFalse(list(dossier.glob("*.tmp")) or list(dossier.glob("mission-*")), "aucune copie de mission ne reste dans le dossier")
        self.assertEqual(self.passage(dossier)["signature_regles"], m.empreinte_regles(configuration), "le passage porte la signature de la mission complète")
        self.assertIn("Règles validées le 17/11/2026", texte_visible(self.appeler(port, "GET", f"/regles?t={jeton}")[2].decode("utf-8")))


class Emballage(Banc):
    def test_pyproject_declare_la_commande_et_le_fichier(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib absent (Python < 3.11)")
        projet = tomllib.loads((RACINE / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(projet["project"]["scripts"]["leadlint-setup"], "leadlint_setup:main")
        self.assertEqual(projet["project"]["scripts"]["leadlint"], "leadlint:main")
        self.assertEqual(projet["project"]["authors"], [{"name": "Zero Gravity AI"}])
        livre = projet["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(livre["skills/leadlint/scripts/leadlint_setup.py"], "leadlint_setup.py")
        inclus = projet["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        for chemin in ("/skills/leadlint/scripts/leadlint_setup.py", "/skills/leadlint-setup"):
            self.assertIn(chemin, inclus)
            self.assertTrue((RACINE / chemin.lstrip("/")).exists(), chemin)

    def test_versions_alignees(self):
        for nom in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            donnees = json.loads((RACINE / nom).read_text(encoding="utf-8"))
            self.assertEqual(donnees.get("version") or donnees["metadata"]["version"], m.VERSION, nom)
        self.assertEqual(m.VERSION, "1.6.0")
        self.assertEqual(s.VERSION, m.VERSION)

    def test_la_competence_est_bien_formee(self):
        texte = (RACINE / "skills" / "leadlint-setup" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(texte.startswith("---\n"))
        entete = texte.split("---\n")[1]
        self.assertRegex(entete, r"(?m)^name: leadlint-setup$")
        self.assertRegex(entete, r"(?m)^description: >-$")
        for declencheur in ("set up leadlint", "installe ma moulinette", "change mes règles LeadLint", "montre-moi mes règles"):
            self.assertIn(declencheur, entete)
        for commande in ("CLAUDE_PLUGIN_ROOT", "uvx --from git+https://github.com/Gremelinn0/sales-leadlint leadlint-setup", "validate", "--force"):
            self.assertIn(commande, texte)
        for lanceur in ("Ouvrir la moulinette.bat", "ouvrir-la-moulinette.command", "no agent and no terminal"):
            self.assertIn(lanceur, texte)
        for mot in ("reglages.json", "tick", "contacts"):
            self.assertIn(mot, texte)
        self.assertNotIn("email_pro_obligatoire", texte)
        self.assertNotIn("mobile_obligatoire", texte)
        self.assertIn("Ouvrir la moulinette", (RACINE / "README.md").read_text(encoding="utf-8"))
        moteur = (RACINE / "skills" / "leadlint" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("leadlint-setup", moteur.split("## When to use it")[1].split("\n")[2], "la compétence du moteur renvoie vers l'agent de setup")

    def test_les_exemples_sont_coherents(self):
        reponses = s.lire_reponses(EXEMPLES / "reponses-exemple.json")[0]
        self.assertEqual(len(reponses["cibles"]), 2)
        self.assertNotIn("email_pro_obligatoire", reponses)
        entetes, lignes = m.lire_tableau(EXEMPLES / "export-brut-exemple.csv")
        self.assertEqual(len(lignes), 30)
        devines = m.deviner_mapping(entetes, m.ENTETES_CANDIDATS)
        for champ in ("nomComplet", "entreprise", "poste", "linkedinProfil", "pays", "activite", "employesVisibles", "emailProfessionnel", "mobile", "siteEntreprise"):
            self.assertIn(champ, devines, champ)
        liste, lignes_liste = m.lire_tableau(EXEMPLES / "exclusions-exemple.csv")
        self.assertEqual(len(lignes_liste), 3)
        self.assertTrue(set(m.deviner_mapping(liste, m.ENTETES_BLACKLIST)) >= {"entreprise", "siteEntreprise"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
