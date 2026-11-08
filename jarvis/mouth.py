"""La bouche de Jarvis : synthèse vocale phrase par phrase, pendant que Claude écrit encore."""
import asyncio
import logging
import random
import re
import tempfile
from pathlib import Path

import edge_tts

from .config import CFG, DATA

log = logging.getLogger("jarvis.mouth")

ACKS = ["Tout de suite.", "Je m'en occupe.", "Bien reçu.", "C'est parti.", "Un instant."]


def speakable(text: str) -> str:
    """Nettoie le markdown pour que la voix ne lise que ce qui se dit."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#>|~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Accumule le flux de texte et rend des phrases complètes dès qu'elles le sont."""

    def __init__(self):
        self.buf = ""
        self.in_code = False

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out = []
        while True:
            # les blocs de code ne se lisent pas
            if self.buf.count("```") >= 2:
                self.buf = re.sub(r"```.*?```", " ", self.buf, count=1, flags=re.S)
                continue
            if "```" in self.buf:
                before = self.buf.split("```", 1)[0]
                if before.strip():
                    out.append(before)
                self.buf = self.buf[len(before):]
                break
            # première fin de phrase assez longue pour sonner naturel (ou un saut de ligne)
            m = next((m for m in re.finditer(r"[.!?…:;]\s|\n", self.buf)
                      if m.end() >= 25 or m.group() == "\n"), None)
            if not m:
                break
            out.append(self.buf[: m.end()])
            self.buf = self.buf[m.end():]
        return [s for s in (speakable(x) for x in out) if s]

    def flush(self) -> list[str]:
        rest, self.buf = speakable(re.sub(r"```.*", " ", self.buf, flags=re.S)), ""
        return [rest] if rest else []


class Mouth:
    def __init__(self, on_state=None):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.on_state = on_state or (lambda speaking: None)
        self.player: asyncio.subprocess.Process | None = None
        self.speaking = False
        self._pending = 0   # phrases reçues pas encore fini de jouer
        self._piper_voice = None
        self._gen = 0
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def say(self, text: str):
        text = speakable(text)
        if text:
            self._pending += 1
            self.queue.put_nowait((self._gen, text))

    def ack(self):
        self.say(random.choice(ACKS))

    async def shut_up(self):
        """Coupe immédiatement la parole et vide la file."""
        self._gen += 1
        while not self.queue.empty():
            self.queue.get_nowait()
            self._pending -= 1
        if self.player and self.player.returncode is None:
            self.player.kill()

    async def wait_idle(self):
        while self._pending > 0 or self.speaking:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.35)  # laisse retomber l'écho de la pièce avant de réécouter

    async def _synth(self, text: str) -> Path | None:
        # voix neuronale Microsoft (internet) → Piper (locale, hors ligne) → voix macOS (None)
        if CFG["tts_engine"] == "edge":
            try:
                out = Path(tempfile.mktemp(suffix=".mp3", prefix="jarvis-"))
                comm = edge_tts.Communicate(text, CFG["edge_voice"], rate=CFG["edge_rate"])
                await asyncio.wait_for(comm.save(str(out)), timeout=6)
                return out
            except Exception as e:
                log.warning("edge-tts indisponible (%s), repli sur Piper", e)
        try:
            return await asyncio.to_thread(self._piper, text)
        except Exception as e:
            log.warning("Piper indisponible (%s), repli sur say", e)
        return None

    def _piper(self, text: str) -> Path:
        import wave
        if self._piper_voice is None:
            from piper import PiperVoice
            self._piper_voice = PiperVoice.load(str(DATA / "voices" / f"{CFG['piper_voice']}.onnx"))
        out = Path(tempfile.mktemp(suffix=".wav", prefix="jarvis-"))
        with wave.open(str(out), "wb") as w:
            self._piper_voice.synthesize_wav(text, w)
        return out

    async def _play(self, text: str, audio: Path | None):
        if audio:
            self.player = await asyncio.create_subprocess_exec("afplay", str(audio))
        else:
            self.player = await asyncio.create_subprocess_exec("say", "-v", CFG["say_voice"], "-r", "200", text)
        await self.player.wait()
        if audio:
            audio.unlink(missing_ok=True)

    async def _loop(self):
        pending = None  # synthèse de la phrase suivante lancée pendant que la courante se joue
        while True:
            if pending is None:
                gen, text = await self.queue.get()
                pending = (gen, text, asyncio.create_task(self._synth(text)))
            gen, text, synth = pending
            pending = None
            audio = await synth
            if gen != self._gen:
                if audio:
                    audio.unlink(missing_ok=True)
                self._pending -= 1
                continue
            if not self.speaking:
                self.speaking = True
                self.on_state(True)
            if not self.queue.empty():
                g2, t2 = self.queue.get_nowait()
                pending = (g2, t2, asyncio.create_task(self._synth(t2)))
            try:
                await self._play(text, audio)
            except Exception:
                log.exception("lecture audio impossible")
            self._pending -= 1
            if pending is None and self.queue.empty():
                await asyncio.sleep(0.15)
                if self.queue.empty():
                    self.speaking = False
                    self.on_state(False)
