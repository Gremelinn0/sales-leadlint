#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leadlint-setup: turn a few answers into your own cleaning script and a page that shows your rules.

Three commands:

  create    reads the answers (reponses.json) and writes a folder: mission.json, matrice.json,
            leadlint.py, moulinette.py (your script), regles.html (your rules in plain French).
  validate  records the HUMAN validation of the rules (date and signature). Run it only when the
            person has explicitly agreed to their rules, after reading regles.html.
  page      writes regles.html again from mission.json (and the last pass, if any).

Examples:

  leadlint-setup create reponses.json -o ma-moulinette --exclusions clients.csv
  leadlint-setup validate ma-moulinette
  python ma-moulinette/moulinette.py export.csv

Nothing leaves the machine: no account, no network call. The folder it writes is self-sufficient
(it holds its own copy of the engine, leadlint.py), so it keeps working wherever you move it.
Messages and files written for the person are in French.

Exit codes: 0 done · 1 unreadable input or invalid answers · 2 (the script, not this tool) preflight blocked.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import html
import io
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ICI = Path(__file__).resolve().parent

try:
    import leadlint
except ImportError:  # lancé de façon isolée : le moteur est à côté de ce fichier
    sys.path.insert(0, str(ICI))
    import leadlint

ErreurEntree = leadlint.ErreurEntree

VERSION = leadlint.VERSION
DEPOT = "https://github.com/Gremelinn0/sales-leadlint"
EXTENSIONS_TABLEAU = {".csv", ".xlsx", ".xlsm", ".txt", ".tsv"}
CHAMPS_FINAUX = ("nomComplet", "entreprise", "poste", "linkedinProfil", "emailProfessionnel", "mobile")
CHAMPS_OBLIGATOIRES_DEFAUT = ["nomComplet", "entreprise", "poste"]
TAILLE_SANS_LIMITE = 1_000_000_000
PHRASE_SUITE = "Ouvrez regles.html, relisez vos règles, puis validez."

# Le nom que la personne lit pour chaque champ du moteur (colonnes d'un export).
LIBELLES_CHAMPS = {
    "nomComplet": "Nom complet", "entreprise": "Entreprise", "poste": "Poste", "linkedinProfil": "LinkedIn profil",
    "linkedinEntreprise": "LinkedIn entreprise", "categorieLinkedIn": "Catégorie LinkedIn", "employesVisibles": "Employés visibles",
    "pays": "Pays", "activite": "Activité", "tagsFournisseur": "Tags du fournisseur",
    "trancheEffectifFournisseur": "Tranche d'effectif du fournisseur", "qualificationActivite": "Qualification de l'activité",
    "siteEntreprise": "Site de l'entreprise", "siren": "SIREN", "emailProfessionnel": "Email professionnel",
    "sourceEmail": "Provenance de l'email", "mobile": "Mobile", "sourceMobile": "Provenance du mobile",
    "effectifLegal": "Effectif légal", "chiffreAffaires": "Chiffre d'affaires", "codeNaf": "Code NAF",
    "statutJuridique": "Statut juridique", "technologies": "Technologies", "departement": "Département",
    "niveauHierarchique": "Niveau hiérarchique", "anciennetePoste": "Ancienneté dans le poste",
}

CLES_REPONSES = {
    "nom", "pour", "pays", "cibles", "postes_exclus", "secteurs", "taille", "email_pro_obligatoire",
    "mobile_obligatoire", "liste_exclusion", "champs_obligatoires", "colonnes",
}
CLES_SECTEURS = {"mots_cles", "mots_exclus", "codes_naf", "codes_naf_exclus"}


# --------------------------------------------------------------------------- les réponses


def _liste_de_mots(valeur: Any, nom: str) -> list:
    """Une liste de mots non vides, sans doublon. Un texte seul devient une liste d'un mot."""
    if valeur is None:
        return []
    if isinstance(valeur, str):
        valeur = [valeur]
    if not isinstance(valeur, list):
        raise ErreurEntree(f"« {nom} » doit être une liste de mots.")
    sortie: list = []
    for item in valeur:
        if not isinstance(item, str):
            raise ErreurEntree(f"« {nom} » ne peut contenir que du texte (reçu : {item!r}).")
        mot = item.strip()
        if not mot:
            continue
        if not leadlint.normaliser_texte(mot):
            raise ErreurEntree(f"« {mot} » (dans « {nom} ») ne contient ni lettre ni chiffre.")
        if mot not in sortie:
            sortie.append(mot)
    return sortie


def _booleen(valeur: Any, nom: str) -> bool:
    if valeur is None:
        return False
    if isinstance(valeur, bool):
        return valeur
    raise ErreurEntree(f"« {nom} » doit valoir true ou false.")


def _nombre(valeur: Any, nom: str) -> Any:
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not math.isfinite(valeur):
        raise ErreurEntree(f"« {nom} » doit être un nombre.")
    return valeur


def _taille(valeur: Any) -> Any:
    if valeur is None:
        return None
    if not isinstance(valeur, dict) or not valeur or not set(valeur) <= {"min", "max"}:
        raise ErreurEntree("« taille » doit contenir « min » et/ou « max » (des nombres d'employés).")
    mini = _nombre(valeur["min"], "taille.min") if valeur.get("min") is not None else None
    maxi = _nombre(valeur["max"], "taille.max") if valeur.get("max") is not None else None
    if mini is None and maxi is None:
        raise ErreurEntree("« taille » doit contenir « min » et/ou « max » (des nombres d'employés).")
    if mini is not None and maxi is not None and mini > maxi:
        raise ErreurEntree(f"taille.min ({mini}) est plus grand que taille.max ({maxi}).")
    return {"min": 0 if mini is None else mini, "max": TAILLE_SANS_LIMITE if maxi is None else maxi}


