#!/usr/bin/env bash
# Install everything the benchmark needs. Safe to re-run.
set -euo pipefail

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="$BENCH/models"
mkdir -p "$MODELS"

# --- system packages (official repos only: extra + omarchy) ---
PKGS=(uv vosk-api python-vosk espeak-ng sox python-sounddevice)
MISSING=()
for p in "${PKGS[@]}"; do pacman -Qq "$p" &>/dev/null || MISSING+=("$p"); done

if (( ${#MISSING[@]} )); then
  echo "==> Installing: ${MISSING[*]}"
  # pkexec: this agent has no terminal for a sudo password prompt, so authenticate
  # in the polkit dialog. Run this script yourself in a terminal to use sudo instead.
  if [[ -t 0 ]]; then sudo pacman -S --needed --noconfirm "${MISSING[@]}"
  else pkexec pacman -S --needed --noconfirm "${MISSING[@]}"; fi
else
  echo "==> System packages already present"
fi

# --- python venv for the whisper/piper tier ---
# Built on the SYSTEM interpreter with site-packages visible, so the
# pacman-installed python-vosk and the wheel-installed faster-whisper are
# importable from one process.
if [[ ! -d "$BENCH/.venv" ]]; then
  echo "==> Creating venv (system python + site-packages)"
  uv venv --python "$(command -v python3)" --system-site-packages "$BENCH/.venv"
fi
echo "==> Installing wheels (faster-whisper, piper-tts)"
VIRTUAL_ENV="$BENCH/.venv" uv pip install --quiet faster-whisper piper-tts sounddevice numpy

# --- vosk model ---
VOSK_SMALL="$MODELS/vosk-model-small-en-us-0.15"
if [[ ! -d "$VOSK_SMALL" ]]; then
  echo "==> Downloading vosk small model (~40MB)"
  curl -fL --progress-bar -o /tmp/vosk-small.zip \
    https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  bsdtar -xf /tmp/vosk-small.zip -C "$MODELS" && rm -f /tmp/vosk-small.zip
fi

# --- piper voices ---
piper_voice() { # <name> <quality>
  local n="$1" q="$2" base="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/$1/$2"
  local dir="$MODELS/piper"; mkdir -p "$dir"
  local f="en_US-$1-$2.onnx"
  [[ -f "$dir/$f" ]] && return 0
  echo "==> Downloading piper voice $f"
  curl -fL --progress-bar -o "$dir/$f"      "$base/$f"
  curl -fL --progress-bar -o "$dir/$f.json" "$base/$f.json"
}
piper_voice lessac medium
piper_voice lessac low

echo
echo "Setup complete."
echo "  Whisper models download on first use (tiny.en 75MB, base.en 145MB, small.en 480MB)."
