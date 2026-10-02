# Contributing

The working notes for this project live in [CLAUDE.md](CLAUDE.md), and they
are the real document: what earns a test, how to run the installer against a
machine that is not yours, why a setting is three things rather than one, the
four ways the permission system can fail open, and which claims in the README
have measurements behind them.

It is named for the tool that reads it automatically, which is a poor name for
something a person should read first. Hence this file.

The short version:

```bash
./bin/agentvoice-check                     # before every commit, ~30s
AGENTVOICE_BOX=1 ./bin/agentvoice-check    # plus eight real installs, ~10min
```

Run the first one. Run the second if you touched `install.sh` — every install
bug this project has had hid behind something the development machine already
had.