def verifier_reponses(brut: Any) -> dict:
    """Contrôle les réponses et les rend normalisées. Une erreur dit en une phrase ce qui ne va pas."""
    if not isinstance(brut, dict):
        raise ErreurEntree("Les réponses doivent être un objet JSON (voir skills/leadlint-setup/exemples/reponses-exemple.json).")
    inconnues = sorted(cle for cle in brut if cle not in CLES_REPONSES and not str(cle).startswith("_"))
    if inconnues:
        raise ErreurEntree(f"Clé inconnue dans les réponses : « {inconnues[0]} ». Clés connues : {', '.join(sorted(CLES_REPONSES))}.")
    cibles_brutes = brut.get("cibles")
    if not isinstance(cibles_brutes, list) or not cibles_brutes:
        raise ErreurEntree("Il faut au moins une cible dans « cibles » (un nom et ses intitulés de poste).")
    cibles: list = []
    for numero, cible in enumerate(cibles_brutes, start=1):
        if not isinstance(cible, dict):
            raise ErreurEntree(f"La cible n°{numero} doit avoir un nom et des intitulés de poste.")
        nom = cible.get("nom")
        if not isinstance(nom, str) or not nom.strip():
            raise ErreurEntree(f"La cible n°{numero} n'a pas de nom.")
        nom = nom.strip()
        if set(cible) - {"nom", "intitules"}:
            raise ErreurEntree(f"La cible « {nom} » ne connaît que « nom » et « intitules ».")
        if any(c["nom"].casefold() == nom.casefold() for c in cibles):
            raise ErreurEntree(f"Deux cibles portent le nom « {nom} » : donnez-leur des noms différents.")
        intitules = _liste_de_mots(cible.get("intitules"), f"intitules de la cible « {nom} »")
        if not intitules:
            raise ErreurEntree(f"La cible « {nom} » n'a aucun intitulé de poste.")
        cibles.append({"nom": nom, "intitules": intitules})
    secteurs_bruts = brut.get("secteurs")
    if secteurs_bruts is None:
        secteurs_bruts = {}
    if not isinstance(secteurs_bruts, dict) or set(secteurs_bruts) - CLES_SECTEURS:
        raise ErreurEntree(f"« secteurs » ne connaît que : {', '.join(sorted(CLES_SECTEURS))}.")
    colonnes = brut.get("colonnes")
    if colonnes is None:
        colonnes = {}
    if not isinstance(colonnes, dict):
        raise ErreurEntree("« colonnes » doit associer un champ à l'entête de votre export (par exemple {\"poste\": \"Title\"}).")
    for champ, entete in colonnes.items():
        if champ not in leadlint.ENTETES_CANDIDATS:
            raise ErreurEntree(f"« {champ} » (dans « colonnes ») n'est pas un champ connu. Champs connus : {', '.join(leadlint.ENTETES_CANDIDATS)}.")
        if not isinstance(entete, str) or not entete.strip():
            raise ErreurEntree(f"L'entête donné pour « {champ} » (dans « colonnes ») doit être un texte.")
    obligatoires = brut.get("champs_obligatoires")
    if obligatoires is None:
        obligatoires = list(CHAMPS_OBLIGATOIRES_DEFAUT)
    obligatoires = _liste_de_mots(obligatoires, "champs_obligatoires")
    for champ in obligatoires:
        if champ not in leadlint.ENTETES_CANDIDATS:
            raise ErreurEntree(f"« {champ} » (dans « champs_obligatoires ») n'est pas un champ connu. Champs connus : {', '.join(leadlint.ENTETES_CANDIDATS)}.")
    for cle in ("nom", "pour"):
        if brut.get(cle) is not None and not isinstance(brut[cle], str):
            raise ErreurEntree(f"« {cle} » doit être un texte.")
    return {
        "nom": " ".join((brut.get("nom") or "").split()) or "Ma prospection",
        "pour": " ".join((brut.get("pour") or "").split()),
        "pays": _liste_de_mots(brut.get("pays"), "pays"),
        "cibles": cibles,
        "postes_exclus": _liste_de_mots(brut.get("postes_exclus"), "postes_exclus"),
        "secteurs": {cle: _liste_de_mots(secteurs_bruts.get(cle), f"secteurs.{cle}") for cle in ("mots_cles", "mots_exclus", "codes_naf", "codes_naf_exclus")},
        "taille": _taille(brut.get("taille")),
        "email_pro_obligatoire": _booleen(brut.get("email_pro_obligatoire"), "email_pro_obligatoire"),
        "mobile_obligatoire": _booleen(brut.get("mobile_obligatoire"), "mobile_obligatoire"),
        "liste_exclusion": _booleen(brut.get("liste_exclusion"), "liste_exclusion"),
        "champs_obligatoires": obligatoires,
        "colonnes": {champ: entete.strip() for champ, entete in colonnes.items()},
    }


def lire_reponses(chemin: Path) -> tuple:
    """(réponses normalisées, octets du fichier). Les octets servent à la signature de la cible."""
    try:
        octets = chemin.read_bytes()
    except OSError as erreur:
        raise ErreurEntree(f"Réponses introuvables ({chemin}) : {erreur.strerror or erreur}") from erreur
    try:
        brut = json.loads(octets.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as erreur:
        raise ErreurEntree(f"Réponses illisibles ({chemin}) : {erreur}") from erreur
    return verifier_reponses(brut), octets


# --------------------------------------------------------------------------- la mission et la matrice


def _regle(critere: str, operateur: str, valeur: Any, si_absent: str = "rejeter", mot_entier: bool = False) -> dict:
    regle = {
        "id": f"r-{critere}-{operateur}", "critereId": critere, "operateur": operateur, "valeur": valeur,
        "importance": "bloquant", "siAbsent": si_absent, "active": True,
    }
    if mot_entier:
        regle["motEntier"] = True
    return regle


def construire(reponses: dict, empreinte_cible: str) -> tuple:
    """(setup de la mission, matrice des cibles). La mission n'est PAS validée : c'est un humain qui la valide."""
    regles: list = []
    secteurs = reponses["secteurs"]
    if reponses["pays"]:
        regles.append(_regle("pays", "est_dans", reponses["pays"], "ignorer"))
    intitules: list = []
    vus: set = set()
    for cible in reponses["cibles"]:
        for intitule in cible["intitules"]:
            cle = leadlint.normaliser_texte(intitule)
            if cle not in vus:
                vus.add(cle)
                intitules.append(intitule)
    regles.append(_regle("poste", "contient_un", intitules, "rejeter", True))
    if reponses["postes_exclus"]:
        regles.append(_regle("poste", "ne_contient_aucun", reponses["postes_exclus"], "ignorer", True))
    for cle, critere, operateur in (
        ("mots_cles", "activite", "contient_un"), ("mots_exclus", "activite", "ne_contient_aucun"),
        ("codes_naf", "code_naf", "est_dans"), ("codes_naf_exclus", "code_naf", "n_est_pas_dans"),
    ):
        if secteurs[cle]:
            regles.append(_regle(critere, operateur, secteurs[cle], "ignorer"))
    if reponses["taille"]:
        regles.append(_regle("employes_visibles", "entre", dict(reponses["taille"]), "ignorer"))
    if reponses["email_pro_obligatoire"]:
        regles.append(_regle("email_pro", "email_professionnel", True))
    if reponses["mobile_obligatoire"]:
        regles.append(_regle("mobile_fr", "mobile_francais", True))
    if reponses["liste_exclusion"]:
        regles.append(_regle("blacklist", "pas_blackliste", True))
    regles.append(_regle("doublon", "unique", True))
    configuration = {
        "mode": "revalidation_b2b", "schemaVersion": 2, "regles": regles,
        "champsObligatoires": list(reponses["champs_obligatoires"]),
        "colonnesFinales": [dict(c) for c in leadlint.COLONNES_B2B_STANDARD if c["champ"] in CHAMPS_FINAUX],
        "mappingCandidats": dict(reponses["colonnes"]), "mappingBlacklist": {},
        "blacklist": {"obligatoire": bool(reponses["liste_exclusion"]), "nom": "Liste d'exclusion", "typeSource": "fichier"},
        "strategie": {"lignes": [], "limites": ""},
        "origine": {"type": "cible", "empreinteCible": empreinte_cible, "empreinteRegles": "", "valideeLe": ""},
    }
    setup = {
        "client": reponses["pour"], "mission": reponses["nom"],
        "description": "Règles de ciblage écrites par leadlint-setup à partir de reponses.json.",
        "mode": "revalidation_b2b", "configuration": configuration,
    }
    matrice = {
        "axes": [{"id": "cible", "libelle": "Cible", "regles": [
            {"categorie": cible["nom"], "priorite": rang, "motsCles": list(cible["intitules"])}
            for rang, cible in enumerate(reponses["cibles"], start=1)
        ]}],
        "derives": [],
    }
    return setup, matrice


def _controles_hors_export(configuration: dict, volume_liste: int, signature_liste: Any) -> list:
    """Les contrôles avant lancement, comme si l'export avait une colonne pour chaque champ obligatoire."""
    simulee = copy.deepcopy(configuration)
    correspondance = dict(simulee["mappingCandidats"])
    for champ in simulee["champsObligatoires"]:
        correspondance.setdefault(champ, leadlint.ENTETES_CANDIDATS[champ][0])
    simulee["mappingCandidats"] = correspondance
    return leadlint.controler_prevol(simulee, 1, volume_liste, signature_liste)


# --------------------------------------------------------------------------- fichiers


def _ecrire_texte(chemin: Path, texte: str) -> None:
    temporaire = chemin.with_name(chemin.name + ".tmp")
    with open(temporaire, "w", encoding="utf-8", newline="\n") as fichier:
        fichier.write(texte)
    os.replace(temporaire, chemin)


def _ecrire_octets(chemin: Path, octets: bytes) -> None:
    temporaire = chemin.with_name(chemin.name + ".tmp")
    temporaire.write_bytes(octets)
    os.replace(temporaire, chemin)


def _ecrire_json(chemin: Path, donnees: Any) -> None:
    _ecrire_texte(chemin, json.dumps(donnees, ensure_ascii=False, indent=2) + "\n")


def _lire_json(chemin: Path) -> Any:
    try:
        return json.loads(chemin.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _copier(source: Path, destination: Path) -> None:
    try:
        meme = source.resolve() == destination.resolve()
    except OSError:
        meme = False
    if not meme:
        _ecrire_octets(destination, source.read_bytes())


def _trouver_exclusions(dossier: Path) -> Any:
    """La liste d'exclusion posée dans le dossier (exclusions.csv, .xlsx…) ; la plus récente s'il y en a plusieurs."""
    candidats = [p for p in dossier.glob("exclusions.*") if p.is_file() and p.suffix.lower() in EXTENSIONS_TABLEAU]
    return max(candidats, key=lambda p: p.stat().st_mtime) if candidats else None


def _mission_validee(chemin: Path) -> bool:
    brut = _lire_json(chemin)
    if not isinstance(brut, dict):
        return False
    configuration = brut.get("configuration") if isinstance(brut.get("configuration"), dict) else brut
    origine = configuration.get("origine")
    date = origine.get("valideeLe") if isinstance(origine, dict) else None
    return isinstance(date, str) and date.strip() != ""


def _liste_exclusion_requise(configuration: dict) -> bool:
    return leadlint.vrai(configuration["blacklist"].get("obligatoire")) or any(
        r.get("operateur") == "pas_blackliste" and leadlint.vrai(r.get("active")) for r in configuration["regles"]
    )


def _lire_liste_exclusion(chemin: Path, memorise: dict) -> tuple:
    """(correspondance des colonnes, signature, nombre de lignes) de la liste d'exclusion."""
    entetes, lignes = leadlint.lire_tableau(chemin)
    if not entetes:
        raise ErreurEntree(f"La liste d'exclusion {chemin.name} est vide.")
    correspondance = leadlint.completer_mapping(memorise, entetes, leadlint.ENTETES_BLACKLIST)
    if not any(leadlint.vrai(v) for v in correspondance.values()):
        raise ErreurEntree(
            f"Je ne reconnais aucune colonne de la liste d'exclusion {chemin.name} (« Entreprise », « Site », « LinkedIn entreprise » ou « SIREN » attendues). "
            f"Entêtes lues : {', '.join(entetes)}."
        )
    objets = leadlint.vers_objets(entetes, lignes)
    if not objets:
        raise ErreurEntree(f"La liste d'exclusion {chemin.name} ne contient aucune ligne.")
    return correspondance, leadlint.empreinte_blacklist(objets, correspondance), len(objets)


def _poser_liste_exclusion(dossier: Path, fichier: Path) -> Path:
    """Garde une copie de la liste dans le dossier (exclusions.csv) pour que le script la retrouve."""
    destination = dossier / f"exclusions{fichier.suffix.lower()}"
    _copier(fichier, destination)
    return destination


# --------------------------------------------------------------------------- create


GABARIT_MOULINETTE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Moulinette : __NOM__
# Cibles : __CIBLES__
# Créée le __DATE__ · généré par leadlint-setup, ne dépend d'aucun compte et n'appelle aucun réseau.
#
# Usage : python moulinette.py <export.csv|xlsx> [--exclusions <fichier>]
#
# Ce script lance le moteur qui est dans ce dossier (leadlint.py, leadlint_setup.py) avec vos règles
# (mission.json, matrice.json). Il ne livre rien tant que vous n'avez pas relu et validé vos règles
# (regles.html). Pour changer une règle, ne modifiez pas mission.json : demandez à votre agent
# « change mes règles LeadLint », puis relisez et revalidez.
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))

