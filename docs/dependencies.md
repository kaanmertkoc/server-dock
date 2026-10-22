# Dependency policy

`uv.lock` records exact versions and SHA-256 hashes for the complete runtime and
build dependency tree. The supported installer uses uv **0.12.8**, refuses a dirty
checkout or mismatched commit, and installs third-party wheels with `--locked`
and `--no-build` into a fresh environment. It then builds only Server Dock,
offline, with the locked setuptools backend. The executable changes only after
installation succeeds. Existing environments are retained under
`~/.local/share/server-dock/releases/` for rollback.

## Install a reviewed revision

Install Python 3.10+ and uv 0.12.8 from their official distributions. Review the
source and lockfile, choose a full 40-character commit SHA, then:

```sh
git clone https://github.com/kaanmertkoc/server-dock.git
cd server-dock
git checkout --detach <reviewed-commit-sha>
./scripts/install.sh <reviewed-commit-sha>
server-dock --version
```

Replace both placeholders with the same reviewed SHA; a branch name or moving tag
is not sufficient. Choose a commit containing this installer. Add `~/.local/bin`
to your PATH if needed. Use `SERVER_DOCK_PYTHON` to choose an existing Python executable. Updates
use the same process with a newly reviewed commit; do not update automatically.
A plain `pip install .`, `pipx install .`, or `uv tool install` does **not** enforce
this project's transitive lockfile and is outside this installation policy.

## What gets installed

The app directly imports Textual and Rich, so both are declared dependencies.
The current runtime tree has **nine third-party packages**:

```text
server-dock
├── textual 8.2.8
│   ├── rich 15.0.0 → markdown-it-py, pygments
│   ├── markdown-it-py 4.2.0 [linkify] → mdurl 0.1.2, linkify-it-py 2.2.0
│   ├── mdit-py-plugins 0.6.1 → markdown-it-py
│   ├── platformdirs 4.13.0 (4.12.4 on Python 3.10)
│   ├── pygments 2.21.0
│   └── typing-extensions 4.16.0
└── rich 15.0.0 (shared above)
```

The build environment also contains **setuptools 84.0.0**. Optional Textual
syntax-highlighting extras are not installed. Python, uv, Git, the operating
system, and GitHub-hosted runner images remain outside the Python lockfile;
CI separately pins uv's version and Linux archive checksum.

## Checks and updates

- CI installs the exact checkout through the same verified installer, tests
  Python 3.10/3.12/3.14, and builds distributions offline.
- The standard-library OSV scanner checks **all locked versions**, including
  Python-specific alternatives and build tools, on pushes, PRs, and weekly.
  Advisories, incomplete responses, or network errors fail the scan. Run it
  locally with `python3.12 scripts/audit_dependencies.py`.
- Dependabot proposes weekly Python and GitHub Actions update PRs with a
  seven-day cooldown for routine releases. Transitive packages are included.
  There is no automatic merging. Review version changes, hashes, upstream
  release notes, and the CI diff before merging.
- uv's seven-day resolution cooldown also applies to manual `uv lock --upgrade`.
  For an urgent security fix, explicitly review and override the cutoff for
  that update; waiting is not the right default for an active vulnerability.
- All Actions use full commit SHAs, read-only repository permissions, and no
  persisted checkout credentials. When updating uv, update its required version
  and CI archive checksum together. Keep setuptools' build-system and build-group
  pins identical. CI checks that these match.

Hashes reject substituted artifacts; they do not prove a package is trustworthy.
OSV reports known vulnerabilities, not every malicious release. A virtual
environment isolates Python packages, **not your macOS permissions**. Dependency
code still runs as your user. Review is required before the initial lock and
every update; no setup can guarantee a dependency is safe.

## Development and packaging

Use the checked-in lock without resolving newer versions:

```sh
uv sync --locked --no-default-groups --group build --no-install-project --no-build
uv sync --locked --no-default-groups --group build --no-build-isolation --offline
.venv/bin/python -m unittest -v
uv build --offline --no-build-isolation --python .venv/bin/python
```

The second development command installs this checkout as editable. The user
installer builds a non-editable copy, so later checkout edits cannot change the
installed app. There is deliberately no separate floating `requirements.txt`.

## Existing Kaan's Dock data

Automatic fallback to `~/.config/kaans-dock/` has been removed. New defaults are
always under `~/.config/server-dock/`; `settings.json` still remembers the display
name and `themes.json` still remembers the theme. Before switching, copy any old
`servers.json` and `themes.json` you want to retain into the new directory after
checking for existing files. Keep the originals and never overwrite newer data.
Alternatively, explicitly pass the old files with `--data` and `--themes`.
