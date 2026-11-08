# BAO

Un assistant vocal pour Mac, façon Tony Stark. Il tourne en permanence, se réveille quand tu dis **« Bao »**, te répond
à voix haute en français et **agit vraiment** sur ta machine : terminal, fichiers, applications, navigateur, projets.

Le cerveau est **Claude Code, connecté à ton abonnement Claude** : pas de clé API, pas de frais en plus.
Version **bêta pour testeurs** : ça marche, mais des bugs restent (voir plus bas). Tes retours sont le but.

```
Micro ─▶ « Bao » ─▶ Whisper (sur ton Mac) ─▶ Claude Code (ton abonnement) ─▶ actions sur le Mac
  ▲                                                                              │
  └───────────────── voix française, phrase par phrase ◀─────────────────────────┘
                 Yeux en verre liquide (bulle en haut de l'écran)
```

## Ce qu'il te faut

- Un Mac **Apple Silicon** (M1 ou plus) sous **macOS 26 ou plus** (les yeux utilisent le vrai Liquid Glass d'Apple)
- **8 Go de RAM minimum**, 16 Go conseillés ; ~3 Go de disque
- Un abonnement **Claude** (Pro ou Max) avec **Claude Code** installé et connecté
- Les outils Apple en ligne de commande, et [uv](https://docs.astral.sh/uv/) (le script te dit quoi installer s'il manque quelque chose)

## Installer

```bash
git clone https://github.com/tanorfall/bao.git ~/BAO
cd ~/BAO
bin/jarvis setup     # vérifie ton Mac, installe tout, télécharge le modèle vocal (~1,6 Go)
bin/jarvis start     # premier lancement
```

Clone-le dans ton dossier personnel (`~/BAO`), pas dans Documents : macOS protège Documents et bloquerait BAO.
(La commande s'appelle `jarvis`, l'ancien nom du projet.)

Au premier lancement, accepte **l'accès au micro**. Pour qu'il démarre avec ta session et se relance tout seul,
`start` installe `~/Applications/BAO.app` : si tu travailles dans des dossiers protégés, donne-lui l'accès
Réglages Système → Confidentialité et sécurité → **Accès complet au disque**.

## Premier lancement : il fait connaissance

BAO ne sait rien de toi au départ. Il commence par te poser quelques questions, une par une, à voix haute :
comment tu t'appelles, ce que tu fais, tes habitudes et tes logiciels, tes projets, ce que tu aimerais déléguer,
comment tu veux qu'il te parle, et les mots qu'il risque de mal entendre. Il retient tout dans sa mémoire
(`data/memory/`, sur ton Mac uniquement) et s'en sert ensuite. Dis « **refais connaissance** » pour recommencer.

## Utiliser

| Tu fais | BAO |
|---|---|
| « Bao » … ta demande | écoute, comprend, agit, répond |
| tu continues à parler après sa réponse | il relance sans mot d'appel (20 s) ; « c'est bon » clôt |
| « Bao » pendant qu'il parle | il se tait et t'écoute |
| « Stop » / « Tais-toi » | coupe tout |
| **maintenir ⌥⌘** et parler | talkie-walkie, sans mot d'appel |
| **maintenir ⌥⌘⇧** et parler | dictée : le texte est tapé dans la fenêtre active |
| « minuteur 10 minutes », « ajoute X à ma liste » | minuteurs et liste de tâches dans la bulle |
| « retiens que… » / « oublie… » | mémoire |

Menu de la barre des menus : parler, arrêter, **Redémarrer BAO**, taille, **Style** (verre liquide, yeux ronds, manga),
position, ouvrir la configuration.

## Les fonctionnalités

- **Yeux en Liquid Glass** : deux gouttes de verre qui suivent ta souris, clignent, s'endorment, changent de teinte
  selon l'humeur (écoute, réflexion, action, erreur).
- **Mot d'appel « Bao »**, repéré localement par Whisper, relance sans mot d'appel, interruption à la voix.
- **Il agit** : ouvre des apps, crée des projets, pilote le Mac avec AppleScript, tout ce que fait Claude Code.
- **Minuteurs, liste de tâches, quêtes** (XP et niveaux quand tu coches une tâche).
- **Mémoire qui apprend** : faits, préférences, leçons tirées de ses erreurs, savoir-faire. Relecture automatique
  après chaque conversation.
- **Compréhension poussée** : morceaux de phrase recollés, son nettoyé, ton vocabulaire donné à Whisper.
- **Voix gratuites sans clé** : voix neuronale Microsoft (internet), Piper ou voix macOS hors ligne.
- **Toujours une réponse** : limite d'abonnement annoncée à voix haute, bascule automatique sur un autre modèle.

## Réglages (`config.json`, créé au premier lancement)

| Clé | Rôle |
|---|---|
| `address_as` | comment il t'appelle (rempli pendant la prise de contact) |
| `model` | `sonnet` (rapide, économe) ou `opus` (plus fort, consomme plus de limite) |
| `permission_mode` | `auto` (conseillé) ou `bypassPermissions` |
| `edge_voice` | `fr-FR-DeniseNeural` (femme), `fr-FR-HenriNeural` (homme)… |
| `whisper_model` | `mlx-community/whisper-large-v3-turbo` (précis, ~1,6 Go de RAM) |
| `vocabulary` | tes mots à toi, donnés à Whisper |
| `input_device` | micro (`bin/jarvis micros` les liste) : un casque comprend bien mieux que le micro du Mac |
| `notch_size`, `notch_position`, `notch_theme` | apparence de la bulle |

Après un changement : `bin/jarvis restart`. Sa personnalité est dans `jarvis/persona.md`.

## Commandes

```
bin/jarvis setup | start | stop | restart | status
bin/jarvis ask "…"     une demande au clavier      bin/jarvis chat   discuter au clavier
bin/jarvis logs        journal en direct           bin/jarvis micros liste les micros
bin/jarvis memoire     ouvre sa mémoire            bin/jarvis uninstall   retire le démarrage automatique
```

## Sécurité

BAO tourne en mode `auto` : il agit sans demander pour le travail courant, un classifieur de Claude Code bloque les
actions dangereuses, et sa consigne l'oblige à te demander une **confirmation orale** avant de supprimer, envoyer,
publier, payer ou toucher aux réglages système. Un assistant vocal peut mal entendre : reste prudent sur les demandes
sensibles. Tout reste sur ton Mac ; seules les demandes envoyées à Claude (et la voix neuronale Microsoft) sortent.

## Problèmes connus (bêta)

- La bulle peut se figer par moments (une protection redémarre la page, mais dis-le si ça arrive).
- Gourmand en mémoire (~2,7 Go avec le modèle vocal complet) : sur un Mac à 8 Go, ferme des applications.
- Réglé et mesuré sur une seule voix et un seul Mac : ta voix, ton accent et ton micro peuvent donner d'autres résultats.
- Le démarrage automatique après un redémarrage complet du Mac est peu testé.

## Faire un retour

Ouvre une *issue* sur GitHub, ou envoie : ta version de macOS, ta puce et ta RAM, ce que tu as dit, ce qui s'est passé,
et les dernières lignes de `bin/jarvis logs`.

## Architecture

| Fichier | Rôle |
|---|---|
| `jarvis/brain.py` | session Claude Code persistante (`claude -p`, stream-json) |
| `jarvis/ears.py`, `audio.py` | micro, mot d'appel, nettoyage du son, Whisper MLX |
| `jarvis/mouth.py` | voix phrase par phrase, coupure instantanée |
| `jarvis/server.py` | orchestrateur, API locale, WebSocket |
| `jarvis/notch.py` + `hud/notch.html` | la bulle : verre liquide, yeux, barre des menus, ⌥⌘ |
| `jarvis/memory.py`, `reflect.py` | mémoire durable et relecture des conversations |
| `jarvis/planner.py`, `intents.py` | minuteurs, tâches, commandes vocales simples |
| `jarvis/persona.md` | sa personnalité, ses règles, la prise de contact |
| `launchd/` | `BAO.app` et démarrage automatique |
