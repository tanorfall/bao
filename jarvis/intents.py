"""Commandes rapides traitées sur place, sans attendre Claude : minuteurs et liste de tâches.

Ne reconnaît que des phrases sans ambiguïté ; tout le reste part chez Claude
(qui sait aussi piloter minuteurs et tâches via l'API locale).
"""
import re

from .planner import Planner, fmt_duration

NUMBERS = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
    "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
    "vingt": 20, "vingt-cinq": 25, "trente": 30, "quarante": 40, "quarante-cinq": 45, "cinquante": 50,
    "soixante": 60, "quatre-vingt-dix": 90,
}
NUM = r"(\d+(?:[.,]\d+)?|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + r")"
UNIT = r"(heures?|h|minutes?|min|mn|secondes?|sec|s)\b"
DURATION = re.compile(rf"{NUM}\s*{UNIT}(?:\s*(?:et\s*)?{NUM}(?:\s*{UNIT})?)?", re.I)
SPECIAL = {"une demi-heure": 1800, "demi-heure": 1800, "un quart d'heure": 900, "quart d'heure": 900,
           "trois quarts d'heure": 2700}

TIMER_WORDS = "minuteur|timer|compte à rebours|alarme"
TIMER_WORD = rf"(?:{TIMER_WORDS})"


def _num(s: str) -> float:
    s = s.lower()
    return float(NUMBERS[s]) if s in NUMBERS else float(s.replace(",", "."))


def _unit(u: str) -> int:
    u = (u or "").lower()
    return 3600 if u.startswith("h") else 1 if u.startswith("s") else 60


def parse_duration(text: str) -> float | None:
    low = text.lower()
    for k, v in SPECIAL.items():
        if k in low:
            return float(v)
    m = DURATION.search(low)
    if not m:
        return None
    total = _num(m.group(1)) * _unit(m.group(2))
    if m.group(3):  # « 1 heure 30 », « 2 minutes 30 secondes »
        second_unit = m.group(4) or ("minutes" if _unit(m.group(2)) == 3600 else "secondes")
        total += _num(m.group(3)) * _unit(second_unit)
    return total


def _clean(s: str) -> str:
    return re.sub(r"^(?:de |d'|du |des |la |le |l'|les |un |une )?", "", s.strip(" .,!?"), flags=re.I).strip()


