# -*- coding: utf-8 -*-
"""Banc de `leadlint hubspot` : un faux HubSpot local (http.server), jamais le vrai.

    python tests/test_hubspot.py

Le faux serveur parle comme l'API CRM v3 sur les points d'accès utilisés : groupes et propriétés (409 si elles existent),
recherche de contacts (filtre GTE, tri croissant, `after`, 10 000 résultats au plus : la page suivante est refusée en 400),
mise à jour par lots (100 au plus). Il note chaque appel et sait tomber en panne (429, 5xx) sur commande. L'horloge est fausse
aussi : le banc ne dort jamais. Aucun appel au vrai HubSpot.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

RACINE = Path(__file__).resolve().parents[1]
PARITE = RACINE / "tests" / "parite"
SCRIPT = RACINE / "skills" / "leadlint" / "scripts" / "leadlint.py"
sys.path.insert(0, str(SCRIPT.parent))

import leadlint as m  # noqa: E402

JETON = "jeton-secret-pour-le-banc-9f3a"
T0 = 1_790_000_000_000  # une date quelconque, en millisecondes
POSTES = ["CEO", "Directeur marketing", "Responsable achats", "Développeur", "", "Head of Sales", "DRH"]


class FauxHubSpot:
    """L'état du faux compte et le journal des appels."""

    def __init__(self) -> None:
        self.groupes: set[str] = set()
        self.proprietes: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.appels: list[tuple[str, str, object]] = []
        self.pannes: list[tuple[str, int, dict]] = []  # (morceau du chemin, statut, en-têtes), servie une fois
        self.pannes_permanentes: dict[str, int] = {}
        self.refus_ecriture = False
        self.violations_plafond = 0
        self.jeton_attendu = JETON
        self.horloge = T0
        self.serveur: ThreadingHTTPServer | None = None

    def ajouter(self, identifiant: int, poste: str, ts: int, **autres) -> None:
        proprietes = {"jobtitle": poste, **autres}
        self.contacts[str(identifiant)] = {"properties": proprietes, "ts": ts}

    def appels_a(self, morceau: str) -> list:
        return [a for a in self.appels if morceau in a[1]]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def demarrer(self) -> None:
        faux = self

        class Gestionnaire(BaseHTTPRequestHandler):
            def log_message(self, *arguments):  # silence
                pass

            def repondre(self, statut: int, corps=None, entetes=None) -> None:
                brut = b"" if corps is None else json.dumps(corps).encode("utf-8")
                self.send_response(statut)
                for cle, valeur in (entetes or {}).items():
                    self.send_header(cle, valeur)
                self.send_header("Content-Length", str(len(brut)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(brut)

            def do_POST(self) -> None:
                longueur = int(self.headers.get("Content-Length") or 0)
                corps = json.loads(self.rfile.read(longueur).decode("utf-8")) if longueur else None
                faux.appels.append(("POST", self.path, corps))
                if self.headers.get("Authorization") != f"Bearer {faux.jeton_attendu}":
                    return self.repondre(401, {"status": "error", "message": "Authentication credentials not found."})
                for index, (morceau, statut, entetes) in enumerate(faux.pannes):
                    if morceau in self.path:
                        del faux.pannes[index]
                        return self.repondre(statut, {"status": "error", "message": "panne simulée"}, entetes)
                for morceau, statut in faux.pannes_permanentes.items():
                    if morceau in self.path:
                        return self.repondre(statut, {"status": "error", "message": "panne permanente simulée"})
                if self.path == "/crm/v3/properties/contacts/groups":
                    if corps["name"] in faux.groupes:
                        return self.repondre(409, {"status": "error", "message": "group exists"})
                    faux.groupes.add(corps["name"])
                    return self.repondre(201, corps)
                if self.path == "/crm/v3/properties/contacts":
                    if corps["name"] in faux.proprietes:
                        return self.repondre(409, {"status": "error", "message": "property exists"})
                    faux.proprietes[corps["name"]] = corps
                    return self.repondre(201, corps)
                if self.path == "/crm/v3/objects/contacts/search":
                    return self.recherche(corps)
                if self.path == "/crm/v3/objects/contacts/batch/update":
                    return self.lot(corps)
                return self.repondre(404, {"status": "error", "message": "inconnu"})

            def recherche(self, corps: dict) -> None:
                filtres = corps["filterGroups"][0]["filters"]
                assert [f["operator"] for f in filtres] == ["GTE"] and filtres[0]["propertyName"] == "lastmodifieddate"
                assert corps["sorts"] == [{"propertyName": "lastmodifieddate", "direction": "ASCENDING"}]
                borne = int(filtres[0]["value"])
                retenus = sorted(((c["ts"], int(i)) for i, c in faux.contacts.items() if c["ts"] >= borne))
                debut, limite = int(corps.get("after") or 0), corps["limit"]
                if debut + limite > 10_000:  # la limite de HubSpot, écrite en dur : le banc ne doit pas suivre une constante du script
                    faux.violations_plafond += 1
                    return self.repondre(400, {"status": "error", "message": "Cannot page beyond 10000 results"})
                page = retenus[debut:debut + limite]
                resultats = []
                for ts, identifiant in page:
                    proprietes = faux.contacts[str(identifiant)]["properties"]
                    voulues = {p: proprietes[p] for p in corps["properties"] if p in proprietes}
                    voulues["lastmodifieddate"] = m._iso_depuis_ms(ts)
                    resultats.append({"id": str(identifiant), "properties": voulues})
                reponse = {"total": len(retenus), "results": resultats}
                if debut + limite < len(retenus):
                    reponse["paging"] = {"next": {"after": str(debut + limite)}}
                self.repondre(200, reponse)

            def lot(self, corps: dict) -> None:
                if faux.refus_ecriture:
                    return self.repondre(400, {"status": "error", "message": "Property values were not valid"})
                if len(corps["inputs"]) > 100:
                    return self.repondre(400, {"status": "error", "message": "too many inputs"})
                faux.horloge += 1
                for entree in corps["inputs"]:
                    fiche = faux.contacts[entree["id"]]
                    fiche["properties"].update(entree["properties"])
                    fiche["ts"] = faux.horloge
                self.repondre(200, {"status": "COMPLETE", "results": [{"id": e["id"]} for e in corps["inputs"]]})

        self.serveur = ThreadingHTTPServer(("127.0.0.1", 0), Gestionnaire)
        threading.Thread(target=self.serveur.serve_forever, daemon=True).start()

    def arreter(self) -> None:
        self.serveur.shutdown()
        self.serveur.server_close()


class BaseHubSpot(unittest.TestCase):
    def setUp(self) -> None:
        self.faux = FauxHubSpot()
        self.faux.demarrer()
        self.addCleanup(self.faux.arreter)
        self.dossier = Path(tempfile.mkdtemp(prefix="leadlint-hubspot-"))
        self.etat = self.dossier / "etat.json"
        self.sommeils: list[float] = []
        for correctif in (
            mock.patch.object(m, "_dormir", lambda secondes: self.sommeils.append(secondes)),
            mock.patch.object(m, "PAUSE_RECHERCHE_HUBSPOT", 0),
            mock.patch.object(m, "_maintenant", lambda: m.EPOCH + m.dt.timedelta(milliseconds=self.faux.horloge)),
            mock.patch.dict(os.environ, {"HUBSPOT_TOKEN": JETON}),
        ):
            correctif.start()
            self.addCleanup(correctif.stop)

    def lancer(self, *arguments: str) -> tuple[int, str, str]:
        sortie, erreurs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
            try:
                code = m.main(["hubspot", *arguments, "--api-url", self.faux.url])
            except SystemExit as arret:
                code = arret.code
        return code, sortie.getvalue(), erreurs.getvalue()

    def sync(self, *arguments: str) -> tuple[int, dict]:
        code, sortie, erreurs = self.lancer("sync", "--etat", str(self.etat), "--json", *arguments)
        try:
            return code, json.loads(sortie)
        except json.JSONDecodeError:
            self.fail(f"sortie illisible (code {code}) : {sortie!r} {erreurs!r}")

    def peupler(self, nombre: int, ts0: int | None = None) -> None:
        ts0 = T0 - 10 * 86_400_000 if ts0 is None else ts0
        for i in range(1, nombre + 1):
            self.faux.ajouter(i, POSTES[i % len(POSTES)], ts0 + i)


class Setup(BaseHubSpot):
    def test_setup_cree_le_groupe_et_les_cinq_proprietes(self):
        code, sortie, _ = self.lancer("setup")
        self.assertEqual(code, 0, sortie)
        self.assertEqual(self.faux.groupes, {"leadlint"})
        self.assertEqual(set(self.faux.proprietes), {"leadlint_segment", "leadlint_niveau", "leadlint_verdict", "leadlint_raison", "leadlint_date"})
        verdict = self.faux.proprietes["leadlint_verdict"]
        self.assertEqual((verdict["type"], verdict["fieldType"]), ("enumeration", "select"))
        self.assertEqual([o["value"] for o in verdict["options"]], ["Conforme", "A verifier", "Rejete"])
        self.assertEqual(self.faux.proprietes["leadlint_date"]["type"], "datetime")
        self.assertTrue(all(p["groupName"] == "leadlint" for p in self.faux.proprietes.values()))

    def test_setup_est_idempotent(self):
        self.lancer("setup")
        avant = json.dumps(self.faux.proprietes, sort_keys=True)
        code, sortie, _ = self.lancer("setup", "--json")
        self.assertEqual(code, 0)
        bilan = json.loads(sortie)
        self.assertEqual(bilan["groupe"], "existe déjà")
        self.assertEqual(set(bilan["proprietes"].values()), {"existe déjà"})
        self.assertEqual(json.dumps(self.faux.proprietes, sort_keys=True), avant)
        self.assertEqual(len(self.faux.appels_a("/properties/contacts")), 2 * 6)  # 6 appels par passage, rien de plus


class SyncPersonas(BaseHubSpot):
    def test_pagination_puis_ecriture_des_seuls_changements(self):
        self.peupler(250)
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0, bilan)
        recherches = self.faux.appels_a("/search")
        self.assertEqual(len(recherches), 3)
        self.assertEqual([r[2].get("after") for r in recherches], [None, "100", "200"])
        self.assertTrue(all(r[2]["limit"] == 100 and r[2]["filterGroups"][0]["filters"][0]["value"] == "0" for r in recherches))
        lots = self.faux.appels_a("/batch/update")
        self.assertEqual([len(l[2]["inputs"]) for l in lots], [100, 100, 50])
        self.assertEqual((bilan["fiches_lues"], bilan["fiches_ecrites"], bilan["fiches_inchangees"]), (250, 250, 0))
        # Le résultat est celui du moteur de `personas`, et la date est écrite avec la valeur.
        ecrit = self.faux.contacts["1"]["properties"]
        attendu = m.segmenter([POSTES[1]], json.loads(m.MATRICE_DEFAUT.read_text(encoding="utf-8")))["lignes"][0]
        self.assertEqual((ecrit["leadlint_segment"], ecrit["leadlint_niveau"]), (attendu["categorie"], attendu["seniorite"]))
        self.assertEqual(self.faux.contacts["4"]["properties"]["leadlint_segment"], m.NON_CLASSE)  # poste vide
        self.assertEqual(ecrit["leadlint_date"], m._iso_depuis_ms(T0))
        self.assertNotIn("leadlint_verdict", ecrit)

    def test_un_second_passage_complet_n_ecrit_rien(self):
        self.peupler(250)
        self.sync("--personas")
        ecritures = len(self.faux.appels_a("/batch/update"))
        code, bilan = self.sync("--personas", "--tout")
        self.assertEqual(code, 0)
        self.assertEqual((bilan["fiches_lues"], bilan["fiches_a_ecrire"], bilan["fiches_inchangees"]), (250, 0, 250))
        self.assertEqual(len(self.faux.appels_a("/batch/update")), ecritures)

    def test_le_passage_suivant_ne_relit_que_le_recent_et_n_ecrit_que_ce_qui_change(self):
        self.peupler(250)
        self.sync("--personas")
        self.assertEqual(json.loads(self.etat.read_text(encoding="utf-8"))["dernier_passage"], m._iso_depuis_ms(T0))
        # Un passage relit ses propres écritures une fois (elles sont datées après son début) et n'y trouve rien à changer.
        self.faux.horloge = T0 + 3_600_000
        code, bilan = self.sync("--personas")
        self.assertEqual((code, bilan["fiches_lues"], bilan["fiches_a_ecrire"]), (0, 250, 0))
        # Trois contacts changent de poste, un quatrième est modifié sans que son poste change.
        self.faux.horloge = T0 + 7_200_000
        for numero, poste in (("10", "CEO"), ("11", "Directeur financier"), ("12", "DRH")):
            self.faux.contacts[numero]["properties"]["jobtitle"] = poste
            self.faux.contacts[numero]["ts"] = self.faux.horloge
        self.faux.contacts["13"]["properties"]["phone"] = "0102030405"
        self.faux.contacts["13"]["ts"] = self.faux.horloge
        avant = len(self.faux.appels)
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0)
        premiere = [a for a in self.faux.appels[avant:] if "/search" in a[1]][0]
        self.assertEqual(premiere[2]["filterGroups"][0]["filters"][0]["value"], str(T0 + 3_600_000 - 120_000))
        self.assertEqual((bilan["fiches_lues"], bilan["fiches_a_ecrire"], bilan["fiches_inchangees"]), (4, 3, 1))
        lot = [a for a in self.faux.appels[avant:] if "/batch/update" in a[1]]
        self.assertEqual({e["id"] for l in lot for e in l[2]["inputs"]}, {"10", "11", "12"})
        self.assertEqual(self.faux.contacts["10"]["properties"]["leadlint_segment"], "Founders")

    def test_dry_run_ne_modifie_rien_et_ne_garde_pas_l_etat(self):
        self.peupler(130)
        code, sortie, _ = self.lancer("sync", "--personas", "--etat", str(self.etat), "--dry-run")
        self.assertEqual(code, 0, sortie)
        self.assertIn("ESSAI À BLANC", sortie)
        self.assertEqual(self.faux.appels_a("/batch/update"), [])
        self.assertFalse(self.etat.exists())
        self.assertTrue(all("leadlint_segment" not in c["properties"] for c in self.faux.contacts.values()))
        code, bilan = self.sync("--personas", "--dry-run")
        self.assertEqual((bilan["fiches_a_ecrire"], bilan["fiches_ecrites"], bilan["etat_enregistre"]), (130, 0, False))
        self.assertEqual(len(bilan["apercu"]), 10)
        # le vrai passage qui suit écrit bien tout : l'essai n'a rien consommé
        code, bilan = self.sync("--personas")
        self.assertEqual((bilan["fiches_ecrites"], bilan["etat_enregistre"]), (130, True))

    def test_l_etat_n_est_ecrit_qu_apres_un_passage_reussi(self):
        self.peupler(30)
        self.faux.refus_ecriture = True
        code, _, erreurs = self.lancer("sync", "--personas", "--etat", str(self.etat))
        self.assertEqual(code, 1)
        self.assertIn("leadlint hubspot setup", erreurs)  # l'erreur dit quoi faire
        self.assertFalse(self.etat.exists())
        self.faux.refus_ecriture = False
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0)
        self.assertTrue(self.etat.exists())

    def test_un_changement_de_regles_refait_un_passage_complet(self):
        self.peupler(20)
        self.sync("--personas")
        self.faux.horloge = T0 + 1000
        avant = len(self.faux.appels)
        code, bilan = self.sync("--personas", "--multi")
        recherche = [a for a in self.faux.appels[avant:] if "/search" in a[1]][0]
        self.assertEqual(recherche[2]["filterGroups"][0]["filters"][0]["value"], "0")
        self.assertIn("passage complet", bilan["depuis"])

    def test_champ_relie_une_autre_propriete(self):
        self.faux.ajouter(1, "", T0 - 1000, intitule_perso="CEO")
        code, bilan = self.sync("--personas", "--champ", "poste=intitule_perso")
        self.assertEqual(code, 0, bilan)
        self.assertEqual(self.faux.contacts["1"]["properties"]["leadlint_segment"], "Founders")
        code, _, erreurs = self.lancer("sync", "--personas", "--champ", "poste=Mauvais Nom")
        self.assertEqual(code, 1)
        self.assertIn("--champ", erreurs)


