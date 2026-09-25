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
#   ./install.sh --uninstall  take it all back off again
#   ./install.sh --dev        run from this checkout instead of a copy
#
# The daemon installs into ~/.local/share/agentvoice/app rather than running
# from the plugin directory, because `omarchy plugin remove` is an rm -rf and
# would otherwise delete the code a running service is executing -- and this
# script with it, leaving no way to finish the job.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agentvoice"
VENV="$DATA/venv"
MODELS="$DATA/models"
APP="$DATA/app"
BINDIR="$HOME/.local/bin"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON_VERSION=3.13

# True when this script is the installed copy rather than a checkout or the
# plugin directory -- which changes what it is safe to delete. Both sides are
# resolved because either can be a symlink.
SELF_IS_APP=0
[[ "$(readlink -f "$ROOT")" == "$(readlink -f "$APP")" ]] && SELF_IS_APP=1

ASSUME_YES=0
WANT_OWW=""
UNINSTALL=0
WITH_WIDGET=0
KEYBINDS=""
WANT_VOICE_ARG=""
DEV=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --uninstall|--remove) UNINSTALL=1 ;;
    --with-widget) WITH_WIDGET=1 ;;
    --keybinds) KEYBINDS=1 ;;
    # Named explicitly rather than read from settings, so the widget can ask
    # for a voice the instant it is chosen without waiting for `omarchy bar
    # set` to land -- reading the setting here would race the write.
    --voice=*) WANT_VOICE_ARG="${arg#*=}" ;;
    --no-keybinds) KEYBINDS=0 ;;
    --dev) DEV=1 ;;
    --oww|--openwakeword) WANT_OWW=1 ;;
    --no-oww) WANT_OWW=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

