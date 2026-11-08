"""La mémoire de BAO : ce qu'il sait de toi, ce que tu préfères, ses leçons et ses savoir-faire.

Tout est en Markdown lisible dans data/memory/ (une ligne par souvenir) : tu peux l'ouvrir, corriger, effacer.
Le journal du jour garde la trace brute des échanges, d'où BAO tire de nouvelles leçons (voir reflect.py).
"""
import datetime as dt
import difflib
import re

from .config import CFG, DATA

MEM = DATA / "memory"
JOURNAL = MEM / "journal"

KINDS = {
    "profil": "Qui est {address_as} (faits durables)",
    "preferences": "Préférences et façons de faire de {address_as}",
    "lecons": "Leçons tirées de mes erreurs (à appliquer sans qu'on me le redise)",
    "savoir_faire": "Savoir-faire : procédures qui ont marché sur ce Mac",
    "projets": "Projets et contexte en cours",
}
LINE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$")
SECRET = re.compile(r"mot de passe|password|\bmdp\b|code (pin|secret|carte)|cvv|\b\d{13,19}\b|sk-[a-z0-9]{10,}", re.I)


def _path(kind):
    return MEM / f"{kind}.md"


def _title(kind):
    return KINDS[kind].replace("{address_as}", CFG["address_as"])


def entries(kind) -> list[tuple[str, str]]:
    p = _path(kind)
    if not p.exists():
        return []
    return [(m.group(1), m.group(2)) for m in map(LINE.match, p.read_text().splitlines()) if m]


def _write(kind, items):
    MEM.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"- [{d}] {t}" for d, t in items)
    _path(kind).write_text(f"# {_title(kind)}\n\n{body}\n" if body else f"# {_title(kind)}\n\n")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def add(kind: str, text: str) -> str | None:
    """Ajoute un souvenir. None s'il est vide, secret ou déjà connu."""
    text = re.sub(r"\s+", " ", text).strip(" .")
    if kind not in KINDS or len(text) < 3 or SECRET.search(text):
        return None
    text = text[0].upper() + text[1:]
    items = entries(kind)
    if any(_similar(text, t) > 0.82 for _, t in items):
        return None
    items.append((dt.date.today().isoformat(), text))
    _write(kind, items)
    return text


def forget(query: str) -> str | None:
    """Efface le souvenir qui correspond le mieux à la phrase donnée."""
    q = query.lower().strip(" .")
    words = {w for w in re.findall(r"\w+", q) if len(w) > 3}   # les mots qui comptent (« test », « zorglub »)
    best, best_score = None, 0.0
    for kind in KINDS:
        for i, (_, t) in enumerate(entries(kind)):
            low = t.lower()
            overlap = len(words & set(re.findall(r"\w+", low))) / len(words) if words else 0
            score = 1.0 if q in low else max(_similar(q, t), overlap)
            if score > best_score:
                best, best_score = (kind, i, t), score
    if not best or best_score < 0.55:
        return None
    kind, i, t = best
    items = entries(kind)
    del items[i]
    _write(kind, items)
    return t


def digest(max_chars: int = 7000) -> str:
    """Tout ce que BAO doit avoir en tête, pour sa consigne de démarrage."""
    parts = []
    for kind in KINDS:
        items = entries(kind)
        if items:
            parts.append(f"### {_title(kind)}\n" + "\n".join(f"- {t}" for _, t in items))
    text = "\n\n".join(parts)
    if len(text) > max_chars:  # on garde les souvenirs les plus récents de chaque rubrique
        per = max_chars // max(1, len(parts))
        text = "\n\n".join(p[-per:] for p in parts)
    return text or "(rien pour l'instant : tout reste à apprendre)"


def snapshot() -> dict:
    return {kind: [{"date": d, "text": t} for d, t in entries(kind)] for kind in KINDS}


def count() -> int:
    return sum(len(entries(k)) for k in KINDS)


