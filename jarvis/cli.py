"""Parler à Jarvis depuis le terminal (le serveur doit tourner).

    jarvis chat            # conversation interactive
    jarvis ask "..."       # une seule demande
"""
import asyncio
import json
import sys

import websockets

from .config import CFG

CYAN, GOLD, GREEN, DIM, RESET = "\033[96m", "\033[33m", "\033[92m", "\033[2m", "\033[0m"


async def one_turn(ws, text: str, speak: bool):
    await ws.send(json.dumps({"type": "text", "text": text, "speak": speak}))
    started = False
    while True:
        m = json.loads(await ws.recv())
        if m["type"] == "delta":
            if not started:
                print(f"{CYAN}JARVIS ›{RESET} ", end="")
                started = True
            print(m["text"], end="", flush=True)
        elif m["type"] == "tool":
            print(f"\n{GOLD}  ⚙ {m['name']}{RESET} {DIM}{m['detail']}{RESET}", flush=True)
            started = False
        elif m["type"] == "done":
            print(f"\n{DIM}  ({m.get('duration')} s){RESET}")
            return


async def main(argv):
    speak = "--silent" not in argv
    argv = [a for a in argv if a != "--silent"]
    url = f"ws://127.0.0.1:{CFG['port']}/ws"
    try:
        ws = await websockets.connect(url, max_size=None)
    except OSError:
        print("Jarvis n'est pas en ligne. Lance-le avec : jarvis start")
        return 1
    async with ws:
        await ws.recv()  # hello
        if argv:
            await one_turn(ws, " ".join(argv), speak)
            return 0
        print(f"{CYAN}J.A.R.V.I.S.{RESET} {DIM}— écris ta demande, Ctrl+D pour quitter{RESET}")
        while True:
            try:
                text = await asyncio.to_thread(input, f"{GREEN}VOUS ›{RESET} ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text.strip():
                await one_turn(ws, text.strip(), speak)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
