"""Tâches et minuteurs (façon PIO) : liste persistée, minuteur, chrono, pause qui reprend le travail.

Tout est enregistré dans data/planner.json, avec des heures absolues : un minuteur survit à un redémarrage.
"""
import json
import time
import uuid

from .config import DATA

PATH = DATA / "planner.json"


def fmt_duration(sec: float) -> str:
    """Durée lisible à voix haute : « 1 heure 30 minutes », « 45 secondes »."""
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    parts = []
    if h:
        parts.append(f"{h} heure{'s' if h > 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m > 1 else ''}")
    if s and not h:
        parts.append(f"{s} seconde{'s' if s > 1 else ''}")
    return " ".join(parts) or "0 seconde"


def level_of(xp: int) -> tuple[int, int, int]:
    """(niveau, xp dans ce niveau, xp nécessaire pour le suivant) : le niveau n demande 2n quêtes."""
    level, need = 1, 2
    while xp >= need:
        xp -= need
        level += 1
        need = 2 * level
    return level, xp, need


class Planner:
    def __init__(self, on_change, on_quest=None):
        self.on_change = on_change   # appelé à chaque modification (diffusion au HUD)
        self.on_quest = on_quest or (lambda event: None)   # quête accomplie / level up
        self.todos: list[dict] = []
        self.timer: dict | None = None
        self.show_todos = True
        self.xp = 0
        self._load()

    # --- persistance ----------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(PATH.read_text())
            self.todos, self.timer = data.get("todos", []), data.get("timer")
            self.show_todos = data.get("show_todos", True)
            self.xp = data.get("xp", 0)
        except Exception:
            pass

    def _save(self):
        PATH.write_text(json.dumps({"todos": self.todos, "timer": self.timer, "show_todos": self.show_todos,
                                    "xp": self.xp}, ensure_ascii=False, indent=1))
        self.on_change()

    def snapshot(self) -> dict:
        level, into, need = level_of(self.xp)
        return {"type": "planner", "now": time.time(), "todos": self.todos, "timer": self.timer,
                "show_todos": self.show_todos, "xp": self.xp, "level": level, "level_xp": into, "level_need": need}

    def _gain(self, title: str, amount: int = 1):
        before = level_of(self.xp)[0]
        self.xp += amount
        after = level_of(self.xp)[0]
        self.on_quest({"type": "quest", "title": title, "xp": amount, "level": after, "levelup": after > before})

    # --- tâches ---------------------------------------------------------------
    def add_todo(self, text: str) -> dict:
        todo = {"id": uuid.uuid4().hex[:6], "text": text.strip().rstrip(".").strip(), "done": False, "t": time.time()}
        self.todos.append(todo)
        self.show_todos = True
        self._save()
        return todo

    def find(self, query: str) -> dict | None:
        """Retrouve une tâche par son id ou par les mots prononcés."""
        import difflib
        q = query.lower().strip(" .!?")
        for t in self.todos:
            if t["id"] == q or q == t["text"].lower():
                return t
        for t in self.todos:
            if q in t["text"].lower() or t["text"].lower() in q:
                return t
        best = difflib.get_close_matches(q, [t["text"].lower() for t in self.todos], n=1, cutoff=0.55)
        return next((t for t in self.todos if t["text"].lower() == best[0]), None) if best else None

    def toggle_todo(self, todo_id: str, done: bool | None = None):
        t = self.find(todo_id)
        if t:
            was = t["done"]
            t["done"] = (not was) if done is None else done
            if t["done"] and not was:
                self._gain(t["text"])
            elif was and not t["done"]:
                self.xp = max(0, self.xp - 1)   # décocher rend l'XP
            self._save()
        return t

    def remove_todo(self, todo_id: str):
        t = self.find(todo_id)
        if t:
            self.todos.remove(t)
            self._save()
        return t

    def clear_done(self):
        self.todos = [t for t in self.todos if not t["done"]]
        self._save()

    def set_show(self, show: bool):
        self.show_todos = show
        self._save()

    # --- minuteur ---------------------------------------------------------------
    def remaining(self) -> float:
        t = self.timer
        if not t or t["kind"] != "timer":
            return 0
        return t["remaining"] if t.get("paused") else max(0.0, t["end_at"] - time.time())

    def start_timer(self, seconds: float, label: str = "", is_break: bool = False, resume: dict | None = None):
        self.timer = {"kind": "timer", "label": label, "duration": seconds, "end_at": time.time() + seconds,
                      "paused": False, "remaining": None, "ringing": False, "is_break": is_break, "resume": resume}
        self._save()

    def start_break(self, seconds: float):
        """Pause de repos : le minuteur de travail est mis de côté, puis repris à la fin de la pause."""
        saved = None
        if self.timer and self.timer["kind"] == "timer" and not self.timer["ringing"] and not self.timer["is_break"]:
            saved = dict(self.timer, paused=True, remaining=self.remaining())
        self.start_timer(seconds, "Pause", is_break=True, resume=saved)

    def start_stopwatch(self, label: str = ""):
        self.timer = {"kind": "stopwatch", "label": label, "start_at": time.time(), "paused": False,
                      "elapsed": 0.0, "ringing": False}
        self._save()

    def elapsed(self) -> float:
        t = self.timer
        if not t or t["kind"] != "stopwatch":
            return 0
        return t["elapsed"] if t["paused"] else t["elapsed"] + time.time() - t["start_at"]

    def pause(self):
        t = self.timer
        if not t or t["paused"] or t["ringing"]:
            return False
        if t["kind"] == "timer":
            t["remaining"] = self.remaining()
        else:
            t["elapsed"] = self.elapsed()
        t["paused"] = True
        self._save()
        return True

    def resume(self):
        t = self.timer
        if not t or not t["paused"]:
            return False
        if t["kind"] == "timer":
            t["end_at"] = time.time() + t["remaining"]
            t["remaining"] = None
        else:
            t["start_at"] = time.time()
        t["paused"] = False
        self._save()
        return True

    def toggle_pause(self):
        return self.resume() or self.pause()

    def extend(self, seconds: float):
        t = self.timer
        if not t or t["kind"] != "timer":
            return False
        if t["paused"]:
            t["remaining"] += seconds
        elif t["ringing"]:
            t.update(ringing=False, end_at=time.time() + seconds)
        else:
            t["end_at"] += seconds
        t["duration"] += seconds
        self._save()
        return True

    def stop(self):
        had = self.timer is not None
        self.timer = None
        self._save()
        return had

    def tick(self) -> dict | None:
        """Appelé régulièrement : renvoie le minuteur qui vient de sonner (une seule fois)."""
        t = self.timer
        if t and t["kind"] == "timer" and not t["paused"] and not t["ringing"] and time.time() >= t["end_at"]:
            t["ringing"] = True
            if not t.get("is_break"):   # une session de travail menée au bout est une quête
                self._gain(t.get("label") or "Session de concentration")
            self._save()
            return t
        return None

    def end_break(self) -> dict | None:
        """Fin d'une pause : on reprend le minuteur de travail mis de côté."""
        t = self.timer
        if t and t.get("is_break") and t.get("resume"):
            self.timer = t["resume"]
            self.resume()
            return self.timer
        return None
