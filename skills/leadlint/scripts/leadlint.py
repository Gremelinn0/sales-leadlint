#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leadlint: a linter for B2B lead lists, built for AI agents. Local and deterministic.

Five commands:

  personas      sorts a column of job titles with a matrix
                (department, seniority, inferred buying role).
  check         revalidates a prospecting export against a mission:
                rules, exclusion list, duplicates, preflight, QA workbook, bilan.
  preflight     says only whether the mission may run, control by control.
  fingerprints  computes the fingerprints the mission memorises
                (strategy, search, filters, exclusion list).
  hubspot       `setup` creates the leadlint_* properties on HubSpot contacts; `sync` classifies the contacts
                modified since the last pass and writes only the values that change (see the section HubSpot).

Examples:

  leadlint personas contacts.xlsx
  leadlint check export.csv --mission mission.json --exclusions exclusions.csv
  leadlint check export.csv --mission mission.json --diagnostic --json

The mission file is the one the web page saves (https://zerogravity.builders/outils/segmentation):
a JSON configuration, or a full setup that contains it. Nothing leaves the machine: no network
call, no data sent. The only exception is the `hubspot` command, which talks to the HubSpot API and
to nothing else, with the token read from the HUBSPOT_TOKEN environment variable.

Exit codes: 0 done (both bilan invariants hold) · 2 preflight blocked (nothing is delivered) ·
1 unreadable input or wrong bilan.

This engine is the exact twin of the web page's (src/lib/segmentation of the ZEROGRAVITY
Marketplace): same rules, same verdicts, same fingerprints. `tests/test_leadlint.py` proves it on
scenarios rendered by the web engine. Verdicts, report lines, JSON keys and workbook labels stay in
French on purpose: they are the web page's.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VERSION = "1.3.0"
ICI = Path(__file__).resolve().parent


def _dossier_matrices() -> Path:
    """Les matrices se trouvent à deux endroits selon la façon dont le script est lancé.

    Installé en paquet (uvx, pip) : `leadlint_data/matrices/` est livré à côté du module.
    Lancé depuis la compétence : `skills/leadlint/matrices/`, un cran au-dessus de `scripts/`.
    """
    for candidat in (ICI / "leadlint_data" / "matrices", ICI.parent / "matrices"):
        if candidat.is_dir():
            return candidat
    return ICI.parent / "matrices"


MATRICE_DEFAUT = _dossier_matrices() / "matrice-generique.json"

# --------------------------------------------------------------------------- utilitaires « comme le navigateur »


class _Indefini:
    """Valeur absente, distincte de null : une clé indéfinie ne compte pas dans une empreinte."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "INDEFINI"


INDEFINI = _Indefini()


def nombre_js(n: float | int) -> str:
    """Écrit un nombre comme le navigateur (`String(10.0)` donne « 10 », pas « 10.0 »)."""
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if math.isnan(n):
        return "NaN"
    if math.isinf(n):
        return "Infinity" if n > 0 else "-Infinity"
    if n.is_integer() and abs(n) < 1e21:
        return str(int(n))
    texte = repr(n)
    if "e" in texte:
        mantisse, exposant = texte.split("e")
        valeur = int(exposant)
        texte = f"{mantisse}e{'+' if valeur > 0 else '-'}{abs(valeur)}"
    return texte


def texte_js(valeur: Any) -> str:
    """`String(valeur ?? "")` : None et l'absence donnent une chaîne vide."""
    if valeur is None or valeur is INDEFINI:
        return ""
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    if isinstance(valeur, (int, float)):
        return nombre_js(valeur)
    if isinstance(valeur, (dt.datetime, dt.date)):
        return valeur.isoformat()
    return str(valeur)


def chaine_js(valeur: Any) -> str:
    """`String(valeur)` strict : null donne « null »."""
    if valeur is None:
        return "null"
    return texte_js(valeur)


_ESPACES = "\t\n\v\f\r                  　﻿"


def rogner(texte: str) -> str:
    """`trim()` du navigateur (il retire aussi l'indicateur d'ordre des octets)."""
    return texte.strip(_ESPACES)


def est_nombre(valeur: Any) -> bool:
    return isinstance(valeur, (int, float)) and not isinstance(valeur, bool)


def est_fini(valeur: Any) -> bool:
    """`Number.isFinite` : un vrai nombre, jamais une chaîne qui en a l'air."""
    return est_nombre(valeur) and math.isfinite(valeur)


def vrai(valeur: Any) -> bool:
    """Vérité au sens du navigateur (0, "", None et l'absence sont faux)."""
    if valeur is None or valeur is INDEFINI:
        return False
    if isinstance(valeur, float) and math.isnan(valeur):
        return False
    if isinstance(valeur, (list, dict)):
        return True
    return bool(valeur)


def json_js(valeur: Any) -> str:
    """`JSON.stringify` d'une valeur simple."""
    if valeur is None or valeur is INDEFINI:
        return "null"
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    if est_nombre(valeur):
        return nombre_js(valeur) if math.isfinite(valeur) else "null"
    if isinstance(valeur, str):
        return json.dumps(valeur, ensure_ascii=False)
    return json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))


def _cle_locale(cle: str) -> tuple:
    """Ordre des clés de `localeCompare` : sans casse d'abord, minuscule avant majuscule ensuite."""
    return (cle.casefold(), cle.swapcase())


def serialiser_stable(valeur: Any) -> str:
    """Sérialisation stable au sens de JSON (clé indéfinie omise). Jumelle de `serialiserStable`."""
    if isinstance(valeur, list):
        return "[" + ",".join("null" if item is INDEFINI else serialiser_stable(item) for item in valeur) + "]"
    if isinstance(valeur, dict):
        entrees = sorted(((cle, contenu) for cle, contenu in valeur.items() if contenu is not INDEFINI), key=lambda kv: _cle_locale(kv[0]))
        return "{" + ",".join(f"{json.dumps(cle, ensure_ascii=False)}:{serialiser_stable(contenu)}" for cle, contenu in entrees) + "}"
    return json_js(valeur)


def fnv1a(texte: str) -> str:
    """FNV-1a 32 bits sur les unités UTF-16, comme `charCodeAt` du navigateur."""
    empreinte = 0x811C9DC5
    octets = texte.encode("utf-16-le", "surrogatepass")
    for index in range(0, len(octets), 2):
        empreinte ^= octets[index] | (octets[index + 1] << 8)
        empreinte = (empreinte * 0x01000193) & 0xFFFFFFFF
    return f"{empreinte:08x}"


def empreinte_texte(valeur: Any) -> str:
    return f"fnv1a-{fnv1a(serialiser_stable(valeur))}"


def _id_local() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- normalisations

DOMAINES_EMAIL_PERSONNELS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr", "live.com",
    "live.fr", "yahoo.com", "yahoo.fr", "icloud.com", "orange.fr", "free.fr", "laposte.net", "proton.me",
    "protonmail.com",
}
FORMES_JURIDIQUES = {"sas", "sasu", "sarl", "eurl", "sa", "sca", "scop", "gie", "ei", "ltd", "limited", "inc", "llc"}


# `\p{Diacritic}` du navigateur : les marques combinantes, et aussi les accents isolés (^ ` ¨ ¯ ´ · ¸)
# et les lettres modificatives. Quelques marques combinantes latines n'en sont pas.
_ACCENTS_ISOLES = {0x5E, 0x60, 0xA8, 0xAF, 0xB4, 0xB7, 0xB8}
_MARQUES_NON_DIACRITIQUES = {0x34F, *range(0x358, 0x35D), *range(0x363, 0x370)}


def _diacritique(caractere: str) -> bool:
    code = ord(caractere)
    if code in _ACCENTS_ISOLES or 0x2B0 <= code <= 0x2FF:
        return True
    return unicodedata.category(caractere) == "Mn" and code not in _MARQUES_NON_DIACRITIQUES


def sans_diacritiques(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if not _diacritique(c))


def normaliser_texte(valeur: Any) -> str:
    """Minuscules, sans accents, ponctuation ramenée à une espace."""
    if valeur is None or valeur is INDEFINI:
        return ""
    texte = sans_diacritiques(texte_js(valeur)).lower().replace("’", " ").replace("'", " ")
    return re.sub(r"[^a-z0-9]+", " ", texte).strip(" ")


def normaliser_nom_entreprise(valeur: Any) -> str:
    texte = normaliser_texte(valeur)
    texte = re.sub(r"\b(s a s u|s a s|s a r l|e u r l|s a|l t d|l l c)\b", lambda m: m.group(0).replace(" ", ""), texte)
    return " ".join(mot for mot in texte.split(" ") if mot not in FORMES_JURIDIQUES).strip(" ")


_HOTE_INTERDIT = re.compile(r"[\x00-\x20#/:<>?@\[\\\]^|%\x7f\"]")


def _encoder_chemin(chemin: str) -> str:
    """Encodage du chemin d'une URL, comme le navigateur (`url.pathname`)."""
    sortie = []
    for caractere in chemin.replace("\\", "/"):
        code = ord(caractere)
        if code <= 0x20 or code >= 0x7F or caractere in "\"<>`{}":
            sortie.extend(f"%{octet:02X}" for octet in caractere.encode("utf-8", "surrogatepass"))
        else:
            sortie.append(caractere)
    segments: list[str] = []
    morceaux = "".join(sortie).split("/")
    for position, morceau in enumerate(morceaux[1:] if morceaux and morceaux[0] == "" else morceaux):
        dernier = position == len(morceaux) - 2
        bas = morceau.lower()
        if bas in {".", "%2e"}:
            if dernier:
                segments.append("")
            continue
        if bas in {"..", ".%2e", "%2e.", "%2e%2e"}:
            if segments:
                segments.pop()
            if dernier:
                segments.append("")
            continue
        segments.append(morceau)
    return "/" + "/".join(segments)


def _analyser_url(brut: str) -> tuple[str, str] | None:
    """(hôte, chemin) d'une adresse, ou None si le navigateur la refuserait."""
    texte = brut if re.match(r"^https?://", brut, re.I) else f"https://{brut}"
    texte = re.sub(r"[\t\n\r]", "", texte)
    try:
        morceaux = urlsplit(texte)
        hote = morceaux.hostname
    except ValueError:
        return None
    if not hote or _HOTE_INTERDIT.search(hote):
        return None
    try:
        hote = ".".join(etiquette.encode("idna").decode("ascii") if etiquette else "" for etiquette in hote.split("."))
    except UnicodeError:
        return None
    hote = _hote_ipv4(hote.lower())
    if hote is None:
        return None
    return hote, _encoder_chemin(morceaux.path or "/")


def _nombre_ipv4(partie: str) -> int | None:
    base = 10
    if partie[:2] in ("0x", "0X"):
        base, partie = 16, partie[2:]
    elif len(partie) > 1 and partie.startswith("0"):
        base, partie = 8, partie[1:]
    if partie == "":
        return 0
    try:
        return int(partie, base) if re.fullmatch(r"[0-9a-fA-F]+", partie) else None
    except ValueError:
        return None


def _hote_ipv4(hote: str) -> str | None:
    """Un hôte qui finit par un nombre est une adresse IPv4 pour le navigateur (« 1.2.3 » devient 1.2.0.3)."""
    parties = hote.split(".")
    if parties[-1] == "" and len(parties) > 1:
        parties = parties[:-1]
    dernier = parties[-1]
    if not (re.fullmatch(r"[0-9]+", dernier) or (dernier[:2] in ("0x", "0X") and re.fullmatch(r"[0-9a-fA-F]*", dernier[2:]))):
        return hote
    if len(parties) > 4 or any(p == "" for p in parties):
        return None
    nombres = [_nombre_ipv4(p) for p in parties]
    if any(n is None for n in nombres) or any(n > 255 for n in nombres[:-1]) or nombres[-1] >= 256 ** (5 - len(nombres)):
        return None
    valeur = nombres[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(nombres[:-1]))
    return ".".join(str((valeur >> decalage) & 255) for decalage in (24, 16, 8, 0))


def normaliser_url_linkedin(valeur: Any) -> str:
    brut = rogner(texte_js(valeur))
    if not brut:
        return ""
    analyse = _analyser_url(brut)
    if not analyse:
        return ""
    hote = re.sub(r"^www\.", "", analyse[0])
    if not hote.endswith("linkedin.com"):
        return ""
    return "https://www.linkedin.com" + re.sub(r"/+$", "", analyse[1]).lower()


def normaliser_domaine(valeur: Any) -> str:
    brut = rogner(texte_js(valeur)).lower()
    if not brut:
        return ""
    analyse = _analyser_url(brut)
    if analyse:
        return re.sub(r"\.$", "", re.sub(r"^www\.", "", analyse[0]))
    sans_protocole = re.sub(r"^www\.", "", re.sub(r"^https?://", "", brut))
    return re.sub(r"\.$", "", re.split(r"[/?#]", sans_protocole)[0])


def normaliser_siren(valeur: Any) -> str:
    chiffres = re.sub(r"[^0-9]", "", texte_js(valeur))
    return chiffres if len(chiffres) == 9 else ""


def normaliser_categorie_linkedin(valeur: Any) -> str:
    bornes = re.search(r"([0-9]{1,5})[^0-9]+([0-9]{1,5})", normaliser_texte(valeur))
    return f"{int(bornes.group(1))}-{int(bornes.group(2))}" if bornes else ""


def lire_nombre(valeur: Any) -> float | int | None:
    if est_nombre(valeur):
        return valeur if math.isfinite(valeur) else None
    nettoyee = re.sub(r"\s", "", texte_js(valeur)).replace(",", ".", 1)
    morceaux = re.findall(r"-?[0-9]+(?:\.[0-9]+)?", nettoyee)
    if not morceaux:
        return None
    try:
        resultat = float("".join(morceaux))
    except ValueError:
        return None
    if not math.isfinite(resultat):
        return None
    return int(resultat) if resultat.is_integer() else resultat


def normaliser_email(valeur: Any) -> str:
    return rogner(texte_js(valeur)).lower()


def email_est_professionnel(valeur: Any) -> bool:
    email = normaliser_email(valeur)
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email)) and email.split("@")[1] not in DOMAINES_EMAIL_PERSONNELS


def normaliser_mobile_francais(valeur: Any) -> str:
    chiffres = re.sub(r"[^0-9]", "", texte_js(valeur))
    if chiffres.startswith("0033"):
        chiffres = chiffres[4:]
    elif chiffres.startswith("33"):
        chiffres = chiffres[2:]
    if chiffres.startswith("0"):
        chiffres = chiffres[1:]
    return f"0{chiffres}" if re.fullmatch(r"[67][0-9]{8}", chiffres) else ""