class Limites(BaseHubSpot):
    def test_un_429_puis_un_nouvel_essai_en_respectant_retry_after(self):
        self.peupler(120)
        self.faux.pannes.append(("/batch/update", 429, {"Retry-After": "7"}))
        self.faux.pannes.append(("/search", 503, {}))
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0, bilan)
        self.assertEqual(bilan["fiches_ecrites"], 120)
        self.assertEqual(self.sommeils, [1.0, 7.0])  # 503 : 1 s ; 429 avec Retry-After de 7 s : 7 s
        self.assertEqual(len(self.faux.appels_a("/batch/update")), 2 + 1)  # le premier lot refusé, puis les deux lots

    def test_l_attente_croit_sans_retry_after(self):
        self.peupler(5)
        self.faux.pannes += [("/search", 500, {}), ("/search", 502, {}), ("/search", 429, {})]
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0)
        self.assertEqual(self.sommeils, [1.0, 2.0, 4.0])

    def test_une_panne_qui_dure_s_arrete_proprement(self):
        self.peupler(5)
        self.faux.pannes_permanentes["/batch/update"] = 429
        code, _, erreurs = self.lancer("sync", "--personas", "--etat", str(self.etat))
        self.assertEqual(code, 1)
        self.assertIn("429", erreurs)
        self.assertEqual(len(self.faux.appels_a("/batch/update")), m.ESSAIS_HUBSPOT)
        self.assertFalse(self.etat.exists())

    def test_un_retry_after_trop_long_n_est_pas_attendu(self):
        self.peupler(5)
        self.faux.pannes.append(("/search", 429, {"Retry-After": "86400"}))
        code, _, erreurs = self.lancer("sync", "--personas", "--etat", str(self.etat))
        self.assertEqual(code, 1)
        self.assertIn("86400", erreurs)
        self.assertEqual(self.sommeils, [])

    def test_le_faux_serveur_refuse_bien_la_page_au_dela_de_10000(self):
        """Témoin : sans lui, le test du plafond serait vert pour rien."""
        self.peupler(10_100)
        client = m.ClientHubSpot(self.faux.url, JETON)
        corps = {"filterGroups": [{"filters": [{"propertyName": "lastmodifieddate", "operator": "GTE", "value": "0"}]}],
                 "sorts": [{"propertyName": "lastmodifieddate", "direction": "ASCENDING"}], "properties": [], "limit": 100, "after": "10000"}
        with self.assertRaises(m.ErreurHubSpot) as erreur:
            client.appeler("POST", "/crm/v3/objects/contacts/search", corps)
        self.assertEqual(erreur.exception.statut, 400)
        self.assertEqual(self.faux.violations_plafond, 1)

    def test_au_dela_de_10000_la_recherche_repart_de_la_derniere_date_vue(self):
        # 10 250 contacts ; des contacts 9 990 à 10 010 partagent la même date : la reprise au plafond ne doit ni les sauter ni les relire.
        self.peupler(10_250)
        base = T0 - 10 * 86_400_000
        for i in range(9_990, 10_011):
            self.faux.contacts[str(i)]["ts"] = base + 9_990
        code, bilan = self.sync("--personas")
        self.assertEqual(code, 0, bilan)
        self.assertEqual(self.faux.violations_plafond, 0)
        self.assertEqual(bilan["redemarrages_au_plafond"], 1)
        self.assertEqual(bilan["fiches_lues"], 10_250)
        ecrits = [e["id"] for l in self.faux.appels_a("/batch/update") for e in l[2]["inputs"]]
        self.assertEqual(len(ecrits), 10_250)
        self.assertEqual(len(set(ecrits)), 10_250)
        self.assertTrue(all("leadlint_segment" in c["properties"] for c in self.faux.contacts.values()))
        recherches = self.faux.appels_a("/search")
        reprises = [r for r in recherches[1:] if "after" not in r[2]]
        self.assertEqual(len(reprises), 1)
        # la première recherche s'arrête au 10 000e résultat, au milieu du groupe de dates identiques : la reprise part de cette date
        self.assertEqual(reprises[0][2]["filterGroups"][0]["filters"][0]["value"], str(T0 - 10 * 86_400_000 + 9_990))

    def test_plus_de_10000_contacts_au_meme_instant_arrete_le_passage(self):
        self.peupler(10_100)
        for contact in self.faux.contacts.values():
            contact["ts"] = T0 - 86_400_000
        code, _, erreurs = self.lancer("sync", "--personas", "--etat", str(self.etat))
        self.assertEqual(code, 1)
        self.assertIn("même instant", erreurs)
        self.assertEqual(self.faux.appels_a("/batch/update"), [])
        self.assertFalse(self.etat.exists())


