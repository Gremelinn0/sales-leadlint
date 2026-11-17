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
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
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
LIGNES_ATTENDUES = {
    1: ("Conforme", "Dirigeants"), 2: ("Conforme", "Direction commerciale"), 3: ("Conforme", "Dirigeants"),
    4: ("Conforme", "Direction commerciale"), 5: ("Conforme", "Dirigeants"), 6: ("Conforme", "Dirigeants"),
    7: ("Conforme", "Direction commerciale"), 8: ("Conforme", "Dirigeants"), 9: ("Conforme", "Direction commerciale"),
    10: ("Conforme", "Dirigeants"), 11: ("Rejeté", "Mauvais intitulé de poste"), 12: ("Rejeté", "Mauvais intitulé de poste"),
    13: ("Rejeté", "Mauvais intitulé de poste"), 14: ("Rejeté", "Mauvais intitulé de poste"), 15: ("Rejeté", "Doublon"),
    16: ("Rejeté", "Doublon"), 17: ("Rejeté", "Liste d'exclusion"), 18: ("Rejeté", "Liste d'exclusion"),
    19: ("Rejeté", "Mauvais intitulé de poste"), 20: ("Rejeté", "Mauvais intitulé de poste"), 21: ("Rejeté", "Email professionnel"),
    22: ("Rejeté", "Pays"), 23: ("Rejeté", "Employés visibles LinkedIn"), 24: ("Rejeté", "Employés visibles LinkedIn"),
    25: ("Rejeté", "Mauvaise entreprise"), 26: ("Rejeté", "Mauvaise entreprise"), 27: ("Rejeté", "Email professionnel"),
    28: ("Rejeté", "Champ obligatoire manquant : entreprise"), 29: ("Rejeté", "Mauvais intitulé de poste"), 30: ("Conforme", "Direction commerciale"),
}
CAUSES_ATTENDUES = {
    "Mauvais intitulé de poste": 7, "Doublon": 2, "Liste d'exclusion": 2, "Email professionnel": 2,
    "Employés visibles LinkedIn": 2, "Mauvaise entreprise": 2, "Pays": 1, "Champ obligatoire manquant " + TIRET_CADRATIN + " entreprise": 1,
}
PAR_CIBLE_ATTENDU = {"Dirigeants": 6, "Direction commerciale": 5}
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

    def monter(self, reponses: dict = None, exclusions: bool = True, valider: bool = True) -> Path:
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
        return dossier

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
            "r-employes_visibles-entre", "r-email_pro-email_professionnel", "r-blacklist-pas_blackliste", "r-doublon-unique",
        ])
        voulu = regles["r-poste-contient_un"]
        self.assertEqual((voulu["importance"], voulu["siAbsent"], voulu.get("motEntier")), ("bloquant", "rejeter", True))
        self.assertEqual(voulu["valeur"], ["CEO", "Fondateur", "Fondatrice", "Founder", "Co-founder", "Co-fondateur", "Gérant", "Directeur général", "Président",
                                           "Directeur commercial", "Directrice commerciale", "Head of Sales", "VP Sales"])
        ecarte = regles["r-poste-ne_contient_aucun"]
        self.assertEqual((ecarte["siAbsent"], ecarte.get("motEntier")), ("ignorer", True))
        self.assertEqual(regles["r-pays-est_dans"]["siAbsent"], "ignorer")
        self.assertEqual(regles["r-employes_visibles-entre"]["valeur"], {"min": 11, "max": 200})
        self.assertEqual(regles["r-email_pro-email_professionnel"]["siAbsent"], "rejeter")
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
        self.assertEqual((passage["total"], passage["conformes"], passage["rejetes"]), (30, 13, 17), "les deux lignes de la liste d'exclusion passent sans elle")
        self.assertNotIn("Liste d'exclusion", passage["causes"]["Rejeté"])
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
        self.assertIn("Lignes 30 · gardées 11 · à vérifier 0 · rejetées 19", sortie)
        self.assertIn("Contrôles du bilan : justes", sortie)
        self.assertNotIn(TIRET_CADRATIN, sortie)
        passage = self.passage(dossier)
        self.assertEqual((passage["total"], passage["conformes"], passage["a_verifier"], passage["rejetes"]), (30, 11, 0, 19))
        self.assertEqual(passage["total"], passage["conformes"] + passage["a_verifier"] + passage["rejetes"], "premier invariant")
        self.assertEqual(passage["a_verifier"] + passage["rejetes"], sum(passage["causes"]["Rejeté"].values()) + sum(passage["causes"]["À vérifier"].values()), "second invariant")
        self.assertEqual(passage["causes"]["Rejeté"], CAUSES_ATTENDUES)
        self.assertEqual(passage["causes"]["À vérifier"], {})
        self.assertEqual(passage["par_cible"], PAR_CIBLE_ATTENDU)
        self.assertEqual(passage["regles_non_appliquees"], [])
        self.assertEqual(passage["fichier"], "export-brut-exemple.csv")
        self.assertEqual(passage["moteur"], f"leadlint {m.VERSION}")
        for chemin in passage["fichiers"].values():
            self.assertTrue((dossier / chemin).is_file(), chemin)

    def test_verdict_ligne_par_ligne(self):
        """Les causes de l'exemple, ligne par ligne, comptées à la main : le moteur de ce dossier donne les mêmes."""
        dossier = self.monter()
        entetes, lignes = m.lire_tableau(self.EXPORT)
        _, configuration = m.charger_mission(dossier / "mission.json")
        configuration["mappingCandidats"] = m.completer_mapping(configuration["mappingCandidats"], entetes, m.ENTETES_CANDIDATS)
        liste_entetes, liste_lignes = m.lire_tableau(dossier / "exclusions.csv")
        configuration["mappingBlacklist"] = m.completer_mapping(configuration["mappingBlacklist"], liste_entetes, m.ENTETES_BLACKLIST)
        verdicts = m.valider_export_b2b(m.vers_objets(entetes, lignes), m.vers_objets(liste_entetes, liste_lignes), configuration)
        obtenu = {v["numeroLigne"] - 1: (v["statut"], m.type_probleme(v).replace(" " + TIRET_CADRATIN + " ", " : ") or None) for v in verdicts}
        for ligne, (statut, cause) in LIGNES_ATTENDUES.items():
            with self.subTest(ligne=ligne):
                if statut == "Conforme":
                    self.assertEqual(obtenu[ligne], ("Conforme", None))
                else:
                    self.assertEqual(obtenu[ligne], (statut, cause))

    def test_le_fichier_nettoye_porte_la_cible(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        import openpyxl
        classeur = openpyxl.load_workbook(dossier / "resultats" / "export-brut-exemple-nettoye.xlsx")
        feuille = classeur.worksheets[0]
        lignes = [[c.value for c in ligne] for ligne in feuille.iter_rows()]
        self.assertEqual(lignes[0], ["Nom complet", "Entreprise", "Poste", "LinkedIn profil", "Email professionnel", "Mobile professionnel", "Cible"])
        self.assertEqual(len(lignes) - 1, 11)
        self.assertEqual(Counter(l[-1] for l in lignes[1:]), Counter(PAR_CIBLE_ATTENDU))
        par_nom = {l[0]: l[-1] for l in lignes[1:]}
        for numero, (statut, cible) in LIGNES_ATTENDUES.items():
            nom = f"Camille Exemple {numero:02d}"
            if statut == "Conforme":
                self.assertEqual(par_nom[nom], cible, nom)
            elif numero not in (15, 16):  # deux doublons portent le nom d'une ligne gardée
                self.assertNotIn(nom, par_nom, nom)
        self.assertEqual(classeur.properties.creator, "leadlint")
        for nom in ("export-brut-exemple-controle-qa.xlsx", "export-brut-exemple-conformes.xlsx"):
            self.assertTrue((dossier / "resultats" / nom).is_file(), nom)

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
        self.assertEqual(self.passage(dossier)["causes"]["Rejeté"], CAUSES_ATTENDUES)
        self.assertEqual(self.passage(dossier)["par_cible"], PAR_CIBLE_ATTENDU)

    def test_regle_modifiee_apres_validation_bloque(self):
        dossier = self.monter()
        mission = self.mission(dossier)
        pays = next(r for r in mission["configuration"]["regles"] if r["id"] == "r-pays-est_dans")
        pays["valeur"].append("Belgique")
        self.ecrire_mission(dossier, mission)
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2, processus.stdout + processus.stderr)
        self.assertIn("Vos règles ont changé depuis la validation", processus.stdout)
        self.assertFalse((dossier / "resultats").exists() and list((dossier / "resultats").glob("*")), "rien n'est livré")
        # Le contrôle négatif : sans ce garde-fou, le même changement ferait passer une ligne de plus (la ligne 22, en Belgique).
        entetes, lignes = m.lire_tableau(self.EXPORT)
        diagnostic = subprocess.run(
            [sys.executable, str(SCRIPTS / "leadlint.py"), "check", str(self.EXPORT), "--mission", str(dossier / "mission.json"),
             "--exclusions", str(dossier / "exclusions.csv"), "--diagnostic", "--json", "-o", str(self.nouveau_dossier())],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(diagnostic.stdout)["conformes"], 12, "le changement modifie bien les verdicts : le blocage n'est pas inutile")

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
        dossier = self.monter()
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
        dossier = self.monter()
        (dossier / "exclusions.csv").unlink()
        processus = moulinette(dossier, str(self.EXPORT))
        self.assertEqual(processus.returncode, 2)
        self.assertIn("Il manque la liste d'exclusion", processus.stdout)
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
        self.assertIn("Règles non appliquées, faute de colonne dans votre export : Code NAF", processus.stdout)
        passage = self.passage(dossier)
        self.assertEqual(passage["regles_non_appliquees"], [{"regle": "r-code_naf-est_dans", "champ": "codeNaf", "colonne": "Code NAF"}])
        self.assertEqual(passage["conformes"], 11, "une règle sans colonne ne rejette personne (donnée absente : ignorée)")
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
        self.assertEqual(self.passage(nouveau)["conformes"], 11)

    def test_deux_passages_de_suite(self):
        dossier = self.monter()
        premier = moulinette(dossier, str(self.EXPORT))
        second = moulinette(dossier, str(self.EXPORT))
        self.assertEqual((premier.returncode, second.returncode), (0, 0))
        self.assertEqual(self.passage(dossier)["conformes"], 11)


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
        for famille in ("Entreprise", "Contact", "Exclusions", "Qualité"):
            self.assertRegex(page, rf"<h3>{famille}</h3>")
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
        dossier = self.monter()
        (dossier / "exclusions.csv").write_text("Entreprise,Site\nAutre,autre.example\n", encoding="utf-8")
        self.assertEqual(setup("page", str(dossier))[0], 0)
        self.assertIn("Liste d'exclusion modifiée depuis la validation : à revalider", texte_visible(self.page(dossier)))

    def test_le_dernier_passage_se_lit_sur_la_page(self):
        dossier = self.monter()
        self.assertEqual(moulinette(dossier, str(self.EXPORT)).returncode, 0)
        visible = texte_visible(self.page(dossier))
        self.assertRegex(visible, r"11\s+Gardés\s+0\s+À vérifier\s+19\s+Rejetés")
        self.assertIn("sur 30 lignes", visible)
        self.assertIn("Mauvais intitulé de poste 7", re.sub(r"\s+", " ", visible))
        self.assertIn("Champ obligatoire manquant : entreprise", visible)
        self.assertIn("Dirigeants 6", re.sub(r"\s+", " ", visible))
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
        (dossier / "exclusions.csv").unlink()
        textes += [("liste absente", moulinette(dossier, str(self.EXPORT)).stdout)]
        for origine, texte in textes:
            with self.subTest(origine=origine):
                self.assertIsNone(MOTS_INTERDITS.search(texte), MOTS_INTERDITS.search(texte) and MOTS_INTERDITS.search(texte).group(0))
                self.assertNotIn(TIRET_CADRATIN, texte)


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
        self.assertEqual(m.VERSION, "1.4.0")
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
        moteur = (RACINE / "skills" / "leadlint" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("leadlint-setup", moteur.split("## When to use it")[1].split("\n")[2], "la compétence du moteur renvoie vers l'agent de setup")

    def test_les_exemples_sont_coherents(self):
        reponses = s.lire_reponses(EXEMPLES / "reponses-exemple.json")[0]
        self.assertEqual(len(reponses["cibles"]), 2)
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
