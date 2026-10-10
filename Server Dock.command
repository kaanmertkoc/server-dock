#!/bin/zsh
# Finder opens .command files in Terminal; use the installed isolated app.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
if ! command -v server-dock >/dev/null 2>&1; then
    print "Install Server Dock first: run 'uv tool install .' in the project folder."
    read "?Press Return to close."
    exit 1
fi
exec server-dock
