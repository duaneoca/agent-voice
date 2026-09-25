#!/usr/bin/env bash
# Run install.sh against a machine that is not this one.
#
# Every install bug found so far was invisible here because this machine
# already had what the installer assumed: a configured widget in shell.json, a
# systemd user session, Omarchy's own commands, gum, uv, and a warm uv cache.
# Two of those assumptions ended a fresh install outright, and neither could be
# reproduced without taking them away.
#
# bubblewrap rather than Docker: unprivileged user namespaces work here and the
# Docker daemon needs a password. What this does give is an empty HOME, a
# PATH with named commands withheld, a cold uv cache and no systemd user bus.
# What it does not give is a different distribution or a different set of
# system libraries -- for that, run the same scenarios in a container.
set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PASS=0; FAIL=0

# A PATH holding everything except the commands a scenario withholds. Listing
# what to keep was tried and rots: the first attempt forgot `dirname`, which
# install.sh calls on line 22.
slim_path() {
  local dir="$1"; shift
  local withheld=" $* "
  mkdir -p "$dir"
  local entry tool
  for entry in ${PATH//:/ }; do
    [[ -d $entry ]] || continue
    for tool in "$entry"/*; do
      tool="$(basename "$tool")"
      [[ $withheld == *" $tool "* ]] && continue
      [[ -e "$dir/$tool" ]] && continue
      ln -s "$entry/$tool" "$dir/$tool" 2>/dev/null || true
    done
  done
}

run_case() {
  local name="$1" expect="$2"; shift 2
  local box; box="$(mktemp -d)"
  local bin="$box/bin"
  slim_path "$bin" "$@"

  # No systemd user bus, no shell.json, no prior install, cold uv cache.
  local out="$box/log"
  # No --tmpfs over $HOME: a fresh mktemp directory is already empty, and a
  # tmpfs exists only inside the namespace -- so the checks below, which run
  # on the outside, found nothing and called a real install a failure.
  bwrap --dev-bind / / \
        --unsetenv DBUS_SESSION_BUS_ADDRESS \
        --unsetenv XDG_RUNTIME_DIR \
        --setenv HOME "$box/home" \
        --setenv PATH "$bin" \
        --setenv XDG_DATA_HOME "$box/home/.local/share" \
        --setenv XDG_CONFIG_HOME "$box/home/.config" \
        --setenv XDG_RUNTIME_DIR "$box/run" \
        --setenv UV_CACHE_DIR "$box/uvcache" \
        -- bash -c "mkdir -p '$box/home' '$box/run' && '$ROOT/install.sh' $INSTALL_ARGS" \
        >"$out" 2>&1 </dev/null
  local rc=$?

  # Exit 0 is not the claim. An installer that returns 0 having done nothing is
  # the same shape of false green as a skipped test suite, and this project has
  # shipped that twice. On success, the pieces have to be on disk; on a refusal,
  # nothing may be.
  local why="" data="$box/home/.local/share/agentvoice"
  if [[ $rc == 0 ]]; then
    local piece
    for piece in "$data/app/daemon/wake_listen.py" "$data/venv/bin/python" \
                 "$data/models/piper" "$box/home/.local/bin/agentvoice" \
                 "$box/home/.config/systemd/user/agentvoice.service"; do
      [[ -e $piece ]] || why="$why missing:${piece#"$box/home/"}"
    done
    [[ -n $(find "$data/models" -name '*.onnx' 2>/dev/null) ]] ||
      why="$why no-model-downloaded"
  else
    [[ -d $data ]] && why="$why refused-but-left:$data"
  fi

  if [[ $rc == "$expect" && -z ${why// } ]]; then
    printf '  %-44s ok (exit %s)\n' "$name" "$rc"; PASS=$((PASS + 1))
  else
    printf '  %-44s FAIL: exit %s, wanted %s%s\n' "$name" "$rc" "$expect" "$why"
    tr '\r' '\n' < "$out" | grep -vE '^#|^ *[0-9.]+%$' | tail -8 | sed 's/^/      /'
    FAIL=$((FAIL + 1))
  fi
  rm -rf "$box"
}

# An install followed by its own uninstall, in the same box, checking that
# nothing agentvoice put there survives. Run separately from run_case because
# it asserts the opposite thing.
round_trip() {
  local name="$1"; shift
  local box; box="$(mktemp -d)"
  local bin="$box/bin"
  slim_path "$bin" "$@"
  local data="$box/home/.local/share/agentvoice"

  bwrap --dev-bind / / \
        --unsetenv DBUS_SESSION_BUS_ADDRESS \
        --setenv HOME "$box/home" \
        --setenv PATH "$bin" \
        --setenv XDG_DATA_HOME "$box/home/.local/share" \
        --setenv XDG_CONFIG_HOME "$box/home/.config" \
        --setenv XDG_RUNTIME_DIR "$box/run" \
        --setenv UV_CACHE_DIR "$box/uvcache" \
        -- bash -c "
          set -e
          mkdir -p '$box/home' '$box/run'
          '$ROOT/install.sh' --no-oww >/dev/null 2>&1
          [[ -x '$data/app/install.sh' ]] || { echo 'no installed copy'; exit 1; }
          '$data/app/install.sh' --uninstall --yes >/dev/null 2>&1
        " >"$box/log" 2>&1 </dev/null
  local rc=$?

  local why=""
  [[ -d $data ]] && why="$why data-left"
  [[ -e $box/home/.local/bin/agentvoice ]] && why="$why command-left"
  [[ -e $box/home/.config/systemd/user/agentvoice.service ]] && why="$why unit-left"

  if [[ $rc == 0 && -z ${why// } ]]; then
    printf '  %-44s ok\n' "$name"; PASS=$((PASS + 1))
  else
    printf '  %-44s FAIL: exit %s%s\n' "$name" "$rc" "$why"
    tail -6 "$box/log" | sed 's/^/      /'
    FAIL=$((FAIL + 1))
  fi
  rm -rf "$box"
}

echo "install.sh against a machine that is not this one:"

INSTALL_ARGS="--no-oww"
run_case "no Omarchy commands, no gum"            0 gum omarchy omarchy-pkg-add
run_case "no shell.json and no systemd user bus"  0 gum
run_case "no jq"                                  1 jq
run_case "no curl"                                1 curl
run_case "no bsdtar"                              1 bsdtar
# No uv and no way to get it: omarchy-pkg-add is gone, stdin is not a tty so
# the sudo branch is skipped, and pkexec cannot prompt without a session. A
# refusal is the right answer; dying halfway through is not.
run_case "no uv and no way to install it"         1 uv omarchy omarchy-pkg-add pkexec sudo

INSTALL_ARGS="--oww"
run_case "with openWakeWord, no Omarchy, no gum"  0 gum omarchy omarchy-pkg-add

round_trip "install then uninstall leaves nothing"  gum omarchy omarchy-pkg-add

echo
echo "  $PASS passed, $FAIL failed"
[[ $FAIL == 0 ]]
