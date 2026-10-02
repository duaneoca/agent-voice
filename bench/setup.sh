#!/usr/bin/env bash
# Install everything the benchmark needs. Safe to re-run.
set -euo pipefail

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="$BENCH/models"
mkdir -p "$MODELS"

# --- system packages (official repos only: extra + omarchy) ---
# vosk now comes from PyPI with everything else; only espeak-ng (the TTS
# fallback the benchmark compares against) still needs to be a system package.
PKGS=(uv espeak-ng)
MISSING=()
for p in "${PKGS[@]}"; do pacman -Qq "$p" &>/dev/null || MISSING+=("$p"); done

if (( ${#MISSING[@]} )); then
  echo "==> Installing: ${MISSING[*]}"
  # sudo when there is a terminal to type a password into, pkexec when there
  # is not -- this gets run from a launcher and from CI as well as by hand.
  if [[ -t 0 ]]; then sudo pacman -S --needed --noconfirm "${MISSING[@]}"
  else pkexec pacman -S --needed --noconfirm "${MISSING[@]}"; fi
else
  echo "==> System packages already present"
fi

# --- python venv ---
# The same requirements files the product installs, on a uv-managed
# interpreter rather than the system one -- so a benchmark result describes
# the stack that ships, and neither breaks when Arch bumps Python. "Same
# requirements" is not "same versions": those files give ranges, so two runs
# weeks apart can resolve differently. Note the resolved versions with any
# number worth keeping.
if [[ ! -d "$BENCH/.venv" ]]; then
  echo "==> Creating venv (uv-managed CPython 3.13)"
  uv venv --python 3.13 "$BENCH/.venv"
fi
echo "==> Installing the pinned runtime"
VIRTUAL_ENV="$BENCH/.venv" uv pip install --quiet \
  -r "$BENCH/../requirements.txt" -r "$BENCH/../requirements-openwakeword.txt"

# --- vosk model ---
VOSK_SMALL="$MODELS/vosk-model-small-en-us-0.15"
# Same contract as install.sh's download(): temp name, verify against
# models.lock, then rename. A half-finished download left under the final name
# is skipped as "already here" by every run afterwards, and an unverified one
# is a model of unknown provenance feeding a benchmark.
MODELS_LOCK="$BENCH/../models.lock"
pinned_sha() {
  [[ -f $MODELS_LOCK ]] || return 0
  awk -v want="$1" '$1=="sha256" && $2==want {print $3; exit}' "$MODELS_LOCK"
}
PIPER_REV="$(awk '$1=="revision" && $2=="piper" {print $3; exit}' \
             "$MODELS_LOCK" 2>/dev/null || true)"
PIPER_REV="${PIPER_REV:-main}"

download() { # <url> <dest> [name-in-the-lock]
  local name="${3:-$(basename "$2")}" tmp want got
  want="$(pinned_sha "$name")"
  if [[ -z $want ]]; then
    echo "==> no hash recorded for $name; add one with ./bench/pin-models.sh" >&2
    return 1
  fi
  tmp="$(mktemp "$(dirname "$2")/.download.XXXXXX")"
  if ! curl -fL --proto '=https' --progress-bar -o "$tmp" "$1"; then
    rm -f "$tmp"; return 1
  fi
  got="$(sha256sum "$tmp" | cut -d' ' -f1)"
  if [[ $got != "$want" ]]; then
    rm -f "$tmp"
    echo "==> $name does not match models.lock ($got, wanted $want)" >&2
    return 1
  fi
  mv -f "$tmp" "$2"
}

if [[ ! -d "$VOSK_SMALL" ]]; then
  echo "==> Downloading vosk small model (~40MB)"
  # mktemp, not /tmp/vosk-small.zip: a fixed name in a world-writable
  # directory is somebody else's file to pre-create.
  zip="$(mktemp -t vosk-small.XXXXXX.zip)"
  download https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip \
    "$zip" vosk-model-small-en-us-0.15.zip
  bsdtar -xf "$zip" -C "$MODELS" && rm -f "$zip"
fi

# --- piper voices ---
piper_voice() { # <name> <quality>
  local base="https://huggingface.co/rhasspy/piper-voices/resolve/$PIPER_REV/en/en_US/$1/$2"
  local dir="$MODELS/piper"; mkdir -p "$dir"
  local f="en_US-$1-$2.onnx"
  [[ -f "$dir/$f" ]] && return 0
  echo "==> Downloading piper voice $f"
  download "$base/$f"      "$dir/$f"
  download "$base/$f.json" "$dir/$f.json"
}
piper_voice lessac medium
piper_voice lessac low

echo
echo "Setup complete."
echo "  Whisper models download on first use (tiny.en 75MB, base.en 145MB, small.en 480MB)."
