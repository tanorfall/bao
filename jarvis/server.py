"""Le système nerveux de Jarvis : relie oreilles, cerveau, bouche et HUD.

    uv run python -m jarvis.server
"""
import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import sys
import time

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chirps, config, intents, memory, reflect, typer
from .brain import Brain
from .planner import Planner
from .config import CFG, LOGS, ROOT
from .ears import Ears
from .mouth import Mouth, SentenceSplitter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS / "jarvis.log")])
log = logging.getLogger("jarvis")

STOP_WORDS = re.compile(r"^\W*(stop|arrête|tais[- ]toi|silence|annule|laisse tomber)\W*$", re.I)
END_WORDS = re.compile(r"^\W*(c'est bon|merci jarvis|merci bao|à plus tard|à tout à l'heure|fin de (la )?conversation|ce sera tout)\W*$", re.I)
RESET_WORDS = re.compile(r"nouvelle conversation|on repart de zéro|efface la conversation", re.I)
# « retiens que … », « oublie que … » : la mémoire se met à jour sur place, sans attendre Claude
REMEMBER = re.compile(r"^(?:bao,?\s+)?(?:retiens|souviens-toi|rappelle-toi|n'oublie pas|mémorise|garde en mémoire|"
                      r"note dans ta mémoire|enregistre dans ta mémoire)\s*(?:bien\s*)?(?:que\s+|qu'|ça\s*:|:|,)?\s*(.{3,})$", re.I)
FORGET = re.compile(r"^(?:bao,?\s+)?oublie\s+(?:que\s+|qu'|le fait que\s+|ce que je t'ai dit sur\s+|tout ce qui concerne\s+)?(.{3,})$", re.I)
# signes que BAO s'est trompé : on en tire une leçon plus vite
CORRECTION = re.compile(r"^(non\b|nan\b)|\b(pas ça|c'est faux|t'as faux|tu t'es trompé|tu te trompes|mal compris|"
                        r"t'as pas compris|tu n'as pas compris|ça marche pas|ça ne marche pas|c'est pas ce que|"
                        r"recommence|n'importe quoi|c'est nul|pas comme ça)\b", re.I)