# Checked up front rather than discovered at the download step, with the
# environment already built and the models missing. Not for an uninstall:
# removal downloads nothing, and refusing to run because a tool the *install*
# needs has since been removed strands the user with something they cannot
# take off. CI found that one -- its runners have no bsdtar.
if [[ $UNINSTALL == 0 ]]; then
  missing=""
  for tool in curl bsdtar jq; do have "$tool" || missing="$missing $tool"; done
  if [[ -n ${missing// } ]]; then
    echo "  missing required tools:$missing" >&2
    echo "  install them with: omarchy pkg add$missing" >&2
    exit 1
  fi
fi
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

# --- keybinds ---------------------------------------------------------------
# The push-to-talk key, the interrupt and the mic release were printed for the
# user to paste, on the grounds that bindings.lua is theirs. Which is true, and
# the result was that they went unbound: a key nobody has bound is a feature
# nobody has. Offered instead, with three rules -- ask first, never overwrite a
# key that is already bound, and put the file back if Hyprland rejects it.
# Markers, not prose. The first version said "remove these if you remove the
# plugin", which is a chore handed to the user by something that could do it
# itself -- and not reliably findable afterwards. Anything written into someone
# else's file has to be removable by the thing that wrote it.
KEYBIND_BEGIN='-- >>> agentvoice keybinds'
KEYBIND_END='-- <<< agentvoice keybinds'
KEYBIND_LINES="
$KEYBIND_BEGIN (added by install.sh, removed by --uninstall)
o.bind(\"F8\", \"Talk to the agent (push-to-talk)\", \"agentvoice talk\")
o.bind(\"F8\", \"End the turn (push-to-talk)\", \"agentvoice talk-end\", { release = true })
o.bind(\"SUPER + CTRL + SPACE\", \"Stop the agent talking\", \"agentvoice interrupt\")
o.bind(\"SUPER + ALT + SPACE\", \"Release or re-engage the mic\", \"agentvoice mic\")
$KEYBIND_END"

# Hyprland reloads bindings.lua on save, so a bad edit is live at once and a
# broken one costs the user their keyboard. Shared by both directions: check,
# and put the file back if it is rejected.
reload_hypr_or_restore() {
  local file="$1" backup="$2"
  have hyprctl || return 0
  hyprctl reload >/dev/null 2>&1 || true
  # A healthy `hyprctl configerrors` prints two newlines and nothing else, so
  # blank lines have to be ignored; filtering only "ok" treated that whitespace
  # as an error and undid every good change.
  if [[ -n "$(hyprctl configerrors 2>/dev/null | grep -vE '^(ok)?$' || true)" ]]; then
    mv "$backup" "$file"
    hyprctl reload >/dev/null 2>&1 || true
    return 1
  fi
  return 0
}

# Take out exactly what was put in, and nothing else. Bindings mentioning
# agentvoice outside the markers were written or moved by hand: they are named
# on the way out rather than deleted, because guessing wrong here deletes
# someone's own work.
remove_keybinds() {
  local file="${XDG_CONFIG_HOME:-$HOME/.config}/hypr/bindings.lua"
  [[ -f $file ]] || return 0

  # -e, because the marker starts with "--" and grep parses that as the end of
  # its own options: without it the match silently never happened and the block
  # was left in place while the uninstall reported success.
  if grep -qF -e "$KEYBIND_BEGIN" "$file"; then
    local backup="$file.agentvoice-backup.$(date -u +%Y%m%d%H%M%S)"
    cp "$file" "$backup"
    local tmp="$file.agentvoice-tmp.$$"
    # Buffered, so the blank line the block was appended after goes with it.
    # That separator sits outside the markers, so a line-at-a-time filter left
    # it behind and the file grew a blank line on every install-and-remove.
    # These files are a few dozen lines; reading it whole costs nothing.
    awk -v b="$KEYBIND_BEGIN" -v e="$KEYBIND_END" '
      { line[NR] = $0 }
      END {
        for (i = 1; i <= NR; i++) {
          if (line[i] == "" && i < NR && index(line[i + 1], b) == 1) continue
          if (index(line[i], b) == 1) {
            while (i <= NR && index(line[i], e) != 1) i++
            continue
          }
          print line[i]
        }
      }' "$file" >"$tmp" && mv "$tmp" "$file"
    if reload_hypr_or_restore "$file" "$backup"; then
      ok "Removed the keybinds from ${file/#$HOME/~} (backup: ${backup/#$HOME/~})"
    else
      warn "Hyprland rejected the edit, so ${file/#$HOME/~} was put back."
    fi
  fi

  local loose
  loose="$(grep -n 'agentvoice ' "$file" 2>/dev/null || true)"
  if [[ -n $loose ]]; then
    echo
    warn "These mention agentvoice and were not added by this, so they stay:"
    printf '%s\n' "$loose" | sed 's/^/    /' >&2
  fi
}

offer_keybinds() {
  local file="${XDG_CONFIG_HOME:-$HOME/.config}/hypr/bindings.lua"

  show_keybinds() {
    echo
    printf '%s\n' "  Optional keybinds for ~/.config/hypr/bindings.lua:"
    printf '%s\n' "$KEYBIND_LINES" | sed 's/^/    /'
  }

  if [[ ! -f $file ]]; then
    # Not an Omarchy machine, or not one using bindings.lua. Print and leave.
    show_keybinds
    return 0
  fi

  if grep -q 'agentvoice talk' "$file"; then
    ok "Keybinds are already in ${file/#$HOME/~}"
    return 0
  fi

  # Never take a key someone is already using. Their own file binds F4, SUPER+E
  # and more; silently shadowing one of those would be worse than not binding.
  local taken=""
  local key
  for key in "F8" "SUPER + CTRL + SPACE" "SUPER + ALT + SPACE"; do
    grep -qF "o.bind(\"$key\"" "$file" && taken="$taken\n    $key"
  done

  if [[ -n $taken ]]; then
    warn "Not touching your keybinds: these are already bound in"
    warn "${file/#$HOME/~}:"
    printf "%b\n" "$taken" >&2
    show_keybinds
    return 0
  fi

  # String comparison, not arithmetic: KEYBINDS is empty when neither flag was
  # passed, and (( "" == 0 )) is true -- which would have meant never asking.
  if [[ $KEYBINDS == 0 ]]; then show_keybinds; return 0; fi
  if [[ $KEYBINDS != 1 ]]; then
    show_keybinds
    echo
    ask "Add these to ${file/#$HOME/~}?" || return 0
  fi

  local backup="$file.agentvoice-backup.$(date -u +%Y%m%d%H%M%S)"
  cp "$file" "$backup"
  printf '%s\n' "$KEYBIND_LINES" >>"$file"

  if ! reload_hypr_or_restore "$file" "$backup"; then
    warn "Hyprland rejected the change, so your bindings.lua was put back."
    show_keybinds
    return 0
  fi
  ok "Added the keybinds to ${file/#$HOME/~} (backup: ${backup/#$HOME/~})"
}

# --- taking it off again ---------------------------------------------------
# Written because there was no way to. Something that installs a daemon, a
# systemd unit, two commands on PATH and the better part of a gigabyte should
# be able to remove them, and a project that cannot be uninstalled is a bad
# guest on somebody's machine.
#
# What it deliberately does not touch: your voice. The trained verifiers and
# the clips they were built from are twenty-five recordings each and cannot be
# regenerated by any amount of downloading. Nor the keyring, which holds API
# keys this never created. Both are named on the way out so the choice is
# yours rather than ours.
if [[ $UNINSTALL == 1 ]]; then
  echo
  if ! (( ASSUME_YES )) && ! ask "Remove the agentvoice engine, service and commands?"; then
    echo "  Left alone."
    # Non-zero on purpose. The widget's Remove button chains this with
    # `&& omarchy plugin remove`, so exiting 0 here would decline the engine
    # and delete the user interface anyway -- the opposite of the answer.
    exit 1
  fi
  say "Removing agentvoice."

  if systemctl --user list-unit-files agentvoice.service &>/dev/null; then
    systemctl --user disable --now agentvoice.service 2>/dev/null || true
  fi
  rm -f "$UNITDIR/agentvoice.service"
  systemctl --user daemon-reload 2>/dev/null || true
  ok "Stopped and removed the user service"

  for cmd in agentvoice agentvoice-train-verifier; do
    rm -f "$BINDIR/$cmd"
  done
  ok "Removed the commands from $BINDIR"

  rm -rf "$VENV" "$MODELS"
  ok "Removed the environment and the models"

  # Last, because this script is usually running from inside it. That is
  # safe: bash holds the script's fd open, so the unlinked inode lives
  # until the last line runs. Measured, not assumed -- tests/test_uninstall.py
  # runs the installed copy and checks it reaches the end.
  if [[ -L $APP ]]; then rm -f "$APP"; else rm -rf "$APP"; fi
  ok "Removed the installed daemon"

  rm -rf "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentvoice"

  remove_keybinds

  # Existing and holding something are different questions, and the first one
  # is the wrong one: install.sh creates these directories itself, so asking
  # `-d` meant promising to have kept twenty-five recordings that were never
  # made, and leaving two empty directories and a parent behind to prove it.
  KEPT=""
  for d in verifiers verifier-training wakewords; do
    [[ -d "$DATA/$d" ]] || continue
    if [[ -n "$(ls -A "$DATA/$d" 2>/dev/null)" ]]; then
      KEPT="$KEPT\n    $DATA/$d"
    else
      rmdir "$DATA/$d" 2>/dev/null || true
    fi
  done
  # rmdir, not rm -rf: it goes only if something above was genuinely kept.
  rmdir "$DATA" 2>/dev/null || true

  # The key file fallback, for machines with no keyring. The keyring gets
  # named on the way out and this did not, so a key left here survived
  # unmentioned -- and when there was none, an empty directory survived
  # instead. Same question as above, asked of the other parent.
  # Named for what is actually in there. "Non-empty" was close enough while
  # endpoint.key was the only thing this directory ever held; vocab.txt lives
  # here too now, and a removal that announces "an API key you put in a file"
  # about a word list is telling the user something untrue about their own
  # secrets -- which is the worst subject to be loose about.
  CFGDIR="${XDG_CONFIG_HOME:-$HOME/.config}/agentvoice"
  KEPT_KEY=""
  KEPT_CFG=""
  if [[ -d $CFGDIR ]]; then
    [[ -f "$CFGDIR/endpoint.key" ]] && KEPT_KEY="$CFGDIR/endpoint.key"
    # Anything else in there is the user's too, and worth naming without
    # calling it a credential.
    if [[ -n "$(find "$CFGDIR" -maxdepth 1 -type f ! -name endpoint.key -print -quit 2>/dev/null)" ]]; then
      KEPT_CFG="$CFGDIR"
    fi
    [[ -z $KEPT_KEY && -z $KEPT_CFG ]] && rmdir "$CFGDIR" 2>/dev/null || true
  fi

  echo
  ok "Done."
  cat <<NEXT

  The bar widget is Omarchy's to remove:
    omarchy plugin remove duaneoca.agentvoice

  Left alone on purpose, because this did not create them and you may
  want them again:
NEXT
  [[ -n $KEPT ]] && printf "  Your voice — recordings and trained verifiers:%b\n\n" "$KEPT"
  [[ -n $KEPT_KEY ]] && printf "  An API key you put in a file:\n    %s\n\n" "$KEPT_KEY"
  [[ -n $KEPT_CFG ]] && printf "  Your own settings files:\n    %s\n\n" "$KEPT_CFG"
  cat <<NEXT
  API keys in the login keyring:
    secret-tool search --all service agentvoice
    secret-tool clear service agentvoice endpoint <host>

  Whisper's model cache, shared with anything else using it:
    ~/.cache/huggingface        ($(du -sh ~/.cache/huggingface 2>/dev/null | cut -f1 || echo "not present"))

  Settings in ~/.config/omarchy/shell.json. Keybinds this added to
  ~/.config/hypr/bindings.lua were taken back out, with a backup; any you
  wrote yourself were named above and left alone.
NEXT
  # --- and the widget, if asked -------------------------------------------
  # This used to be chained in the widget's own Remove button:
  #
  #   install.sh --uninstall && omarchy plugin remove duaneoca.agentvoice --yes
  #
  # which worked until it did not. `omarchy plugin remove` disabled the plugin,
  # removed its entry from shell.json, and its `rm -rf` then stopped partway:
  # .git, bin/, Panel.qml and more gone, daemon/, Settings.qml and the rest
  # still there. Nothing held the directory and it had no unusual attributes,
  # so the removal was interrupted rather than refused. The next thing the user
  # saw was `omarchy plugin add` refusing, because the id was still in use, with
  # no hint that a previous removal had not finished.
  #
  # It lives here instead of in a QML string for two reasons: shell logic
  # assembled in QML is exactly how the Remove button once swallowed its own
  # --uninstall flag, and a half-removed widget should be reported by something
  # that can check for it.
  if (( WITH_WIDGET )); then
    echo
    if have omarchy; then
      omarchy plugin remove duaneoca.agentvoice --yes || \
        warn "omarchy could not remove the widget."
    else
      warn "No omarchy command here, so the widget was left alone."
    fi
    WIDGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/duaneoca.agentvoice"
    if [[ -e $WIDGET_DIR ]]; then
      echo
      warn "The widget folder is still there, so this did not finish:"
      warn "    $WIDGET_DIR"
      warn "Adding the plugin again will refuse while it exists. Remove it with:"
      warn "    rm -rf \"$WIDGET_DIR\""
    fi
  fi

  exit 0
fi

# --- uv, the only thing we need from the system ---------------------------
if ! have uv; then
  say "Installing uv (the official 'extra' repo package)…"
  # Each branch may legitimately not apply: no Omarchy helper, no tty to take a
  # password, no pkexec to ask graphically. Tolerate all three and judge by
  # whether uv is there afterwards -- unguarded, the last branch failed with
  # bash's own "pkexec: command not found" and exit 127, which names the wrong
  # problem and is the only thing a non-Omarchy machine would have seen.
  if have omarchy-pkg-add; then omarchy-pkg-add uv || true
  elif [[ -t 0 ]] && have sudo; then sudo pacman -S --needed --noconfirm uv || true
  elif have pkexec; then pkexec pacman -S --needed --noconfirm uv || true
  fi
fi

# uv is not in the dependency check at the top because this tries to install it.
# It still has to be here afterwards, and the message has to say so in the same
# terms as the others rather than as a stack of shell errors.
if ! have uv; then
  echo "  missing required tool: uv, and it could not be installed for you" >&2
  echo "  install it with: sudo pacman -S uv" >&2
  echo "  or see: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
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

# Settings still override the default: if the wake engine is already set to
# openWakeWord, installing without it produces a daemon configured for an
# engine it cannot start. This used to run ahead of a prompt; it now runs ahead
# of the default being off.
if [[ -z $WANT_OWW ]] && have jq; then
  if [[ "$(jq -r '[.bar.layout[]?[]? | select(.id=="duaneoca.agentvoice")][0].engine // empty' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json" 2>/dev/null)" == "openwakeword" ]]; then
    say "Settings already use openWakeWord; installing it."
    WANT_OWW=1
  fi
fi

# No prompt here any more. openWakeWord is optional -- the default engine is
# Vosk and the daemon falls back to it -- but this asked during every fresh
# install, with gum's affirmative preselected, which is a demand wearing a
# question mark. An optional component belongs where someone goes looking for
# it, so the settings page offers it: the engine dropdown marks it as not
# installed, and choosing it explains and installs.
#
# `--yes` no longer implies it either. That was added because --yes used to
# reach ask(), which declines, so unattended installs silently skipped the
# engine with real rejection -- a sensible fix while this was a prompt. With no
# prompt, "yes to everything" has nothing to answer, and having --yes install a
# component the interactive path does not offer is the same inconsistency from
# the other side. --oww is the way to ask for it.
if [[ -z $WANT_OWW ]]; then
  WANT_OWW=0
  cat <<'WHY'

  Wake word: Vosk, which takes any phrase. It scores phonetic neighbours as
  high as the real one -- measured here, "hey cloud" scores 1.00 against a
  "hey claude" grammar, which is how a podcast wakes it.

  openWakeWord has four fixed phrases, real rejection, and can be trained on
  your own voice. It is a further 154MB and is not installed by default.
  Choose it under Engine in the Agent Voice settings, or re-run this with
  --oww.

WHY
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

# What the settings already ask for. An uninstall leaves shell.json alone,
# so a reinstall that fetched only the default would leave the voice set to
# something that is not there -- which degrades to silence with the reason in
# a log nobody reads.
configured() {
  local key="$1"
  local file="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json"
  have jq || return 0
  # Both guards matter under `set -e`. jq exits 2 on a file that is not there,
  # and this runs inside a command substitution being assigned, so that 2 left
  # the whole installer dead -- after 700MB of downloads, with nothing printed.
  # A machine with no shell.json is not an error: it is every machine where the
  # widget has not been configured yet, which is most of them on a first run.
  # Neither of the two machines this was written on could reproduce it.
  [[ -f $file ]] || return 0
  jq -r --arg k "$key" \
    '[.bar.layout[]?[]? | select(.id=="duaneoca.agentvoice")][0][$k] // empty' \
    "$file" 2>/dev/null || true
}

fetch_vosk
fetch_voice lessac medium

# Whatever was asked for on the command line, then whatever the settings ask
# for. Both, because a --voice run is also an install and the configured voice
# still has to be present afterwards.
# Deduplicated, and silent about the ones already here: fetch_voice announces
# a real download and returns early otherwise, so an extra line of its own said
# "fetching" twice for one voice and once for a voice already on disk.
fetched=""
for v in "$WANT_VOICE_ARG" "$(configured voice)"; do
  [[ -n $v && $v != lessac-medium ]] || continue
  [[ " $fetched " == *" $v "* ]] && continue
  fetched="$fetched $v"
  fetch_voice "${v%-*}" "${v##*-}"
done

# --- the daemon itself -----------------------------------------------------
# Copied out of the plugin directory so removing the bar widget cannot delete
# a running service, and so the uninstaller survives to be run. In a checkout
# --dev links instead, because editing a copy and restarting the original is
# a bad afternoon.
# When $APP *is* where this script lives, `rm -rf "$APP"` deletes the source it
# is about to copy from; the cp then fails and set -e leaves an empty app/ and
# a broken install. That is reachable: the widget's Remove button runs
# $APP/install.sh, and a mis-passed flag once turned that into an install.
if (( SELF_IS_APP )); then
  ok "The daemon in $APP is already the one being installed"
elif [[ $DEV == 1 ]]; then
  rm -rf "$APP"
  ln -s "$ROOT" "$APP"
  ok "Linked $APP -> $ROOT (development)"
else
  rm -rf "$APP"
  mkdir -p "$APP"
  cp -r "$ROOT/daemon" "$ROOT/bin" "$ROOT/desktop" "$APP/"
  cp "$ROOT/install.sh" "$ROOT/requirements.txt" \
     "$ROOT/requirements-openwakeword.txt" "$APP/"
  ok "Installed the daemon into $APP"
fi

# --- commands on PATH ------------------------------------------------------
mkdir -p "$BINDIR"
for cmd in agentvoice agentvoice-train-verifier; do
  ln -sf "$APP/bin/$cmd" "$BINDIR/$cmd"
done
ok "Installed agentvoice and agentvoice-train-verifier into $BINDIR"

# --- service ---------------------------------------------------------------
# Written rather than copied, so the unit carries this checkout's real path.
mkdir -p "$UNITDIR"
sed -e "s|@ROOT@|$APP|g" -e "s|@VENV@|$VENV|g" \
    "$ROOT/desktop/agentvoice.service.in" > "$UNITDIR/agentvoice.service"
# Unguarded, this was the last line of a successful install and could undo it:
# `set -e` turned a machine with no systemd user session -- a container, an ssh
# login without one, a distribution that is not using systemd -- into an exit
# after every file was already in place, with the failure attributed to
# nothing. The uninstall path has always tolerated this; the install path did
# not. Say what happened and carry on, because everything needed to start it by
# hand is installed by this point.
if ! systemctl --user daemon-reload 2>/dev/null; then
  warn "No systemd user session here, so the service was not registered."
  warn "The unit is at $UNITDIR/agentvoice.service; run 'agentvoice start' to"
  warn "run the daemon in the foreground instead."
fi
ok "Installed the user service"

echo
ok "Done."
cat <<NEXT

  Add the bar widget, if you have not already:
    omarchy plugin add https://github.com/duaneoca/agent-voice.git --enable

  Start listening:
    agentvoice start

  The Whisper model downloads on first use (~75MB).

NEXT

offer_keybinds
