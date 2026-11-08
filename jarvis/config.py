"""Configuration de Jarvis : valeurs par défaut fusionnées avec config.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

DEFAULTS = {
    # Comment Jarvis s'adresse à toi
    "address_as": "Boss",
    # Cerveau : Claude Code via ton abonnement (aucune clé API)
    "model": "sonnet",                   # rapide et économe pour ta limite ; "opus" plus puissant mais plus gourmand
    "effort": "medium",
    # Apprentissage : après une conversation, BAO relit les échanges et met sa mémoire à jour (data/memory)
    "learning": True,
    "reflect_model": "sonnet",           # modèle qui fait la relecture (léger, ça tourne en fond)
    "reflect_after_seconds": 60,         # silence avant la relecture (20 s après une correction)
    # "auto" : Jarvis agit seul, un classifieur bloque les actions dangereuses.
    # "bypassPermissions" : aucun garde-fou (à tes risques).
    "permission_mode": "auto",
    "workspace": str(Path.home() / "Documents"),
    # Voix
    "tts_engine": "edge",                 # "edge" (voix neuronale Microsoft, internet) ou "piper" (locale, hors ligne)
    "edge_voice": "fr-FR-DeniseNeural",
    "edge_rate": "+8%",
    "say_voice": "Amélie",
    "piper_voice": "fr_FR-siwis-medium",  # voix locale (data/voices), utilisée sans internet ou si tts_engine = "piper"
    # Oreilles
    "language": "fr",
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "wake_word_enabled": True,
    "wake_word": "bao",                  # "bao" (repéré par Whisper) ou "hey_jarvis" (openWakeWord)
    "wake_threshold": 0.5,
    "silence_seconds": 1.1,
    # Comprendre le mieux possible
    "clean_audio": True,                 # nettoie le son avant Whisper (grave coupé, voix faible remontée)
    "continue_seconds": 1.8,             # après une pause, il attend encore ce temps : la suite est recollée à ta demande
    "continue_min_audio": 2.0,           # (seulement pour les phrases d'au moins 2 s : les ordres courts partent tout de suite)
    "input_device": "",                  # micro : nom ou numéro (« jarvis micros » les liste) ; vide = micro du Mac
    "vocabulary_ignore": [],             # mots repérés à ne jamais donner à Whisper (minuscules)
    "vocabulary": [],                    # tes mots à toi (noms, marques, logiciels) : BAO les apprend en faisant connaissance
    "max_listen_seconds": 30,
    "followup_seconds": 20,              # écoute une relance sans mot d'appel après une réponse
    # HUD
    "port": 7777,
    "open_hud_on_start": True,
    "hud_mode": "notch",                 # "notch" (bulle discrète dans le coin) ou "full" (grande fenêtre Chrome)
    "notch_position": "right",           # "right" (coin en haut à droite) ou "center" (sous l'encoche)
    "notch_size": "M",                   # S, M, L, XL
    "notch_theme": "glass",              # "glass" (verre liquide Apple), "pio" (yeux ronds) ou "shadow" (manga)
    # Ergonomie (façon PIO)
    "push_to_talk": True,                # maintenir ⌥⌘ pour parler, ⌥⌘⇧ pour dicter
    "dictation_enter": False,            # appuyer sur Entrée après une dictée
    "chirps": True,                      # petits sons de robot (écoute, compris, fini, erreur)
    "sleep_after_seconds": 60,           # les yeux s'endorment sans activité
}

CONFIG_PATH = ROOT / "config.json"


def save(key, value):
    """Écrit un réglage dans config.json (créé au besoin) et dans la configuration en cours."""
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg[key] = value
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    CFG[key] = value


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n")
    # un modèle vocal local peut être donné relativement au projet (data/models/...)
    local = ROOT / cfg["whisper_model"]
    if local.exists():
        cfg["whisper_model"] = str(local)
    return cfg


CFG = load()