class Cle(BaseHubSpot):
    def test_la_cle_ne_s_affiche_ni_ne_s_ecrit_jamais(self):
        self.peupler(5)
        textes = []
        for arguments in (("setup",), ("sync", "--personas", "--etat", str(self.etat)), ("sync", "--personas", "--etat", str(self.etat), "--dry-run", "--json")):
            code, sortie, erreurs = self.lancer(*arguments)
            textes += [sortie, erreurs]
        self.faux.jeton_attendu = "autre-chose"  # 401 : le chemin d'erreur ne doit pas la citer non plus
        code, sortie, erreurs = self.lancer("sync", "--personas", "--etat", str(self.etat))
        self.assertEqual(code, 1)
        self.assertIn("401", erreurs)
        textes += [sortie, erreurs, self.etat.read_text(encoding="utf-8")]
        for texte in textes:
            self.assertNotIn(JETON, texte)
        for fichier in self.dossier.iterdir():
            self.assertNotIn(JETON, fichier.read_text(encoding="utf-8"))

    def test_sans_cle_rien_ne_part(self):
        with mock.patch.dict(os.environ, {"HUBSPOT_TOKEN": ""}):
            code, _, erreurs = self.lancer("setup")
        self.assertEqual(code, 1)
        self.assertIn("HUBSPOT_TOKEN", erreurs)
        self.assertEqual(self.faux.appels, [])

    def test_une_adresse_http_non_locale_est_refusee_avant_tout_envoi(self):
        sortie, erreurs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
            code = m.main(["hubspot", "setup", "--api-url", "http://exemple.invalid"])
        self.assertEqual(code, 1)
        self.assertIn("https", erreurs.getvalue())
        self.assertEqual(m._verifier_url_hubspot("https://api.hubapi.com/"), "https://api.hubapi.com")


