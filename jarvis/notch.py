"""Le visage de Jarvis : deux yeux vivants dans une bulle flottante (façon PIO), plus une icône de barre de menus.

- les yeux suivent la souris, s'endorment quand tu ne fais rien, changent d'humeur avec Jarvis ;
- maintenir ⌥⌘ = parler, ⌥⌘⇧ = dicter dans la fenêtre active ;
- la bulle s'ouvre quand il écoute ou parle.
"""
import json
import subprocess
import time
import sys
import threading
import urllib.request

import objc
import Quartz
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered, NSColor,
                    NSEvent, NSGlassEffectView, NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular,
                    NSImage, NSMenu, NSMenuItem, NSPanel, NSScreen, NSStatusBar, NSWindowBelow,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary)
from Foundation import NSMakeRect, NSObject, NSTimer, NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration

from .config import CFG, CONFIG_PATH, ROOT

NONACTIVATING_PANEL = 1 << 7
MARGIN = 8
OPTION, COMMAND, SHIFT, CONTROL = 1 << 19, 1 << 20, 1 << 17, 1 << 18
PTT_DELAY = 0.25          # tenir ⌥⌘ un quart de seconde (évite les raccourcis ⌥⌘X)
SIZES = {"S": 0.8, "M": 1.0, "L": 1.3, "XL": 1.65}
THEMES = {"glass": "Verre liquide (Apple)", "pio": "Yeux ronds (PIO)", "shadow": "Manga (Ombre)"}
API = f"http://127.0.0.1:{CFG['port']}"
PARENT = __import__("os").getppid()   # le serveur qui nous a lancés

# humeur → symbole SF de la barre de menus
MOOD_ICONS = {"normal": "eye", "sleepy": "moon.zzz", "listening": "waveform", "thinking": "brain",
              "working": "gearshape.2", "speaking": "speaker.wave.2", "error": "exclamationmark.triangle",
              "laugh": "face.smiling", "wink": "hand.thumbsup", "boot": "eye.slash"}