from leadlint_setup import lancer_moulinette  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(lancer_moulinette(ICI, sys.argv[1:]))
'''


def _ligne(texte: str) -> str:
    """Une seule ligne, pour un commentaire."""
    return " ".join(texte.split())


def texte_moulinette(reponses: dict, date: str) -> str:
    cibles = ", ".join(c["nom"] for c in reponses["cibles"])
    return (GABARIT_MOULINETTE.replace("__NOM__", _ligne(reponses["nom"])).replace("__CIBLES__", _ligne(cibles)).replace("__DATE__", date))


def creer(chemin_reponses: Path, dossier: Path, exclusions: Any = None, force: bool = False) -> dict:
    reponses, octets = lire_reponses(chemin_reponses)
    if exclusions is not None:
        _lire_liste_exclusion(exclusions, {})  # la liste se lit et ses colonnes se reconnaissent avant d'écrire quoi que ce soit
    mission = dossier / "mission.json"
    if mission.exists() and not force and _mission_validee(mission):
        raise ErreurEntree(
            f"{mission} est déjà validée. Pour repartir de vos réponses et refaire valider, ajoutez --force : "
            "la validation précédente sera effacée."
        )
    setup, matrice = construire(reponses, hashlib.sha256(octets).hexdigest())
    dossier.mkdir(parents=True, exist_ok=True)
    _ecrire_json(mission, setup)
    # Le va-et-vient : la mission écrite se relit par le moteur et ses règles passeraient le contrôle avant lancement.
    _, configuration = leadlint.charger_mission(mission)
    controle = next(c for c in _controles_hors_export(configuration, 1, None) if c["id"] == "configuration")
    if configuration["mode"] != "revalidation_b2b" or not controle["valide"] or leadlint.cible_validee(configuration):
        raise ErreurEntree("Les règles écrites ne se relisent pas comme prévu : mission.json n'est pas utilisable. Ce n'est pas votre faute, signalez-le.")
    _ecrire_json(dossier / "matrice.json", matrice)
    _copier(Path(leadlint.__file__), dossier / "leadlint.py")
    _copier(Path(__file__), dossier / "leadlint_setup.py")
    _ecrire_texte(dossier / "moulinette.py", texte_moulinette(reponses, dt.date.today().isoformat()))
    _copier(chemin_reponses, dossier / "reponses.json")
    fichiers = ["mission.json", "matrice.json", "leadlint.py", "leadlint_setup.py", "moulinette.py", "regles.html", "reponses.json"]
    if exclusions is not None:
        fichiers.append(_poser_liste_exclusion(dossier, exclusions).name)
    ecrire_page(dossier)
    requise = reponses["liste_exclusion"]
    fournie = exclusions is not None or _trouver_exclusions(dossier) is not None
    return {
        "outil": "leadlint-setup", "version": VERSION, "commande": "create", "dossier": str(dossier.resolve()),
        "mission": reponses["nom"], "fichiers": fichiers, "validee": False,
        "liste_exclusion_requise": requise, "liste_exclusion_fournie": fournie,
        "page": str((dossier / "regles.html").resolve()), "suite": PHRASE_SUITE,
    }


# --------------------------------------------------------------------------- validate


def _date_iso(texte: Any) -> str:
    if texte is None:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(str(texte)).isoformat()
    except ValueError as erreur:
        raise ErreurEntree(f"Date illisible : {texte!r} (attendu : AAAA-MM-JJ).") from erreur


def valider(dossier: Path, exclusions: Any = None, date: Any = None) -> dict:
    mission = dossier / "mission.json"
    if not mission.is_file():
        raise ErreurEntree(f"mission.json est introuvable dans {dossier} : lancez d'abord « leadlint-setup create ».")
    jour = _date_iso(date)
    brut = _lire_json(mission)
    if not isinstance(brut, dict):
        raise ErreurEntree(f"{mission} est illisible.")
    _, configuration = leadlint.charger_mission(mission)
    if not leadlint.origine_est_cible(configuration):
        raise ErreurEntree("Cette mission n'a pas été écrite par leadlint-setup (pas d'origine « cible ») : je ne la valide pas.")
    brute = copy.deepcopy(brut)
    cible = brute["configuration"] if isinstance(brute.get("configuration"), dict) else brute
    liste: Any = None
    if _liste_exclusion_requise(configuration):
        fichier = exclusions if exclusions is not None else _trouver_exclusions(dossier)
        if fichier is None:
            raise ErreurEntree("Cette mission écarte les entreprises de votre liste d'exclusion : donnez le fichier avec --exclusions <fichier> (ou posez-le dans le dossier sous le nom exclusions.csv).")
        if exclusions is not None:
            fichier = _poser_liste_exclusion(dossier, fichier)
        correspondance, signature, volume = _lire_liste_exclusion(fichier, configuration["mappingBlacklist"])
        cible["mappingBlacklist"] = correspondance
        cible["blacklist"] = {**configuration["blacklist"], "volumeAttendu": volume, "empreinte": signature, "miseAJourLe": jour}
        liste = {"fichier": fichier.name, "volume": volume, "signature": signature}
    signature_regles = leadlint.empreinte_regles(configuration)
    cible["origine"] = {**cible["origine"], "empreinteRegles": signature_regles, "valideeLe": jour}
    temporaire = mission.with_name("mission.json.tmp")
    _ecrire_json(temporaire, brute)
    try:
        _, relue = leadlint.charger_mission(temporaire)
        problemes = [c["libelle"] for c in _controles_hors_export(relue, liste["volume"] if liste else 0, liste["signature"] if liste else None) if c["bloquant"] and not c["valide"]]
        if not leadlint.cible_validee(relue) or problemes:
            raise ErreurEntree("La validation écrite ne passerait pas le contrôle avant lancement" + (f" ({'; '.join(problemes)})" if problemes else "") + " : rien n'a été modifié.")
        os.replace(temporaire, mission)
    finally:
        if temporaire.exists():
            temporaire.unlink()
    ecrire_page(dossier)
    return {
        "outil": "leadlint-setup", "version": VERSION, "commande": "validate", "dossier": str(dossier.resolve()),
        "validee_le": jour, "signature_regles": signature_regles, "liste_exclusion": liste,
        "page": str((dossier / "regles.html").resolve()),
    }


# --------------------------------------------------------------------------- la page des règles

CSS = """
:root{--bg:#f4f6f8;--carte:#ffffff;--texte:#17202a;--doux:#5c6877;--trait:#e1e6ec;--accent:#0e7490;--accent-fond:#e2f3f7;
--ok:#14663a;--ok-fond:#dff5e6;--alerte:#8a4b08;--alerte-fond:#fdf0d5;--ko:#9b1c1c;--ko-fond:#fde4e4}
@media (prefers-color-scheme:dark){:root{--bg:#10151b;--carte:#171e26;--texte:#e8edf2;--doux:#9aa7b5;--trait:#273240;--accent:#4fb8d1;
--accent-fond:#16313a;--ok:#6fd49a;--ok-fond:#15301f;--alerte:#f0b866;--alerte-fond:#3a2c12;--ko:#f19a9a;--ko-fond:#3a1818}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--texte);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:36px 16px 56px}
h1{font-size:clamp(1.6rem,4vw,2.3rem);line-height:1.15;margin:.15em 0 .1em;letter-spacing:-.01em}
h2{font-size:1.15rem;margin:2.4rem 0 .9rem;letter-spacing:.01em}
h3{font-size:.95rem;margin:0 0 .7rem;color:var(--accent);text-transform:uppercase;letter-spacing:.06em}
p{margin:0}
.surtitre{color:var(--accent);font-weight:600;font-size:.85rem;text-transform:uppercase;letter-spacing:.08em}
.pour{color:var(--doux);font-size:1.05rem}
.sceau{display:inline-flex;align-items:center;gap:.6rem;margin-top:1.1rem;padding:.55rem .95rem;border-radius:999px;font-weight:600;font-size:.95rem}
.sceau .point{width:.7rem;height:.7rem;border-radius:50%;background:currentColor;flex:none}
.sceau-valide{background:var(--ok-fond);color:var(--ok)}
.sceau-attente{background:var(--alerte-fond);color:var(--alerte)}
.sceau-modifie{background:var(--ko-fond);color:var(--ko)}
.cibles{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.carte{background:var(--carte);border:1px solid var(--trait);border-radius:14px;padding:18px}
.carte-cible .rang{color:var(--doux);font-size:.8rem}
.carte-cible .nom{font-weight:700;font-size:1.05rem;margin:.1rem 0 .6rem}
.pastille{display:inline-block;margin:0 6px 6px 0;padding:.18rem .65rem;border-radius:999px;background:var(--accent-fond);color:var(--accent);font-size:.85rem}
.familles{display:grid;gap:12px;grid-template-columns:1fr}
@media (min-width:640px){.familles{grid-template-columns:1fr 1fr}}
@media (min-width:1040px){.familles{grid-template-columns:repeat(4,1fr)}}
.regle{padding:.85rem 0;border-top:1px solid var(--trait)}
.regle:first-of-type{border-top:0;padding-top:0}
.regle-titre{font-weight:700}
.regle-phrase{color:var(--texte);margin-top:.15rem}
.etat{display:inline-block;margin-left:.4rem;padding:0 .5rem;border-radius:999px;background:var(--alerte-fond);color:var(--alerte);font-size:.75rem;font-weight:600}
details{margin-top:.4rem;color:var(--doux);font-size:.88rem}
summary{cursor:pointer;color:var(--accent);font-weight:600}
dl{margin:.5rem 0 0;display:grid;grid-template-columns:auto 1fr;gap:.2rem .8rem}
dt{color:var(--doux)}
dd{margin:0;color:var(--texte);overflow-wrap:anywhere}
.doux{color:var(--doux);font-size:.9rem}
.vide{color:var(--doux);font-style:italic}
.chiffres{display:grid;gap:12px;grid-template-columns:repeat(3,1fr)}
.chiffre{border-radius:14px;padding:16px 10px;text-align:center}
.chiffre .nombre{display:block;font-size:clamp(2rem,7vw,3rem);font-weight:800;line-height:1.1}
.chiffre .legende{font-size:.9rem;font-weight:600}
.chiffre-ok{background:var(--ok-fond);color:var(--ok)}
.chiffre-verifier{background:var(--alerte-fond);color:var(--alerte)}
.chiffre-rejete{background:var(--ko-fond);color:var(--ko)}
.total{margin:.8rem 0 1rem;color:var(--doux)}
.deux{display:grid;gap:12px;grid-template-columns:1fr}
@media (min-width:760px){.deux{grid-template-columns:1fr 1fr}}
ul.compte{list-style:none;margin:0;padding:0}
ul.compte li{display:flex;justify-content:space-between;gap:1rem;padding:.4rem 0;border-top:1px solid var(--trait)}
ul.compte li:first-child{border-top:0}
ul.compte b{font-variant-numeric:tabular-nums}
.encart{margin-top:12px;padding:14px 16px;border-radius:12px;background:var(--alerte-fond);color:var(--alerte);font-weight:600}
.encart.doux{background:var(--accent-fond);color:var(--accent)}
ol.etapes{margin:0;padding-left:1.3rem}
ol.etapes li{margin:.35rem 0}
code{font:.9em ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;background:var(--accent-fond);color:var(--accent);padding:.1rem .4rem;border-radius:6px;overflow-wrap:anywhere}
footer{margin-top:3rem;padding-top:1.2rem;border-top:1px solid var(--trait);color:var(--doux);font-size:.88rem}
footer a{color:var(--accent)}
"""

FAMILLES = (
    ("entreprise", "Entreprise", "Aucune règle sur l'entreprise."),
    ("contact", "Contact", "Aucune règle sur le contact."),
    ("exclusion", "Exclusions", "Aucune liste d'exclusion."),
    ("qualite", "Qualité", "Aucune règle de qualité."),
)

HORS_REGLE = {"bloquant": "rejeté", "verification": "à vérifier", "informatif": "sans effet sur le verdict"}
DONNEE_ABSENTE = {"ignorer": "ignorée", "rejeter": "rejetée", "verifier": "à vérifier"}


def _nombre_affiche(valeur: Any) -> str:
    if isinstance(valeur, float) and valeur.is_integer():
        valeur = int(valeur)
    return str(valeur)


def _liste_texte(valeurs: Any) -> str:
    return ", ".join(str(v) for v in valeurs) if isinstance(valeurs, list) else str(valeurs)


def _phrase_taille(valeur: Any) -> str:
    mini, maxi = valeur.get("min", 0), valeur.get("max", TAILLE_SANS_LIMITE)
    if isinstance(maxi, (int, float)) and maxi >= TAILLE_SANS_LIMITE:
        return f"Au moins {_nombre_affiche(mini)} employés visibles sur LinkedIn."
    if mini == 0:
        return f"Au plus {_nombre_affiche(maxi)} employés visibles sur LinkedIn."
    return f"Entre {_nombre_affiche(mini)} et {_nombre_affiche(maxi)} employés visibles sur LinkedIn."


def _decrire(regle: dict) -> tuple:
    """(famille, libellé en gras, une phrase en français courant) d'une règle."""
    critere, operateur, valeur = regle.get("critereId"), regle.get("operateur"), regle.get("valeur")
    connu = {
        ("pays", "est_dans"): ("Pays", f"L'entreprise doit être dans l'un de ces pays : {_liste_texte(valeur)}."),
        ("activite", "contient_un"): ("Activité voulue", f"L'activité de l'entreprise doit contenir au moins un de ces mots : {_liste_texte(valeur)}."),
        ("activite", "ne_contient_aucun"): ("Activité écartée", f"Écarté si l'activité de l'entreprise contient : {_liste_texte(valeur)}."),
        ("code_naf", "est_dans"): ("Code NAF voulu", f"Le code NAF doit être l'un de ceux-ci : {_liste_texte(valeur)}."),
        ("code_naf", "n_est_pas_dans"): ("Code NAF écarté", f"Écarté si le code NAF est l'un de ceux-ci : {_liste_texte(valeur)}."),
        ("poste", "contient_un"): ("Poste voulu", f"Le poste doit contenir au moins un de ces mots : {_liste_texte(valeur)}."),
        ("poste", "ne_contient_aucun"): ("Postes écartés", f"Écarté si le poste contient : {_liste_texte(valeur)}."),
        ("email_pro", "email_professionnel"): ("Email professionnel", "Le contact doit avoir un email professionnel, pas une boîte personnelle (Gmail, Outlook, Orange…)."),
        ("mobile_fr", "mobile_francais"): ("Mobile", "Le contact doit avoir un mobile français (06 ou 07)."),
        ("blacklist", "pas_blackliste"): ("Liste d'exclusion", "Une entreprise de votre liste d'exclusion est écartée."),
        ("doublon", "unique"): ("Doublons", "Un même contact n'est gardé qu'une fois."),
    }
    if (critere, operateur) in connu:
        titre, phrase = connu[(critere, operateur)]
    elif critere == "employes_visibles" and operateur == "entre" and isinstance(valeur, dict):
        titre, phrase = "Taille", _phrase_taille(valeur)
    else:
        titre = leadlint.DEFINITIONS.get(critere, (None, str(critere), None))[1]
        verbe = leadlint.LIBELLES_OPERATEURS.get(operateur, str(operateur))
        brut = _valeurs_detail(valeur)
        phrase = f"{titre} {verbe}" + (f" : {brut}" if brut else "") + "."
    portee = leadlint.DEFINITIONS.get(critere, (None, None, "qualite"))[2]
    return (portee if portee in {f[0] for f in FAMILLES} else "qualite"), titre, phrase


def _valeurs_detail(valeur: Any) -> Any:
    if isinstance(valeur, list):
        return _liste_texte(valeur)
    if isinstance(valeur, dict) and ("min" in valeur or "max" in valeur):
        return f"de {_nombre_affiche(valeur.get('min', 0))} à {_nombre_affiche(valeur.get('max', ''))}"
    return None


def _detail(regle: dict) -> list:
    lignes = [("Test", leadlint.LIBELLES_OPERATEURS.get(regle.get("operateur"), str(regle.get("operateur"))))]
    valeurs = _valeurs_detail(regle.get("valeur"))
    if valeurs:
        lignes.append(("Valeurs", valeurs))
    lignes.append(("Hors règle", HORS_REGLE.get(regle.get("importance"), str(regle.get("importance")))))
    if regle.get("operateur") not in ("pas_blackliste", "unique"):
        lignes.append(("Donnée absente", DONNEE_ABSENTE.get(regle.get("siAbsent"), "rejetée")))
    if regle.get("motEntier") is True:
        lignes.append(("Mots", "mots entiers seulement"))
    return lignes


def _date_fr(iso: Any) -> str:
    try:
        return dt.date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(iso)


def _sans_cadratin(texte: Any) -> str:
    """Le moteur écrit ses causes avec un tiret cadratin : ce qu'on montre à la personne n'en a pas."""
    return str(texte).replace(" \u2014 ", " : ").replace("\u2014", ":")


def _e(texte: Any) -> str:
    return html.escape(str(texte))


def _bloc_regle(titre: str, phrase: str, detail: list, inactive: bool = False, visible: str = "") -> str:
    lignes = "".join(f"<dt>{_e(cle)}</dt><dd>{_e(valeur)}</dd>" for cle, valeur in detail)
    etat = '<span class="etat">désactivée</span>' if inactive else ""
    en_plus = f'<p class="regle-phrase doux">{_e(visible)}</p>' if visible else ""
    return (f'<div class="regle"><p class="regle-titre">{_e(titre)}{etat}</p><p class="regle-phrase">{_e(phrase)}</p>{en_plus}'
            f"<details><summary>Détail</summary><dl>{lignes}</dl></details></div>")


def _etat_liste(dossier: Path, configuration: dict) -> str:
    """« modifiee » si la liste d'exclusion posée dans le dossier n'est plus celle qui a été validée."""
    reference = configuration["blacklist"]
    if not _liste_exclusion_requise(configuration) or not leadlint.vrai(reference.get("empreinte")):
        return "inconnu"
    fichier = _trouver_exclusions(dossier)
    if fichier is None:
        return "absente"
    try:
        _, signature, volume = _lire_liste_exclusion(fichier, configuration["mappingBlacklist"])
    except (ErreurEntree, OSError):
        return "modifiee"
    return "ok" if signature == reference.get("empreinte") and volume == reference.get("volumeAttendu") else "modifiee"


def _sceau(configuration: dict, etat_liste: str) -> tuple:
    origine = configuration.get("origine")
    if not isinstance(origine, dict) or origine.get("type") != "cible":
        return "attente", "Pas encore validées : rien n'est livré tant que vous n'avez pas relu"
    date = origine.get("valideeLe")
    if not (isinstance(date, str) and date.strip()):
        return "attente", "Pas encore validées : rien n'est livré tant que vous n'avez pas relu"
    if not leadlint.cible_validee(configuration):
        return "modifie", "Règles modifiées depuis la validation : à revalider"
    if etat_liste == "modifiee":
        return "modifie", "Liste d'exclusion modifiée depuis la validation : à revalider"
    return "valide", f"Règles validées le {_date_fr(date)}"


def _colonnes_texte(colonnes: list) -> str:
    uniques: list = []
    for colonne in colonnes:
        if colonne not in uniques:
            uniques.append(colonne)
    noms = ", ".join(f"« {c} »" for c in uniques)
    return f"la colonne {noms}" if len(uniques) == 1 else f"les colonnes {noms}"


def _section_passage(passage: Any, configuration: dict) -> str:
    if not isinstance(passage, dict):
        return '<section><h2>Dernier passage</h2><div class="carte"><p class="vide">Aucun fichier passé pour l\'instant.</p></div></section>'

    def entier(cle: str) -> int:
        valeur = passage.get(cle)
        return valeur if isinstance(valeur, int) and not isinstance(valeur, bool) else 0

    tuiles = (
        ("chiffre-ok", entier("conformes"), "Gardés"), ("chiffre-verifier", entier("a_verifier"), "À vérifier"),
        ("chiffre-rejete", entier("rejetes"), "Rejetés"),
    )
    chiffres = "".join(f'<div class="chiffre {classe}"><span class="nombre">{n}</span><span class="legende">{libelle}</span></div>' for classe, n, libelle in tuiles)
    origine = f"{_e(passage.get('fichier', ''))}" + (f" · passé le {_e(_date_fr(passage['date']))}" if passage.get("date") else "")
    causes = passage.get("causes") if isinstance(passage.get("causes"), dict) else {}
    lignes_causes: list = []
    for statut in ("Rejeté", "À vérifier"):
        detail = causes.get(statut)
        if isinstance(detail, dict):
            lignes_causes += [(cause, n, statut) for cause, n in detail.items() if isinstance(n, int)]
    lignes_causes.sort(key=lambda t: -t[1])
    visibles, reste = lignes_causes[:8], lignes_causes[8:]
    li_causes = "".join(f"<li><span>{_e(_sans_cadratin(cause))}{' (à vérifier)' if statut == 'À vérifier' else ''}</span><b>{n}</b></li>" for cause, n, statut in visibles)
    if reste:
        li_causes += f"<li><span>{len(reste)} autres causes</span><b>{sum(n for _, n, _ in reste)}</b></li>"
    par_cible = passage.get("par_cible") if isinstance(passage.get("par_cible"), dict) else {}
    li_cibles = "".join(f"<li><span>{_e(cible)}</span><b>{n}</b></li>" for cible, n in par_cible.items() if isinstance(n, int))
    bloc_causes = f'<div class="carte"><h3>Pourquoi des lignes sont écartées</h3><ul class="compte">{li_causes or "<li><span>Aucune ligne écartée.</span></li>"}</ul></div>'
    bloc_cibles = f'<div class="carte"><h3>Gardés, par cible</h3><ul class="compte">{li_cibles or "<li><span>Aucun contact gardé.</span></li>"}</ul></div>'
    encarts = ""
    non_appliquees = passage.get("regles_non_appliquees")
    if isinstance(non_appliquees, list) and non_appliquees:
        colonnes = [str(r.get("colonne") or r.get("libelle")) for r in non_appliquees if isinstance(r, dict)]
        encarts += (f'<div class="encart">Règles non appliquées : votre fichier n\'a pas {_e(_colonnes_texte(colonnes))}. '
                    "Les lignes ne sont pas contrôlées sur ces points.</div>")
    signature = passage.get("signature_regles")
    if signature and signature != leadlint.empreinte_regles(configuration):
        encarts += '<div class="encart doux">Ce passage date d\'avant votre dernière modification des règles.</div>'
    return (f'<section><h2>Dernier passage</h2><div class="chiffres">{chiffres}</div>'
            f'<p class="total">sur {entier("total")} lignes · {origine}</p>'
            f'<div class="deux">{bloc_causes}{bloc_cibles}</div>{encarts}</section>')


def rendre_page(identite: dict, configuration: dict, cibles: list, passage: Any = None, etat_liste: str = "inconnu") -> str:
    nom = identite.get("mission") or "Ma prospection"
    pour = identite.get("client")
    etat, texte_sceau = _sceau(configuration, etat_liste)
    familles: dict = {cle: [] for cle, _, _ in FAMILLES}
    for regle in configuration["regles"]:
        if not isinstance(regle, dict):
            continue
        famille, titre, phrase = _decrire(regle)
        if regle.get("operateur") == "pas_blackliste":
            reference = configuration["blacklist"]
            volume = reference.get("volumeAttendu")
            contenu = f"{volume} lignes validées" if isinstance(volume, int) and not isinstance(volume, bool) else "pas encore chargée"
            nom_liste = str(reference.get("nom") or "Liste d'exclusion")
            familles["exclusion"].append(_bloc_regle(titre, phrase, [("Liste", nom_liste), ("Contenu", contenu)] + _detail(regle)[1:],
                                                     not leadlint.vrai(regle.get("active")), f"{nom_liste} : {contenu}."))
            continue
        familles[famille].append(_bloc_regle(titre, phrase, _detail(regle), not leadlint.vrai(regle.get("active"))))
    obligatoires = configuration.get("champsObligatoires") or []
    if obligatoires:
        noms = [LIBELLES_CHAMPS.get(c, c) for c in obligatoires]
        familles["qualite"].append(_bloc_regle("Champs obligatoires", f"Une ligne est écartée s'il manque : {', '.join(noms)}.", [("Champs", ", ".join(noms)), ("Hors règle", "rejeté")]))
    cartes_familles = "".join(
        f'<article class="carte"><h3>{_e(titre)}</h3>{"".join(familles[cle]) or f"<p class=vide>{_e(vide)}</p>"}</article>'
        for cle, titre, vide in FAMILLES
    )
    cartes_cibles = "".join(
        f'<div class="carte carte-cible"><p class="rang">Cible {rang}</p><p class="nom">{_e(cible["categorie"])}</p>'
        + "".join(f'<span class="pastille">{_e(mot)}</span>' for mot in cible["motsCles"]) + "</div>"
        for rang, cible in enumerate(cibles, start=1)
    ) or '<p class="vide">Aucune cible dans matrice.json.</p>'
    note_cibles = '<p class="total">Si un poste correspond à plusieurs cibles, la première de la liste l\'emporte.</p>' if len(cibles) > 1 else ""
    note_liste = "<li>La liste d'exclusion est lue dans <code>exclusions.csv</code> posé à côté du script, ou avec <code>--exclusions mon-fichier.csv</code>.</li>" if _liste_exclusion_requise(configuration) else ""
    return (
        '<!doctype html>\n<html lang="fr">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="color-scheme" content="light dark">\n'
        f"<title>Règles de ciblage : {_e(nom)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        f'<header><p class="surtitre">Vos règles de ciblage</p><h1>{_e(nom)}</h1>'
        + (f'<p class="pour">pour {_e(pour)}</p>' if pour else "")
        + f'<p class="sceau sceau-{etat}"><span class="point"></span><span>{_e(texte_sceau)}</span></p></header>\n'
        f'<section><h2>Vos cibles</h2><div class="cibles">{cartes_cibles}</div>{note_cibles}</section>\n'
        f'<section><h2>Ce qu\'on garde, ce qu\'on écarte</h2><div class="familles">{cartes_familles}</div></section>\n'
        f"{_section_passage(passage, configuration)}\n"
        '<section><h2>Comment s\'en servir</h2><div class="carte"><ol class="etapes">'
        "<li>Passez votre export avec <code>python moulinette.py mon-export.csv</code>.</li>"
        "<li>Les résultats arrivent dans le dossier <code>resultats</code> : le fichier se terminant par <code>-nettoye.xlsx</code> contient les contacts gardés, avec leur cible.</li>"
        f"<li>Pour changer une règle, demandez à votre agent : « change mes règles LeadLint ».</li>{note_liste}"
        "</ol></div></section>\n"
        f'<footer><a href="{DEPOT}" rel="noopener">leadlint</a> · gratuit, local, vos fichiers ne quittent pas votre ordinateur · Zero Gravity AI</footer>\n'
        "</main>\n</body>\n</html>\n"
    )


def ecrire_page(dossier: Path) -> Path:
    """Écrit regles.html depuis mission.json, matrice.json et dernier-passage.json (s'il existe)."""
    identite, configuration = leadlint.charger_mission(dossier / "mission.json")
    if configuration["mode"] != "revalidation_b2b":
        raise ErreurEntree("Cette mission est une matrice de personas : il n'y a pas de règles à montrer.")
    matrice = _lire_json(dossier / "matrice.json")
    axes = matrice.get("axes") if isinstance(matrice, dict) else None
    regles = axes[0].get("regles") if isinstance(axes, list) and axes and isinstance(axes[0], dict) else None
    cibles = [r for r in regles if isinstance(r, dict) and isinstance(r.get("motsCles"), list)] if isinstance(regles, list) else []
    contenu = rendre_page(identite, configuration, cibles, _lire_json(dossier / "dernier-passage.json"), _etat_liste(dossier, configuration))
    chemin = dossier / "regles.html"
    _ecrire_texte(chemin, contenu)
    return chemin


# --------------------------------------------------------------------------- la moulinette


def _lancer_moteur(argv: list) -> tuple:
    """Lance `leadlint check … --json` dans ce processus. Rend (code, bilan) ; sans bilan, l'erreur du moteur."""
    sortie, erreurs = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
        try:
            code = leadlint.main(argv)
        except SystemExit as arret:
            code = arret.code if isinstance(arret.code, int) else 1
    texte = sortie.getvalue()
    if not texte.strip():
        message = re.sub(r"^leadlint\s*:\s*", "", erreurs.getvalue().strip())
        raise ErreurEntree(message or "Le contrôle n'a rien rendu.")
    try:
        return code, json.loads(texte)
    except json.JSONDecodeError as erreur:
        raise ErreurEntree(f"Le contrôle a rendu un texte illisible : {erreur}") from erreur


def _expliquer_blocage(dossier: Path, configuration: dict, export: Path, exclusions: Any, bilan: dict) -> list:
    """En français simple : ce qui bloque, et quoi faire."""
    bloques = {c["id"] for c in bilan["prevol"]["controles"] if c["bloquant"] and not c["valide"]}
    lignes: list = []
    if "cible_validee" in bloques:
        origine = configuration.get("origine") or {}
        date = origine.get("valideeLe")
        if not (isinstance(date, str) and date.strip()):
            lignes += [
                "Vos règles ne sont pas encore validées.",
                "  Relisez regles.html puis demandez à votre agent de valider, ou lancez vous-même :",
                f'  leadlint-setup validate "{dossier}"',
                f'  (sans installation : python "{dossier / "leadlint_setup.py"}" validate "{dossier}")',
            ]
        else:
            lignes += [
                "Vos règles ont changé depuis la validation (mission.json a été modifié après coup).",
                "  Relisez regles.html : si les règles vous conviennent, revalidez avec « leadlint-setup validate », sinon demandez à votre agent de les remettre comme avant.",
            ]
    if "blacklist" in bloques:
        if exclusions is None:
            lignes += ["Il manque la liste d'exclusion.", "  Posez-la dans le dossier sous le nom exclusions.csv, ou ajoutez --exclusions <fichier>."]
        else:
            lignes += [f"Je ne reconnais pas les colonnes de la liste d'exclusion {Path(exclusions).name} (« Entreprise », « Site », « LinkedIn entreprise » ou « SIREN » attendues)."]
    elif "empreinte" in bloques and leadlint.vrai(configuration["blacklist"].get("empreinte")):
        attendu = configuration["blacklist"].get("volumeAttendu")
        try:
            trouve = len(leadlint.lire_tableau(Path(exclusions))[1]) if exclusions is not None else None
        except (ErreurEntree, OSError):
            trouve = None
        precision = f" ({attendu} lignes validées, {trouve} aujourd'hui)" if attendu is not None and trouve is not None else ""
        lignes += [f"La liste d'exclusion n'est plus celle que vous avez validée{precision}.", "  Si le changement est voulu, revalidez avec « leadlint-setup validate »."]
    if "configuration" in bloques:
        entetes = leadlint.lire_tableau(export)[0]
        correspondance = leadlint.completer_mapping(configuration["mappingCandidats"], entetes, leadlint.ENTETES_CANDIDATS)
        manquants = [c for c in configuration["champsObligatoires"] if not correspondance.get(c)]
        if manquants:
            noms = ", ".join(f"« {LIBELLES_CHAMPS.get(c, c)} »" for c in manquants)
            lignes += [
                f"Il manque dans votre export {'la colonne' if len(manquants) == 1 else 'les colonnes'} {noms}.",
                f"  Colonnes lues : {', '.join(entetes)}.",
                "  Renommez la colonne dans votre export, ou demandez à votre agent de dire à quelle colonne elle correspond.",
            ]
        else:
            lignes += ["Les règles ou les colonnes finales ne sont pas complètes : demandez à votre agent de relancer « leadlint-setup create »."]
    if "candidats" in bloques:
        lignes += [f"Votre export {export.name} ne contient aucune ligne."]
    return lignes


def _causes_texte(causes: dict) -> str:
    return " · ".join(f"{_sans_cadratin(cause)} {n}" for cause, n in causes.items()) or "aucune"


def passer(dossier: Path, export: Path, exclusions: Any = None) -> int:
    """Une passe : contrôle avant lancement, vérification de chaque ligne, fichier nettoyé avec sa cible, bilan."""
    dossier = dossier.resolve()
    chemin_mission = dossier / "mission.json"
    if not chemin_mission.is_file():
        raise ErreurEntree(f"mission.json est introuvable dans {dossier} : lancez d'abord « leadlint-setup create ».")
    identite, configuration = leadlint.charger_mission(chemin_mission)
    nom = " / ".join(v for v in (identite.get("client"), identite.get("mission")) if v) or "ma mission"
    if exclusions is None:
        exclusions = _trouver_exclusions(dossier)
    resultats = dossier / "resultats"
    commande = ["check", str(export), "--mission", str(chemin_mission), "-o", str(resultats), "--json", "--lignes"]
    if exclusions is not None:
        commande += ["--exclusions", str(exclusions)]
    code, bilan = _lancer_moteur(commande)
    if code == 2 or not bilan.get("livre"):
        sortie = [f"« {nom} » · {export.name}", "Le contrôle avant lancement bloque : rien n'est livré."]
        sortie += _expliquer_blocage(dossier, configuration, export, exclusions, bilan)
        sys.stdout.write("\n".join(sortie) + "\n")
        return 2
    justes = all(bilan["invariants"].values())
    matrice = _lire_json(dossier / "matrice.json")
    try:
        axe = matrice["axes"][0]
    except (TypeError, KeyError, IndexError) as erreur:
        raise ErreurEntree("matrice.json est illisible : relancez « leadlint-setup create ».") from erreur
    entetes_c, lignes_c = leadlint.lire_tableau(Path(bilan["fichiers"]["conformes"]))
    if len(lignes_c) != bilan["conformes"]:
        justes = False
    colonnes = configuration["colonnesFinales"]
    position = next((i for i, c in enumerate(colonnes) if c.get("champ") == "poste"), None)
    cibles = list(dict.fromkeys(r["categorie"] for r in axe.get("regles", []))) + [leadlint.NON_CLASSE]
    comptes: Counter = Counter()
    lignes_nettoyees: list = []
    for ligne in lignes_c:
        cible = leadlint.classer(ligne[position] if position is not None and position < len(ligne) else "", axe)
        comptes[cible] += 1
        lignes_nettoyees.append(list(ligne) + [cible])
    nettoye = resultats / f"{leadlint._base(export)}-nettoye.xlsx"
    classeur = leadlint._openpyxl()[0].Workbook()
    classeur.properties.creator = "leadlint"
    classeur.remove(classeur.active)
    leadlint._feuille(classeur, "Contacts nettoyés", "ContactsNettoyes", list(entetes_c) + ["Cible"], lignes_nettoyees,
                      [34 if re.search(r"linkedin|email", e, re.I) else 22 for e in list(entetes_c) + ["Cible"]])
    classeur.save(nettoye)
    entetes_export = leadlint.lire_tableau(export)[0]
    correspondance = leadlint.completer_mapping(configuration["mappingCandidats"], entetes_export, leadlint.ENTETES_CANDIDATS)
    non_appliquees = []
    for regle in configuration["regles"]:
        champ = leadlint.DEFINITIONS.get(regle.get("critereId"), (None,))[0]
        if leadlint.vrai(regle.get("active")) and champ and not correspondance.get(champ):
            non_appliquees.append({"regle": regle.get("id"), "champ": champ, "colonne": LIBELLES_CHAMPS.get(champ, champ)})
    fichiers = {"nettoye": nettoye.relative_to(dossier).as_posix()}
    for cle in ("controle_qa", "conformes"):
        fichiers[cle] = Path(bilan["fichiers"][cle]).resolve().relative_to(dossier.resolve()).as_posix()
    passage = {
        "fichier": export.name, "date": dt.datetime.now().replace(microsecond=0).isoformat(),
        "moteur": f"leadlint {leadlint.VERSION}", "total": bilan["total"], "conformes": bilan["conformes"],
        "a_verifier": bilan["a_verifier"], "rejetes": bilan["rejetes"], "causes": bilan["causes"],
        "par_cible": {cible: comptes[cible] for cible in cibles if comptes[cible]},
        "regles_non_appliquees": non_appliquees, "signature_regles": leadlint.empreinte_regles(configuration), "fichiers": fichiers,
    }
    _ecrire_json(dossier / "dernier-passage.json", passage)
    page = ecrire_page(dossier)
    sortie = [
        f"« {nom} » · {export.name}",
        f"Lignes {bilan['total']} · gardées {bilan['conformes']} · à vérifier {bilan['a_verifier']} · rejetées {bilan['rejetes']}",
        f"Rejetées, par cause : {_causes_texte(bilan['causes']['Rejeté'])}",
        f"Gardées, par cible : {_causes_texte(passage['par_cible'])}",
        f"Contrôles du bilan : {'justes' if justes else 'FAUX'}",
    ]
    if bilan["causes"]["À vérifier"]:
        sortie.insert(3, f"À vérifier, par cause : {_causes_texte(bilan['causes']['À vérifier'])}")
    if non_appliquees:
        sortie.append(f"Règles non appliquées, faute de colonne dans votre export : {', '.join(dict.fromkeys(r['colonne'] for r in non_appliquees))}.")
    sortie += [f"Contacts nettoyés : {nettoye}", f"Contrôle ligne par ligne : {(dossier / fichiers['controle_qa'])}", f"Vos règles et ce passage : {page}"]
    sys.stdout.write("\n".join(sortie) + "\n")
    return 0 if justes else 1


def lancer_moulinette(dossier: Path, argv: list) -> int:
    """Le point d'entrée de moulinette.py : `python moulinette.py <export> [--exclusions <fichier>]`."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    parseur = argparse.ArgumentParser(prog="python moulinette.py", description="Clean an export with the rules of this folder (mission.json). Writes into resultats/.")
    parseur.add_argument("export", type=Path, help="candidates export .xlsx, .xlsm or .csv")
    parseur.add_argument("--exclusions", type=Path, help="exclusion list .xlsx or .csv (default: exclusions.* in this folder)")
    args = parseur.parse_args(argv)
    try:
        return passer(Path(dossier).resolve(), args.export.resolve(), args.exclusions.resolve() if args.exclusions else None)
    except ErreurEntree as erreur:
        sys.stderr.write(f"moulinette : {erreur}\n")
        return 1


# --------------------------------------------------------------------------- ligne de commande


def _sortie(donnees: dict, en_json: bool, texte: list) -> None:
    sys.stdout.write((json.dumps(donnees, ensure_ascii=False, indent=2) if en_json else "\n".join(texte)) + "\n")


def commande_create(args) -> int:
    resultat = creer(args.reponses, args.sortie, args.exclusions, args.force)
    texte = [f"leadlint-setup : « {resultat['mission']} » est prête dans {resultat['dossier']}", "Écrit : " + " · ".join(resultat["fichiers"])]
    if resultat["liste_exclusion_requise"] and not resultat["liste_exclusion_fournie"]:
        texte.append("Il manque la liste d'exclusion : donnez-la à la validation avec --exclusions <fichier>.")
    texte += [f"Page des règles : {resultat['page']}", PHRASE_SUITE]
    _sortie(resultat, args.json, texte)
    return 0


def commande_validate(args) -> int:
    resultat = valider(args.dossier, args.exclusions, args.date)
    texte = [f"leadlint-setup : règles validées le {_date_fr(resultat['validee_le'])} (signature {resultat['signature_regles']})."]
    if resultat["liste_exclusion"]:
        texte.append(f"Liste d'exclusion : {resultat['liste_exclusion']['volume']} lignes validées ({resultat['liste_exclusion']['fichier']}).")
    texte += [f"Page à jour : {resultat['page']}", "Vous pouvez passer votre export : python moulinette.py mon-export.csv"]
    _sortie(resultat, args.json, texte)
    return 0


def commande_page(args) -> int:
    chemin = ecrire_page(args.dossier)
    sys.stdout.write(f"leadlint-setup : page à jour : {chemin}\n")
    return 0


def construire_parseur() -> argparse.ArgumentParser:
    parseur = argparse.ArgumentParser(prog="leadlint-setup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--version", action="version", version=f"leadlint-setup {VERSION}")
    sous = parseur.add_subparsers(dest="commande", required=True, metavar="{create,validate,page}")

    creer_ = sous.add_parser("create", help="write the folder (mission, matrix, script, rules page) from the answers", formatter_class=argparse.RawDescriptionHelpFormatter)
    creer_.add_argument("reponses", type=Path, help="answers JSON file (see skills/leadlint-setup/exemples/reponses-exemple.json)")
    creer_.add_argument("-o", "--sortie", type=Path, required=True, metavar="FOLDER", help="folder to write (created if absent)")
    creer_.add_argument("--exclusions", type=Path, metavar="FILE", help="exclusion list .xlsx or .csv, copied into the folder")
    creer_.add_argument("--force", action="store_true", help="overwrite a mission that was already validated (the validation is erased)")
    creer_.add_argument("--json", action="store_true", help="JSON on standard output (for an agent)")
    creer_.set_defaults(action=commande_create)

    valider_ = sous.add_parser(
        "validate", help="record the human validation of the rules (only after the person has explicitly agreed)",
        description="Records the validation date and the rules signature in mission.json. RUN THIS ONLY WHEN THE PERSON HAS EXPLICITLY AGREED to their rules,\n"
                    "after reading regles.html. An agent never runs it on its own initiative and never edits mission.json by hand to make a list pass.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    valider_.add_argument("dossier", type=Path, help="the folder written by create")
    valider_.add_argument("--exclusions", type=Path, metavar="FILE", help="exclusion list to validate (default: exclusions.* in the folder)")
    valider_.add_argument("--date", metavar="YYYY-MM-DD", help="validation date (default: today)")
    valider_.add_argument("--json", action="store_true", help="JSON on standard output (for an agent)")
    valider_.set_defaults(action=commande_validate)

    page = sous.add_parser("page", help="write regles.html again from mission.json")
    page.add_argument("dossier", type=Path, help="the folder written by create")
    page.set_defaults(action=commande_page)
    return parseur


def main(argv: Any = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = construire_parseur().parse_args(argv)
    try:
        return args.action(args)
    except ErreurEntree as erreur:
        sys.stderr.write(f"leadlint-setup : {erreur}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
