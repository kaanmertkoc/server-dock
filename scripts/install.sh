#!/bin/sh
# Install a reviewed commit without resolving new dependency versions.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
revision=${1:-}
case "$revision" in ''|*[!0-9a-f]*) echo "Usage: $0 <full reviewed commit SHA>" >&2; exit 2;; esac
[ "${#revision}" -eq 40 ] || { echo "Use a full 40-character commit SHA." >&2; exit 2; }
[ "$(git rev-parse HEAD)" = "$revision" ] || { echo "Check out the reviewed commit first." >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Install from a clean, reviewed checkout." >&2; exit 1; }

install_dir=${SERVER_DOCK_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/server-dock}
bin_dir=${SERVER_DOCK_BIN_DIR:-$HOME/.local/bin}
if [ -d "$bin_dir/server-dock" ] || { [ -e "$bin_dir/server-dock" ] && [ ! -L "$bin_dir/server-dock" ]; }; then
    echo "Refusing to replace an existing file or directory: $bin_dir/server-dock" >&2
    exit 1
fi
mkdir -p "$install_dir/releases" "$bin_dir"
install_dir=$(CDPATH= cd -- "$install_dir" && pwd)
stage=$(mktemp -d "$install_dir/releases/$revision.XXXXXX")
installed=no
cleanup() { if [ "$installed" != yes ]; then rm -rf -- "$stage"; fi; }
trap cleanup 0
trap 'exit 1' HUP INT TERM
export UV_PROJECT_ENVIRONMENT="$stage/venv"
# First verify every third-party wheel from uv.lock; no dependency builds.
uv sync --locked --no-default-groups --group build --no-install-project --no-build \
    --python "${SERVER_DOCK_PYTHON:-python3}" --no-python-downloads
# The only build is our reviewed source, using the already verified backend offline.
uv sync --locked --no-default-groups --group build --no-build-isolation --no-editable \
    --offline --python "${SERVER_DOCK_PYTHON:-python3}" --no-python-downloads
"$UV_PROJECT_ENVIRONMENT/bin/server-dock" --version
ln -sfn "$UV_PROJECT_ENVIRONMENT/bin/server-dock" "$bin_dir/server-dock"
installed=yes
printf 'Installed commit %s. Run %s/server-dock\n' "$revision" "$bin_dir"