def limit_message(text: str) -> str:
    """« You've hit your session limit · resets 1pm » → phrase parlée avec l'heure de retour."""
    who = CFG["address_as"]
    m = re.search(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        when = f"{h} h {m.group(2)}" if m.group(2) and m.group(2) != "00" else f"{h} h"
        return f"{who}, j'ai atteint la limite de ton abonnement Claude. Je ne peux plus réfléchir avant {when}."
    return f"{who}, j'ai atteint la limite de ton abonnement Claude. Je reviens dès qu'elle se réinitialise."


class Jarvis:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.history: list[dict] = []
        self.state = "boot"
        self.voice_on = True
        self.brain = Brain()
        self.mouth = Mouth(on_state=self._on_speaking)
        self.ears = Ears(on_wake=self._on_wake_thread, on_level=self._on_level_thread)
        self.planner = Planner(on_change=lambda: self.broadcast(self.planner.snapshot()), on_quest=self._on_quest)
        self.alarm: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conversation: asyncio.Task | None = None
        self.turn: asyncio.Task | None = None
        self._level = 0.0
        self.started = time.time()
        self.turns = 0
        self.unreflected: list[dict] = []   # échanges pas encore relus par la réflexion
        self.memory_notes: list[str] = []   # nouveautés de mémoire à signaler au cerveau au prochain tour
        self.reflect_task: asyncio.Task | None = None
        self.correction_seen = False

    # --- diffusion vers le HUD ---------------------------------------------------
    def broadcast(self, event: dict):
        data = json.dumps(event, ensure_ascii=False)
        for ws in list(self.clients):
            asyncio.create_task(self._send(ws, data))

    async def _send(self, ws, data):
        try:
            await ws.send_text(data)
        except Exception:
            self.clients.discard(ws)

    def set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.broadcast({"type": "state", "state": state})

    def _on_speaking(self, speaking: bool):
        self.broadcast({"type": "speaking", "on": speaking})

    def chirp(self, name: str, mood: str | None = None):
        """Petit cri de robot + humeur passagère des yeux (wink, error, surprise…)."""
        if CFG["chirps"]:
            chirps.play(name)
        if mood:
            self.broadcast({"type": "mood", "mood": mood})

    # --- callbacks du thread audio -----------------------------------------------
    def _on_level_thread(self, level: float):
        self._level = level

    def _on_wake_thread(self, text: str | None = None):
        self.loop.call_soon_threadsafe(self.wake, text)

    # --- conversation vocale ---------------------------------------------------
    def wake(self, text: str | None = None):
        """« Bao » ou bouton micro : coupe ce qui se passe et écoute (ou traite tout de suite « Bao, fais X »)."""
        self.silence_alarm()
        if self.conversation and not self.conversation.done():
            if self.state == "listening":
                return
            self.conversation.cancel()
        self.conversation = asyncio.create_task(self._converse(text))

    async def listen_utterance(self, onset: float) -> str | None:
        """Écoute une demande ENTIÈRE. None si personne n'a parlé, "" si rien de clair.

        Quand tu fais une pause en réfléchissant (2-3 s), l'enregistrement s'arrête, mais BAO écoute encore un instant :
        si tu reprends, les morceaux sont recollés en une seule demande au lieu d'arriver en fragments.
        """
        audio = await asyncio.to_thread(self.ears.record, onset)
        if audio is None:
            return None
        parts = []
        for _ in range(5):
            longish = len(audio) >= CFG["continue_min_audio"] * 16000
            # on transcrit ce morceau PENDANT qu'on écoute la suite : sinon ce que tu dis maintenant serait perdu
            tr = asyncio.create_task(asyncio.to_thread(self.ears.transcribe, audio))
            more = None
            if longish and CFG["continue_seconds"] > 0:
                more = await asyncio.to_thread(self.ears.record, CFG["continue_seconds"])
            if more is None:
                self.chirp("end")                      # c'est fini pour de bon : le petit bip, puis on décode
                self.set_state("transcribing")
            parts.append(await tr)
            if more is None:
                break
            audio = more
        text = " ".join(p.strip() for p in parts if p and p.strip())
        if len(parts) > 1:
            log.info("demande recollée en %d morceaux : %s", len(parts), text)
        return text

    async def _converse(self, first: str | None = None):
        try:
            await self.mouth.shut_up()
            if self.turn and not self.turn.done():
                await self.brain.interrupt()
            self.broadcast({"type": "wake"})
            self.chirp("wake", "surprise")
            await asyncio.sleep(chirps.duration("wake"))  # le micro ne doit pas prendre le bip pour ta voix
            onset = 6.0
            asked_again = False
            while True:
                if first:
                    text, first = first, None
                else:
                    self.set_state("listening")
                    text = await self.listen_utterance(onset)
                    if text is None:
                        break
                    if not text:
                        # quelqu'un a parlé mais rien de clair : on le dit (une fois) au lieu de se taire
                        if asked_again:
                            break
                        asked_again = True
                        self.broadcast({"type": "mood", "mood": "confused"})
                        self.mouth.say(f"Pardon {CFG['address_as']}, j'ai pas bien entendu. Tu répètes ?")
                        await self.mouth.wait_idle()
                        onset = 8.0
                        continue
                self.broadcast({"type": "mood", "mood": "wink"})
                if END_WORDS.match(text):
                    self.history.append({"role": "user", "text": text, "source": "voice", "t": time.time()})
                    self.broadcast({"type": "user", "text": text, "source": "voice"})
                    await self._finish_local(f"Très bien, {CFG['address_as']}. Je reste en veille.", True)
                    break
                self.turn = asyncio.create_task(self.handle(text, source="voice"))
                await self.turn
                if STOP_WORDS.match(text):
                    break
                if not CFG["followup_seconds"]:
                    break
                onset = CFG["followup_seconds"]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("erreur pendant la conversation")
        finally:
            if asyncio.current_task() is self.conversation:
                self.ears.set_mode("wake")
                self.set_state("idle")

    # --- traitement d'une demande ---------------------------------------------
    async def handle(self, text: str, source: str = "text", speak: bool = True) -> str:
        speak = speak and self.voice_on
        self.turns += 1
        self.history.append({"role": "user", "text": text, "source": source, "t": time.time()})
        self.broadcast({"type": "user", "text": text, "source": source})
        memory.journal("user", text)
        self.unreflected.append({"role": "user", "text": text})
        if CORRECTION.search(text):
            self.correction_seen = True
            self.broadcast({"type": "mood", "mood": "confused"})

        if STOP_WORDS.match(text):
            self.silence_alarm()
            await self.mouth.shut_up()
            await self.brain.interrupt()
            return ""
        if RESET_WORDS.search(text):
            await self.brain.start(fresh=True)
            reply = f"Mémoire de conversation effacée, {CFG['address_as']}. Je repars de zéro."
            return await self._finish_local(reply, speak, source)
        # mémoire : « retiens que … » / « oublie … »
        mem = self.memory_command(text)
        if mem:
            return await self._finish_local(mem, speak, source)
        # minuteurs et tâches : traités sur place, instantanément
        local = intents.handle(text, self.planner, CFG["address_as"])
        if local:
            reply, sound = local
            self.chirp(sound, "wink")
            return await self._finish_local(reply, speak, source)

        self.ears.set_mode("barge")  # « Hey Jarvis » peut l'interrompre pendant qu'il parle
        self.set_state("thinking")
        splitter = SentenceSplitter()
        full, tools = "", []
        spoken_anything = False
        t0 = time.time()

        async def ack_if_slow():
            await asyncio.sleep(2.5)
            if speak and not spoken_anything:
                self.mouth.ack()
        ack = asyncio.create_task(ack_if_slow())
        done = {}
        try:
            prompt = text
            if self.memory_notes:  # ce qui a été appris hors de sa vue (commandes locales, réflexion)
                prompt = ("(Note système, mémoire mise à jour : " + " ; ".join(self.memory_notes) + ")\n\n" + text)
                self.memory_notes.clear()
            for attempt in range(2):
                async for ev in self.brain.ask(prompt):
                    if ev["type"] == "text":
                        full += ev["text"]
                        self.broadcast({"type": "delta", "text": ev["text"]})
                        for sentence in splitter.feed(ev["text"]):
                            if speak:
                                spoken_anything = True
                                self.mouth.say(sentence)
                                self.set_state("speaking")
                    elif ev["type"] == "message_start" and full and not full.endswith("\n"):
                        full += "\n"
                        self.broadcast({"type": "delta", "text": "\n"})
                    elif ev["type"] == "tool":
                        tools.append(ev)
                        self.set_state("working")
                        self.broadcast({"type": "tool", "name": ev["name"], "detail": ev["detail"]})
                    elif ev["type"] == "done":
                        done = ev
                # limite d'abonnement : on retente une fois sur Sonnet (souvent seule la limite d'Opus est atteinte)
                if done.get("limit") and not full and attempt == 0 and CFG["model"] != "sonnet":
                    log.warning("limite atteinte sur %s : %s — bascule sur Sonnet", CFG["model"], done["limit"])
                    CFG["model"] = "sonnet"   # jusqu'au prochain redémarrage
                    await self.brain.start()
                    if speak:
                        self.mouth.say(f"Limite d'Opus atteinte, {CFG['address_as']}. Je passe sur Sonnet.")
                    continue
                break
        finally:
            ack.cancel()
        for sentence in splitter.flush():
            if speak:
                self.mouth.say(sentence)
        if done.get("limit") and not full:
            # plus aucune réponse possible : on le dit clairement, avec l'heure de retour
            full = limit_message(done["limit"])
            self.chirp("error", "sleepy")
            if speak:
                self.mouth.say(full)
        elif not full and not done.get("error"):
            # jamais de silence : soit il a agi sans rien dire, soit il a bugué
            full = "C'est fait." if tools else f"Désolé {CFG['address_as']}, j'ai bugué. Tu peux répéter ?"
            if speak:
                self.mouth.say(full)
        elif done.get("error") and not full:
            full = f"Désolé, {CFG['address_as']}, mon cerveau a eu un raté. Je relance."
            self.chirp("error", "error")
            if speak:
                self.mouth.say(full)
        elif not speak and done:
            self.chirp("done", "wink")
        self.history.append({"role": "jarvis", "text": full, "tools": tools, "t": time.time()})
        memory.journal("bao", full, [t["name"] for t in tools])
        self.unreflected.append({"role": "bao", "text": full, "tools": tools, "error": bool(done.get("error"))})
        self.schedule_reflection()
        self.broadcast({"type": "done", "duration": round(time.time() - t0, 1),
                        "cost": done.get("cost"), "ok": done.get("ok", False)})
        if speak:
            self.set_state("speaking")
            await self.mouth.wait_idle()
        self.ears.set_mode("muted")  # la suite (relance ou veille) décide
        if source != "voice":
            self.ears.set_mode("wake")
            self.set_state("idle")
        return full

    async def _finish_local(self, reply: str, speak: bool, source: str = "voice") -> str:
        self.history.append({"role": "jarvis", "text": reply, "tools": [], "t": time.time()})
        memory.journal("bao", reply)
        self.unreflected.append({"role": "bao", "text": reply})
        self.broadcast({"type": "delta", "text": reply})
        self.broadcast({"type": "done", "duration": 0, "cost": 0, "ok": True})
        if speak:
            self.set_state("speaking")
            self.mouth.say(reply)
            await self.mouth.wait_idle()
        if source != "voice":
            self.ears.set_mode("wake")
            self.set_state("idle")
        return reply

    # --- mémoire et apprentissage ---------------------------------------------------
    def memory_command(self, text: str) -> str | None:
        who = CFG["address_as"]
        m = REMEMBER.match(text.strip())
        if m:
            fact = m.group(1).strip(" .")
            kind = memory.guess_kind(fact)
            saved = memory.add(kind, f"{who} a dit : « {fact} »")
            if not saved:
                return f"Je le savais déjà, {who}." if not memory.SECRET.search(fact) else \
                    f"Ça, je ne le garde pas, {who} : jamais de mot de passe ni de code dans ma mémoire."
            self.memory_notes.append(saved)
            self.memory_event([{"kind": kind, "text": saved}], [])
            return f"C'est gravé, {who}."
        m = FORGET.match(text.strip())
        if m:
            gone = memory.forget(m.group(1))
            if not gone:
                return f"Je ne trouve rien là-dessus dans ma mémoire, {who}."
            self.memory_notes.append(f"OUBLIÉ (ne plus en tenir compte) : {gone}")
            self.memory_event([], [gone])
            return f"C'est oublié, {who}."
        return None

    def memory_event(self, added: list[dict], removed: list[str]):
        lesson = any(a["kind"] == "lecons" for a in added)
        self.broadcast({"type": "memory", "added": added, "removed": removed, "count": memory.count(), "lesson": lesson})
        if added or removed:
            self.chirp("quest" if lesson else "ok", "excited" if lesson else "wink")

    def schedule_reflection(self):
        """Relit la conversation une fois le calme revenu (plus vite après une correction)."""
        if not CFG["learning"]:
            return
        if self.reflect_task and not self.reflect_task.done():
            self.reflect_task.cancel()
        self.reflect_task = asyncio.create_task(self._reflect_later())

    async def _reflect_later(self):
        await asyncio.sleep(20 if self.correction_seen else CFG["reflect_after_seconds"])
        while self.state not in ("idle", "boot"):  # on ne relit pas pendant qu'il travaille
            await asyncio.sleep(5)
        turns, self.unreflected, self.correction_seen = self.unreflected, [], False
        if not any(t["role"] == "bao" for t in turns):
            return
        try:
            res = await reflect.reflect(turns)
        except Exception:
            log.exception("réflexion impossible")
            self.unreflected = turns + self.unreflected  # on réessaiera au prochain calme
            return
        if res["added"] or res["removed"]:
            self.memory_notes += [a["text"] for a in res["added"]]
            self.memory_notes += [f"OUBLIÉ : {r}" for r in res["removed"]]
            self.memory_event(res["added"], res["removed"])

    def _on_quest(self, event: dict):
        """Quête accomplie (tâche cochée, session terminée) : notification façon « Système »."""
        self.broadcast(event)
        self.chirp("levelup" if event["levelup"] else "quest", "excited" if event["levelup"] else "wink")

    # --- minuteur : alarme ------------------------------------------------------
    def silence_alarm(self):
        t = self.planner.timer
        if t and t.get("ringing"):
            self.planner.stop()
        if self.alarm and not self.alarm.done():
            self.alarm.cancel()

    async def planner_loop(self):
        while True:
            await asyncio.sleep(0.2)
            rung = self.planner.tick()
            if not rung:
                continue
            if rung.get("is_break") and rung.get("resume"):
                self.planner.end_break()
                self.chirp("done", "surprise")
                if self.voice_on:
                    self.mouth.say(f"Fin de la pause, {CFG['address_as']}. On reprend.")
            else:
                self.alarm = asyncio.create_task(self._ring(rung))

    async def _ring(self, timer: dict):
        label = f" pour {timer['label']}" if timer.get("label") and not timer.get("is_break") else ""
        what = "La pause est finie" if timer.get("is_break") else f"Ton minuteur{label} est terminé"
        if self.state == "idle":
            self.ears.set_mode("barge")  # pas de faux réveil sur l'écho de l'alarme
        if self.voice_on:
            self.mouth.say(f"{CFG['address_as']}, {what}.")
        try:
            for _ in range(40):  # une minute au plus
                if not (self.planner.timer and self.planner.timer.get("ringing")):
                    break
                self.chirp("alarm", "scared")
                await asyncio.sleep(1.5)
        finally:
            if self.planner.timer and self.planner.timer.get("ringing"):
                self.planner.stop()
            if self.state == "idle":
                self.ears.set_mode("wake")

    async def ask_text(self, text: str, speak: bool = True):
        if self.conversation and not self.conversation.done():
            self.conversation.cancel()
        await self.mouth.shut_up()
        self.turn = asyncio.create_task(self.handle(text, source="text", speak=speak))
        return await self.turn

    # --- touche maintenue (⌥⌘) et dictée (⌥⌘⇧) -----------------------------------------
    async def ptt_start(self):
        if self.conversation and not self.conversation.done():
            self.conversation.cancel()
        await self.mouth.shut_up()
        if self.turn and not self.turn.done():
            await self.brain.interrupt()
        self.ears.begin_manual()
        self.chirp("wake")
        self.set_state("listening")

    async def ptt_stop(self, dictate: bool):
        if self.ears.mode != "record" or not (self.ears._rec or {}).get("manual"):
            return
        self.ears.stop_manual()
        audio = await asyncio.to_thread(self.ears.wait_manual)
        self.chirp("end")
        self.set_state("transcribing")
        text = await asyncio.to_thread(self.ears.transcribe, audio)
        if not text:
            self.chirp("error", "confused")
        elif dictate:
            pasted = await asyncio.to_thread(typer.type_text, text, CFG["dictation_enter"])
            self.chirp("ok", "wink")
            self.broadcast({"type": "dictated", "text": text, "pasted": pasted})
        else:
            self.broadcast({"type": "mood", "mood": "wink"})
            self.turn = asyncio.create_task(self.handle(text, source="voice"))
            await self.turn
        self.ears.set_mode("wake")
        self.set_state("idle")

    async def stop(self):
        await self.mouth.shut_up()
        await self.brain.interrupt()
        if self.conversation and not self.conversation.done():
            self.conversation.cancel()
        self.ears.set_mode("wake")
        self.set_state("idle")

    # --- boucles de fond --------------------------------------------------------
    async def level_loop(self):
        last = -1.0
        while True:
            await asyncio.sleep(1 / 20)
            # le niveau du micro ne sert qu'à animer les yeux pendant l'écoute : inutile le reste du temps
            if self.state == "listening" and abs(self._level - last) > 0.01:
                last = self._level
                self.broadcast({"type": "level", "v": round(self._level, 3)})

    async def stats_loop(self):
        net0 = psutil.net_io_counters()
        while True:
            await asyncio.sleep(2)
            net = psutil.net_io_counters()
            bat = psutil.sensors_battery()
            self.broadcast({
                "type": "stats",
                "state": self.state,
                "speaking": self.mouth.speaking,
                "cpu": psutil.cpu_percent(),
                "mem": psutil.virtual_memory().percent,
                "disk": psutil.disk_usage(str(ROOT)).percent,
                "battery": round(bat.percent) if bat else None,
                "plugged": bat.power_plugged if bat else None,
                "down": (net.bytes_recv - net0.bytes_recv) / 2,
                "up": (net.bytes_sent - net0.bytes_sent) / 2,
                "uptime": int(time.time() - self.started),
                "turns": self.turns,
            })
            net0 = net

    async def boot(self):
        self.loop = asyncio.get_running_loop()
        self.mouth.start()
        self.ears.start()
        await self.brain.start()
        asyncio.create_task(self.level_loop())
        asyncio.create_task(self.stats_loop())
        asyncio.create_task(self.planner_loop())
        hour = dt.datetime.now().hour
        hello = "Bonjour" if 5 <= hour < 18 else "Bonsoir"
        if needs_onboarding():
            self.set_state("idle")
            self.wake("Premier lancement : présente-toi et fais connaissance avec moi.")   # l'interview démarre toute seule
        else:
            self.mouth.say(f"{hello} {CFG['address_as']}. Tous les systèmes sont en ligne. Je suis à votre écoute.")
            self.ears.set_mode("wake")
            self.set_state("idle")
        if CFG["open_hud_on_start"]:
            start_hud()


NOTCH_PROC = None
ONBOARDED = ROOT / "data" / "onboarded"


def needs_onboarding() -> bool:
    """Premier lancement : BAO ne sait encore rien de son utilisateur, il commence par faire connaissance."""
    return not ONBOARDED.exists() and memory.count() < 3


def start_hud():
    """"notch" : petite bulle discrète dans le coin de l'écran ; "full" : grande fenêtre Chrome."""
    global NOTCH_PROC
    if CFG["hud_mode"] != "notch":
        return open_hud()
    subprocess.run(["pkill", "-f", "jarvis.notch"])
    NOTCH_PROC = subprocess.Popen([sys.executable, "-m", "jarvis.notch"], cwd=ROOT)


def open_hud():
    url = f"http://localhost:{CFG['port']}"
    chrome = "/Applications/Google Chrome.app"
    try:
        subprocess.Popen(["open", "-na", chrome, "--args", f"--app={url}", "--window-size=1440,900"])
    except Exception:
        subprocess.Popen(["open", url])


# --- application web -------------------------------------------------------------
J = Jarvis()
app = FastAPI(title="JARVIS")
app.mount("/static", StaticFiles(directory=ROOT / "hud"), name="static")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(J.boot())


@app.on_event("shutdown")
async def _shutdown():
    if NOTCH_PROC:
        NOTCH_PROC.terminate()
    await J.mouth.shut_up()
    await J.brain.stop()


@app.get("/notch")
async def notch():
    return FileResponse(ROOT / "hud" / "notch.html")


@app.get("/")
async def index():
    return FileResponse(ROOT / "hud" / "index.html")


class Ask(BaseModel):
    text: str
    speak: bool = True


@app.post("/api/ask")
async def api_ask(body: Ask):
    """Pour Raccourcis/Siri, scripts, etc. : renvoie la réponse finale."""
    return {"reply": await J.ask_text(body.text, body.speak)}


@app.post("/api/listen")
async def api_listen():
    J.wake()
    return {"ok": True}


@app.post("/api/stop")
async def api_stop():
    await J.stop()
    return {"ok": True}


class Ptt(BaseModel):
    action: str          # "start" | "stop"
    dictate: bool = False


@app.post("/api/ptt")
async def api_ptt(body: Ptt):
    """Appelé par les yeux (notch) quand ⌥⌘ est enfoncé puis relâché."""
    if body.action == "start":
        await J.ptt_start()
    else:
        task = asyncio.create_task(J.ptt_stop(body.dictate))
        task.add_done_callback(lambda t: t.cancelled() or not t.exception()
                               or log.error("touche maintenue", exc_info=t.exception()))
    return {"ok": True}


class TodoCmd(BaseModel):
    action: str              # add | toggle | done | remove | clear_done | show | hide
    text: str = ""
    id: str = ""


class TimerCmd(BaseModel):
    action: str              # start | break | stopwatch | pause | resume | toggle | extend | stop
    seconds: float = 0
    label: str = ""


def todo_command(c: TodoCmd):
    p = J.planner
    {"add": lambda: p.add_todo(c.text), "toggle": lambda: p.toggle_todo(c.id or c.text),
     "done": lambda: p.toggle_todo(c.id or c.text, True), "remove": lambda: p.remove_todo(c.id or c.text),
     "clear_done": p.clear_done, "show": lambda: p.set_show(True), "hide": lambda: p.set_show(False)}[c.action]()


def timer_command(c: TimerCmd):
    p = J.planner
    if c.action == "stop":
        J.silence_alarm()
    {"start": lambda: p.start_timer(c.seconds, c.label), "break": lambda: p.start_break(c.seconds),
     "stopwatch": lambda: p.start_stopwatch(c.label), "pause": p.pause, "resume": p.resume,
     "toggle": p.toggle_pause, "extend": lambda: p.extend(c.seconds), "stop": p.stop}[c.action]()


@app.get("/api/planner")
async def api_planner():
    """Tâches et minuteur en cours (utilisé aussi par Claude pour répondre)."""
    return J.planner.snapshot()


@app.post("/api/todo")
async def api_todo(c: TodoCmd):
    todo_command(c)
    return J.planner.snapshot()


@app.post("/api/timer")
async def api_timer(c: TimerCmd):
    timer_command(c)
    return J.planner.snapshot()


class MemoryCmd(BaseModel):
    action: str              # add | forget
    kind: str = "profil"     # profil | preferences | lecons | savoir_faire | projets
    text: str


@app.get("/api/memory")
async def api_memory():
    """Tout ce que BAO a appris (data/memory)."""
    return {"count": memory.count(), **memory.snapshot()}


@app.post("/api/memory")
async def api_memory_cmd(c: MemoryCmd):
    """Utilisé par BAO lui-même (via curl) pour retenir ou oublier quelque chose."""
    if c.action == "add":
        saved = memory.add(c.kind, c.text)
        if saved:
            J.memory_event([{"kind": c.kind, "text": saved}], [])
        return {"saved": saved}
    gone = memory.forget(c.text)
    if gone:
        J.memory_event([], [gone])
    return {"forgotten": gone}


@app.post("/api/reset")
async def api_reset():
    asyncio.create_task(J.ask_text("nouvelle conversation"))
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    return JSONResponse({"state": J.state, "session": J.brain.session_id, "turns": J.turns,
                         "model": CFG["model"], "uptime": int(time.time() - J.started)})


class ProfileCmd(BaseModel):
    address_as: str | None = None
    vocabulary_add: list[str] | None = None
    onboarded: bool | None = None


@app.post("/api/profile")
def profile_command(cmd: ProfileCmd):
    """Ce que BAO apprend en faisant connaissance : comment t'appeler, tes mots difficiles, fin de l'interview."""
    if cmd.address_as and cmd.address_as.strip():
        config.save("address_as", cmd.address_as.strip()[:40])
    if cmd.vocabulary_add:
        words = list(CFG["vocabulary"])
        for w in cmd.vocabulary_add:
            if w.strip() and w.strip().lower() not in {x.lower() for x in words}:
                words.append(w.strip()[:40])
        config.save("vocabulary", words[:60])
    if cmd.onboarded:
        ONBOARDED.parent.mkdir(exist_ok=True)
        ONBOARDED.write_text("ok")
    return {"ok": True, "address_as": CFG["address_as"], "vocabulary": CFG["vocabulary"]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    J.clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "hello", "state": J.state, "history": J.history[-40:], "voice": J.voice_on,
        "config": {k: CFG[k] for k in ("address_as", "model", "edge_voice", "wake_word_enabled",
                                       "notch_size", "sleep_after_seconds", "push_to_talk")},
    }, ensure_ascii=False))
    await ws.send_text(json.dumps(J.planner.snapshot(), ensure_ascii=False))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            if kind == "text" and msg.get("text", "").strip():
                asyncio.create_task(J.ask_text(msg["text"].strip(), msg.get("speak", True)))
            elif kind == "listen":
                J.wake()
            elif kind == "stop":
                await J.stop()
            elif kind == "voice":
                J.voice_on = bool(msg.get("on"))
                if not J.voice_on:
                    await J.mouth.shut_up()
                J.broadcast({"type": "voice", "on": J.voice_on})
            elif kind == "reset":
                asyncio.create_task(J.ask_text("nouvelle conversation"))
            elif kind == "mood":   # tester une humeur des yeux depuis n'importe quel client
                J.broadcast({"type": "mood", "mood": str(msg.get("mood", "normal"))})
            elif kind == "todo":
                todo_command(TodoCmd(**{k: v for k, v in msg.items() if k != "type"}))
            elif kind == "timer":
                timer_command(TimerCmd(**{k: v for k, v in msg.items() if k != "type"}))
    except WebSocketDisconnect:
        pass
    finally:
        J.clients.discard(ws)


def main():
    uvicorn.run(app, host="127.0.0.1", port=CFG["port"], log_level="warning")


if __name__ == "__main__":
    main()