# --------------------------------------------------------------------------- critères (jumeau de regles.ts)

ENTETES_CANDIDATS: "OrderedDict[str, list[str]]" = OrderedDict([
    ("nomComplet", ["nom complet", "full name", "nom", "contact"]),
    ("entreprise", ["entreprise", "company", "company name", "societe", "account"]),
    ("poste", ["poste", "job title", "title", "fonction"]),
    ("linkedinProfil", ["linkedin profil", "linkedin profile", "linkedin url", "person linkedin url"]),
    ("linkedinEntreprise", ["linkedin entreprise", "company linkedin", "company linkedin url"]),
    ("categorieLinkedIn", ["categorie linkedin", "categorie linkedin 11 50", "linkedin company size", "company size", "taille linkedin"]),
    ("employesVisibles", ["nombre d employes visibles", "employes visibles", "employees on linkedin", "linkedin employees"]),
    ("pays", ["pays", "country", "company country"]),
    ("activite", ["activite", "activity", "industry", "industrie", "description"]),
    ("tagsFournisseur", ["tags fournisseur", "tags pharow", "tags", "source tags", "provider tags"]),
    ("trancheEffectifFournisseur", ["tranche effectif fournisseur", "tranche effectif pharow", "effectif france pharow", "company headcount range"]),
    ("qualificationActivite", ["qualification activite", "qualification metier qa", "qa activite", "business model qa"]),
    ("siteEntreprise", ["site entreprise", "company website", "website", "site", "domain", "domaine"]),
    ("siren", ["siren", "numero siren", "company siren"]),
    ("emailProfessionnel", ["email professionnel", "work email", "professional email", "email"]),
    ("sourceEmail", ["source email", "email source", "provenance email", "provenance de l email"]),
    ("mobile", ["mobile", "mobile obligatoire 06 07", "telephone mobile", "phone", "telephone", "numero de telephone"]),
    ("sourceMobile", ["source mobile", "phone source", "mobile source", "provenance mobile", "provenance du mobile", "provenance professionnelle du mobile"]),
    ("effectifLegal", ["effectif legal", "effectif france", "legal headcount"]),
    ("chiffreAffaires", ["chiffre d affaires", "chiffre affaires", "ca", "revenue"]),
    ("codeNaf", ["code naf", "naf", "ape", "code ape"]),
    ("statutJuridique", ["statut juridique", "forme juridique", "legal form"]),
    ("technologies", ["technologies", "tech stack", "stack"]),
    ("departement", ["departement", "department", "fonction departement"]),
    ("niveauHierarchique", ["niveau hierarchique", "seniority", "seniorite"]),
    ("anciennetePoste", ["anciennete poste", "anciennete dans le poste", "tenure"]),
])
ENTETES_BLACKLIST: "OrderedDict[str, list[str]]" = OrderedDict(
    (champ, ENTETES_CANDIDATS[champ]) for champ in ("entreprise", "linkedinEntreprise", "siteEntreprise", "siren")
)
CHAMPS_NUMERIQUES = {"employesVisibles", "effectifLegal", "chiffreAffaires", "anciennetePoste"}
CHAMPS_CONTACT = {"nomComplet", "poste", "linkedinProfil", "emailProfessionnel", "mobile", "sourceEmail", "sourceMobile", "departement", "niveauHierarchique", "anciennetePoste"}

# id -> (champ, libellé, portée)
DEFINITIONS: dict[str, tuple[str | None, str, str]] = {
    "pays": ("pays", "Pays", "entreprise"),
    "activite": ("activite", "Activité / expressions", "entreprise"),
    "tags_source": ("tagsFournisseur", "Tag du fournisseur", "entreprise"),
    "tranche_effectif_source": ("trancheEffectifFournisseur", "Tranche d'effectif du fournisseur", "entreprise"),
    "qualification_activite": ("qualificationActivite", "Qualification métier QA", "entreprise"),
    "categorie_linkedin": ("categorieLinkedIn", "Catégorie LinkedIn", "entreprise"),
    "employes_visibles": ("employesVisibles", "Employés visibles LinkedIn", "entreprise"),
    "effectif_legal": ("effectifLegal", "Effectif légal", "entreprise"),
    "chiffre_affaires": ("chiffreAffaires", "Chiffre d'affaires", "entreprise"),
    "code_naf": ("codeNaf", "Code NAF", "entreprise"),
    "statut_juridique": ("statutJuridique", "Statut juridique", "entreprise"),
    "technologies": ("technologies", "Technologies", "entreprise"),
    "poste": ("poste", "Poste du décideur", "contact"),
    "departement": ("departement", "Département", "contact"),
    "niveau_hierarchique": ("niveauHierarchique", "Niveau hiérarchique", "contact"),
    "email_pro": ("emailProfessionnel", "Email professionnel", "contact"),
    "source_email": ("sourceEmail", "Provenance de l'email", "contact"),
    "mobile_fr": ("mobile", "Mobile français 06/07", "contact"),
    "source_mobile": ("sourceMobile", "Provenance professionnelle du mobile", "contact"),
    "blacklist": (None, "Entreprise absente de la liste d'exclusion", "exclusion"),
    "doublon": (None, "Contact unique", "qualite"),
}

# Ce que dit le classeur d'une règle : le nom de l'opérateur en français courant (jumeau de LIBELLES_OPERATEURS de regles.ts).
# L'identifiant de l'opérateur, lui, ne change pas : c'est la clé du fichier de mission.
LIBELLES_OPERATEURS = {
    "est_dans": "est dans la liste", "contient_un": "contient au moins un", "ne_contient_aucun": "ne contient aucun",
    "n_est_pas_dans": "n'est pas dans la liste",
    "entre": "est compris entre", "present": "est renseigné", "email_professionnel": "est un email professionnel",
    "mobile_francais": "est un mobile français 06/07", "provenance_professionnelle": "indique une provenance professionnelle",
    "pas_blackliste": "n'est pas dans la liste d'exclusion", "unique": "n'est pas un doublon",
}

COLONNES_B2B_STANDARD = [
    {"champ": "nomComplet", "libelle": "Nom complet"},
    {"champ": "entreprise", "libelle": "Entreprise"},
    {"champ": "poste", "libelle": "Poste"},
    {"champ": "linkedinProfil", "libelle": "LinkedIn profil"},
    {"champ": "categorieLinkedIn", "libelle": "Catégorie LinkedIn"},
    {"champ": "employesVisibles", "libelle": "Nombre d'employés visibles"},
    {"champ": "emailProfessionnel", "libelle": "Email professionnel"},
    {"champ": "mobile", "libelle": "Mobile professionnel"},
]


# --------------------------------------------------------------------------- lecture d'une mission (jumeau de migrerConfigurationB2B)


def _regle(critere_id: str, operateur: str, valeur: Any, importance: str = "bloquant", si_absent: str = "rejeter") -> dict:
    return {"id": _id_local(), "critereId": critere_id, "operateur": operateur, "valeur": valeur, "importance": importance, "siAbsent": si_absent, "active": True}


def _defaut(valeur: Any, defaut: Any) -> Any:
    """L'opérateur `??` : None et l'absence prennent la valeur par défaut."""
    return defaut if valeur is None or valeur is INDEFINI else valeur


def migrer_filtres_source(filtres: Any) -> list[dict]:
    if not isinstance(filtres, list):
        return []
    sortie: list[dict] = []
    for filtre in filtres:
        if isinstance(filtre, str):
            libelle, _, reste = filtre.partition(":")
            sortie.append({
                "id": _id_local(), "recherche": "Recherche historique", "etape": "comptes",
                "libelle": rogner(libelle) or "Filtre historique", "valeurs": [rogner(reste) or "À confirmer"],
                "logique": "ET", "preuve": "configuration", "obligatoire": True,
                "note": "Filtre migré : relier à une règle avant le prochain run si ce critère porte sur chaque ligne.",
            })
            continue
        if not isinstance(filtre, dict):
            continue
        preuve = filtre.get("preuve")
        valeurs = filtre.get("valeurs")
        recu = {
            "id": filtre.get("id") or _id_local(),
            "recherche": rogner(texte_js(filtre.get("recherche"))) or "Recherche sans nom",
            "etape": "contacts" if filtre.get("etape") == "contacts" else "comptes",
            "libelle": rogner(texte_js(filtre.get("libelle"))) or "Filtre sans nom",
            "valeurs": [v for v in (chaine_js(x) for x in valeurs) if v] if isinstance(valeurs, list) else ["À confirmer"],
            "logique": filtre.get("logique") if filtre.get("logique") in ("ET", "OU", "EXCLURE") else "ET",
            "preuve": preuve if preuve in ("export", "configuration", "calcul", "qa_manuelle") else "configuration",
            "regleId": filtre.get("regleId", INDEFINI),
            "obligatoire": filtre.get("obligatoire") is not False,
            "note": filtre.get("note", INDEFINI),
        }
        sortie.append(recu)
    return sortie


def migrer_strategie(strategie: Any) -> dict:
    if not isinstance(strategie, dict):
        return {"lignes": [], "limites": ""}
    lignes = strategie.get("lignes")
    return {
        "lignes": [{**ligne, "id": ligne.get("id") or _id_local(), "obligatoire": ligne.get("obligatoire") is not False} for ligne in lignes if isinstance(ligne, dict)] if isinstance(lignes, list) else [],
        "limites": _defaut(strategie.get("limites"), ""),
        "valideeLe": strategie.get("valideeLe", INDEFINI),
        "empreinteValidee": strategie.get("empreinteValidee", INDEFINI),
    }


def _pharow(source: dict) -> dict:
    pharow = source.get("pharow") if isinstance(source.get("pharow"), dict) else {}
    return {
        "nomRecherche": "", "nomListeExclusion": "", **pharow,
        "filtres": migrer_filtres_source(pharow.get("filtres")),
        "echantillonTemoins": _defaut(pharow.get("echantillonTemoins"), []),
    }


def migrer_configuration_b2b(source: dict) -> dict:
    blacklist_defaut = {"obligatoire": True, "nom": "Liste d'exclusion", "typeSource": "fichier"}
    if source.get("schemaVersion") == 2 and isinstance(source.get("regles"), list):
        return {
            **source, "mode": "revalidation_b2b", "schemaVersion": 2, "regles": source["regles"],
            "champsObligatoires": _defaut(source.get("champsObligatoires"), []),
            "colonnesFinales": _defaut(source.get("colonnesFinales"), [dict(c) for c in COLONNES_B2B_STANDARD]),
            "mappingCandidats": _defaut(source.get("mappingCandidats"), {}),
            "mappingBlacklist": _defaut(source.get("mappingBlacklist"), {}),
            "blacklist": _defaut(source.get("blacklist"), blacklist_defaut),
            "strategie": migrer_strategie(source.get("strategie")),
            "pharow": _pharow(source),
        }
    ancien = source.get("criteres") if isinstance(source.get("criteres"), dict) else {}
    regles: list[dict] = []
    if ancien.get("paysAcceptes"):
        regles.append(_regle("pays", "est_dans", ancien["paysAcceptes"]))
    if ancien.get("motsClesActivite"):
        regles.append(_regle("activite", "contient_un", ancien["motsClesActivite"]))
    if ancien.get("categoriesLinkedIn"):
        regles.append(_regle("categorie_linkedin", "est_dans", ancien["categoriesLinkedIn"]))
    if est_fini(ancien.get("employesVisiblesMin")) and est_fini(ancien.get("employesVisiblesMax")):
        regles.append(_regle("employes_visibles", "entre", {"min": ancien["employesVisiblesMin"], "max": ancien["employesVisiblesMax"]}))
    if ancien.get("postesAcceptes"):
        regles.append(_regle("poste", "est_dans", ancien["postesAcceptes"]))
    if ancien.get("emailProfessionnelObligatoire"):
        regles.append(_regle("email_pro", "email_professionnel", True))
    if ancien.get("sourceEmailObligatoire"):
        regles.append(_regle("source_email", "present", True, "verification", "verifier"))
    if ancien.get("mobileProfessionnelObligatoire"):
        regles.append(_regle("mobile_fr", "mobile_francais", True))
    if ancien.get("sourceMobileObligatoire"):
        regles.append(_regle("source_mobile", "provenance_professionnelle", True, "verification", "verifier"))
    regles += [_regle("blacklist", "pas_blackliste", True), _regle("doublon", "unique", True)]
    return {
        "mode": "revalidation_b2b", "schemaVersion": 2, "regles": regles,
        "champsObligatoires": _defaut(ancien.get("champsObligatoires"), []),
        "colonnesFinales": _defaut(ancien.get("colonnesFinales"), [dict(c) for c in COLONNES_B2B_STANDARD]),
        "mappingCandidats": _defaut(source.get("mappingCandidats"), {}),
        "mappingBlacklist": _defaut(source.get("mappingBlacklist"), {}),
        "blacklist": _defaut(source.get("blacklist"), blacklist_defaut),
        "strategie": {"lignes": [], "limites": ""},
        "pharow": _pharow(source),
    }