def handle(text: str, p: Planner, address: str) -> tuple[str, str] | None:
    """Renvoie (réponse parlée, cri) si la phrase est une commande rapide, sinon None."""
    t = text.strip().rstrip(".!?").strip()
    low = t.lower()

    # --- arrêter / pause / reprise -------------------------------------------------
    if re.search(rf"\b(arrête|arrêtes|annule|stoppe|coupe|supprime)\b.*\b(minuteur|timer|chrono|chronomètre|alarme|pause)\b", low):
        return ("Minuteur arrêté." if p.stop() else "Aucun minuteur en cours."), "ok"
    if re.search(rf"\b(mets?|met)\b.*\b(minuteur|timer|chrono|chronomètre)\b.*\ben pause\b|\bpause (le|du) (minuteur|timer|chrono)", low):
        return ("Minuteur en pause." if p.pause() else "Rien à mettre en pause."), "ok"
    if re.search(r"\b(reprends|relance|redémarre|continue)\b.*\b(minuteur|timer|chrono|chronomètre)\b", low):
        return ("C'est reparti." if p.resume() else "Aucun minuteur en pause."), "ok"

    # --- ajouter du temps -------------------------------------------------------
    m = re.search(rf"\b(ajoute|rajoute)\b(.+?)\b(au|sur le|à mon|au)\s+(minuteur|timer)", low)
    if m and parse_duration(m.group(2)):
        sec = parse_duration(m.group(2))
        if p.extend(sec):
            return f"J'ajoute {fmt_duration(sec)}.", "ok"
        return "Aucun minuteur en cours.", "error"

    # --- pause de repos ---------------------------------------------------------
    m = re.search(r"\b(pause|break)\b\s+(de\s+)?(.+)", low)
    if m and not re.search(TIMER_WORD, low) and parse_duration(m.group(3)) and len(low) < 60:
        sec = parse_duration(m.group(3))
        p.start_break(sec)
        resume = " Je reprendrai ton minuteur de travail ensuite." if p.timer.get("resume") else ""
        return f"Pause de {fmt_duration(sec)}, {address}.{resume}", "ok"

    # --- chronomètre -------------------------------------------------------------
    if re.search(r"\b(lance|démarre|mets|start)\b.*\bchrono(mètre)?\b|^chrono(mètre)?$", low) and not parse_duration(low):
        p.start_stopwatch()
        return "Chrono lancé.", "ok"

    # --- minuteur ---------------------------------------------------------------
    if re.search(rf"\b({TIMER_WORDS}|chrono|rappelle-moi dans|réveille-moi dans)\b", low) or re.match(r"^(minuteur|timer)", low):
        sec = parse_duration(low)
        if sec:
            lm = re.search(r"\bpour\s+(.+)$", t, re.I)
            label = lm.group(1).strip(" .,!?")[:40] if lm else ""
            p.start_timer(sec, label)
            return f"Minuteur de {fmt_duration(sec)}{' pour ' + label if label else ''}, c'est parti.", "ok"

    # --- liste de tâches : afficher / cacher / lire -------------------------------------------
    if re.search(r"\b(montre|affiche|ouvre)\b.*\b(ma|la|mes)\s+(liste|tâches)\b", low):
        p.set_show(True)
        return "Voilà ta liste.", "ok"
    if re.search(r"\b(cache|masque|ferme|range)\b.*\b(ma|la|mes)\s+(liste|tâches)\b", low):
        p.set_show(False)
        return "Liste rangée.", "ok"
    if re.search(r"\b(lis|qu'est-ce qu'il y a|c'est quoi|quelles sont)\b.*\b(ma liste|mes tâches|la liste)\b", low):
        todo = [x["text"] for x in p.todos if not x["done"]]
        p.set_show(True)
        if not todo:
            return "Ta liste est vide, profites-en.", "ok"
        return f"Tu as {len(todo)} tâche{'s' if len(todo) > 1 else ''} : " + ", ".join(todo) + ".", "ok"
    if re.search(r"\b(vide|nettoie|efface)\b.*\b(tâches (terminées|faites)|ce qui est fait)\b", low):
        p.clear_done()
        return "J'ai retiré les tâches terminées.", "ok"

    # --- ajouter une tâche ------------------------------------------------------
    m = (re.match(r"^(?:ajoute|rajoute|note|mets|inscris)\s+(?:la tâche\s+)?(.+?)\s+(?:à|a|dans|sur)\s+(?:ma|la|mes)\s+(?:liste|tâches)(?:\s+de\s+\w+)?$", low)
         or re.match(r"^(?:nouvelle tâche|ajoute la tâche|rajoute la tâche)\s*:?\s*(.+)$", low))
    if m:
        item = _clean(t[m.start(1):m.end(1)])
        if item:
            p.add_todo(item[0].upper() + item[1:])
            return f"C'est noté : {item}.", "ok"

    # --- terminer / supprimer une tâche ---------------------------------------
    m = re.match(r"^(?:j'ai fini|j'ai terminé|c'est fait|coche|termine|marque comme fait)\s*:?\s*(?:la tâche\s+)?(.+)$", low)
    if m and p.todos:
        todo = p.find(_clean(m.group(1)))
        if todo:
            p.toggle_todo(todo["id"], True)
            return f"Bien joué, « {todo['text']} » est cochée.", "ok"
    m = re.match(r"^(?:supprime|enlève|retire|efface)\s+(?:la tâche\s+)?(.+?)(?:\s+(?:de|dans)\s+(?:ma|la)\s+liste)?$", low)
    if m and p.todos:
        todo = p.find(_clean(m.group(1)))
        if todo:
            p.remove_todo(todo["id"])
            return f"« {todo['text']} » retirée de la liste.", "ok"
    return None
