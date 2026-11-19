#!/usr/bin/env bash
# Run as the Codex user, outside an active Codex sandbox.
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
    echo 'Run as the Codex user (ai-agent), without sudo.' >&2
    exit 1
fi
python3 - <<'PY'
from pathlib import Path
import shutil
import subprocess
import time

launcher = Path.home() / '.local/bin/srv-codex'
if launcher.is_symlink() or not launcher.is_file():
    raise SystemExit(f'Expected a regular launcher file: {launcher}')
original = launcher.read_text()
command = 'codex --sandbox workspace-write -c sandbox_workspace_write.network_access=true "$@"'
if command in original.splitlines():
    print('Network access is already configured for future launches.')
else:
    if original.splitlines().count('codex') != 1:
        raise SystemExit('Unexpected launcher contents; no changes made.')
    updated = original.replace('\ncodex\n', '\n' + command + '\n', 1)
    if updated == original:
        raise SystemExit('Launch command not found; no changes made.')
    subprocess.run(['bash', '-n'], input=updated, text=True, check=True)
    backup = launcher.with_name(launcher.name + f'.backup-network-{time.time_ns()}')
    shutil.copy2(launcher, backup)
    launcher.write_text(updated)
    print(f'Updated: {launcher}')
    print(f'Backup: {backup}')
print('The current Codex process is unchanged.')
print('Exit Codex with /quit, then run: ~/.local/bin/srv-codex')
print('To resume this conversation, run: ~/.local/bin/srv-codex resume --last')
PY
