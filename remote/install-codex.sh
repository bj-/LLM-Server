#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" != 0
umask 077
export PATH="$HOME/.local/bin:$PATH"
install -d -m 700 "$HOME/.local/bin" "$HOME/.local/share/srv/setup"
if ! command -v codex >/dev/null 2>&1; then
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 180 \
        https://chatgpt.com/codex/install.sh -o "$HOME/.local/share/srv/setup/codex-install.sh"
    sha256sum "$HOME/.local/share/srv/setup/codex-install.sh"
    CODEX_NON_INTERACTIVE=1 sh "$HOME/.local/share/srv/setup/codex-install.sh"
fi
codex --version
for file in "$HOME/.local/bin/srv-codex" "$HOME/.local/bin/srv-session"; do
    test ! -L "$file"
    if [ -f "$file" ]; then cp -p "$file" "$file.srv-backup-$(date +%s%N)"; fi
done
cat > "$HOME/.local/bin/srv-codex" <<'CODEX'
#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces"
if ! codex login status >/dev/null 2>&1; then
    printf 'Sign in with your personal ChatGPT subscription using the URL and device code below.\n'
    if ! codex login --device-auth; then
        printf '\nLogin did not complete. This shell stays open for diagnosis or codex login retry.\n'
        exec bash -i
    fi
fi
set +e
codex --sandbox workspace-write -c sandbox_workspace_write.network_access=true "$@"
result=$?
printf '\nCodex exited with code %s. Run codex resume or codex to continue.\n' "$result"
exec bash -i
CODEX
cat > "$HOME/.local/bin/srv-session" <<'SESSION'
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
case "${TERM:-}" in
    ''|dumb) export TERM=xterm-256color ;;
esac
# One command queue also works when no tmux server is running yet.
exec tmux start-server \; set-option -g mouse on \; new-session -A -s codex -c "$HOME/workspaces" "$HOME/.local/bin/srv-codex"
SESSION
chmod 700 "$HOME/.local/bin/srv-codex" "$HOME/.local/bin/srv-session"
printf 'CODEX_AND_TMUX_LAUNCHER_READY\n'
