"""Nettoyage du son avant la reconnaissance vocale : ce que Whisper reçoit doit être net et d'un niveau confortable."""
import numpy as np

RATE = 16000


def prepare(audio: np.ndarray, rate: int = RATE) -> np.ndarray:
    """float32 entre -1 et 1 → même forme, sans ronflement grave et à un niveau de parole correct.

    - enlève la composante continue et tout ce qui est sous 85 Hz (ventilateur, table qui vibre, souffle du micro) ;
    - remonte les voix lointaines ou chuchotées (gain jusqu'à ×8) sans jamais saturer ;
    - ne touche pas à un son déjà fort.
    """
    if audio.size < rate // 4:
        return audio
    a = audio.astype(np.float32) - float(audio.mean())
    spec = np.fft.rfft(a)
    spec[: int(85 * a.size / rate)] = 0
    a = np.fft.irfft(spec, n=a.size).astype(np.float32)
    # niveau de la parole = les passages forts, pas la moyenne (qui est tirée vers le bas par les silences)
    peak = float(np.percentile(np.abs(a), 99.5))
    if peak > 1e-4:
        a *= float(np.clip(0.6 / peak, 1.0, 8.0))
    return np.clip(a, -1.0, 1.0)