def post(path: str, payload: dict | None = None):
    """Appel HTTP au serveur sans bloquer l'interface."""
    def run():
        try:
            req = urllib.request.Request(API + path, data=json.dumps(payload or {}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def save_setting(key, value):
    cfg = json.loads(CONFIG_PATH.read_text())
    cfg[key] = value
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    CFG[key] = value


class Face(NSObject):
    """Relie la fenêtre, la page des yeux, le clavier, la souris et la barre de menus."""

    def initWithPanel_web_(self, panel, web):
        self = objc.super(Face, self).init()
        self.panel, self.web = panel, web
        self.size = (120.0, 56.0)
        self.ptt_since = None      # moment où ⌥⌘ a été enfoncé
        self.ptt_on = False
        self.ptt_dictate = False
        self.last_gaze = None
        self.last_gaze_t = 0.0
        self.status = None
        self.glass = {}            # nom → NSGlassEffectView (vrai Liquid Glass d'Apple)
        self.glass_spec = {}       # nom → dernière forme demandée par la page
        return self

    # --- page → natif -----------------------------------------------------------
    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        body = message.body()
        if message.name() == "size":
            self.size = (float(body["w"]), float(body["h"]))
            self.place()
        elif message.name() == "mood":
            self.set_icon(str(body))
        elif message.name() == "glass":
            for name, spec in dict(body).items():
                self.glass_spec[str(name)] = dict(spec)
            self.layout_glass()

    @objc.python_method
    def layout_glass(self):
        """Place les plaques de verre derrière la page, aux formes que la page décrit (repère haut-gauche)."""
        content = self.panel.contentView()
        height = content.bounds().size.height
        for name, s in self.glass_spec.items():
            view = self.glass.get(name)
            if view is None:
                view = NSGlassEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
                view.setStyle_(NSGlassEffectViewStyleClear if s.get("clear") else NSGlassEffectViewStyleRegular)
                content.addSubview_positioned_relativeTo_(view, NSWindowBelow, self.web)
                self.glass[name] = view
            hidden = bool(s.get("hidden")) or s["w"] < 1 or s["h"] < 1
            view.setHidden_(hidden)
            if hidden:
                continue
            view.setFrame_(NSMakeRect(s["x"], height - s["y"] - s["h"], s["w"], s["h"]))
            view.setCornerRadius_(min(float(s.get("r", 0)), s["w"] / 2, s["h"] / 2))
            tint = s.get("tint")
            view.setTintColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*tint) if tint else None)

    @objc.python_method
    def place(self):
        w, h = self.size
        if CFG["notch_position"] == "center":   # accroché sous l'encoche, comme une Dynamic Island
            full = NSScreen.mainScreen().frame()
            x = full.origin.x + (full.size.width - w) / 2
            y = full.origin.y + full.size.height - h
        else:
            area = NSScreen.mainScreen().visibleFrame()
            x = area.origin.x + area.size.width - w - MARGIN
            y = area.origin.y + area.size.height - h - MARGIN
        self.panel.setFrame_display_(NSMakeRect(x, y, w, h), True)
        self.layout_glass()   # la hauteur a changé : on recale le verre (repère inversé)

    @objc.python_method
    def js(self, code: str):
        self.web.evaluateJavaScript_completionHandler_(code, None)

    # --- boucle 15 Hz : clavier, souris, inactivité ----------------------------------------
    def tick_(self, _timer):
        import time
        now = time.time()
        flags = NSEvent.modifierFlags()
        held = flags & OPTION and flags & COMMAND and not flags & CONTROL
        if CFG["push_to_talk"]:
            if held:
                if self.ptt_since is None:
                    self.ptt_since = now
                if flags & SHIFT:
                    self.ptt_dictate = True
                if not self.ptt_on and now - self.ptt_since >= PTT_DELAY:
                    self.ptt_on = True
                    post("/api/ptt", {"action": "start"})
                    self.js(f"window.ptt && ptt(true, {str(self.ptt_dictate).lower()})")
                elif self.ptt_on and self.ptt_dictate:
                    self.js("window.ptt && ptt(true, true)")
            else:
                if self.ptt_on:
                    post("/api/ptt", {"action": "stop", "dictate": self.ptt_dictate})
                    self.js("window.ptt && ptt(false, false)")
                self.ptt_since, self.ptt_on, self.ptt_dictate = None, False, False

        # regard : position du curseur dans le repère de la page (origine en haut à gauche)
        m, f = NSEvent.mouseLocation(), self.panel.frame()
        gaze = (round((m.x - f.origin.x) / 6) * 6, round((f.origin.y + f.size.height - m.y) / 6) * 6)
        far = abs(gaze[0] - f.size.width / 2) > 900 or abs(gaze[1] - f.size.height / 2) > 900   # souris loin : inutile
        if gaze != self.last_gaze and now - self.last_gaze_t >= 0.066 and not (far and self.last_gaze):   # 15 mises à jour/s au plus
            self.last_gaze, self.last_gaze_t = gaze, now
            self.js(f"window.gaze && gaze({gaze[0]}, {gaze[1]})")

    def watchParent_(self, _timer):
        """Si le serveur meurt, la bulle part avec lui : sinon macOS croit BAO encore ouvert et ne le relance pas."""
        import os
        if os.getppid() != PARENT:
            NSApplication.sharedApplication().terminate_(None)

    def webViewWebContentProcessDidTerminate_(self, _web):
        """macOS a tué le processus de la page (mémoire) : sans ça la bulle reste figée."""
        self.reload()

    @objc.python_method
    def reload(self):
        now = time.time()
        if now - getattr(self, "last_reload", 0) > 5:
            self.last_reload = now
            self.web.reload()

    def health_(self, _timer):
        """La page doit battre toutes les secondes ; sinon elle est gelée : on la recharge."""
        def done(result, error):
            if error is not None or result is None or result > 12000:
                self.reload()
        self.web.evaluateJavaScript_completionHandler_("Date.now() - (window.__beat || Date.now())", done)

    def idle_(self, _timer):
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)
        self.js(f"window.idle && idle({idle:.1f})")

    # --- barre de menus -----------------------------------------------------------
    @objc.python_method
    def build_menu(self):
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        self.set_icon("boot")
        menu = NSMenu.alloc().init()

        def item(title, action, key="", tag=0):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            it.setTarget_(self)
            it.setTag_(tag)
            menu.addItem_(it)
            return it

        item("Parler  (maintiens ⌥⌘)", "listen:")
        item("Arrêter la tâche en cours", "stopTask:")
        item("Nouvelle conversation", "reset:")
        menu.addItem_(NSMenuItem.separatorItem())
        item("Afficher / masquer la liste de tâches", "toggleTodos:")
        item("Arrêter le minuteur", "stopTimer:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.toggle_item = item("Masquer les yeux", "toggleFace:")
        size_menu = NSMenu.alloc().init()
        for i, name in enumerate(SIZES):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(name, "setSize:", "")
            it.setTarget_(self)
            it.setTag_(i)
            it.setState_(1 if name == CFG["notch_size"] else 0)
            size_menu.addItem_(it)
        size_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Taille", None, "")
        size_item.setSubmenu_(size_menu)
        menu.addItem_(size_item)
        theme_menu = NSMenu.alloc().init()
        for key, title in THEMES.items():
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "setTheme:", "")
            it.setTarget_(self)
            it.setRepresentedObject_(key)
            it.setState_(1 if key == CFG["notch_theme"] else 0)
            theme_menu.addItem_(it)
        theme_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Style", None, "")
        theme_item.setSubmenu_(theme_menu)
        menu.addItem_(theme_item)
        self.pos_item = item("Placer sous l'encoche" if CFG["notch_position"] != "center"
                             else "Placer dans le coin", "togglePosition:")
        mood_menu = NSMenu.alloc().init()
        for i, mood in enumerate(["normal", "sleepy", "surprise", "excited", "laugh", "thinking",
                                  "working", "listening", "scared", "error", "wink"]):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(mood, "testMood:", "")
            it.setTarget_(self)
            it.setRepresentedObject_(mood)
            mood_menu.addItem_(it)
        mood_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Tester une humeur", None, "")
        mood_item.setSubmenu_(mood_menu)
        menu.addItem_(mood_item)
        menu.addItem_(NSMenuItem.separatorItem())
        item("Ouvrir le grand HUD", "openHud:")
        item("Ouvrir la configuration", "openConfig:")
        item("Redémarrer BAO", "restartJarvis:")
        item("Arrêter Jarvis", "quitJarvis:")
        self.status.setMenu_(menu)

    @objc.python_method
    def set_icon(self, mood):
        if not self.status:
            return
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(MOOD_ICONS.get(mood, "eye"), "JARVIS")
        if img:
            img.setTemplate_(True)
            self.status.button().setImage_(img)

    # actions du menu
    def listen_(self, _):
        post("/api/listen")

    def stopTask_(self, _):
        post("/api/stop")

    def reset_(self, _):
        post("/api/reset")

    def toggleTodos_(self, _):
        self.js("window.toggleTodos && toggleTodos()")

    def stopTimer_(self, _):
        post("/api/timer", {"action": "stop"})

    def toggleFace_(self, sender):
        if self.panel.isVisible():
            self.panel.orderOut_(None)
            sender.setTitle_("Afficher les yeux")
        else:
            self.panel.orderFrontRegardless()
            sender.setTitle_("Masquer les yeux")

    def setSize_(self, sender):
        name = list(SIZES)[sender.tag()]
        for it in sender.menu().itemArray():
            it.setState_(1 if it is sender else 0)
        save_setting("notch_size", name)
        self.js(f"window.setScale && setScale({SIZES[name]})")

    def togglePosition_(self, sender):
        center = CFG["notch_position"] != "center"
        save_setting("notch_position", "center" if center else "right")
        sender.setTitle_("Placer dans le coin" if center else "Placer sous l'encoche")
        self.js(f"window.setDock && setDock('{CFG['notch_position']}')")
        self.place()

    def setTheme_(self, sender):
        name = str(sender.representedObject())
        for it in sender.menu().itemArray():
            it.setState_(1 if it is sender else 0)
        save_setting("notch_theme", name)
        self.js(f"window.setTheme && setTheme('{name}')")

    def testMood_(self, sender):
        self.js(f"window.flash && flash('{sender.representedObject()}', 2500)")

    def openHud_(self, _):
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app=http://localhost:{CFG['port']}",
                          "--window-size=1440,900"])

    def openConfig_(self, _):
        subprocess.Popen(["open", "-t", str(CONFIG_PATH)])

    def restartJarvis_(self, _):
        # le serveur est tué, launchd le relance (et la bulle avec lui)
        subprocess.Popen(["/bin/zsh", str(ROOT / "bin" / "jarvis"), "restart"], start_new_session=True)

    def quitJarvis_(self, _):
        subprocess.Popen(["/bin/zsh", str(ROOT / "bin" / "jarvis"), "stop"])


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # pas d'icône dans le Dock

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 120, 56), NONACTIVATING_PANEL, NSBackingStoreBuffered, False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setLevel_(25)  # au-dessus des fenêtres, comme la barre de menus
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorStationary)

    config = WKWebViewConfiguration.alloc().init()
    web = WKWebView.alloc().initWithFrame_configuration_(panel.contentView().bounds(), config)
    face = Face.alloc().initWithPanel_web_(panel, web)
    config.userContentController().addScriptMessageHandler_name_(face, "size")
    config.userContentController().addScriptMessageHandler_name_(face, "mood")
    config.userContentController().addScriptMessageHandler_name_(face, "glass")
    web.setNavigationDelegate_(face)
    web.setValue_forKey_(False, "drawsBackground")
    web.setAutoresizingMask_(18)  # largeur + hauteur flexibles
    panel.contentView().addSubview_(web)

    face.place()
    face.build_menu()
    url = (f"http://localhost:{CFG['port']}/notch?scale={SIZES.get(CFG['notch_size'], 1)}"
           f"&dock={CFG['notch_position']}&sleep={CFG['sleep_after_seconds']}&theme={CFG['notch_theme']}")
    web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    panel.orderFrontRegardless()

    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1 / 15, face, "tick:", None, True)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, face, "idle:", None, True)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, face, "watchParent:", None, True)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(4.0, face, "health:", None, True)
    app.run()


if __name__ == "__main__":
    sys.exit(main())
