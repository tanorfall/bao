"""Les oreilles de Bao : micro permanent, mot d'appel (« Bao » via Whisper, ou « Hey Jarvis »), enregistrement."""
import collections
import logging
import re
import threading
import time

import numpy as np
import sounddevice as sd

from . import memory
from .audio import prepare
from .config import CFG

log = logging.getLogger("jarvis.ears")

RATE = 16000
FRAME = 1280  # 80 ms, la taille attendue par openWakeWord

# Phrases que Whisper invente sur du silence
HALLUCINATIONS = re.compile(
    r"sous-titr|amara\.org|merci d'avoir regardé|abonnez-vous|^\W*$|^(merci|ok|euh|hum)\W*$", re.I)

# « Bao » en début de phrase, avec les façons dont Whisper l'écrit parfois
NAME = re.compile(r"^\W*(?:(?:hey|hé|eh|coucou|ok|allô|allo)\W+)?(?:bao|baô|bahô|baho|bao|b\.\s?u\.?)(?=\W|$)\W*", re.I)


class Ears:
    def __init__(self, on_wake, on_level):
        self.on_wake = on_wake          # appelé (depuis le thread audio) quand « Hey Jarvis » est entendu
        self.on_level = on_level        # niveau micro 0..1 pour le HUD
        self.mode = "wake" if CFG["wake_word_enabled"] else "off"
        self.noise = 60.0
        self.preroll = collections.deque(maxlen=8)   # ~0,6 s gardées avant le début de la parole
        self._rec = None
        self._rec_done = threading.Event()
        self._wake_model = None
        self._last_level = 0.0
        self._whisper_ready = False
        self._whisper_lock = threading.Lock()   # MLX n'aime pas deux transcriptions en même temps
        self._spot = None                       # phrase en cours d'écoute pour repérer « Bao »
        self._spot_queue = collections.deque(maxlen=2)   # phrases à vérifier (on garde les plus récentes)
        self._spot_wakeup = threading.Event()

    # --- démarrage ------------------------------------------------------------
    def start(self):
        if CFG["wake_word_enabled"] and CFG["wake_word"] == "hey_jarvis":
            import openwakeword
            from openwakeword.model import Model
            self._wake_model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
        threading.Thread(target=self._mic_loop, daemon=True, name="mic").start()
        threading.Thread(target=self._warm_whisper, daemon=True, name="whisper-warmup").start()
        threading.Thread(target=self._spot_worker, daemon=True, name="wake-spot").start()

    def _warm_whisper(self):
        import mlx_whisper
        mlx_whisper.transcribe(np.zeros(RATE, dtype=np.float32), path_or_hf_repo=CFG["whisper_model"],
                               language=CFG["language"])
        self._whisper_ready = True
        log.info("Whisper prêt")

    # --- boucle micro -----------------------------------------------------------
    def _mic_loop(self):
        while True:
            try:
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                    device=CFG["input_device"] or None) as stream:
                    log.info("micro ouvert")
                    peak, n = 0, 0
                    while True:
                        frame, _ = stream.read(FRAME)
                        self._on_frame(frame[:, 0].copy())
                        if n < 100:  # diagnostic sur les 8 premières secondes
                            peak, n = max(peak, int(np.abs(frame).max())), n + 1
                            if n == 100:
                                if peak == 0:
                                    log.error("MICRO MUET : macOS bloque l'accès. Réglages Système → "
                                              "Confidentialité et sécurité → Micro.")
                                else:
                                    log.info("micro OK (pic %d)", peak)
            except Exception as e:
                log.error("micro indisponible : %s (nouvel essai dans 3 s)", e)
                time.sleep(3)

    def _on_frame(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        level = min(1.0, rms / 3000)
        self._last_level = level * 0.6 + self._last_level * 0.4
        self.on_level(self._last_level if self.mode != "muted" else 0.0)

        if self.mode == "record":
            self._record_frame(frame, rms)
            return
        self.preroll.append(frame)
        if self.mode in ("wake", "off") and rms < self.noise * 3:
            self.noise = 0.98 * self.noise + 0.02 * max(rms, 20)
        # "barge" : Jarvis parle, on peut l'interrompre mais avec un seuil plus strict (écho des haut-parleurs)
        if self.mode in ("wake", "barge") and self._wake_model is not None:
            score = self._wake_model.predict(frame)
            threshold = CFG["wake_threshold"] if self.mode == "wake" else max(0.85, CFG["wake_threshold"])
            if max(score.values()) >= threshold:
                self._wake_model.reset()
                log.info("mot d'appel détecté (%.2f)", max(score.values()))
                self.on_wake()
        elif self.mode in ("wake", "barge") and CFG["wake_word_enabled"]:
            self._spot_frame(frame, rms)

    # --- mot d'appel « Bao » : on découpe les phrases courtes et Whisper vérifie le début ----------
    def _spot_frame(self, frame, rms):
        # pendant qu'il parle, seuil plus haut pour ne pas se réveiller sur l'écho de sa propre voix
        threshold = max(self.noise * (3.5 if self.mode == "wake" else 8), 250)
        now = time.time()
        s = self._spot
        if s is None:
            if rms > threshold:
                self._spot = {"frames": list(self.preroll)[-4:], "loud": 1, "last": now, "t0": now}
            return
        s["frames"].append(frame)
        if rms > threshold:
            s["loud"] += 1
            s["last"] = now
        if now - s["last"] > 0.6 or now - s["t0"] > 8:
            self._spot = None
            if s["loud"] >= 3 and self._whisper_ready:
                # jamais jeté : si Whisper est occupé, la phrase attend son tour
                self._spot_queue.append(np.concatenate(s["frames"]))
                self._spot_wakeup.set()

    def _spot_worker(self):
        while True:
            self._spot_wakeup.wait()
            self._spot_wakeup.clear()
            while self._spot_queue:
                try:
                    self._check_name(self._spot_queue.popleft())
                except Exception:
                    log.exception("repérage du mot d'appel")

    def _check_name(self, audio):
        if self.mode not in ("wake", "barge"):
            return
        # « Bao » est au début : on ne transcrit que les 3 premières secondes (rapide, même sur une vidéo qui parle)
        pad = np.zeros(RATE // 4, dtype=audio.dtype)   # un peu de silence autour aide Whisper sur un mot court
        head = np.concatenate([pad, audio[: RATE * 3], pad])
        text = self.transcribe(head, quick=True)
        m = NAME.match(text)
        if not m or self.mode not in ("wake", "barge"):
            return
        if len(audio) > RATE * 3:  # « Bao, ouvre Spotify et mets… » d'un seul souffle : on prend la phrase entière
            text = self.transcribe(audio) or text
            m = NAME.match(text) or m
        rest = text[m.end():].strip(" ,.!?")
        log.info("mot d'appel « Bao » détecté : %s", text)
        self.on_wake(rest if len(rest.split()) >= 2 else None)

    # --- enregistrement ---------------------------------------------------------
    def _record_frame(self, frame, rms):
        r = self._rec
        r["frames"].append(frame)
        now = time.time()
        if r.get("manual"):  # touche maintenue : c'est le relâchement qui décide
            if now - r["t0"] > 120:
                self.stop_manual()
            return
        threshold = max(self.noise * 3.5, 250)
        if rms > threshold:
            r["speech_frames"] += 1
            if r["speech_frames"] >= 2:
                r["started"] = True
            r["last_voice"] = now
        if not r["started"] and now - r["t0"] > r["onset_timeout"]:
            r["result"] = None
            self._rec_done.set()
        elif r["started"] and (now - r["last_voice"] > CFG["silence_seconds"]
                               or now - r["t0"] > CFG["max_listen_seconds"]):
            r["result"] = np.concatenate(r["frames"])
            self._rec_done.set()

    def record(self, onset_timeout: float = 5.0) -> np.ndarray | None:
        """Bloque jusqu'à la fin de la phrase ; None si personne n'a parlé."""
        self._rec_done.clear()
        self._rec = {"frames": list(self.preroll)[-4:], "t0": time.time(), "started": False,
                     "speech_frames": 0, "last_voice": time.time(), "onset_timeout": onset_timeout,
                     "result": None}
        self.mode = "record"
        self._rec_done.wait(timeout=CFG["max_listen_seconds"] + onset_timeout + 2)
        self.mode = "muted"
        return self._rec["result"]

    def begin_manual(self):
        """Touche enfoncée : on enregistre tout de suite, jusqu'à stop_manual."""
        self._rec_done.clear()
        self._rec = {"frames": list(self.preroll)[-2:], "t0": time.time(), "manual": True, "result": None}
        self.mode = "record"

    def wait_manual(self) -> np.ndarray | None:
        self._rec_done.wait(timeout=125)
        self.mode = "muted"
        return self._rec["result"]

    def stop_manual(self):
        r = self._rec
        if r and r.get("manual") and not self._rec_done.is_set():
            r["result"] = np.concatenate(r["frames"]) if r["frames"] else None
            self._rec_done.set()

    def prompt(self) -> str:
        """Consigne donnée à Whisper : tes mots à toi (clients, projets, logiciels) s'écrivent juste."""
        base = list(CFG["vocabulary"])
        have = " ".join(base).lower()
        # les mots repérés seuls (un nom de marque seul, un nom de logiciel seul) n'apportent rien s'ils sont déjà dans un terme complet
        mined = [w for w in memory.vocabulary() if w.lower() not in have and w.lower() not in CFG["vocabulary_ignore"]]
        words = list(dict.fromkeys(base + mined))[:36]
        return f"Bao, {CFG['address_as']} te parle en français. Mots possibles : " + ", ".join(words) + "."

    def transcribe(self, audio: np.ndarray, quick: bool = False) -> str:
        """quick : repérage du mot d'appel (une seule passe, filtre strict) ; sinon une demande (plus tolérant)."""
        import mlx_whisper
        if audio is None or len(audio) < RATE * 0.4:
            return ""
        t = time.time()
        with self._whisper_lock:
            x = audio.astype(np.float32) / 32768.0
            if CFG["clean_audio"]:
                x = prepare(x)   # sans ronflement grave, voix remontée si elle est faible
            result = mlx_whisper.transcribe(
                x, path_or_hf_repo=CFG["whisper_model"],
                language=CFG["language"], condition_on_previous_text=False,
                # pas de relances en boucle sur la musique ou le bruit (c'était 17 s pour « Amen »)
                temperature=0.0 if quick else (0.0, 0.3), compression_ratio_threshold=2.2,
                initial_prompt=self.prompt())
        # on jette les segments que Whisper n'est pas sûr d'avoir entendus (bruit de fond, écho)
        # une vraie demande après « Bao » doit passer même chuchotée : filtre plus souple
        # (en repérage, la phrase doit en plus commencer par « Bao » : un mot court seul a une confiance basse)
        max_nsp, min_lp = (0.6, -1.4) if quick else (0.85, -1.4)
        segs = [s for s in result.get("segments", [])
                if s.get("no_speech_prob", 0) < max_nsp and s.get("avg_logprob", 0) > min_lp
                and s.get("compression_ratio", 0) < 2.4]   # au-delà : Whisper tourne en boucle sur du bruit
        text = " ".join(s["text"].strip() for s in segs).strip() if result.get("segments") else result["text"].strip()
        log.info("%s (%.1fs) : %s", "repérage" if quick else "transcription", time.time() - t, text)
        if HALLUCINATIONS.search(text):
            return ""
        # sur du silence, Whisper recrache parfois la liste de vocabulaire du prompt
        echo = sum(w in text for w in [*CFG["vocabulary"], "Mots possibles"])
        if echo >= 3:
            log.info("transcription ignorée (écho du vocabulaire)")
            return ""
        words = re.findall(r"\w+", text.lower())
        if len(words) >= 12 and len(set(words)) / len(words) < 0.3:   # « je ne sais pas » ×40
            log.info("transcription ignorée (boucle répétitive)")
            return ""
        return text

    def set_mode(self, mode: str):
        """wake | barge | muted | off"""
        if mode in ("wake", "barge") and not CFG["wake_word_enabled"]:
            mode = "off"
        if mode in ("wake", "barge") and self._wake_model is not None:
            self._wake_model.reset()
        if mode not in ("wake", "barge"):
            self._spot = None
        self.mode = mode
