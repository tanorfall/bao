"""Petits cris de robot (façon PIO) : sinus avec vibrato tremblé et enveloppe courte, générés une fois en WAV."""
import subprocess
import wave

import numpy as np

from .config import DATA

RATE = 44100
SOUNDS = DATA / "sounds"

# nom → liste de (fréquence de départ, fréquence d'arrivée, durée en s)
SHAPES = {
    "wake":  [(660, 990, .07), (990, 1320, .08)],          # écoute : montant
    "end":   [(1180, 760, .09)],                            # fin d'écoute : descendant
    "ok":    [(880, 880, .05), (1320, 1320, .07)],          # compris
    "done":  [(1400, 1050, .07), (1050, 700, .11)],         # tâche finie : descendant
    "error": [(330, 250, .14), (250, 180, .16)],            # erreur : grave
    "laugh": [(900, 1250, .05), (950, 1300, .05), (1000, 1380, .07)],
    "quest": [(988, 988, .06), (1319, 1319, .11)],                               # notification « Système »
    "levelup": [(523, 523, .07), (659, 659, .07), (784, 784, .07), (1047, 1047, .09), (1568, 1568, .2)],
    "alarm":[(1568, 1568, .09), (1175, 1175, .09), (1568, 1568, .09), (1175, 1175, .12)],
}


def _render(shape) -> np.ndarray:
    parts = []
    for f0, f1, dur in shape:
        n = int(RATE * dur)
        t = np.arange(n) / RATE
        freq = np.linspace(f0, f1, n) * (1 + 0.025 * np.sin(2 * np.pi * 38 * t))  # vibrato tremblé
        phase = 2 * np.pi * np.cumsum(freq) / RATE
        env = np.minimum(1, t / 0.008) * np.exp(-3.5 * t / dur)
        parts.append(np.sin(phase) * env + 0.25 * np.sin(2 * phase) * env)
        parts.append(np.zeros(int(RATE * 0.012)))
    return np.concatenate(parts) * 0.35


def _ensure(name: str):
    path = SOUNDS / f"{name}.wav"
    if not path.exists():
        SOUNDS.mkdir(parents=True, exist_ok=True)
        pcm = (_render(SHAPES[name]) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
    return path


def play(name: str):
    """Joue sans bloquer."""
    subprocess.Popen(["afplay", "-v", "0.6", str(_ensure(name))])


def duration(name: str) -> float:
    return sum(d + 0.012 for _, _, d in SHAPES[name])
