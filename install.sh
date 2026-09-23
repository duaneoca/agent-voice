#!/bin/bash
# Install the agentvoice daemon.
#
# The bar widget arrives with `omarchy plugin add`, which clones this repo into
# ~/.config/omarchy/plugins/duaneoca.agentvoice/. That gets you the icon and
# the settings screen, but the thing that listens is a Python daemon with a
# few hundred megabytes of models behind it -- so it is a separate, explicit
# step, the way Omarchy installs Voxtype.
#
#   ./install.sh              interactive
#   ./install.sh --yes        no prompts, core engine only
#   ./install.sh --yes --oww  no prompts, both wake engines
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agentvoice"
VENV="$DATA/venv"
MODELS="$DATA/models"
BINDIR="$HOME/.local/bin"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON_VERSION=3.13

ASSUME_YES=0
WANT_OWW=""
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --oww|--openwakeword) WANT_OWW=1 ;;
    --no-oww) WANT_OWW=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
say()  { if have gum; then gum style --foreground 4 "  $*"; else echo "  $*"; fi; }
ok()   { if have gum; then gum style --foreground 2 "  $*"; else echo "  $*"; fi; }
warn() { if have gum; then gum style --foreground 3 "  $*"; else echo "  $*" >&2; fi; }

ask() {
  local prompt="$1"
  (( ASSUME_YES )) && return 1
  if have gum && [[ -t 0 ]]; then gum confirm "$prompt"; else
    read -r -p "  $prompt [y/N] " a; [[ ${a,,} == y* ]]
  fi
}

# --- uv, the only thing we need from the system ---------------------------
if ! have uv; then
  say "Installing uv (the official 'extra' repo package)…"
  if have omarchy-pkg-add; then omarchy-pkg-add uv
  elif [[ -t 0 ]]; then sudo pacman -S --needed --noconfirm uv
  else pkexec pacman -S --needed --noconfirm uv; fi
fi

# --- interpreter and wheels -----------------------------------------------
# Deliberately a uv-managed CPython rather than /usr/bin/python: Arch is
# rolling, and a venv built against the system interpreter stops importing the
# next time Arch bumps it.
say "Creating the environment (CPython $PYTHON_VERSION, managed by uv)…"
mkdir -p "$DATA" "$MODELS" "$DATA/verifiers" "$DATA/wakewords"
[[ -d $VENV ]] || uv venv --python "$PYTHON_VERSION" "$VENV" >/dev/null

say "Installing dependencies…"
VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$ROOT/requirements.txt"

if [[ -z $WANT_OWW ]]; then
  echo
  cat <<'WHY'
  A second wake-word engine is available. Vosk takes any phrase but scores
  phonetic neighbours as high as the real one -- measured here, "hey cloud"
  scores 1.00 against a "hey claude" grammar, which is how a podcast wakes it.
  openWakeWord has four fixed phrases and real rejection, and can be trained
  on your own voice afterwards. It costs about 154MB.

WHY
  if ask "Install openWakeWord as well?"; then WANT_OWW=1; else WANT_OWW=0; fi
fi

if [[ $WANT_OWW == 1 ]]; then
  say "Installing openWakeWord…"
  VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$ROOT/requirements-openwakeword.txt"
fi

# --- models ----------------------------------------------------------------
fetch_vosk() {
  local dir="$MODELS/vosk-model-small-en-us-0.15"
  [[ -d $dir ]] && return 0
  say "Downloading the Vosk model (40MB)…"
  curl -fL --progress-bar -o "$MODELS/.vosk.zip" \
    https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  bsdtar -xf "$MODELS/.vosk.zip" -C "$MODELS" && rm -f "$MODELS/.vosk.zip"
}

fetch_voice() {
  local name="$1" quality="$2"
  local dir="$MODELS/piper" file="en_US-$1-$2.onnx"
  mkdir -p "$dir"
  [[ -f "$dir/$file" ]] && return 0
  local base="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/$1/$2"
  say "Downloading the voice $file (60MB)…"
  curl -fL --progress-bar -o "$dir/$file"      "$base/$file"
  curl -fL --progress-bar -o "$dir/$file.json" "$base/$file.json"
}

fetch_vosk
fetch_voice lessac medium

# --- commands on PATH ------------------------------------------------------
mkdir -p "$BINDIR"
for cmd in agentvoice agentvoice-train-verifier; do
  ln -sf "$ROOT/bin/$cmd" "$BINDIR/$cmd"
done
ok "Installed agentvoice and agentvoice-train-verifier into $BINDIR"

# --- service ---------------------------------------------------------------
# Written rather than copied, so the unit carries this checkout's real path.
mkdir -p "$UNITDIR"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@VENV@|$VENV|g" \
    "$ROOT/desktop/agentvoice.service.in" > "$UNITDIR/agentvoice.service"
systemctl --user daemon-reload
ok "Installed the user service"

echo
ok "Done."
cat <<NEXT

  Add the bar widget, if you have not already:
    omarchy plugin add https://github.com/duaneoca/agent-voice.git --enable

  Start listening:
    agentvoice start

  The Whisper model downloads on first use (~75MB).

  Optional keybinds -- paste into ~/.config/hypr/bindings.lua. They are not
  installed for you, because that file is yours:

    o.bind("F8", "Talk to the agent (push-to-talk)", "agentvoice talk")
    o.bind("F8", "End the turn (push-to-talk)", "agentvoice talk-end", { release = true })
    o.bind("SUPER + CTRL + SPACE", "Stop the agent talking", "agentvoice interrupt")
    o.bind("SUPER + ALT + SPACE", "Release or re-engage the mic", "agentvoice mic")
NEXT
