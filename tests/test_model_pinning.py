"""What arrives from the network is checked before it is installed.

The voices and the Vosk model came from a mutable location --
huggingface.co/.../resolve/main is a branch, not a version -- with nothing
checking what came back. Two consequences: an install of the same commit was
not reproducible, and nothing would have noticed a substitution. A Piper
voice is an ONNX model the daemon loads, so a bad one is at minimum an attack
on a parser.

These drive install.sh's own download() rather than a copy of it.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "models.lock"


def _function(name: str) -> str:
    """Lift one function out of install.sh so a test can drive it."""
    lines = (ROOT / "install.sh").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _harness(tmp_path: Path, served: bytes, lock: str, name: str = "thing.bin") -> tuple:
    """install.sh's download(), pointed at a local file instead of the network.

    `curl` is stubbed rather than mocked at a higher level, so the real
    function -- temp file, hash check, rename -- is what runs.
    """
    (tmp_path / "served").write_bytes(served)
    (tmp_path / "models.lock").write_text(lock)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    # Accepts curl's flags, copies the local file to wherever -o points.
    (bin_ / "curl").write_text(
        '#!/bin/bash\nout=""\nwhile [ $# -gt 0 ]; do\n'
        '  case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) shift ;; esac\n'
        'done\ncp "$SERVED" "$out"\n')
    (bin_ / "curl").chmod(0o755)

    script = (
        "set -uo pipefail\n"
        'warn() { echo "WARN: $*" >&2; }\n'
        f'MODELS_LOCK="{tmp_path}/models.lock"\n'
        + _function("pinned_sha")
        + _function("download")
        + f'download https://example.invalid/{name} "{tmp_path}/dest.bin" {name}\n'
        'echo "exit=$?"\n'
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_}{os.pathsep}{os.environ['PATH']}"
    env["SERVED"] = str(tmp_path / "served")
    done = subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)
    return done, tmp_path / "dest.bin"


class TestDownloadVerification:
    def test_a_matching_file_is_installed(self, tmp_path):
        body = b"the real model"
        digest = hashlib.sha256(body).hexdigest()
        done, dest = _harness(tmp_path, body, f"sha256 thing.bin {digest}\n")
        assert "exit=0" in done.stdout, done.stderr
        assert dest.read_bytes() == body

    def test_a_substituted_file_is_refused_and_not_left_behind(self, tmp_path):
        """The case the hash exists for."""
        wanted = hashlib.sha256(b"the real model").hexdigest()
        done, dest = _harness(tmp_path, b"something else entirely",
                              f"sha256 thing.bin {wanted}\n")
        assert "exit=0" not in done.stdout
        assert not dest.exists(), "a file that failed its hash was installed"
        assert "does not match" in done.stderr

    def test_a_truncated_file_is_refused(self, tmp_path):
        """A dropped connection used to leave a short file under the final
        name, which every later run then skipped as "already here"."""
        full = b"x" * 4096
        digest = hashlib.sha256(full).hexdigest()
        done, dest = _harness(tmp_path, full[:100], f"sha256 thing.bin {digest}\n")
        assert not dest.exists()

    def test_a_file_with_no_recorded_hash_is_not_installed(self, tmp_path):
        """Refusing is the point: an unpinned name is one nobody has
        vouched for, and the message says how to vouch for it."""
        done, dest = _harness(tmp_path, b"anything", "sha256 other.bin deadbeef\n")
        assert not dest.exists()
        assert "no hash recorded" in done.stderr
        assert "pin-models.sh" in done.stderr

    def test_no_temp_files_are_left_in_the_target_directory(self, tmp_path):
        wanted = hashlib.sha256(b"the real model").hexdigest()
        _harness(tmp_path, b"wrong", f"sha256 thing.bin {wanted}\n")
        assert not list(tmp_path.glob(".download.*"))


class TestTheLockItself:
    def test_it_pins_a_revision_rather_than_a_branch(self):
        import re
        text = LOCK.read_text()
        assert re.search(r"^revision piper [0-9a-f]{40}$", text, re.M), \
            "a branch name is not a version"

    def test_every_hash_is_a_sha256(self):
        import re
        for name, digest in re.findall(r"^sha256 (\S+)\s+(\S+)$",
                                       LOCK.read_text(), re.M):
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (name, digest)

    def test_both_files_of_every_voice_are_pinned(self):
        """A voice whose .json never arrived loads as a stack trace."""
        import re
        names = set(re.findall(r"^sha256 (\S+)", LOCK.read_text(), re.M))
        for name in sorted(n for n in names if n.endswith(".onnx")):
            assert f"{name}.json" in names, f"{name} has no .json hash"

    def test_the_generator_exists_and_parses(self):
        """The refresh path has to be a script, not a paragraph: a hash
        nobody can regenerate becomes a hash nobody updates."""
        gen = ROOT / "bench" / "pin-models.sh"
        assert gen.is_file() and os.access(gen, os.X_OK)
        done = subprocess.run(["bash", "-n", str(gen)], capture_output=True)
        assert done.returncode == 0, done.stderr

    def test_the_generator_and_the_lock_agree_on_the_voice_list(self):
        """Adding a voice to one and not the other is how a voice the UI
        offers becomes one the installer refuses."""
        import re
        gen = (ROOT / "bench" / "pin-models.sh").read_text()
        listed = re.search(r"VOICES=\(([^)]*)\)", gen, re.S)
        assert listed, "the generator must name the voices it pins"
        voices = listed.group(1).split()
        names = set(re.findall(r"^sha256 (\S+)", LOCK.read_text(), re.M))
        for v in voices:
            assert f"en_US-{v}.onnx" in names, f"{v} is generated but not locked"
        assert len([n for n in names if n.endswith(".onnx")]) == len(voices)