class SyncMission(BaseHubSpot):
    def mission(self, scenario: str = "origine-cible") -> tuple[Path, Path]:
        entree = json.loads((PARITE / f"b2b-{scenario}.entree.json").read_text(encoding="utf-8"))
        mission, exclusions = self.dossier / "mission.json", self.dossier / "exclusions.csv"
        mission.write_text(json.dumps({"client": "Client fictif", "mission": "Cible test", "configuration": entree["configuration"]}, ensure_ascii=False), encoding="utf-8")
        with exclusions.open("w", newline="", encoding="utf-8") as flux:
            ecrivain = csv.DictWriter(flux, fieldnames=list(entree["blacklist"][0]))
            ecrivain.writeheader()
            ecrivain.writerows(entree["blacklist"])
        return mission, exclusions

    def contacts(self) -> None:
        bon = {"firstname": "Camille", "lastname": "Dupont", "company": "Nuage 1", "country": "France", "hs_linkedin_url": "https://www.linkedin.com/in/dupont-1", "industry": "ESN"}
        self.faux.ajouter(1, "Co-founder", T0 - 5000, **bon)
        self.faux.ajouter(2, "Co-founder", T0 - 4000, **{**bon, "lastname": "Martin", "country": "Allemagne", "hs_linkedin_url": "https://www.linkedin.com/in/martin-2"})
        self.faux.ajouter(3, "Gérant", T0 - 3000, **{**bon, "lastname": "Durand", "company": "Orbite S.A.S.", "hs_linkedin_url": "https://www.linkedin.com/in/durand-3"})
        self.faux.ajouter(4, "", T0 - 2000, **{**bon, "lastname": "Petit", "hs_linkedin_url": "https://www.linkedin.com/in/petit-4"})
        self.faux.ajouter(5, "Co-founder", T0 - 1000, **{**bon, "lastname": "Bis"})  # même profil LinkedIn que la fiche 1

    def test_la_mission_rend_les_verdicts_de_check_sur_les_fiches(self):
        mission, exclusions = self.mission()
        self.contacts()
        code, bilan = self.sync("--mission", str(mission), "--exclusions", str(exclusions))
        self.assertEqual(code, 0, bilan)
        propres = {i: c["properties"] for i, c in self.faux.contacts.items()}
        self.assertEqual([propres[str(i)]["leadlint_verdict"] for i in range(1, 6)], ["Conforme", "Rejete", "Rejete", "A verifier", "Conforme"])
        self.assertEqual(bilan["verdicts"], {"Conforme": 2, "À vérifier": 1, "Rejeté": 2})
        self.assertEqual(propres["1"]["leadlint_segment"], "Client fictif / Cible test")
        self.assertEqual(propres["2"].get("leadlint_segment", ""), "")  # vide : rien à écrire
        self.assertIn("Pays", propres["2"]["leadlint_raison"])
        self.assertIn("Liste d'exclusion", propres["3"]["leadlint_raison"])
        self.assertEqual(propres["1"].get("leadlint_raison", ""), "")
        self.assertEqual(propres["1"]["leadlint_niveau"], "Founder")
        # les doublons ne se jugent pas sur une page : la fiche 5 reste conforme, et le passage le dit
        self.assertTrue(any("doublons" in note for note in bilan["notes"]))
        # le même verdict qu'une passe `check` sur les mêmes lignes
        lignes = [{"Nom complet": "Camille Dupont", "Entreprise": "Nuage 1", "Poste": "Co-founder", "LinkedIn profil": "https://www.linkedin.com/in/dupont-1", "Pays": "France", "Activité": "ESN"}]
        configuration = m.migrer_configuration_b2b(json.loads((PARITE / "b2b-origine-cible.entree.json").read_text(encoding="utf-8"))["configuration"])
        verdict = m.valider_export_b2b(lignes, [], {**configuration, "mappingCandidats": {"nomComplet": "Nom complet", "entreprise": "Entreprise", "poste": "Poste", "linkedinProfil": "LinkedIn profil", "pays": "Pays", "activite": "Activité"}})[0]["statut"]
        self.assertEqual(verdict, "Conforme")
        # rejoué : plus rien à écrire
        code, bilan = self.sync("--mission", str(mission), "--exclusions", str(exclusions), "--tout")
        self.assertEqual((bilan["fiches_lues"], bilan["fiches_a_ecrire"]), (5, 0))

    def test_une_mission_non_validee_bloque_avant_toute_lecture(self):
        mission, exclusions = self.mission("origine-cible-non-validee")
        self.contacts()
        code, sortie, _ = self.lancer("sync", "--mission", str(mission), "--exclusions", str(exclusions))
        self.assertEqual(code, 2)
        self.assertIn("BLOQUÉ", sortie)
        self.assertEqual(self.faux.appels, [])

    def test_une_mission_modifiee_apres_validation_bloque(self):
        mission, exclusions = self.mission("origine-cible-modifiee")
        code, sortie, _ = self.lancer("sync", "--mission", str(mission), "--exclusions", str(exclusions))
        self.assertEqual(code, 2, sortie)
        self.assertEqual(self.faux.appels, [])

    def test_une_mission_d_export_de_fournisseur_ne_passe_pas_ici(self):
        mission, exclusions = self.mission("esn-validee")
        code, sortie, _ = self.lancer("sync", "--mission", str(mission), "--exclusions", str(exclusions))
        self.assertEqual(code, 2)
        self.assertIn("origine « cible »", sortie)
        self.assertEqual(self.faux.appels, [])

    def test_sans_la_liste_d_exclusion_la_mission_bloque(self):
        mission, _ = self.mission()
        code, sortie, _ = self.lancer("sync", "--mission", str(mission))
        self.assertEqual(code, 2)
        self.assertEqual(self.faux.appels, [])

    def test_une_matrice_de_personas_donnee_comme_mission_se_traite_en_personas(self):
        self.peupler(10)
        matrice = RACINE / "skills" / "leadlint" / "matrices" / "matrice-generique.json"
        code, bilan = self.sync("--mission", str(matrice))
        self.assertEqual((code, bilan["mode"], bilan["fiches_ecrites"]), (0, "personas", 10))


if __name__ == "__main__":
    unittest.main(verbosity=1)
