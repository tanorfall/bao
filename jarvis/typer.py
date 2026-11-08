"""Dictée : écrit un texte dans la fenêtre active (presse-papiers + ⌘V, compatible avec tout clavier)."""
import logging
import threading
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString
from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions

log = logging.getLogger("jarvis.typer")

KEY_V, KEY_RETURN = 9, 36


def _tap(key: int, flags: int = 0):
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, key, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)


def type_text(text: str, press_enter: bool = False) -> bool:
    """Colle le texte là où se trouve le curseur. False si macOS refuse (texte laissé dans le presse-papiers)."""
    pb = NSPasteboard.generalPasteboard()
    previous = pb.stringForType_(NSPasteboardTypeString)
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    if not AXIsProcessTrusted():
        AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
        log.warning("Accessibilité non autorisée : texte laissé dans le presse-papiers")
        return False
    _tap(KEY_V, Quartz.kCGEventFlagMaskCommand)
    if press_enter:
        time.sleep(0.08)
        _tap(KEY_RETURN)

    def restore():  # on rend son presse-papiers à l'utilisateur
        time.sleep(0.6)
        if previous is not None and pb.stringForType_(NSPasteboardTypeString) == text:
            pb.clearContents()
            pb.setString_forType_(previous, NSPasteboardTypeString)
    threading.Thread(target=restore, daemon=True).start()
    return True