def journal(role: str, text: str, tools: list[str] | None = None):
    """Trace brute de la journée (data/memory/journal/AAAA-MM-JJ.md)."""
    JOURNAL.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    who = CFG["address_as"] if role == "user" else "BAO"
    extra = f" _(outils : {', '.join(tools)})_" if tools else ""
    with open(JOURNAL / f"{now.date().isoformat()}.md", "a") as f:
        f.write(f"- {now:%H:%M} **{who}** : {text.strip()}{extra}\n")


def guess_kind(text: str) -> str:
    """Rubrique la plus probable pour un « retiens que … » dit à voix haute."""
    low = text.lower()
    if re.search(r"\b(je préfère|j'aime|je n'aime pas|j'aime pas|je déteste|toujours|jamais|appelle-moi|tutoie|"
                 r"vouvoie|parle-moi|réponds|par défaut)\b", low):
        return "preferences"
    if re.search(r"\b(projet|client|site|appli|app|deadline|livrer|maquette)\b", low):
        return "projets"
    if re.search(r"\b(quand tu|pour (ouvrir|lancer|faire)|il faut (utiliser|lancer|taper)|la commande)\b", low):
        return "savoir_faire"
    return "profil"


# --- vocabulaire : les mots de ton quotidien, donnés à Whisper pour qu'il les écrive juste ---------------------
_COMMON = {w.lower() for w in (
    "Je Tu Il Elle On Nous Vous Ils Elles Le La Les Un Une Des Du De Dans Pour Avec Sans Mais Donc Et Ou Où Si Ça Cela "
    "Ce Cet Cette Ces Mon Ma Mes Ton Ta Tes Son Sa Ses Quand Comme Pas Oui Non Ok Merci Bonjour Salut Alors Aussi Voilà "
    "Peux Veux Fais Fait Dis Dit Vas Est Sont Sera Avait Avoir Être Faire Mets Mettre Ouvre Lance Crée Cherche Ajoute "
    "Quel Quelle Quels Comment Pourquoi Combien Que Qui Quoi Tout Tous Toute Très Bien Bon Bonne Bao Boss Chrome "
    "Hier Demain Aujourd Lundi Mardi Mercredi Jeudi Vendredi Samedi Dimanche Janvier Février Mars Avril Mai Juin "
    "Juillet Août Septembre Octobre Novembre Décembre Note Système Mémoire Quête Leçon Test Notre Votre Leur Leurs"
).split()}
_TOKEN = re.compile(r"(?<![.!?]\s)(?<!^)(?<!\n)\b([A-ZÀ-Ý][\wÀ-ÿ'’.-]{2,}|[A-Z][a-z]+[A-Z][\wÀ-ÿ]+)")
_vocab_cache: tuple[float, list[str]] = (0.0, [])


def vocabulary(limit: int = 28) -> list[str]:
    """Noms propres et termes récurrents (clients, projets, logiciels), pour la consigne de Whisper."""
    import collections
    import time
    global _vocab_cache
    if time.time() - _vocab_cache[0] < 600 and _vocab_cache[1]:
        return _vocab_cache[1][:limit]
    counts: collections.Counter = collections.Counter()
    for kind in KINDS:                       # la mémoire : chaque souvenir compte double (il a été jugé durable)
        for _, text in entries(kind):
            for m in _TOKEN.finditer(" " + text):
                counts[m.group(1).strip(".-")] += 2
    if JOURNAL.exists():                     # les journaux des derniers jours : ce que tu dis vraiment
        for f in sorted(JOURNAL.glob("*.md"))[-7:]:
            for line in f.read_text().splitlines():
                if f"**{CFG['address_as']}** :" not in line:   # seulement ce que TU dis, pas les réponses de BAO
                    continue
                for m in _TOKEN.finditer(line.split(" : ", 1)[-1]):
                    counts[m.group(1).strip(".-")] += 1
    ranked = [w for w, c in counts.most_common(200)
              if c >= 3 and w.lower() not in _COMMON and not w.lower().startswith(("j'", "c'", "l'", "d'", "qu'", "n'"))]
    _vocab_cache = (time.time(), ranked)
    return ranked[:limit]
