"""Le cerveau de Jarvis : une session Claude Code persistante pilotée en stream-json.

Passe par le binaire `claude` déjà connecté à ton abonnement : aucune clé API.
La session survit aux redémarrages (son id est gardé dans data/state.json).
"""
import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from . import memory
from .config import CFG, DATA, ROOT

log = logging.getLogger("jarvis.brain")
STATE = DATA / "state.json"
LIMIT = re.compile(r"hit your (session|usage|weekly|opus)? ?limit|usage limit|out of usage credits|limit reached|rate.limit", re.I)


def _claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def _persona() -> str:
    text = (ROOT / "jarvis" / "persona.md").read_text()
    # ce qu'il a appris jusqu'ici, relu à chaque démarrage du cerveau
    text += "\n\n## Ce que tu as appris (ta mémoire, data/memory)\n" + memory.digest()
    return text.replace("{address_as}", CFG["address_as"])


def _tool_detail(name: str, inp: dict) -> str:
    for key in ("description", "command", "file_path", "path", "url", "query", "pattern", "prompt"):
        if inp.get(key):
            return str(inp[key])[:160]
    return ""


class Brain:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = self._load_session()
        self._queue: asyncio.Queue | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._skip = 0
        self.busy = False

    # --- session ---------------------------------------------------------
    def _load_session(self):
        try:
            return json.loads(STATE.read_text()).get("session_id")
        except Exception:
            return None

    def _save_session(self, sid):
        self.session_id = sid
        STATE.write_text(json.dumps({"session_id": sid}))

    async def start(self, fresh: bool = False):
        await self.stop()
        self._skip = 0
        if fresh:
            self.session_id = None
        args = [
            _claude_bin(), "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--model", CFG["model"],
            "--effort", CFG["effort"],
            "--fallback-model", "opus" if CFG["model"] == "sonnet" else "sonnet",  # secours : l'autre modèle
            "--permission-mode", CFG["permission_mode"],
            "--append-system-prompt", _persona(),
            "--system-prompt-snapshot", "off",  # une modif de persona.md s'applique au prochain redémarrage

            "--name", "JARVIS",
        ]
        if self.session_id:
            args += ["--resume", self.session_id]
        env = dict(os.environ)
        env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
        log.info("lancement de Claude (session=%s)", self.session_id)
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=CFG["workspace"], env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_loop(self.proc))
        asyncio.create_task(self._drain_stderr(self.proc))

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None

    async def _drain_stderr(self, proc):
        async for line in proc.stderr:
            log.warning("claude stderr: %s", line.decode(errors="replace").rstrip())

    # --- lecture du flux ----------------------------------------------------
    def _emit(self, event: dict):
        # restes d'un tour abandonné : on les avale jusqu'à son résultat
        if self._skip:
            if event["type"] == "done":
                self._skip -= 1
            return
        if self._queue is not None:
            self._queue.put_nowait(event)

    async def _read_loop(self, proc):
        async for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if msg.get("session_id") and msg["session_id"] != self.session_id:
                self._save_session(msg["session_id"])
            # On ignore ce que disent les sous-agents : seul Jarvis parle.
            if msg.get("parent_tool_use_id"):
                continue
            if kind == "stream_event":
                ev = msg["event"]
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    self._emit({"type": "text", "text": ev["delta"]["text"]})
                elif ev.get("type") == "message_start":
                    self._emit({"type": "message_start"})
            elif kind == "assistant":
                for block in msg["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        self._emit({"type": "tool", "name": block["name"],
                                    "detail": _tool_detail(block["name"], block.get("input", {}))})
            elif kind == "result":
                text = msg.get("result") or ""
                self._emit({"type": "done", "ok": msg.get("subtype") == "success" and not msg.get("is_error"),
                            "result": text,
                            "error": text if msg.get("is_error") else None,
                            # limite de l'abonnement atteinte : Claude ne répond plus jusqu'à la réinitialisation
                            "limit": text if LIMIT.search(text) else None,
                            "duration_ms": msg.get("duration_ms"),
                            "cost": msg.get("total_cost_usd")})
        # le processus est mort
        rc = await proc.wait()
        log.warning("Claude s'est arrêté (code %s)", rc)
        if proc is self.proc:
            self._skip = 0
            self._emit({"type": "done", "ok": False, "result": "", "error": f"Claude s'est arrêté (code {rc})"})
            self.proc = None

    # --- API ---------------------------------------------------------------
    async def ask(self, text: str):
        """Envoie une demande et produit les événements de la réponse au fil de l'eau."""
        async with self._lock:
            if not self.proc or self.proc.returncode is not None:
                await self.start()
            self._queue = asyncio.Queue()
            self.busy = True
            payload = {"type": "user", "message": {"role": "user", "content": text}}
            finished = False
            try:
                self.proc.stdin.write((json.dumps(payload) + "\n").encode())
                await self.proc.stdin.drain()
                while True:
                    ev = await self._queue.get()
                    if ev["type"] == "done":
                        finished = True
                    yield ev
                    if finished:
                        break
            finally:
                self._queue = None
                if not finished and self.proc and self.proc.returncode is None:
                    # tour abandonné (nouvelle demande) : on coupe Claude et on ignore la fin
                    self._skip += 1
                    self._send_interrupt()
                self.busy = False

    def _send_interrupt(self):
        req = {"type": "control_request", "request_id": uuid.uuid4().hex,
               "request": {"subtype": "interrupt"}}
        self.proc.stdin.write((json.dumps(req) + "\n").encode())

    async def interrupt(self):
        """Coupe la réponse en cours."""
        if not (self.proc and self.busy):
            return
        try:
            self._send_interrupt()
            await self.proc.stdin.drain()
        except Exception:
            await self.stop()
            self._emit({"type": "done", "ok": False, "result": "", "error": "interrompu"})
