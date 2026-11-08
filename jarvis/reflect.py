"""Auto-réflexion : après une conversation, BAO relit ce qui s'est dit et met sa mémoire à jour.

Un appel Claude séparé, sans outils ni session, extrait les faits durables, les préférences, les leçons
tirées des erreurs (quand {address_as} l'a corrigé ou qu'une action a raté) et les savoir-faire qui ont marché.
Rien n'est envoyé ailleurs qu'à Claude, via ton abonnement.
"""
import asyncio
import json
import logging
import shutil
from pathlib import Path

from . import memory
from .config import CFG, DATA

log = logging.getLogger("bao.reflect")

SCHEMA = {
    "type": "object",
    "properties": {
        "add": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": list(memory.KINDS)},
                           "text": {"type": "string"}},
            "required": ["kind", "text"]}},
        "remove": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["add", "remove"],
}

PROMPT = """Tu es la mémoire à long terme de BAO, l'assistant vocal de {address}.
Voici ce que BAO sait déjà :

{digest}

Voici les derniers échanges (les plus récents en bas) :

{transcript}

Mets la mémoire à jour. Ajoute seulement ce qui servira encore dans plusieurs jours :
- profil : faits durables sur {address} (métier, entreprise, entourage, lieux, outils).
- preferences : comment {address} veut être aidé ou servi (ton, longueur, habitudes, choix par défaut).
- lecons : chaque fois que {address} a corrigé BAO, s'est plaint, ou qu'une action a échoué, écris une règle
  courte et actionnable à l'impératif (« Quand …, fais … plutôt que … »). C'est le plus important.
- savoir_faire : une procédure précise qui a marché sur ce Mac et resservira (commande, appli, chemin).
- projets : projets en cours, avec leur dossier ou leur état.
Dans remove, recopie mot pour mot les souvenirs existants devenus faux ou contredits.
Une entrée = une phrase autonome, en français, sans date. N'ajoute rien de déjà connu, rien d'anodin,
jamais de mot de passe, de numéro de carte ou de clé. Si rien ne mérite d'être retenu, renvoie des listes vides."""


def _claude() -> str:
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


async def reflect(turns: list[dict]) -> dict:
    """Analyse les échanges et applique les changements. Renvoie {"added": [...], "removed": [...]}."""
    lines = []
    for t in turns[-30:]:
        who = CFG["address_as"] if t["role"] == "user" else "BAO"
        tools = f" [outils : {', '.join(x['name'] for x in t.get('tools', []))}]" if t.get("tools") else ""
        err = " [ÉCHEC]" if t.get("error") else ""
        lines.append(f"{who} : {t['text'][:600]}{tools}{err}")
    prompt = PROMPT.format(address=CFG["address_as"], digest=memory.digest(), transcript="\n".join(lines))
    proc = await asyncio.create_subprocess_exec(
        _claude(), "-p", "--model", CFG["reflect_model"], "--effort", "low", "--tools", "",
        "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA), prompt,
        cwd=str(DATA), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    data = json.loads(out or b"{}")
    result = data.get("structured_output") or {}
    if data.get("is_error") or not result:
        log.warning("réflexion sans résultat : %s", (data.get("result") or err.decode())[:200])
        return {"added": [], "removed": []}
    removed = [r for r in (memory.forget(x) for x in result.get("remove", [])) if r]
    added = [{"kind": a["kind"], "text": t} for a in result.get("add", [])
             if (t := memory.add(a["kind"], a["text"]))]
    log.info("réflexion : +%d / -%d souvenirs", len(added), len(removed))
    return {"added": added, "removed": removed}
