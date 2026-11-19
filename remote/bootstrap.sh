#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 24.04 ]; then
    printf 'Expected Ubuntu 24.04, got %s %s. Stop for review.\n' "$ID" "$VERSION_ID" >&2
    exit 1
fi
agent_user=__AGENT_USER__
public_key=__PUBLIC_KEY__
if id "$agent_user" >/dev/null 2>&1; then
    if [ "$(id -u "$agent_user")" -lt 20000 ] || [ "$(id -u "$agent_user")" -gt 20999 ]; then
        printf 'Existing user is outside managed UID range 20000..20999. Stop for review.\n' >&2
        exit 1
    fi
    test "$(getent passwd "$agent_user" | cut -d: -f6)" = "/home/$agent_user"
    if id -nG "$agent_user" | tr ' ' '\n' | grep -Eq '^(sudo|admin|docker|lxd)$'; then
        printf 'Existing user has elevated group membership. Stop for review.\n' >&2
        exit 1
    fi
else
    # Container process UIDs may be absent from /etc/passwd; avoid those as well.
    agent_uid="$(python3 - <<'PY'
import pwd
from pathlib import Path
used = {entry.pw_uid for entry in pwd.getpwall()}
for entry in Path('/proc').iterdir():
    if entry.name.isdigit():
        try:
            for line in (entry / 'status').read_text().splitlines():
                if line.startswith('Uid:'):
                    used.update(map(int, line.split()[1:]))
        except (OSError, ProcessLookupError):
            pass
for value in range(20000, 21000):
    if value not in used:
        print(value)
        break
else:
    raise SystemExit('No unused UID in 20000..20999')
PY
)"
    useradd --uid "$agent_uid" --create-home --shell /bin/bash "$agent_user"
fi
agent_home="/home/$agent_user"
test ! -L "$agent_home"
test ! -L "$agent_home/.ssh"
install -d -m 700 -o "$agent_user" -g "$(id -gn "$agent_user")" "$agent_home/.ssh"
file="$agent_home/.ssh/authorized_keys"
test ! -L "$file"
if [ -f "$file" ]; then cp -p "$file" "$file.srv-backup-$(date +%s%N)"; fi
touch "$file"
if ! awk -v wanted="$public_key" '$1 " " $2 == wanted {found=1} END {exit !found}' "$file"; then
    printf '\n%s\n' "$public_key" >> "$file"
fi
chown "$agent_user:$(id -gn "$agent_user")" "$file"
chmod 600 "$file"
chmod 700 "$agent_home"
install -d -m 700 -o "$agent_user" -g "$(id -gn "$agent_user")" "$agent_home/workspaces"
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
    install -y --no-install-recommends ca-certificates curl git tmux python3 wireguard-tools rsync sshfs
# Do not change SSH, firewall or default routes during initial provisioning.
printf 'BASE_PACKAGES_AND_USER_READY\n'