def charger_mission(chemin: Path) -> tuple[dict, dict]:
    """Rend (identité de la mission, configuration). Accepte un setup, une ligne de base ou une configuration nue."""
    try:
        brut = json.loads(chemin.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as erreur:
        raise ErreurEntree(f"Mission illisible ({chemin}) : {erreur}") from erreur
    if not isinstance(brut, dict):
        raise ErreurEntree(f"Mission illisible ({chemin}) : un objet JSON est attendu.")
    configuration = brut.get("configuration") if isinstance(brut.get("configuration"), dict) else brut
    identite = {cle: brut.get(cle) for cle in ("id", "client", "mission", "description") if brut.get(cle)}
    if configuration.get("mode") == "persona" or "axes" in configuration or "matrice" in configuration:
        return identite, {"mode": "persona", "matrice": configuration.get("matrice", configuration), "multiCategories": bool(configuration.get("multiCategories"))}
    return identite, migrer_configuration_b2b(configuration)


# --------------------------------------------------------------------------- signatures et contrôle avant lancement (jumeau de setups.ts et b2b.ts)


def empreinte_strategie(strategie: dict) -> str:
    return empreinte_texte({"lignes": strategie.get("lignes", []), "limites": rogner(texte_js(strategie.get("limites")))})


def empreinte_filtres(filtres: list) -> str:
    return empreinte_texte(filtres)


def empreinte_regles(configuration: dict) -> str:
    """Signature des règles : les règles, les champs obligatoires et les colonnes finales, par le même calcul que les autres.

    Elle distingue une cible validée d'une cible dont une règle a été modifiée à la main. Jumelle de `calculerEmpreinteRegles`.
    Une propriété optionnelle d'une règle (`motEntier`) n'y figure que sur les règles qui l'ont.
    """
    return empreinte_texte({
        "regles": configuration["regles"], "champsObligatoires": configuration["champsObligatoires"], "colonnesFinales": configuration["colonnesFinales"],
    })


def origine_est_cible(configuration: dict) -> bool:
    """Une mission d'origine « cible » : une cible validée par un humain, sans recherche chez un fournisseur."""
    origine = configuration.get("origine")
    return isinstance(origine, dict) and origine.get("type") == "cible"


def cible_validee(configuration: dict) -> bool:
    """La date de validation est renseignée et les règles n'ont pas changé depuis (signature recalculée égale)."""
    if not origine_est_cible(configuration):
        return False
    origine = configuration["origine"]
    date, signature = origine.get("valideeLe"), origine.get("empreinteRegles")
    return isinstance(date, str) and rogner(date) != "" and isinstance(signature, str) and signature != "" and signature == empreinte_regles(configuration)


def empreinte_recherche(configuration: dict) -> str:
    pharow = configuration["pharow"]
    return empreinte_texte({
        "strategie": empreinte_strategie(configuration["strategie"]),
        "filtres": empreinte_filtres(pharow["filtres"]),
        "nomRecherche": rogner(texte_js(pharow.get("nomRecherche"))),
        "nomListeExclusion": rogner(texte_js(pharow.get("nomListeExclusion"))),
        "volumeResultats": pharow.get("volumeResultats", INDEFINI),
        "echantillonTemoins": [rogner(texte_js(item)) for item in pharow.get("echantillonTemoins", []) if rogner(texte_js(item))],
        "captureNom": pharow.get("captureNom", INDEFINI),
        "captureEmpreinte": pharow.get("captureEmpreinte", INDEFINI),
    })


PARENTS_GENERIQUES = {"informatique", "it", "technologie", "technologies", "services", "logiciel", "logiciels"}
# `\W` du navigateur = tout sauf [A-Za-z0-9_] : « é » y compte comme séparateur.
TERMES_ESN_TROP_LARGES = re.compile(r"(^|[^A-Za-z0-9_])(informatique|it|software|data|cloud|développement|conseil tech)([^A-Za-z0-9_]|$)", re.I)


def erreurs_strategie(strategie: dict) -> list[str]:
    lignes = strategie.get("lignes", [])
    erreurs: list[str] = []
    if not lignes:
        erreurs.append("La stratégie ne contient aucun critère.")
    if not rogner(texte_js(strategie.get("limites"))):
        erreurs.append("Les limites connues des filtres ne sont pas décrites.")
    champ = lambda ligne, cle: rogner(texte_js(ligne.get(cle)))  # noqa: E731
    if any(vrai(l.get("obligatoire")) and (not champ(l, "critereClient") or not champ(l, "parametrage") or not champ(l, "controleAval")) for l in lignes):
        erreurs.append("Chaque critère obligatoire doit préciser le besoin, le paramétrage exact et le contrôle aval.")
    principales = [l for l in lignes if vrai(l.get("obligatoire")) and l.get("categorie") == "type_entreprise" and l.get("logique") != "EXCLURE" and l.get("precision") == "granulaire"]
    if not principales:
        erreurs.append("Le type d'entreprise exige au moins une voie principale granulaire.")

    def uniquement_generique(parametrage: str) -> bool:
        valeurs = [rogner(v).lower() for v in re.split(r"[;,/|\n]+", parametrage.split(":")[-1])]
        return all(v in PARENTS_GENERIQUES for v in valeurs if v)

    if any(uniquement_generique(texte_js(l.get("parametrage"))) for l in principales):
        erreurs.append("Un secteur parent générique ne constitue pas une stratégie sectorielle granulaire.")
    if any(";" in texte_js(l.get("parametrage")) or "," in texte_js(l.get("parametrage")) for l in principales):
        erreurs.append("Les critères d'activité doivent conserver leur logique booléenne : une condition par ligne avec ET, OU ou EXCLURE.")
    if any(re.search(r"esn", texte_js(l.get("critereClient")), re.I) and TERMES_ESN_TROP_LARGES.search(texte_js(l.get("parametrage"))) for l in principales):
        erreurs.append("Une recherche ESN ne peut pas accepter un terme IT générique comme condition sectorielle.")
    return erreurs


def strategie_validee(strategie: dict) -> bool:
    return not erreurs_strategie(strategie) and vrai(strategie.get("valideeLe")) and strategie.get("empreinteValidee") == empreinte_strategie(strategie)


def recherche_validee(configuration: dict) -> bool:
    pharow = configuration["pharow"]
    temoins = [item for item in pharow.get("echantillonTemoins", []) if rogner(texte_js(item))]
    return (
        strategie_validee(configuration["strategie"])
        and bool(rogner(texte_js(pharow.get("nomRecherche"))) and rogner(texte_js(pharow.get("nomListeExclusion"))))
        and len(pharow["filtres"]) > 0
        and all(rogner(f["recherche"]) and rogner(f["libelle"]) and len(f["valeurs"]) > 0 for f in pharow["filtres"])
        and est_fini(pharow.get("volumeResultats")) and pharow["volumeResultats"] > 0
        and 5 <= len(temoins) <= 10
        and vrai(pharow.get("captureNom")) and vrai(pharow.get("captureEmpreinte")) and vrai(pharow.get("rechercheValideeLe"))
        and pharow.get("rechercheValideeEmpreinte") == empreinte_recherche(configuration)
    )


def _index_blacklist(lignes: list[dict], mapping: dict) -> dict[str, set]:
    index = {"linkedin": set(), "domaines": set(), "sirens": set(), "noms": set()}
    for ligne in lignes:
        lire = lambda champ: ligne.get(mapping[champ]) if vrai(mapping.get(champ)) else None  # noqa: E731
        for cle, valeur in (
            ("linkedin", normaliser_url_linkedin(lire("linkedinEntreprise"))),
            ("domaines", normaliser_domaine(lire("siteEntreprise"))),
            ("sirens", normaliser_siren(lire("siren"))),
            ("noms", normaliser_nom_entreprise(lire("entreprise"))),
        ):
            if valeur:
                index[cle].add(valeur)
    return index


def empreinte_blacklist(lignes: list[dict], mapping: dict) -> str:
    index = _index_blacklist(lignes, mapping)
    identifiants = sorted(
        [f"linkedin:{v}" for v in index["linkedin"]] + [f"domaine:{v}" for v in index["domaines"]]
        + [f"siren:{v}" for v in index["sirens"]] + [f"nom:{v}" for v in index["noms"]],
        key=lambda texte: texte.encode("utf-16-be", "surrogatepass"),
    )
    return f"fnv1a-{fnv1a(chr(10).join(identifiants))}"


def controler_prevol(configuration: dict, candidats_charges: int, blacklist_chargee: int, empreinte_bl: str | None) -> list[dict]:
    regles = [r for r in configuration["regles"] if vrai(r.get("active"))]
    pharow = configuration["pharow"]
    mapping = configuration["mappingCandidats"]
    reference = configuration["blacklist"]
    ids_valides = all(r.get("critereId") in DEFINITIONS for r in regles)
    blacklist_requise = vrai(reference.get("obligatoire")) or any(r.get("operateur") == "pas_blackliste" for r in regles)

    def intervalle_valide(regle: dict) -> bool:
        valeur = regle.get("valeur")
        return isinstance(valeur, dict) and est_fini(valeur.get("min")) and est_fini(valeur.get("max")) and valeur["min"] <= valeur["max"]

    intervalles = all(intervalle_valide(r) for r in regles if r.get("operateur") == "entre")
    listes = all(isinstance(r.get("valeur"), list) and len(r["valeur"]) > 0 for r in regles if r.get("operateur") in ("est_dans", "contient_un", "ne_contient_aucun", "n_est_pas_dans"))
    champs_regles = [DEFINITIONS[r["critereId"]][0] for r in regles if r.get("critereId") in DEFINITIONS and DEFINITIONS[r["critereId"]][0]]
    # Une mission d'origine « cible » vient d'un fichier importé, qui n'a pas toutes les colonnes : seuls les champs obligatoires doivent
    # être reliés. Une règle dont la colonne manque suit sa politique `siAbsent` (voir `_evaluer_regle`).
    cible = origine_est_cible(configuration)
    champs_a_relier = set(configuration["champsObligatoires"]) if cible else set(configuration["champsObligatoires"]) | set(champs_regles)
    mappings_complets = all(vrai(mapping.get(champ)) for champ in champs_a_relier)
    mapping_blacklist = any(vrai(v) for v in configuration["mappingBlacklist"].values())
    volume = reference.get("volumeAttendu", INDEFINI)
    volume_conforme = est_nombre(volume) and volume == blacklist_chargee
    empreinte_conforme = vrai(reference.get("empreinte")) and vrai(empreinte_bl) and reference.get("empreinte") == empreinte_bl
    controle = lambda ident, libelle, valide, bloquant=True: {"id": ident, "libelle": libelle, "valide": bool(valide), "bloquant": bool(bloquant)}  # noqa: E731
    controle_configuration = controle("configuration", "Règles, colonnes finales et correspondance des colonnes du fichier candidats complètes", len(regles) > 0 and ids_valides and intervalles and listes and len(configuration["colonnesFinales"]) > 0 and mappings_complets)
    controle_blacklist = controle("blacklist", "Liste d'exclusion chargée et colonnes de correspondance définies", blacklist_chargee > 0 and mapping_blacklist, blacklist_requise)
    controle_empreinte = controle("empreinte", "Volume et signature de la liste d'exclusion cohérents", blacklist_chargee > 0 and volume_conforme and empreinte_conforme, blacklist_requise)
    controle_candidats = controle("candidats", "Fichier candidats prêt", candidats_charges > 0)
    # Une mission d'origine « cible » : la recherche chez un fournisseur n'existe pas, ses sept contrôles laissent place à un seul.
    if cible:
        return [controle("cible_validee", "Cible validée et inchangée", cible_validee(configuration)), controle_configuration, controle_blacklist, controle_empreinte, controle_candidats]
    filtres = pharow["filtres"]
    filtres_valides = len(filtres) > 0 and all(vrai(f.get("id")) and rogner(f["recherche"]) and rogner(f["libelle"]) and len(f["valeurs"]) > 0 for f in filtres)
    export_conforme = vrai(pharow.get("exportBrutNom")) and vrai(pharow.get("exportCaptureLe")) and pharow.get("exportBrutVolume", INDEFINI) == candidats_charges and est_nombre(pharow.get("exportBrutVolume")) and candidats_charges > 0
    saisis, capture = pharow.get("filtresSaisisApresExtractionLe"), pharow.get("exportCaptureLe")
    saisis_apres = vrai(saisis) and vrai(capture) and texte_js(saisis) >= texte_js(capture)
    empreinte_filtres_conforme = vrai(pharow.get("filtresConfirmesEmpreinte")) and pharow.get("filtresConfirmesEmpreinte") == empreinte_filtres(filtres)

    def couvert(filtre: dict) -> bool:
        if filtre["preuve"] == "configuration":
            return True
        liee = next((r for r in regles if r.get("id") == filtre.get("regleId") and filtre.get("regleId") is not INDEFINI), None)
        if not liee:
            return False
        if filtre["preuve"] == "calcul":
            return True
        champ = DEFINITIONS.get(liee.get("critereId"), (None,))[0]
        return bool(champ and vrai(mapping.get(champ)))

    couverts = all(couvert(f) for f in filtres if vrai(f.get("obligatoire")))
    return [
        controle("strategie", "Stratégie critère par critère validée et inchangée", strategie_validee(configuration["strategie"])),
        controle("recherche_validee", "Recherche réelle validée avec capture, volume, échantillon et limites", recherche_validee(configuration)),
        controle_configuration,
        controle_blacklist,
        controle_empreinte,
        controle("liste_pharow", "Bonne recherche et bonne liste d'exclusion Pharow renseignées", rogner(texte_js(pharow.get("nomRecherche"))) and rogner(texte_js(pharow.get("nomListeExclusion")))),
        controle("exclusion_pharow", "Exclusion confirmée active dans Pharow", vrai(pharow.get("exclusionConfirmeeLe"))),
        controle("export_source", "Export brut conservé et identifié dans le navigateur", export_conforme),
        controle("filtres", "Filtres définitifs recopiés après l'extraction, confirmés et inchangés", filtres_valides and saisis_apres and vrai(pharow.get("filtresConfirmesLe")) and empreinte_filtres_conforme),
        controle("couverture_filtres", "Chaque filtre obligatoire est relié à une règle et à sa preuve", couverts),
        controle_candidats,
    ]


def prevol_autorise(controles: list[dict]) -> bool:
    return all(not c["bloquant"] or c["valide"] for c in controles)


# --------------------------------------------------------------------------- validation B2B (jumeau de validerExportB2B)


def deviner_mapping(entetes: list[str], synonymes: "OrderedDict[str, list[str]]") -> dict[str, str]:
    normalisees = {normaliser_texte(entete): entete for entete in entetes}
    resultat: dict[str, str] = {}
    for champ, noms in synonymes.items():
        exact = next((nom for nom in map(normaliser_texte, noms) if nom in normalisees), None)
        if exact is not None:
            resultat[champ] = normalisees[exact]
    return resultat


def completer_mapping(memorise: dict, entetes: list[str], synonymes) -> dict:
    """Comme la page : le mapping deviné, recouvert par le mapping mémorisé quand sa colonne existe."""
    return {**deviner_mapping(entetes, synonymes), **{champ: colonne for champ, colonne in memorise.items() if vrai(colonne) and colonne in entetes}}


def _normaliser_champ(champ: str, brut: Any) -> Any:
    if champ in ("linkedinProfil", "linkedinEntreprise"):
        return normaliser_url_linkedin(brut)
    if champ == "siteEntreprise":
        return normaliser_domaine(brut)
    if champ == "siren":
        return normaliser_siren(brut)
    if champ == "emailProfessionnel":
        return normaliser_email(brut)
    if champ == "mobile":
        return normaliser_mobile_francais(brut)
    if champ in CHAMPS_NUMERIQUES:
        nombre = lire_nombre(brut)
        return "" if nombre is None else nombre
    if champ == "categorieLinkedIn":
        return normaliser_categorie_linkedin(brut)
    return rogner(texte_js(brut))


def valeurs_normalisees(ligne: dict, mapping: dict) -> dict:
    sortie = {}
    for champ in ENTETES_CANDIDATS:
        colonne = mapping.get(champ)
        brut = ligne.get(colonne, INDEFINI) if vrai(colonne) else INDEFINI
        if brut is INDEFINI or brut is None or rogner(texte_js(brut)) == "":
            continue
        normalisee = _normaliser_champ(champ, brut)
        if normalisee != "" or est_nombre(normalisee):
            sortie[champ] = normalisee
    return sortie


def _correspondance_blacklist(valeurs: dict, index: dict) -> str | None:
    linkedin, domaine = texte_js(valeurs.get("linkedinEntreprise")), texte_js(valeurs.get("siteEntreprise"))
    siren, nom = texte_js(valeurs.get("siren")), normaliser_nom_entreprise(valeurs.get("entreprise"))
    if linkedin and linkedin in index["linkedin"]:
        return "URL LinkedIn entreprise"
    if domaine and domaine in index["domaines"]:
        return "domaine"
    if siren and siren in index["sirens"]:
        return "SIREN"
    if nom and nom in index["noms"]:
        return "nom normalisé"
    return None


def _doublon(valeurs: dict, vus: dict, ligne: int) -> int | None:
    identifiants = [
        f"linkedin:{valeurs['linkedinProfil']}" if vrai(valeurs.get("linkedinProfil")) else "",
        f"email:{valeurs['emailProfessionnel']}" if vrai(valeurs.get("emailProfessionnel")) else "",
        f"mobile:{valeurs['mobile']}" if vrai(valeurs.get("mobile")) else "",
        f"contact:{normaliser_texte(valeurs['nomComplet'])}|{normaliser_texte(valeurs['entreprise'])}" if vrai(valeurs.get("nomComplet")) and vrai(valeurs.get("entreprise")) else "",
    ]
    identifiants = [i for i in identifiants if i]
    for identifiant in identifiants:
        if identifiant in vus:
            return vus[identifiant]
    for identifiant in identifiants:
        vus[identifiant] = ligne
    return None


def _trouver_attendu(attendus: list[str], texte: str, mot_entier: bool = False) -> str | None:
    """La première expression attendue que le texte contient.

    Par défaut : un mot de trois lettres ou moins se cherche comme un mot, les autres comme une suite de lettres. Avec `mot_entier`,
    toute expression se cherche en mots entiers du texte normalisé (« make » n'est pas dans « makeup », mais dans « make com » :
    le point a été normalisé en espace). Jumelle de `trouverExpression`.
    """
    if mot_entier:
        enrobe = f" {texte} "
        return next((attendu for attendu in attendus if f" {attendu} " in enrobe), None)
    mots = set(texte.split(" "))
    for attendu in attendus:
        if (len(attendu) <= 3 and " " not in attendu and attendu in mots) or ((len(attendu) > 3 or " " in attendu) and attendu in texte):
            return attendu
    return None


def _evaluer_valeur(regle: dict, champ: str | None, normalisee: Any, blacklist: str | None, doublon: int | None) -> tuple[bool, str]:
    operateur = regle.get("operateur")
    texte = normaliser_texte(normalisee)
    if operateur == "pas_blackliste":
        return (not blacklist, f"Entreprise présente dans la liste d'exclusion ({blacklist})" if blacklist else "Entreprise absente de la liste d'exclusion")
    if operateur == "unique":
        return (not doublon, f"Doublon de la ligne {doublon}" if doublon else "Contact unique")
    if operateur == "present":
        rempli = rogner(texte_js(normalisee)) != ""
        return (rempli, "Valeur renseignée" if rempli else "Valeur absente")
    if operateur == "email_professionnel":
        ok = email_est_professionnel(normalisee)
        return (ok, "Email professionnel valide" if ok else "Email invalide ou personnel")
    if operateur == "mobile_francais":
        ok = bool(normaliser_mobile_francais(normalisee))
        return (ok, "Mobile français 06/07 valide" if ok else "Mobile français 06/07 invalide")
    if operateur == "provenance_professionnelle":
        perso = re.search(r"\b(perso|personnel|personnelle|personal|private)\b", texte) is not None
        pro = re.search(r"\b(pro|professionnel|professionnelle|work|business|company)\b", texte) is not None
        return (pro and not perso, "La provenance indique une donnée personnelle" if perso else "Provenance professionnelle confirmée" if pro else "Provenance professionnelle non démontrée")
    if operateur == "entre":
        intervalle = regle.get("valeur") if isinstance(regle.get("valeur"), dict) else {}
        borne_min, borne_max = intervalle.get("min"), intervalle.get("max")
        nombre = normalisee if est_nombre(normalisee) else lire_nombre(normalisee)
        try:
            ok = nombre is not None and float(nombre) >= float(borne_min) and float(nombre) <= float(borne_max)
        except (TypeError, ValueError):
            ok = False
        affiche = lambda v: nombre_js(v) if est_nombre(v) else texte_js(v) if v is not None else "undefined"  # noqa: E731
        lu = affiche(nombre) if nombre is not None else "valeur illisible"
        intervalle_texte = f"{affiche(borne_min)}–{affiche(borne_max)}"
        return (ok, f"{lu} est dans l'intervalle {intervalle_texte}" if ok else f"{lu} est hors de l'intervalle {intervalle_texte}")
    valeur = regle.get("valeur")
    attendus = [a for a in (normaliser_texte(v) for v in valeur) if a] if isinstance(valeur, list) else []
    if operateur == "contient_un":
        trouve = _trouver_attendu(attendus, texte, regle.get("motEntier") is True)
        return (trouve is not None, f"Correspondance : {trouve}" if trouve is not None else "Aucun mot-clé accepté")
    if operateur == "ne_contient_aucun":
        trouve = _trouver_attendu(attendus, texte, regle.get("motEntier") is True)
        return (trouve is None, f"Expression exclue : {trouve}" if trouve is not None else "Aucune expression exclue")
    if operateur == "n_est_pas_dans":
        trouve = next((attendu for attendu in attendus if texte == attendu), None)
        return (trouve is None, f"Valeur exclue : {trouve}" if trouve is not None else "Valeur absente de la liste exclue")
    assistant = champ == "poste" and re.search(r"\b(assistant|assistante|chief of staff|bras droit)\b", texte) is not None
    trouve = None
    if not assistant:
        for attendu in attendus:
            if (texte == attendu or texte.startswith(f"{attendu} ") or texte.endswith(f" {attendu}")) if champ == "poste" else texte == attendu:
                trouve = attendu
                break
    return (trouve is not None, f"Valeur acceptée : {trouve}" if trouve is not None else "Poste assistant exclu" if assistant else "Valeur hors liste")


def _evaluation(regle_id, critere_id, libelle, portee, importance, statut, brute, normalisee, colonne, raison) -> dict:
    return {
        "regleId": regle_id, "critereId": critere_id, "libelle": libelle, "portee": portee, "importance": importance,
        "statut": statut, "valeurBrute": brute, "valeurNormalisee": normalisee, "colonneSource": colonne, "raison": raison,
    }


def _evaluer_regle(regle: dict, source: dict, normalisees: dict, mapping: dict, blacklist: str | None, doublon: int | None) -> dict:
    critere_id = texte_js(regle.get("critereId"))
    definition = DEFINITIONS.get(critere_id)
    champ = definition[0] if definition else None
    colonne = (mapping.get(champ) or "") if champ else "Contrôle calculé"
    if champ and colonne:
        brute = source.get(colonne, INDEFINI)
    elif regle.get("operateur") == "pas_blackliste":
        brute = blacklist if blacklist is not None else "absent"
    elif regle.get("operateur") == "unique":
        brute = doublon if doublon is not None else "unique"
    else:
        brute = ""
    normalisee = normalisees.get(champ, "") if champ else texte_js(brute)
    importance = regle.get("importance")
    if not definition:
        return _evaluation(regle.get("id"), critere_id, critere_id, "qualite", importance, "Non évalué", texte_js(brute), texte_js(normalisee), colonne, "Critère inconnu")
    _, libelle, portee = definition
    if champ is not None and normalisee == "" and not est_nombre(normalisee):
        return _evaluation(regle.get("id"), critere_id, libelle, portee, importance, "Donnée absente", texte_js(brute), "", colonne, f"Donnée absente — politique : {texte_js(regle.get('siAbsent'))}")
    reussi, raison = _evaluer_valeur(regle, champ, normalisee, blacklist, doublon)
    return _evaluation(regle.get("id"), critere_id, libelle, portee, importance, "Réussi" if reussi else "Échoué", texte_js(brute), texte_js(normalisee), colonne, raison)


def valider_export_b2b(candidats: list[dict], blacklist: list[dict], configuration: dict) -> list[dict]:
    """Rend une ligne validée par candidat : statut, raisons, cause principale, évaluations, valeurs."""
    index = _index_blacklist(blacklist, configuration["mappingBlacklist"])
    vus: dict[str, int] = {}
    mapping = configuration["mappingCandidats"]
    politique = {r.get("id"): r.get("siAbsent") for r in reversed(configuration["regles"])}
    regles_actives = [r for r in configuration["regles"] if vrai(r.get("active"))]
    lignes = []
    for position, source in enumerate(candidats):
        numero = position + 2
        valeurs = valeurs_normalisees(source, mapping)
        trouvee = _correspondance_blacklist(valeurs, index)
        doublon = _doublon(valeurs, vus, numero)
        evaluations = []
        for champ in configuration["champsObligatoires"]:
            colonne = mapping.get(champ) or ""
            brute = source.get(colonne, INDEFINI) if colonne else ""
            presente = champ in valeurs
            evaluations.append(_evaluation(
                f"obligatoire:{champ}", f"obligatoire:{champ}", f"Champ obligatoire : {champ}",
                "contact" if champ in CHAMPS_CONTACT else "entreprise", "bloquant",
                "Réussi" if presente else "Donnée absente", texte_js(brute), texte_js(valeurs.get(champ)), colonne,
                "Champ renseigné" if presente else "Champ obligatoire absent",
            ))
        evaluations += [_evaluer_regle(r, source, valeurs, mapping, trouvee, doublon) for r in regles_actives]

        def politique_de(evaluation: dict, defaut: str) -> str:
            valeur = politique.get(evaluation["regleId"], INDEFINI)
            return defaut if valeur is None or valeur is INDEFINI else valeur

        rejette = lambda e: e["importance"] == "bloquant" and (e["statut"] in ("Échoué", "Non évalué") or (e["statut"] == "Donnée absente" and politique_de(e, "rejeter") == "rejeter"))  # noqa: E731
        fait_verifier = lambda e: e["statut"] == "Non évalué" or (e["statut"] == "Échoué" and e["importance"] == "verification") or (e["statut"] == "Donnée absente" and politique_de(e, "verifier") == "verifier")  # noqa: E731
        cause_rejet = next((e for e in evaluations if rejette(e)), None)
        cause_verification = None if cause_rejet else next((e for e in evaluations if fait_verifier(e)), None)
        statut = "Rejeté" if cause_rejet else "À vérifier" if cause_verification else "Conforme"
        lignes.append({
            "numeroLigne": numero, "statut": statut,
            "raisons": [f"{e['libelle']} : {e['raison']}" for e in evaluations if e["statut"] != "Réussi"],
            "causePrincipale": cause_rejet or cause_verification,
            "evaluations": evaluations, "valeurs": valeurs, "source": source,
        })
    return lignes


def type_probleme(ligne: dict) -> str:
    """Libellé de la cause principale, comme la colonne « Type de problème » de la page."""
    evaluation = ligne.get("causePrincipale")
    if not evaluation:
        return ""
    critere = evaluation["critereId"]
    if critere == "blacklist":
        return "Liste d'exclusion"
    if critere == "doublon":
        return "Doublon"
    if critere in ("poste", "obligatoire:poste"):
        return "Mauvais intitulé de poste"
    if critere in ("activite", "tags_source", "qualification_activite"):
        return "Entreprise à vérifier" if ligne["statut"] == "À vérifier" else "Mauvaise entreprise"
    if critere.startswith("obligatoire:"):
        return f"Champ obligatoire manquant — {re.sub(r'^Champ obligatoire[ ]*:[ ]*', '', evaluation['libelle'], flags=re.I)}"
    return evaluation["libelle"]


def bilan_b2b(lignes: list[dict]) -> dict:
    """Le bilan obligatoire après chaque passe : totaux, une cause principale par ligne non conforme."""
    comptes = Counter(ligne["statut"] for ligne in lignes)
    causes: dict[str, Counter] = {"À vérifier": Counter(), "Rejeté": Counter()}
    for ligne in lignes:
        if ligne["statut"] != "Conforme":
            causes[ligne["statut"]][type_probleme(ligne)] += 1
    conformes, a_verifier, rejetes = comptes["Conforme"], comptes["À vérifier"], comptes["Rejeté"]
    somme_causes = sum(sum(c.values()) for c in causes.values())
    return {
        "total": len(lignes), "conformes": conformes, "a_verifier": a_verifier, "rejetes": rejetes,
        "causes": {statut: dict(compteur.most_common()) for statut, compteur in causes.items()},
        "invariants": {
            "total = conformes + à vérifier + rejetés": len(lignes) == conformes + a_verifier + rejetes,
            "à vérifier + rejetés = somme des causes": a_verifier + rejetes == somme_causes,
        },
    }


# --------------------------------------------------------------------------- personas (jumeau de engine.ts)

NON_CLASSE = "Non catégorisé"
COLONNES_POSTE = ("poste", "intitule de poste", "intitule du poste", "intitule", "fonction", "job title", "title", "position")


def normaliser_intitule(texte: Any) -> str:
    if texte is None or texte is INDEFINI:
        return ""
    return rogner(sans_diacritiques(texte_js(texte)).lower())


def _mot_entier(cible: str, cle: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(cle) + r"(?![a-z0-9])", cible) is not None


def classer(intitule: Any, axe: dict, multi: bool = False) -> str:
    cible = normaliser_intitule(intitule)
    if not cible:
        return NON_CLASSE
    trouvees = []
    for regle in axe.get("regles", []):
        for mot in regle.get("motsCles", []):
            cle = normaliser_intitule(mot)
            if cle and (_mot_entier(cible, cle) if len(cle) <= 3 else cle in cible):
                trouvees.append((_defaut(regle.get("priorite"), 999), regle["categorie"]))
                break
    if not trouvees:
        return NON_CLASSE
    trouvees.sort(key=lambda t: t[0])
    if not multi:
        return trouvees[0][1]
    return ", ".join(OrderedDict.fromkeys(categorie for _, categorie in trouvees))


def segmenter(intitules: list, matrice: dict, multi: bool = False) -> dict:
    axes = matrice.get("axes", [])
    lignes, distributions, manquants = [], {a["id"]: Counter() for a in axes}, {a["id"]: Counter() for a in axes}
    for intitule in intitules:
        valeurs = {}
        for axe in axes:
            categorie = classer(intitule, axe, multi)
            valeurs[axe["id"]] = categorie
            distributions[axe["id"]][categorie] += 1
            if categorie == NON_CLASSE and normaliser_intitule(intitule):
                manquants[axe["id"]][texte_js(intitule)] += 1
        for derive in matrice.get("derives", []) or []:
            valeurs[derive["id"]] = derive.get("table", {}).get(valeurs.get(derive["depuis"], ""), _defaut(derive.get("defaut"), ""))
        lignes.append(valeurs)
    return {"lignes": lignes, "distributions": distributions, "nonClasses": manquants, "total": len(intitules)}


def deviner_colonne_poste(entetes: list[str]) -> int:
    normalisees = [normaliser_intitule(e) for e in entetes]
    for connue in COLONNES_POSTE:
        if connue in normalisees:
            return normalisees.index(connue)
    return next((i for i, e in enumerate(normalisees) if "poste" in e or "titre" in e or "title" in e), -1)


# --------------------------------------------------------------------------- fichiers


class ErreurEntree(Exception):
    """Une entrée illisible : on s'arrête avec un message clair, jamais avec une trace."""


def _cellule(valeur: Any) -> Any:
    if valeur is None:
        return ""
    if isinstance(valeur, (dt.datetime, dt.date)):
        return valeur.isoformat()
    return valeur


def lire_tableau(chemin: Path) -> tuple[list[str], list[list[Any]]]:
    """(entêtes, lignes) d'un .xlsx, .xlsm ou .csv ; les lignes vides sont ignorées, comme dans la page."""
    if not chemin.exists():
        raise ErreurEntree(f"Fichier introuvable : {chemin}")
    if chemin.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            import openpyxl
        except ImportError as erreur:  # pragma: no cover - dépend de l'environnement
            raise ErreurEntree("openpyxl est requis pour lire un fichier Excel : python -m pip install openpyxl") from erreur
        classeur = openpyxl.load_workbook(chemin, data_only=True, read_only=True)
        brutes = [[_cellule(c) for c in ligne] for ligne in classeur.worksheets[0].iter_rows(values_only=True)]
        classeur.close()
    elif chemin.suffix.lower() in {".csv", ".txt", ".tsv"}:
        octets = chemin.read_bytes()
        try:
            texte = octets.decode("utf-8-sig")
        except UnicodeDecodeError:
            texte = octets.decode("cp1252", errors="replace")
        premiere = texte.split("\n", 1)[0]
        separateur = None
        if premiere.lower().startswith("sep=") and len(premiere.strip()) >= 5:
            separateur, texte = premiere.strip()[4], texte.split("\n", 1)[1] if "\n" in texte else ""
        if separateur is None:
            separateur = max(",;\t|", key=lambda s: premiere.count(s)) if any(s in premiere for s in ",;\t|") else ","
        brutes = [list(ligne) for ligne in csv.reader(io.StringIO(texte, newline=""), delimiter=separateur)]
    else:
        raise ErreurEntree(f"Format non pris en charge : {chemin.name} (attendu .xlsx, .xlsm ou .csv)")
    lignes = [ligne for ligne in brutes if any(rogner(texte_js(c)) for c in ligne)]
    if not lignes:
        return [], []
    entetes = [texte_js(c) for c in lignes[0]]
    return entetes, lignes[1:]


def vers_objets(entetes: list[str], lignes: list[list[Any]]) -> list[dict]:
    """Comme la page : une ligne devient {entête: valeur} ; à entête répétée, la dernière colonne l'emporte."""
    return [{entete: (ligne[i] if i < len(ligne) else "") for i, entete in enumerate(entetes)} for ligne in lignes]


def securiser(valeur: Any) -> Any:
    """Neutralise une cellule qui commencerait comme une formule de tableur."""
    if isinstance(valeur, str) and re.match(r"^[=+\-@]", valeur):
        return f"'{valeur}"
    return valeur


def _openpyxl():
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError as erreur:  # pragma: no cover
        raise ErreurEntree("openpyxl est requis pour écrire le classeur : python -m pip install openpyxl") from erreur
    return openpyxl, Alignment, Font, PatternFill, get_column_letter, DataValidation, Table, TableStyleInfo


def _feuille(classeur, titre: str, nom_table: str, entetes: list[str], lignes: list[list[Any]], largeurs: list[int]):
    _, Alignment, Font, PatternFill, lettre, _, Table, TableStyleInfo = _openpyxl()
    feuille = classeur.create_sheet(titre)
    feuille.append(list(entetes))
    for ligne in lignes:
        feuille.append([c[0] if isinstance(c, tuple) else c for c in ligne])
    for numero, ligne in enumerate(lignes, start=2):
        for colonne, cellule in enumerate(ligne, start=1):
            if isinstance(cellule, tuple):
                feuille.cell(numero, colonne).hyperlink = cellule[1]
                feuille.cell(numero, colonne).style = "Hyperlink"
    if lignes:
        table = Table(displayName=nom_table, ref=f"A1:{lettre(len(entetes))}{len(lignes) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        feuille.add_table(table)
    feuille.freeze_panes = "A2"
    for index, largeur in enumerate(largeurs, start=1):
        feuille.column_dimensions[lettre(index)].width = largeur
    for cellule in feuille[1]:
        cellule.font = Font(bold=True, color="FFFFFFFF")
        cellule.fill = PatternFill("solid", fgColor="FF164E63")
        cellule.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
    feuille.row_dimensions[1].height = 30
    return feuille


ENTETES_CONTROLE_QA = [
    "Verdict", "Nom complet", "Entreprise", "Poste", "LinkedIn profil", "Catégorie LinkedIn", "Employés visibles",
    "Email professionnel", "Mobile 06/07", "Type de problème", "Pourquoi", "Source entreprise", "Source LinkedIn",
    "Contrôlé le", "N° ligne source",
]


def _lien(valeur: Any) -> Any:
    texte = rogner(texte_js(valeur))
    return (texte, texte) if re.match(r"^https?://", texte, re.I) else texte_js(securiser(texte))


def ecrire_classeur_b2b(chemin: Path, lignes: list[dict], configuration: dict, prevol: dict, mention: str) -> None:
    openpyxl, Alignment, Font, PatternFill, _, DataValidation, _, _ = _openpyxl()
    classeur = openpyxl.Workbook()
    classeur.properties.creator = "leadlint"
    classeur.remove(classeur.active)
    mapping = configuration["mappingCandidats"]
    brute = lambda ligne, champ: ligne["source"].get(mapping[champ], "") if mapping.get(champ) else ligne["valeurs"].get(champ, "")  # noqa: E731
    sur = lambda v: v if est_nombre(v) else texte_js(securiser(texte_js(v)))  # noqa: E731
    controle = [[
        l["statut"], sur(l["valeurs"].get("nomComplet", "")), sur(l["valeurs"].get("entreprise", "")), sur(l["valeurs"].get("poste", "")),
        _lien(l["valeurs"].get("linkedinProfil", "")), sur(l["valeurs"].get("categorieLinkedIn", "")), sur(l["valeurs"].get("employesVisibles", "")),
        sur(l["valeurs"].get("emailProfessionnel", "")), sur(l["valeurs"].get("mobile", "")), type_probleme(l),
        " | ".join(texte_js(securiser(r)) for r in l["raisons"]), _lien(brute(l, "siteEntreprise")), _lien(brute(l, "linkedinEntreprise")),
        None, l["numeroLigne"],
    ] for l in lignes]
    feuille = _feuille(classeur, "Contrôle QA", "ControleQA", ENTETES_CONTROLE_QA, controle, [16, 23, 25, 24, 36, 18, 18, 30, 18, 29, 56, 34, 38, 16, 16])
    validation = DataValidation(type="list", formula1='"Conforme,À vérifier,Rejeté"', allow_blank=False, showErrorMessage=True, errorTitle="Verdict invalide", error="Choisir Conforme, À vérifier ou Rejeté.")
    feuille.add_data_validation(validation)
    couleurs = {"Conforme": ("FFDCFCE7", "FF166534"), "Rejeté": ("FFFEE2E2", "FF991B1B")}
    for numero in range(2, len(lignes) + 2):
        cellule = feuille.cell(numero, 1)
        fond, texte = couleurs.get(cellule.value, ("FFFEF3C7", "FF92400E"))
        cellule.fill = PatternFill("solid", fgColor=fond)
        cellule.font = Font(bold=True, color=texte)
        cellule.alignment = Alignment(vertical="center", horizontal="center")
        validation.add(cellule)
        feuille.cell(numero, 14).number_format = "yyyy-mm-dd"
        for colonne in (11, 12, 13):
            feuille.cell(numero, colonne).alignment = Alignment(vertical="top", wrap_text=True)

    audit = [[
        l["numeroLigne"], l["statut"], e["portee"], e["libelle"], e["importance"], e["statut"], e["colonneSource"],
        sur(e["valeurBrute"]), sur(e["valeurNormalisee"]), sur(e["raison"]),
    ] for l in lignes for e in l["evaluations"]]
    _feuille(classeur, "Contrôle leadlint", "ControleLeadlint",
             ["N° ligne source", "Statut leadlint", "Portée", "Critère", "Importance", "Résultat critère", "Colonne source", "Valeur brute", "Valeur normalisée", "Diagnostic"],
             audit, [16, 20, 16, 32, 15, 20, 24, 30, 30, 48])

    bilan = bilan_b2b(lignes)
    resume = [
        ["Version du schéma", configuration.get("schemaVersion", 2)],
        ["Lignes analysées", bilan["total"]],
        ["Conformes leadlint", bilan["conformes"]],
        ["À vérifier", bilan["a_verifier"]],
        ["Rejetées", bilan["rejetes"]],
        ["Contrôle effectué le", dt.datetime.now().replace(microsecond=0)],
        ["Signature de la liste d'exclusion", texte_js(configuration["blacklist"].get("empreinte")) or "Non mémorisée"],
        *[[f"Critère — {DEFINITIONS.get(r.get('critereId'), (None, r.get('critereId')))[1]}",
           f"{LIBELLES_OPERATEURS.get(r.get('operateur'), r.get('operateur'))} | {json.dumps(r.get('valeur'), ensure_ascii=False, separators=(',', ':'))} | {r.get('importance')} | absent={r.get('siAbsent')}"]
          for r in configuration["regles"] if vrai(r.get("active"))],
        ["Contrôle avant lancement", "autorisé" if prevol["autorise"] else f"BLOQUÉ — {' ; '.join(c['libelle'] for c in prevol['controles'] if c['bloquant'] and not c['valide'])}"],
        ["Nature du passage", mention],
        ["Outil", f"leadlint {VERSION}"],
    ]
    feuille = _feuille(classeur, "Résumé", "ResumeLeadlint", ["Indicateur", "Valeur"], resume, [46, 90])
    feuille["B7"].number_format = "yyyy-mm-dd hh:mm"
    classeur.save(chemin)


def ecrire_conformes_b2b(chemin: Path, lignes: list[dict], configuration: dict) -> None:
    openpyxl = _openpyxl()[0]
    classeur = openpyxl.Workbook()
    classeur.properties.creator = "leadlint"
    classeur.remove(classeur.active)
    colonnes = configuration["colonnesFinales"]
    entetes = [c.get("libelle", c.get("champ")) for c in colonnes]
    valeurs = [[(v if est_nombre(v) else texte_js(securiser(texte_js(v)))) for v in (l["valeurs"].get(c.get("champ"), "") for c in colonnes)] for l in lignes if l["statut"] == "Conforme"]
    _feuille(classeur, "Conformes", "ConformesClient", entetes, valeurs, [34 if re.search(r"linkedin|email", e, re.I) else 22 for e in entetes])
    classeur.save(chemin)


def ecrire_personas(chemin: Path, entetes: list[str], lignes: list[list[Any]]) -> None:
    openpyxl = _openpyxl()[0]
    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = "Données qualifiées"
    feuille.append(entetes)
    for ligne in lignes:
        feuille.append([v if est_nombre(v) else texte_js(securiser(texte_js(v))) for v in ligne])
    classeur.save(chemin)


# --------------------------------------------------------------------------- commandes


def _base(chemin: Path) -> str:
    return re.sub(r"\.(xlsx|xlsm|xls|csv)$", "", chemin.name, flags=re.I) or "export"


def _ecrire_sortie(donnees: dict, en_json: bool, texte: list[str]) -> None:
    if en_json:
        sys.stdout.write(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write("\n".join(texte) + "\n")


def _charger_b2b(args) -> tuple[dict, dict, list[dict], list[dict], list[str]]:
    identite, configuration = charger_mission(args.mission)
    if configuration["mode"] != "revalidation_b2b":
        raise ErreurEntree("Cette mission est une matrice de personas : utilise la commande « personas ».")
    entetes, lignes = lire_tableau(args.export)
    if not entetes:
        raise ErreurEntree(f"{args.export.name} est vide.")
    configuration["mappingCandidats"] = completer_mapping(configuration["mappingCandidats"], entetes, ENTETES_CANDIDATS)
    blacklist: list[dict] = []
    if args.exclusions:
        entetes_bl, lignes_bl = lire_tableau(args.exclusions)
        configuration["mappingBlacklist"] = completer_mapping(configuration["mappingBlacklist"], entetes_bl, ENTETES_BLACKLIST)
        blacklist = vers_objets(entetes_bl, lignes_bl)
    return identite, configuration, vers_objets(entetes, lignes), blacklist, entetes


def _prevol(configuration: dict, candidats: list, blacklist: list) -> dict:
    empreinte = empreinte_blacklist(blacklist, configuration["mappingBlacklist"]) if blacklist else None
    controles = controler_prevol(configuration, len(candidats), len(blacklist), empreinte)
    return {"autorise": prevol_autorise(controles), "controles": controles, "empreinte_blacklist_chargee": empreinte}


def _nom_mission(identite: dict, chemin: Path) -> str:
    return " / ".join(v for v in (identite.get("client"), identite.get("mission")) if v) or chemin.stem


def commande_b2b(args) -> int:
    identite, configuration, candidats, blacklist, _ = _charger_b2b(args)
    prevol = _prevol(configuration, candidats, blacklist)
    nom = _nom_mission(identite, args.mission)
    bloques = [c for c in prevol["controles"] if c["bloquant"] and not c["valide"]]
    if not prevol["autorise"] and not args.diagnostic:
        _ecrire_sortie(
            {"outil": "leadlint", "version": VERSION, "commande": "b2b", "mission": identite, "prevol": prevol, "livre": False},
            args.json,
            [f"leadlint « {nom} » — {args.export.name}", f"Contrôle avant lancement BLOQUÉ : {len(bloques)} contrôle(s) à lever, rien n'est livré."]
            + [f"  ✗ {c['libelle']}" for c in bloques]
            + ["Pour voir les verdicts sans rien livrer : ajoute --diagnostic."],
        )
        return 2
    lignes = valider_export_b2b(candidats, blacklist, configuration)
    bilan = bilan_b2b(lignes)
    dossier = args.sortie or args.export.parent
    dossier.mkdir(parents=True, exist_ok=True)
    diagnostic = not prevol["autorise"]
    suffixe = "-diagnostic" if diagnostic else ""
    fichiers = {"controle_qa": str(dossier / f"{_base(args.export)}{suffixe}-controle-qa.xlsx")}
    mention = "Diagnostic non livrable (contrôle avant lancement non passé)" if diagnostic else "Passage livrable (contrôle avant lancement passé)"
    ecrire_classeur_b2b(Path(fichiers["controle_qa"]), lignes, configuration, prevol, mention)
    if not diagnostic:
        fichiers["conformes"] = str(dossier / f"{_base(args.export)}-conformes.xlsx")
        ecrire_conformes_b2b(Path(fichiers["conformes"]), lignes, configuration)
    sortie = {
        "outil": "leadlint", "version": VERSION, "commande": "b2b", "mission": identite,
        "prevol": prevol, "diagnostic": diagnostic, "livre": not diagnostic, **bilan, "fichiers": fichiers,
    }
    if args.lignes:
        sortie["lignes"] = [{"numeroLigne": l["numeroLigne"], "statut": l["statut"], "cause": type_probleme(l), "raisons": l["raisons"]} for l in lignes]
    causes = lambda statut: " · ".join(f"{cause} {n}" for cause, n in bilan["causes"][statut].items()) or "aucune"  # noqa: E731
    _ecrire_sortie(sortie, args.json, [
        f"leadlint « {nom} » — {args.export.name}",
        "Contrôle avant lancement : autorisé" if not diagnostic else f"Contrôle avant lancement non passé ({len(bloques)} contrôle(s)) : DIAGNOSTIC NON LIVRABLE",
        f"Lignes {bilan['total']} · conformes {bilan['conformes']} · à vérifier {bilan['a_verifier']} · rejetées {bilan['rejetes']}",
        f"Rejetées, par cause : {causes('Rejeté')}",
        f"À vérifier, par cause : {causes('À vérifier')}",
        f"Contrôles du bilan : {'justes' if all(bilan['invariants'].values()) else 'FAUX'}",
        "Écrit : " + " · ".join(fichiers.values()),
    ])
    return 0 if all(bilan["invariants"].values()) else 1


def commande_prevol(args) -> int:
    identite, configuration, candidats, blacklist, _ = _charger_b2b(args)
    prevol = _prevol(configuration, candidats, blacklist)
    _ecrire_sortie(
        {"outil": "leadlint", "version": VERSION, "commande": "prevol", "mission": identite, "prevol": prevol},
        args.json,
        [f"Contrôle avant lancement « {_nom_mission(identite, args.mission)} » : {'autorisé' if prevol['autorise'] else 'BLOQUÉ'}"]
        + [f"  {'✓' if c['valide'] else '✗' if c['bloquant'] else '·'} {c['libelle']}" for c in prevol["controles"]],
    )
    return 0 if prevol["autorise"] else 2


# Ce que la ligne de commande écrit pour chaque valeur de `fingerprints` (les clés du JSON, elles, ne changent pas).
ETIQUETTES_SIGNATURES = {
    "strategie": "Signature de la stratégie", "recherche": "Signature de la recherche", "filtres": "Signature des filtres",
    "erreurs_strategie": "Erreurs de la stratégie", "blacklist": "Signature de la liste d'exclusion", "blacklist_volume": "Lignes de la liste d'exclusion",
    "regles": "Signature des règles",
}


def commande_empreintes(args) -> int:
    identite, configuration = charger_mission(args.mission)
    if configuration["mode"] != "revalidation_b2b":
        raise ErreurEntree("Les signatures ne concernent qu'une mission B2B.")
    empreintes = {
        "strategie": empreinte_strategie(configuration["strategie"]),
        "recherche": empreinte_recherche(configuration),
        "filtres": empreinte_filtres(configuration["pharow"]["filtres"]),
        "erreurs_strategie": erreurs_strategie(configuration["strategie"]),
    }
    if origine_est_cible(configuration):  # la signature des règles n'existe que pour cette origine : les autres sorties restent celles d'avant
        empreintes["regles"] = empreinte_regles(configuration)
    if args.exclusions:
        entetes, lignes = lire_tableau(args.exclusions)
        mapping = completer_mapping(configuration["mappingBlacklist"], entetes, ENTETES_BLACKLIST)
        objets = vers_objets(entetes, lignes)
        empreintes.update({"blacklist": empreinte_blacklist(objets, mapping), "blacklist_volume": len(objets)})
    _ecrire_sortie({"outil": "leadlint", "version": VERSION, "commande": "empreintes", "mission": identite, **empreintes}, args.json,
                   [f"{ETIQUETTES_SIGNATURES[cle]} : {(' | '.join(valeur) or 'aucune') if cle == 'erreurs_strategie' else valeur}" for cle, valeur in empreintes.items()])
    return 0


def commande_personas(args) -> int:
    if args.matrice:
        _, configuration = charger_mission(args.matrice)
        if configuration["mode"] != "persona":
            raise ErreurEntree("Ce fichier est une mission B2B : utilise la commande « check ».")
        matrice, multi = configuration["matrice"], args.multi or configuration["multiCategories"]
    else:
        matrice, multi = json.loads(MATRICE_DEFAUT.read_text(encoding="utf-8")), args.multi
    entetes, lignes = lire_tableau(args.entree)
    if not entetes:
        raise ErreurEntree(f"{args.entree.name} est vide.")
    if args.colonne:
        cibles = [i for i, e in enumerate(entetes) if normaliser_intitule(e) == normaliser_intitule(args.colonne)]
        if not cibles:
            raise ErreurEntree(f"Colonne introuvable : {args.colonne!r}. Entêtes lues : {entetes}")
        index = cibles[0]
    else:
        index = deviner_colonne_poste(entetes)
        if index < 0:
            raise ErreurEntree(f"Impossible de deviner la colonne des intitulés de poste : précise-la avec -c. Entêtes lues : {entetes}")
    intitules = [ligne[index] if index < len(ligne) else "" for ligne in lignes]
    resultat = segmenter(intitules, matrice, multi)
    cles = [a["id"] for a in matrice.get("axes", [])] + [d["id"] for d in matrice.get("derives", []) or []]
    libelles = [a["libelle"] for a in matrice.get("axes", [])] + [d["libelle"] for d in matrice.get("derives", []) or []]
    destination = args.sortie or args.entree.with_name(f"{_base(args.entree)}-qualifie.xlsx")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ecrire_personas(destination, list(entetes) + libelles, [list(l) + [v.get(c, "") for c in cles] for l, v in zip(lignes, resultat["lignes"])])
    axes = []
    for axe in matrice.get("axes", []):
        distribution = resultat["distributions"][axe["id"]]
        axes.append({
            "id": axe["id"], "libelle": axe["libelle"], "classees": resultat["total"] - distribution.get(NON_CLASSE, 0),
            "distribution": dict(distribution.most_common()),
            "non_classes": dict(resultat["nonClasses"][axe["id"]].most_common(20)),
        })
    texte = [f"Colonne lue : {entetes[index]!r} — {resultat['total']} lignes"]
    for axe in axes:
        detail = " | ".join(f"{k} {v}" for k, v in axe["distribution"].items() if k != NON_CLASSE)
        texte.append(f"  {axe['libelle']} : {axe['classees']}/{resultat['total']} classées — {detail}")
        if axe["non_classes"]:
            texte.append("    non classés : " + " | ".join(f"{k} ({v})" for k, v in list(axe["non_classes"].items())[:5]))
    texte.append(f"Écrit : {destination}")
    _ecrire_sortie({"outil": "leadlint", "version": VERSION, "commande": "personas", "colonne": entetes[index], "total": resultat["total"], "axes": axes, "fichier": str(destination)}, args.json, texte)
    return 0


# --------------------------------------------------------------------------- HubSpot : propriétés et synchronisation
#
# LeadLint calcule hors de HubSpot et n'écrit que des propriétés `leadlint_*` sur les fiches de contact ; HubSpot agit ensuite par ses
# listes et ses workflows. Deux commandes : `hubspot setup` (crée le groupe et les propriétés, sans rien écraser) et `hubspot sync`
# (relit les contacts modifiés depuis le dernier passage, les classe avec le moteur ci-dessus, n'écrit que ce qui change).
# Bibliothèque standard seulement. La clé est lue dans HUBSPOT_TOKEN, jamais affichée, jamais écrite.

URL_HUBSPOT = "https://api.hubapi.com"
GROUPE_HUBSPOT = "leadlint"
VERDICTS_HUBSPOT = {"Conforme": "Conforme", "À vérifier": "A verifier", "Rejeté": "Rejete"}
CHAMPS_ECRITS_HUBSPOT = ("leadlint_segment", "leadlint_niveau", "leadlint_verdict", "leadlint_raison")
TAILLE_PAGE_HUBSPOT = 100          # fiches par page de recherche, et par appel d'écriture
PLAFOND_RECHERCHE_HUBSPOT = 10_000  # résultats au plus par recherche : la page suivante est refusée
ESSAIS_HUBSPOT = 6
ATTENTE_MAX_HUBSPOT = 120.0        # secondes ; au-delà d'un Retry-After plus long, on s'arrête avec un message
PAUSE_RECHERCHE_HUBSPOT = 0.2      # la recherche est limitée à 5 appels par seconde
RECOUVREMENT_HUBSPOT = 120         # secondes relues avant le dernier passage : une fiche modifiée pendant le passage n'est pas perdue
FICHIER_ETAT_HUBSPOT = "leadlint-hubspot-etat.json"
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

# Champ de LeadLint -> propriété de contact HubSpot. Modifiable avec --champ champ=propriete.
CORRESPONDANCE_HUBSPOT: "OrderedDict[str, str]" = OrderedDict([
    ("poste", "jobtitle"), ("emailProfessionnel", "email"), ("mobile", "mobilephone"), ("entreprise", "company"), ("pays", "country"),
    ("siteEntreprise", "website"), ("linkedinProfil", "hs_linkedin_url"), ("activite", "industry"),
    ("employesVisibles", "numemployees"), ("chiffreAffaires", "annualrevenue"),
])
NOM_COMPLET_HUBSPOT = "__nom_complet"  # clé interne : prénom et nom, assemblés ici


def _option(valeur: str, etiquette: str, ordre: int) -> dict:
    return {"label": etiquette, "value": valeur, "displayOrder": ordre, "hidden": False}


def proprietes_hubspot() -> list[dict]:
    """Les cinq propriétés de contact du groupe leadlint."""
    def texte(nom: str, etiquette: str, description: str) -> dict:
        return {"name": nom, "label": etiquette, "description": description, "groupName": GROUPE_HUBSPOT, "type": "string", "fieldType": "text"}
    return [
        texte("leadlint_segment", "LeadLint segment", "Segment retenu par LeadLint (département du poste, ou cible de la mission)."),
        texte("leadlint_niveau", "LeadLint niveau", "Niveau hiérarchique déduit du poste."),
        {
            "name": "leadlint_verdict", "label": "LeadLint verdict", "description": "Verdict de LeadLint pour la mission appliquée.",
            "groupName": GROUPE_HUBSPOT, "type": "enumeration", "fieldType": "select",
            "options": [_option("Conforme", "Conforme", 0), _option("A verifier", "À vérifier", 1), _option("Rejete", "Rejeté", 2)],
        },
        texte("leadlint_raison", "LeadLint raison", "Cause principale du verdict."),
        {"name": "leadlint_date", "label": "LeadLint date", "description": "Dernière écriture de LeadLint sur cette fiche.",
         "groupName": GROUPE_HUBSPOT, "type": "datetime", "fieldType": "date"},
    ]


class ErreurHubSpot(ErreurEntree):
    """Une erreur de HubSpot ou du réseau. Le message ne contient jamais la clé."""

    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.statut = statut


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    """Une redirection n'est jamais suivie : la clé ne part que vers l'adresse demandée."""

    def redirect_request(self, *arguments, **options):
        return None


def _maintenant() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _dormir(secondes: float) -> None:
    time.sleep(secondes)


def _iso_depuis_ms(ms: int) -> str:
    return (EPOCH + dt.timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms_depuis_texte(valeur: Any) -> int:
    """Millisecondes depuis 1970 : HubSpot rend une date ISO (ou, selon la propriété, des millisecondes en texte)."""
    texte = "" if valeur is None else str(valeur).strip()
    if texte.isdigit():
        return int(texte)
    try:
        date = dt.datetime.fromisoformat(texte.replace("Z", "+00:00"))
    except ValueError as erreur:
        raise ErreurHubSpot(f"Date illisible dans la réponse de HubSpot : {texte!r}") from erreur
    if date.tzinfo is None:
        date = date.replace(tzinfo=dt.timezone.utc)
    return (date - EPOCH) // dt.timedelta(milliseconds=1)


def _verifier_url_hubspot(url: str) -> str:
    morceaux = urlsplit(url)
    if morceaux.scheme == "https" and morceaux.hostname:
        return url.rstrip("/")
    if morceaux.scheme == "http" and morceaux.hostname in ("localhost", "127.0.0.1", "::1"):  # un essai local, jamais la clé en clair sur le réseau
        return url.rstrip("/")
    raise ErreurEntree("L'adresse de l'API HubSpot doit être en https (http seulement pour localhost).")


class ClientHubSpot:
    """Un petit client HTTP : nouvel essai sur 429, 5xx et coupure réseau, avec attente croissante."""

    def __init__(self, url: str, jeton: str) -> None:
        self.url = _verifier_url_hubspot(url)
        self._jeton = jeton
        self._derniere_recherche = 0.0
        self._ouvreur = urllib.request.build_opener(_SansRedirection)

    def appeler(self, methode: str, chemin: str, corps: Any = None, acceptes: tuple = (), recherche: bool = False) -> tuple[int, Any]:
        """Rend (statut, JSON). Un statut d'erreur lève ErreurHubSpot, sauf ceux de `acceptes`."""
        if recherche and PAUSE_RECHERCHE_HUBSPOT > 0:
            attente = self._derniere_recherche + PAUSE_RECHERCHE_HUBSPOT - time.monotonic()
            if attente > 0:
                _dormir(attente)
            self._derniere_recherche = time.monotonic()
        donnees = json.dumps(corps).encode("utf-8") if corps is not None else None
        requete = urllib.request.Request(self.url + chemin, data=donnees, method=methode, headers={
            "Authorization": f"Bearer {self._jeton}", "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": f"leadlint/{VERSION}",
        })
        for essai in range(1, ESSAIS_HUBSPOT + 1):
            retry_apres = None
            try:
                with self._ouvreur.open(requete, timeout=60) as reponse:
                    statut, brut = reponse.status, reponse.read()
            except urllib.error.HTTPError as erreur:
                statut, brut = erreur.code, erreur.read()
                valeur = erreur.headers.get("Retry-After") if erreur.headers else None
                try:
                    retry_apres = float(valeur) if valeur is not None else None
                except ValueError:
                    retry_apres = None
                erreur.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as erreur:
                statut, brut = 0, str(getattr(erreur, "reason", erreur)).encode("utf-8", "replace")
            if statut == 429 or statut >= 500 or statut == 0:
                if essai == ESSAIS_HUBSPOT:
                    raise ErreurHubSpot(f"HubSpot : {statut or 'réseau'} sur {methode} {chemin} après {ESSAIS_HUBSPOT} essais.")
                if retry_apres is not None and retry_apres > ATTENTE_MAX_HUBSPOT:
                    raise ErreurHubSpot(f"HubSpot demande d'attendre {int(retry_apres)} s (quota atteint) : le passage s'arrête, rien n'est enregistré comme fait.")
                attente = min(max(retry_apres or 0.0, float(2 ** (essai - 1))), ATTENTE_MAX_HUBSPOT)
                sys.stderr.write(f"leadlint : HubSpot {statut or 'réseau'} sur {methode} {chemin}, nouvel essai dans {attente:g} s ({essai}/{ESSAIS_HUBSPOT - 1}).\n")
                _dormir(attente)
                continue
            try:
                contenu = json.loads(brut.decode("utf-8")) if brut.strip() else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                contenu = None
            if statut >= 300 and statut not in acceptes:
                message = contenu.get("message") if isinstance(contenu, dict) and isinstance(contenu.get("message"), str) else ""
                raise ErreurHubSpot(f"HubSpot : {statut} sur {methode} {chemin}" + (f" : {message[:300]}" if message else "."), statut)
            return statut, contenu
        raise ErreurHubSpot("HubSpot : aucune réponse.")  # pragma: no cover


def _client_hubspot(args) -> ClientHubSpot:
    jeton = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not jeton:
        raise ErreurEntree("La variable d'environnement HUBSPOT_TOKEN est vide ou absente : y mettre la clé de service HubSpot (elle n'est jamais écrite dans un fichier).")
    if any(c in jeton for c in "\r\n\x00"):
        raise ErreurEntree("HUBSPOT_TOKEN contient un caractère interdit.")
    return ClientHubSpot(args.api_url, jeton)


def commande_hubspot_setup(args) -> int:
    """Crée, sans rien écraser, le groupe et les propriétés : un 409 de HubSpot veut dire « existe déjà »."""
    client = _client_hubspot(args)
    statut, _ = client.appeler("POST", "/crm/v3/properties/contacts/groups", {"name": GROUPE_HUBSPOT, "label": "LeadLint", "displayOrder": -1}, acceptes=(409,))
    groupe = "existe déjà" if statut == 409 else "créé"
    proprietes = {}
    for propriete in proprietes_hubspot():
        statut, _ = client.appeler("POST", "/crm/v3/properties/contacts", propriete, acceptes=(409,))
        proprietes[propriete["name"]] = "existe déjà" if statut == 409 else "créée"
    _ecrire_sortie(
        {"outil": "leadlint", "version": VERSION, "commande": "hubspot setup", "groupe": groupe, "proprietes": proprietes}, args.json,
        [f"Groupe {GROUPE_HUBSPOT} : {groupe}"] + [f"  {nom} : {etat}" for nom, etat in proprietes.items()],
    )
    return 0


# ----- sync

def _correspondance_hubspot(champs: list[str] | None) -> "OrderedDict[str, str]":
    correspondance = OrderedDict(CORRESPONDANCE_HUBSPOT)
    for brut in champs or []:
        champ, _, propriete = brut.partition("=")
        if champ not in ENTETES_CANDIDATS or not re.fullmatch(r"[a-z][a-z0-9_]*", propriete):
            raise ErreurEntree(f"--champ attend champ=propriete (champ de LeadLint : {', '.join(ENTETES_CANDIDATS)} ; propriété HubSpot en minuscules) : reçu {brut!r}.")
        correspondance[champ] = propriete
    return correspondance


def _axes_personas(matrice: dict) -> tuple[str, str | None]:
    """L'axe écrit dans leadlint_segment (le département) et celui de leadlint_niveau (la séniorité)."""
    axes = [a["id"] for a in matrice.get("axes", [])]
    if not axes:
        raise ErreurEntree("Cette matrice n'a aucun axe.")
    segment = "categorie" if "categorie" in axes else axes[0]
    reste = [a for a in axes if a != segment]
    niveau = "seniorite" if "seniorite" in axes and segment != "seniorite" else (reste[0] if reste else None)
    return segment, niveau


def _lire_matrice_hubspot(chemin: Path | None) -> tuple[dict, bool]:
    if chemin is None:
        return json.loads(MATRICE_DEFAUT.read_text(encoding="utf-8")), False
    _, configuration = charger_mission(chemin)
    if configuration["mode"] != "persona":
        raise ErreurEntree("Ce fichier est une mission B2B, pas une matrice de personas.")
    return configuration["matrice"], configuration["multiCategories"]


class _Classeur:
    """Ce que sync sait faire d'une page de fiches : les classer. Une seule logique, celle de `personas` ou de `check`."""

    def __init__(self, args, correspondance: "OrderedDict[str, str]") -> None:
        self.args, self.correspondance = args, correspondance
        self.configuration: dict | None = None
        self.blacklist: list[dict] = []
        self.compteurs: Counter = Counter()
        self.note: list[str] = []
        matrice_fichier = args.matrice
        if args.personas:
            self.mode = "personas"
            self.matrice, self.multi = _lire_matrice_hubspot(matrice_fichier)
            self.multi = self.multi or args.multi
        else:
            identite, configuration = charger_mission(args.mission)
            if configuration["mode"] == "persona":
                self.mode, self.matrice, self.multi = "personas", configuration["matrice"], configuration["multiCategories"] or args.multi
            else:
                self.mode = "mission"
                self.matrice, self.multi = _lire_matrice_hubspot(matrice_fichier)
                self.nom = _nom_mission(identite, args.mission)
                self.configuration = configuration
                self._preparer_mission()
        self.axe_segment, self.axe_niveau = _axes_personas(self.matrice)
        self.ecrits = ("leadlint_segment", "leadlint_niveau", "leadlint_verdict", "leadlint_raison") if self.mode == "mission" else ("leadlint_segment", "leadlint_niveau")
        if self.axe_niveau is None:
            self.ecrits = tuple(c for c in self.ecrits if c != "leadlint_niveau")
        champs = ["poste"] if self.mode == "personas" else list(self.correspondance)
        self.proprietes = list(OrderedDict.fromkeys(
            ["lastmodifieddate", *CHAMPS_ECRITS_HUBSPOT, *(("firstname", "lastname") if self.mode == "mission" else ()), *(self.correspondance[c] for c in champs if c in self.correspondance)]))
        self.signature = empreinte_texte({
            "mode": self.mode, "matrice": self.matrice, "multi": self.multi, "ecrits": list(self.ecrits), "correspondance": self.correspondance,
            "regles": self.configuration["regles"] if self.configuration else None,
            "champsObligatoires": self.configuration["champsObligatoires"] if self.configuration else None,
            "exclusions": self.empreinte_exclusions if self.configuration else None, "nom": getattr(self, "nom", None),
        })

    def _preparer_mission(self) -> None:
        """Charge la liste d'exclusion, relie les colonnes aux propriétés HubSpot et passe le contrôle avant lancement."""
        configuration, args = self.configuration, self.args
        self.empreinte_exclusions = None
        if args.exclusions:
            entetes, lignes = lire_tableau(args.exclusions)
            configuration["mappingBlacklist"] = completer_mapping(configuration["mappingBlacklist"], entetes, ENTETES_BLACKLIST)
            self.blacklist = vers_objets(entetes, lignes)
            self.empreinte_exclusions = empreinte_blacklist(self.blacklist, configuration["mappingBlacklist"]) if self.blacklist else None
        configuration["mappingCandidats"] = {"nomComplet": NOM_COMPLET_HUBSPOT, **self.correspondance}
        # Un contrôle avant lancement porte sur un fichier d'export ; une lecture de HubSpot n'en a pas : un contact tient lieu de fichier prêt.
        controles = controler_prevol(configuration, 1, len(self.blacklist), self.empreinte_exclusions)
        self.controles = controles
        self.autorise = prevol_autorise(controles)
        if not self.autorise:
            return
        # Les doublons se jugent sur toute la liste ; un passage ne voit qu'une page : cette règle est laissée de côté ici.
        self.evaluation = copy.deepcopy(configuration)
        saute = [r for r in self.evaluation["regles"] if r.get("operateur") == "unique" and vrai(r.get("active"))]
        for regle in saute:
            regle["active"] = False
        if saute:
            self.note.append("Règle des doublons non appliquée : un passage ne voit qu'une page de contacts, pas toute la base.")

    def champs_sans_propriete(self) -> list[str]:
        configuration = self.configuration or {}
        requis = set(configuration.get("champsObligatoires", [])) | {DEFINITIONS[r["critereId"]][0] for r in configuration.get("regles", []) if vrai(r.get("active")) and r.get("critereId") in DEFINITIONS and DEFINITIONS[r["critereId"]][0]}
        return sorted(c for c in requis if c != "nomComplet" and c not in self.correspondance)

    def souhaite(self, fiches: list[dict]) -> list[dict]:
        """Pour chaque fiche, les valeurs leadlint_* voulues."""
        propres = [fiche.get("properties") or {} for fiche in fiches]
        postes = [p.get(self.correspondance["poste"]) for p in propres]
        resultat = segmenter(postes, self.matrice, self.multi)["lignes"]
        voulus = []
        for valeurs in resultat:
            voulus.append({"leadlint_segment": valeurs[self.axe_segment], **({"leadlint_niveau": valeurs[self.axe_niveau]} if self.axe_niveau else {})})
        if self.mode == "personas":
            return voulus
        candidats = []
        for p in propres:
            candidat = {prop: p[prop] for prop in self.correspondance.values() if p.get(prop) not in (None, "")}
            candidat[NOM_COMPLET_HUBSPOT] = rogner(f"{p.get('firstname') or ''} {p.get('lastname') or ''}")
            candidats.append(candidat)
        for voulu, ligne in zip(voulus, valider_export_b2b(candidats, self.blacklist, self.evaluation)):
            self.compteurs[ligne["statut"]] += 1
            cause = ligne["causePrincipale"]
            voulu["leadlint_verdict"] = VERDICTS_HUBSPOT[ligne["statut"]]
            voulu["leadlint_raison"] = f"{type_probleme(ligne)} : {cause['raison']}"[:500] if cause else ""
            voulu["leadlint_segment"] = self.nom if ligne["statut"] != "Rejeté" else ""
        return voulus


def _lire_etat_hubspot(chemin: Path) -> dict | None:
    if not chemin.exists():
        return None
    try:
        etat = json.loads(chemin.read_text(encoding="utf-8"))
        assert isinstance(etat, dict) and isinstance(etat.get("dernier_passage"), str)
        _ms_depuis_texte(etat["dernier_passage"])
    except (OSError, ValueError, AssertionError) as erreur:
        raise ErreurEntree(f"Fichier d'état illisible ({chemin}) : supprime-le pour refaire un passage complet.") from erreur
    return etat


def _ecrire_etat_hubspot(chemin: Path, etat: dict) -> None:
    """Écriture en deux temps : un passage interrompu ne laisse jamais un fichier à moitié écrit."""
    temporaire = chemin.with_name(chemin.name + ".tmp")
    temporaire.write_text(json.dumps(etat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporaire, chemin)


def lire_contacts_modifies(client: ClientHubSpot, proprietes: list[str], depuis_ms: int, traiter, bilan: dict) -> None:
    """Parcourt les contacts modifiés depuis `depuis_ms`, du plus ancien au plus récent, 100 par page, et passe chaque page à `traiter`.

    Une recherche rend 10 000 résultats au plus. Au plafond, on repart de la dernière date vue ; les fiches déjà lues à cette date exacte
    sont écartées par leur numéro. Si une recherche entière ne rend rien de nouveau (plus de 10 000 fiches à la même milliseconde), on
    s'arrête avec une erreur plutôt que de sauter des fiches.
    """
    borne, deja_lus = depuis_ms, set()
    dernier_ts, ids_au_dernier_ts = None, set()
    while True:
        apres, lus, nouveaux, suite = None, 0, 0, False
        while True:
            corps = {
                "filterGroups": [{"filters": [{"propertyName": "lastmodifieddate", "operator": "GTE", "value": str(borne)}]}],
                "sorts": [{"propertyName": "lastmodifieddate", "direction": "ASCENDING"}],
                "properties": proprietes, "limit": TAILLE_PAGE_HUBSPOT,
            }
            if apres is not None:
                corps["after"] = apres
            _, reponse = client.appeler("POST", "/crm/v3/objects/contacts/search", corps, recherche=True)
            resultats = (reponse or {}).get("results") or []
            bilan["recherches"] += 1
            page = []
            for fiche in resultats:
                ts = _ms_depuis_texte((fiche.get("properties") or {}).get("lastmodifieddate"))
                lus += 1
                if ts == borne and fiche["id"] in deja_lus:
                    continue
                if dernier_ts is None or ts > dernier_ts:
                    dernier_ts, ids_au_dernier_ts = ts, set()
                if ts == dernier_ts:
                    ids_au_dernier_ts.add(fiche["id"])
                nouveaux += 1
                page.append(fiche)
            if page:
                traiter(page)
            apres = (((reponse or {}).get("paging") or {}).get("next") or {}).get("after")
            if not apres:
                break
            if lus >= PLAFOND_RECHERCHE_HUBSPOT:
                suite = True
                break
        if not suite:
            return
        if nouveaux == 0 or dernier_ts is None:
            raise ErreurHubSpot(f"Plus de {PLAFOND_RECHERCHE_HUBSPOT} contacts modifiés au même instant : impossible de les lire par pages sans en sauter. Rien n'est enregistré comme fait.")
        bilan["redemarrages"] += 1
        borne, deja_lus = dernier_ts, set(ids_au_dernier_ts)


def commande_hubspot_sync(args) -> int:
    client = _client_hubspot(args)
    correspondance = _correspondance_hubspot(args.champ)
    classeur = _Classeur(args, correspondance)
    base = {"outil": "leadlint", "version": VERSION, "commande": "hubspot sync", "mode": classeur.mode, "essai_a_blanc": bool(args.dry_run)}
    if classeur.mode == "mission" and not classeur.autorise:
        manquants = classeur.champs_sans_propriete()
        bloques = [c for c in classeur.controles if c["bloquant"] and not c["valide"]]
        _ecrire_sortie(
            {**base, "livre": False, "prevol": {"autorise": False, "controles": classeur.controles}}, args.json,
            [f"leadlint hubspot sync « {classeur.nom} » : contrôle avant lancement BLOQUÉ ({len(bloques)} contrôle(s) à lever), rien n'est lu ni écrit."]
            + [f"  ✗ {c['libelle']}" for c in bloques]
            + ([f"  Champs sans propriété HubSpot : {', '.join(manquants)} (ajouter --champ champ=propriete)."] if manquants else [])
            + ["  Une mission issue d'un export de fournisseur ne passe pas ici : utiliser une mission d'origine « cible » validée par un humain."],
        )
        return 2

    etat_chemin = args.etat or Path(FICHIER_ETAT_HUBSPOT)
    etat = _lire_etat_hubspot(etat_chemin)
    if args.tout:
        depuis_ms, depuis_texte = 0, "passage complet demandé"
    elif args.depuis:
        depuis_ms = _ms_depuis_texte(args.depuis)
        depuis_texte = f"depuis {args.depuis}"
    elif etat and etat.get("signature") == classeur.signature:
        depuis_ms = max(0, _ms_depuis_texte(etat["dernier_passage"]) - RECOUVREMENT_HUBSPOT * 1000)
        depuis_texte = f"depuis le dernier passage ({etat['dernier_passage']})"
    else:
        depuis_ms = 0
        depuis_texte = "passage complet (les règles ou la correspondance ont changé depuis le dernier passage)" if etat else "premier passage, complet"

    debut = _maintenant()
    date_ecriture = _iso_depuis_ms((debut - EPOCH) // dt.timedelta(milliseconds=1))
    bilan = {"recherches": 0, "redemarrages": 0}
    stats = {"lues": 0, "inchangees": 0}
    a_ecrire: list[tuple[str, dict]] = []

    def traiter(fiches: list[dict]) -> None:
        for fiche, voulu in zip(fiches, classeur.souhaite(fiches)):
            actuel = fiche.get("properties") or {}
            change = {nom: valeur for nom, valeur in voulu.items() if nom in classeur.ecrits and (actuel.get(nom) or "") != valeur}
            stats["lues"] += 1
            if change:
                a_ecrire.append((str(fiche["id"]), {**change, "leadlint_date": date_ecriture}))
            else:
                stats["inchangees"] += 1

    # Tout se lit avant d'écrire : écrire déplace les fiches dans le tri par date de modification et ferait sauter des pages.
    lire_contacts_modifies(client, classeur.proprietes, depuis_ms, traiter, bilan)

    ecrites, erreurs = 0, 0
    if not args.dry_run:
        for debut_lot in range(0, len(a_ecrire), TAILLE_PAGE_HUBSPOT):
            lot = a_ecrire[debut_lot:debut_lot + TAILLE_PAGE_HUBSPOT]
            try:
                _, reponse = client.appeler("POST", "/crm/v3/objects/contacts/batch/update", {"inputs": [{"id": i, "properties": p} for i, p in lot]})
            except ErreurHubSpot as erreur:
                if erreur.statut == 400:
                    raise ErreurHubSpot(f"{erreur} Les propriétés existent-elles ? Lancer d'abord « leadlint hubspot setup ».", 400) from erreur
                raise
            en_erreur = len((reponse or {}).get("errors") or []) if isinstance(reponse, dict) else 0
            erreurs += en_erreur
            ecrites += len(lot) - en_erreur
    etat_enregistre = False
    if not args.dry_run and erreurs == 0:
        _ecrire_etat_hubspot(etat_chemin, {
            "version": 1, "dernier_passage": _iso_depuis_ms((debut - EPOCH) // dt.timedelta(milliseconds=1)), "signature": classeur.signature,
            "fiches_lues": stats["lues"], "fiches_ecrites": ecrites,
        })
        etat_enregistre = True
    sortie = {
        **base, "depuis": depuis_texte, "fiches_lues": stats["lues"], "fiches_a_ecrire": len(a_ecrire), "fiches_ecrites": ecrites,
        "fiches_inchangees": stats["inchangees"], "erreurs": erreurs, "recherches": bilan["recherches"], "redemarrages_au_plafond": bilan["redemarrages"],
        "etat_enregistre": etat_enregistre, "notes": classeur.note,
    }
    if classeur.mode == "mission":
        sortie["verdicts"] = {statut: classeur.compteurs.get(statut, 0) for statut in VERDICTS_HUBSPOT}
    if args.dry_run:
        sortie["apercu"] = [{"id": i, **{k: v for k, v in p.items() if k != "leadlint_date"}} for i, p in a_ecrire[:10]]
    titre = f"leadlint hubspot sync{' « ' + classeur.nom + ' »' if classeur.mode == 'mission' else ' (personas)'} : {depuis_texte}"
    texte = [titre + (" — ESSAI À BLANC, rien n'est écrit" if args.dry_run else "")]
    texte.append(f"Contacts lus {stats['lues']} · à écrire {len(a_ecrire)} · inchangés {stats['inchangees']}" + ("" if args.dry_run else f" · écrits {ecrites} · en erreur {erreurs}"))
    if classeur.mode == "mission":
        texte.append("Verdicts : " + " · ".join(f"{statut} {classeur.compteurs.get(statut, 0)}" for statut in VERDICTS_HUBSPOT))
    if bilan["redemarrages"]:
        texte.append(f"Plafond de {PLAFOND_RECHERCHE_HUBSPOT} résultats atteint {bilan['redemarrages']} fois : recherche reprise à la dernière date vue.")
    texte += classeur.note
    if args.dry_run:
        texte += [f"  fiche {i} : " + ", ".join(f"{k}={v!r}" for k, v in p.items() if k != "leadlint_date") for i, p in a_ecrire[:10]]
        if len(a_ecrire) > 10:
            texte.append(f"  … et {len(a_ecrire) - 10} autres fiches")
    texte.append(f"État du dernier passage : {'enregistré dans ' + str(etat_chemin) if etat_enregistre else 'non modifié'}")
    _ecrire_sortie(sortie, args.json, texte)
    return 0 if erreurs == 0 else 1


class _FormateurAide(argparse.RawDescriptionHelpFormatter):
    """Le formateur d'origine, qui n'annonce que le premier nom d'une option longue.

    Les anciens noms (`--moulinette` pour `--mission`, `--blacklist` pour `--exclusions`) restent acceptés mais ne
    figurent plus dans l'aide : elle ne parle que de liste d'exclusion.
    """

    def _format_action_invocation(self, action):
        noms = action.option_strings
        action.option_strings = noms[:1] if noms and noms[0].startswith("--") else noms
        try:
            return super()._format_action_invocation(action)
        finally:
            action.option_strings = noms


class _Parseur(argparse.ArgumentParser):
    """Un message d'erreur d'usage ne cite pas non plus les anciens noms (`--mission/--moulinette`, `b2b`, `prevol`, `empreintes`)."""

    def error(self, message):
        message = re.sub(r"/--(?:moulinette|blacklist)\b", "", message)
        message = re.sub(r"\(choose from ([^)]*)\)", lambda trouve: "(choose from personas, check, preflight, fingerprints, hubspot)" if "b2b" in trouve.group(1) else trouve.group(0), message)
        super().error(message)


def _option_mission(commande) -> None:
    commande.add_argument("--mission", "--moulinette", dest="mission", metavar="MISSION", type=Path, required=True, help="mission JSON file")


def _option_exclusions(commande, aide: str) -> None:
    commande.add_argument("--exclusions", "--blacklist", dest="exclusions", metavar="FILE", type=Path, help=aide)


def construire_parseur() -> argparse.ArgumentParser:
    parseur = _Parseur(prog="leadlint", description=__doc__, formatter_class=_FormateurAide)
    parseur.add_argument("--version", action="version", version=f"leadlint {VERSION}")
    sous = parseur.add_subparsers(dest="commande", required=True, metavar="{personas,check,preflight,fingerprints,hubspot}")

    personas = sous.add_parser("personas", help="sort a column of job titles", formatter_class=_FormateurAide)
    personas.add_argument("entree", type=Path, help="file .xlsx, .xlsm or .csv")
    personas.add_argument("-m", "--matrice", type=Path, help="JSON matrix or \"persona\" mission (default: the generic matrix)")
    personas.add_argument("-o", "--sortie", type=Path, help="output file (default: <input>-qualifie.xlsx)")
    personas.add_argument("-c", "--colonne", help="exact name of the job-title column (guessed otherwise)")
    personas.add_argument("--multi", action="store_true", help="keep every category found, by priority")
    personas.add_argument("--json", action="store_true", help="JSON bilan on standard output (for an agent)")
    personas.set_defaults(action=commande_personas)

    # Noms anglais d'abord. Les anciens noms français (b2b, prevol, empreintes) construisent le même parseur, sans texte
    # d'aide : mêmes options, même action, mêmes sorties, acceptés comme avant, mais absents de `--help`.
    for nom, ancien, aide, action in (
        ("check", "b2b", "revalidate an export against a mission", commande_b2b),
        ("preflight", "prevol", "say only whether the mission may run", commande_prevol),
    ):
        for nom_commande, aide_commande in ((nom, aide), (ancien, None)):
            commande = sous.add_parser(nom_commande, formatter_class=_FormateurAide, **({"help": aide_commande} if aide_commande else {}))
            commande.add_argument("export", type=Path, help="candidates export .xlsx, .xlsm or .csv")
            _option_mission(commande)
            _option_exclusions(commande, "exclusion list .xlsx or .csv")
            commande.add_argument("--json", action="store_true", help="JSON bilan on standard output (for an agent)")
            if nom == "check":
                commande.add_argument("-o", "--sortie", type=Path, help="output folder (default: the export's folder)")
                commande.add_argument("--diagnostic", action="store_true", help="return the verdicts even if the preflight blocks; nothing deliverable")
                commande.add_argument("--lignes", action="store_true", help="add each line's verdict to the JSON bilan")
            commande.set_defaults(action=action)

    for nom_commande, aide_commande in (("fingerprints", "compute the fingerprints the mission memorises"), ("empreintes", None)):
        empreintes = sous.add_parser(nom_commande, formatter_class=_FormateurAide, **({"help": aide_commande} if aide_commande else {}))
        _option_mission(empreintes)
        _option_exclusions(empreintes, "exclusion list, for its fingerprint and volume")
        empreintes.add_argument("--json", action="store_true", help="JSON output")
        empreintes.set_defaults(action=commande_empreintes)

    hubspot = sous.add_parser("hubspot", help="write leadlint_* properties on HubSpot contacts (setup, sync)", formatter_class=_FormateurAide)
    sous_hubspot = hubspot.add_subparsers(dest="action_hubspot", required=True, metavar="{setup,sync}")
    for nom_commande, aide_commande, action in (
        ("setup", "create the leadlint property group and the five contact properties (safe to repeat)", commande_hubspot_setup),
        ("sync", "classify the contacts modified since the last pass and write only the values that change", commande_hubspot_sync),
    ):
        commande = sous_hubspot.add_parser(nom_commande, help=aide_commande, description=aide_commande + ". The key is read from the HUBSPOT_TOKEN environment variable.", formatter_class=_FormateurAide)
        commande.add_argument("--api-url", default=URL_HUBSPOT, help=f"HubSpot API address (default {URL_HUBSPOT}; http only for localhost)")
        commande.add_argument("--json", action="store_true", help="JSON bilan on standard output (for an agent)")
        if nom_commande == "sync":
            source = commande.add_mutually_exclusive_group(required=True)
            source.add_argument("--mission", "--moulinette", dest="mission", metavar="MISSION", type=Path, help="mission JSON file (a target mission validated by a human), or a personas matrix")
            source.add_argument("--personas", action="store_true", help="only sort job titles: leadlint_segment and leadlint_niveau, no verdict")
            commande.add_argument("-m", "--matrice", type=Path, help="personas matrix JSON (default: the generic matrix)")
            commande.add_argument("--multi", action="store_true", help="keep every category found, by priority")
            _option_exclusions(commande, "exclusion list .xlsx or .csv (mission only)")
            commande.add_argument("--etat", type=Path, metavar="FILE", help=f"state file holding the date of the last pass (default: ./{FICHIER_ETAT_HUBSPOT})")
            commande.add_argument("--depuis", metavar="DATE", help="read the contacts modified since this ISO date instead of the state file")
            commande.add_argument("--tout", action="store_true", help="read every contact (full pass)")
            commande.add_argument("--champ", action="append", metavar="CHAMP=PROPRIETE", help="link a LeadLint field to a HubSpot contact property (repeatable), e.g. codeNaf=naf")
            commande.add_argument("--dry-run", action="store_true", help="read and classify, write nothing, keep the state file as it is")
        commande.set_defaults(action=action)
    return parseur


def main(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = construire_parseur().parse_args(argv)
    try:
        return args.action(args)
    except ErreurEntree as erreur:
        sys.stderr.write(f"leadlint : {erreur}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
