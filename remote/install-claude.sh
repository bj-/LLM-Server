#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" != 0
umask 077
export PATH="$HOME/.local/bin:$PATH"
install -d -m 700 "$HOME/.local/bin" "$HOME/.local/share/srv/setup"
if ! command -v claude >/dev/null 2>&1; then
    curl --fail --show-error --location --proto '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 180 \
        https://claude.ai/install.sh -o "$HOME/.local/share/srv/setup/claude-install.sh"
    sha256sum "$HOME/.local/share/srv/setup/claude-install.sh"
    bash "$HOME/.local/share/srv/setup/claude-install.sh" stable
fi
claude --version
for file in "$HOME/.local/bin/srv-claude" "$HOME/.local/bin/srv-session-claude"; do
    test ! -L "$file"
    if [ -f "$file" ]; then cp -p "$file" "$file.srv-backup-$(date +%s%N)"; fi
done
cat > "$HOME/.local/bin/srv-claude" <<'CLAUDE'
#!/usr/bin/env bash
set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces"
claude
result=$?
printf '\nClaude exited with code %s. Run claude --continue or claude to continue.\n' "$result"
exec bash -i
CLAUDE
cat > "$HOME/.local/bin/srv-session-claude" <<'SESSION'
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
case "${TERM:-}" in
    ''|dumb) export TERM=xterm-256color ;;
esac
# One command queue also works when no tmux server is running yet.
exec tmux start-server \; set-option -g mouse on \; new-session -A -s claude -c "$HOME/workspaces" "$HOME/.local/bin/srv-claude"
SESSION
chmod 700 "$HOME/.local/bin/srv-claude" "$HOME/.local/bin/srv-session-claude"
printf 'CLAUDE_AND_TMUX_LAUNCHER_READY\n'
