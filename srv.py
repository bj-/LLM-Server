#!/usr/bin/env python3
"""Переносимый launcher персонального сервера CLI-агентов. Python 3.9+."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Порт должен быть целым числом") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Порт должен быть в диапазоне 1–65535")
    return port


def host_name(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value):
            raise argparse.ArgumentTypeError("Некорректный IP или DNS-адрес")
    return value


def user_name(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", value) or value == "root":
        raise argparse.ArgumentTypeError("Нужен непривилегированный Linux-пользователь")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["setup", "keygen", "enroll", "doctor", "bootstrap", "agent-install", "git-key", "git-identity", "vpn-stage", "connect", "verify", "files-add", "files-sync", "files-stop", "guide"])
    p.add_argument("--host", required=True, type=host_name)
    p.add_argument("--port", type=port_number, default=22)
    p.add_argument("--user", type=user_name, default="ai-agent")
    p.add_argument("--agent", choices=["codex", "claude"], default="codex")
    p.add_argument("--git-name", help="Имя автора Git; без флага берётся локальное git config")
    p.add_argument("--git-email", help="Email автора Git; без флага берётся локальное git config")
    p.add_argument("--identity", type=Path)
    p.add_argument("--wg-config", type=Path, help="Готовый WG-конфиг (подключение отдельным этапом)")
    p.add_argument("--trust-host-fingerprint", help="Явно подтверждённый SHA256-отпечаток нового Ed25519-ключа хоста")
    p.add_argument("--state-dir", type=Path, default=ROOT / ".local")
    p.add_argument("--folder", type=Path, help="Выбранная локальная папка")
    p.add_argument("--mode", choices=["live", "sync", "both"], default="both")
    p.add_argument("--read-write", action="store_true", help="Разрешить запись в выбранную локальную папку")
    p.add_argument("--watch", action="store_true", help="Повторять синхронизацию, пока работает launcher")
    p.add_argument("--sync-interval", type=port_number, default=15, help="Интервал между циклами синхронизации, с")
    p.add_argument("--remote-file-port", type=port_number, default=22022)
    p.add_argument("--allow-large", action="store_true", help="Явно разрешить передачу с оценкой свыше 120 с")
    return p


def remote_command(script: str) -> str:
    """Only a base64 alphabet crosses the remote shell quoting boundary."""
    data = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return "printf %s " + shlex.quote(data) + " | base64 -d | bash"


class Launcher:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        safe_host = re.sub(r"[^A-Za-z0-9.-]", "_", args.host)
        self.identity = (args.identity or Path.home() / ".ssh" / f"srv_{safe_host}_{args.port}_ed25519").expanduser().resolve()
        self.state_dir = args.state_dir.expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / f"{safe_host}_{args.port}.json"
        self.events_file = self.state_dir / f"{safe_host}_{args.port}.events.jsonl"
        self.state = {"host": args.host, "port": args.port, "user": args.user,
                      "identity": str(self.identity), "steps": {}, "wireguard": "pending_config"}
        if self.state_file.exists():
            prior = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (prior.get("identity"), prior.get("user")) != (str(self.identity), args.user):
                raise RuntimeError("Профиль уже содержит другого пользователя/ключ. Используйте отдельный --state-dir.")
            self.state = prior

    def event(self, step: str, status: str, reset_steps=(), updates=None) -> None:
        # Separate connect and file workers must not overwrite each other's steps.
        with self.state_file.with_suffix(".lock").open("a+b") as lock:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + 10
            while True:
                lock.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Локальное состояние занято другим процессом")
                    time.sleep(0.05)
            fresh = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else self.state
            for name in reset_steps:
                fresh["steps"].pop(name, None)
            if updates:
                fresh.update(updates)
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            fresh["steps"][step] = {"status": status, "time": stamp}
            temp = self.state_file.with_suffix(".tmp")
            temp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.state_file)
            self.state = fresh
            with self.events_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": stamp, "step": step, "status": status}, ensure_ascii=False) + "\n")

    def local_requirements(self) -> None:
        for name in ("ssh", "ssh-keygen"):
            if not shutil.which(name):
                raise RuntimeError(f"Не найден {name}. Установите OpenSSH Client.")

    def write_server_guide(self) -> Path:
        """Keep machine-specific instructions and private notes outside the export."""
        directory = ROOT / ".local" / "server-guides"
        if directory.is_symlink() or not directory.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError("Каталог памяток должен находиться внутри проекта без ссылки")
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
        stem = self.state_file.stem + "-" + self.args.user
        credentials = directory / f"{stem}.credentials.md"
        guide = directory / f"{stem}-{self.args.agent}.md"
        for path in (credentials, guide):
            if path.is_symlink():
                raise RuntimeError("Не перезаписываю ссылку вместо локальной памятки")
        try:
            descriptor = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # Personal notes and credentials must survive every rerun.
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    f"# Личные данные {self.args.host}:{self.args.port}\n\n"
                    f"Рабочий пользователь: {self.args.user}. Для нового пользователя пароль "
                    "не создаётся: вход по SSH-ключу. Пароли существующих аккаунтов не меняются.\n\n"
                    "Пароль root: скрипт не получает и не сохраняет.\n"
                    "Парольная фраза SSH-ключа: вводится в OpenSSH, скрипту недоступна.\n\n"
                    "## Личные заметки и учётные данные\n\n"
                    "Можно заполнить вручную. Этот файл не перезаписывается при повторном запуске.\n"
                )
        def command(tokens):
            if os.name == "nt":
                return "& " + " ".join("'" + str(token).replace("'", "''") + "'" for token in tokens)
            return shlex.join([str(token) for token in tokens])
        common = ["--host", self.args.host, "--port", str(self.args.port),
                  "--user", self.args.user, "--agent", self.args.agent,
                  "--identity", str(self.identity), "--state-dir", str(self.state_dir)]
        def launcher(action, *extra):
            return command([sys.executable, str(ROOT / "srv.py"), action, *common, *extra])
        folder = (self.args.folder or ROOT).expanduser().resolve()
        from srv_files import folder_id
        link = folder_id(folder)
        home = f"/home/{self.args.user}"
        lines = [f"# Сервер {self.args.host}:{self.args.port}", "",
                 f"Пользователь: `{self.args.user}`. Агент: `{self.args.agent}`.",
                 f"SSH-ключ на клиенте: `{self.identity}`.",
                 f"Профиль launcher: `{self.state_file}`.",
                 f"Личные данные: [{credentials.name}]({credentials.name}).", "",
                 "Файл обновляется автоматически; личные записи храните в файле данных рядом.", "",
                 "## Команды на клиенте", "",
                 "```powershell" if os.name == "nt" else "```bash",
                 launcher("connect"), launcher("verify"), launcher("agent-install"),
                 command(self.ssh_args(self.args.user, tty=True)),
                 command(self.ssh_args("root", tty=True)),
                 launcher("files-add", "--folder", str(folder), "--mode", "both", "--read-write", "--watch",
                          "--remote-file-port", str(self.args.remote_file_port),
                          "--sync-interval", str(self.args.sync_interval)),
                 launcher("files-stop", "--folder", str(folder)), "```", "",
                 "## Файлы и работа на сервере", "",
                 f"Команды файлового моста выше относятся к `{folder}`.",
                 "Пути станут доступны после успешного files-add:",
                 f"- Прямой доступ: `{home}/workspaces/live/{link}`.",
                 f"- Серверная копия: `{home}/workspaces/sync/{link}`.",
                 f"- Git-ключ: `{home}/.ssh/id_ed25519`; регистрируется только публичная часть `.pub`.",
                 "", "Отсоединиться: Ctrl+B, затем D. История: Ctrl+B, затем [; выход — Esc.",
                 "Включить мышь в текущем tmux: Ctrl+B, затем : и set -g mouse on.", "",
                 "## Состояние настройки", "",
                 f"WireGuard: `{self.state.get('wireguard', 'pending_config')}`."]
        for name, value in sorted(self.state.get("steps", {}).items()):
            lines.append(f"- {name}: {value['status']} ({value['time']}).")
        # Open with restrictive permissions from the start, including on first creation.
        descriptor = os.open(guide, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        if os.name != "nt":
            guide.chmod(0o600)
            credentials.chmod(0o600)
        return guide

    def trust_host(self) -> None:
        expected = self.args.trust_host_fingerprint
        if not expected:
            return
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected):
            raise RuntimeError("Ожидался подтверждённый SHA256-отпечаток хоста")
        target = self.args.host if self.args.port == 22 else f"[{self.args.host}]:{self.args.port}"
        keys = []
        if shutil.which("ssh-keyscan"):
            scan = subprocess.run(["ssh-keyscan", "-T", "10", "-p", str(self.args.port),
                                   "-t", "ed25519", self.args.host], capture_output=True, check=False, timeout=20)
            keys = [line for line in scan.stdout.decode("ascii").splitlines()
                    if line and not line.startswith("#")]
        if not keys:
            # Some Windows ssh-keyscan builds advertise a KEX they cannot implement.
            # Quarantine the offered key without sending any authentication credential.
            print("Получаю ключ через SSH без авторизации во временное хранилище.", flush=True)
            with tempfile.TemporaryDirectory(prefix="srv-host-probe-") as probe_dir:
                probe_known = Path(probe_dir) / "known_hosts"
                subprocess.run(["ssh", "-p", str(self.args.port),
                                "-o", "KexAlgorithms=curve25519-sha256",
                                "-o", "HostKeyAlgorithms=ssh-ed25519",
                                "-o", "StrictHostKeyChecking=accept-new",
                                "-o", f"UserKnownHostsFile={probe_known}",
                                "-o", f"GlobalKnownHostsFile={os.devnull}",
                                "-o", "BatchMode=yes", "-o", "PubkeyAuthentication=no",
                                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                                "-o", "PreferredAuthentications=none", "-o", "ConnectTimeout=10",
                                f"root@{self.args.host}", "true"], capture_output=True, check=False, timeout=20)
                if probe_known.is_file():
                    keys = [line for line in probe_known.read_text(encoding="ascii").splitlines()
                            if line and not line.startswith("#")]
        if not keys:
            raise RuntimeError("Не удалось получить Ed25519-ключ сервера; known_hosts не изменён")
        fingerprints = []
        for key in keys:
            result = subprocess.run(["ssh-keygen", "-l", "-f", "-"],
                                    input=(key + "\n").encode("ascii"), capture_output=True, check=False)
            fields = result.stdout.decode("ascii", errors="replace").split()
            if result.returncode or len(fields) < 2:
                raise RuntimeError("Не удалось проверить отпечаток; known_hosts не изменён")
            fingerprints.append(fields[1])
        if any(value != expected for value in fingerprints):
            raise RuntimeError("Новый ключ не совпал с подтверждённым отпечатком; known_hosts не изменён")
        known = Path.home() / ".ssh" / "known_hosts"
        known.parent.mkdir(parents=True, exist_ok=True)
        new_fields = keys[0].split()
        if len(new_fields) != 3 or new_fields[1] != "ssh-ed25519":
            raise RuntimeError("Некорректный ответ ssh-keyscan; known_hosts не изменён")
        if known.exists():
            lookup = subprocess.run(["ssh-keygen", "-F", target, "-f", str(known)],
                                    capture_output=True, check=False)
            for line in lookup.stdout.decode("ascii", errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 3 and fields[1:3] == new_fields[1:3]:
                    self.event("trust_host", "ok")
                    return
        self.event("trust_host", "running")
        if known.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = known.with_name(f"known_hosts.srv-backup-{stamp}")
            shutil.copy2(known, backup)
            self.call(["ssh-keygen", "-R", target, "-f", str(known)], "remove_old_host_entry")
            print(f"Резервная копия known_hosts: {backup}", flush=True)
        with known.open("a", encoding="ascii") as stream:
            stream.write(f"\n{target} {new_fields[1]} {new_fields[2]}\n")
        if os.name != "nt":
            known.chmod(0o600)
        # A reinstalled host may have lost authorized_keys, users and packages.
        reset = ("root_key_verified", "agent_key_verified", "bootstrap", "install_codex", "install_claude", "ready_for_codex", "ready_for_claude", "verify")
        self.event("trust_host", "ok", reset)
        print(f"Подтверждён ключ {self.args.host}:{self.args.port}: {expected}", flush=True)

    def call(self, command: list[str], step: str) -> None:
        # Inherit the real console. Never capture passwords, passphrases or OAuth codes.
        self.event(step, "running")
        result = subprocess.run(command, check=False)
        self.event(step, "ok" if result.returncode == 0 else f"exit_{result.returncode}")
        if result.returncode:
            raise RuntimeError(f"Шаг {step}: код {result.returncode}. Повторных попыток пароля автоматически не будет.")

    def ssh_args(self, user: str, *, password: bool = False, tty: bool = False) -> list[str]:
        command = ["ssh", "-p", str(self.args.port), "-i", str(self.identity),
                   "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=ask",
                   "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=3", "-o", "NumberOfPasswordPrompts=1",
                   "-o", "ForwardAgent=no"]
        if password:
            command += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
                        "-o", "KbdInteractiveAuthentication=no"]
        else:
            command += ["-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                        "-o", "PreferredAuthentications=publickey"]
        if tty:
            command.append("-t")
        command.append(f"{user}@{self.args.host}")
        return command

    def ssh_script(self, user: str, script: str, step: str, *, password: bool = False) -> None:
        self.call(self.ssh_args(user, password=password) + [remote_command(script)], step)

    def keygen(self) -> None:
        public = Path(str(self.identity) + ".pub")
        if self.identity.exists() or public.exists():
            if not self.identity.is_file() or not public.is_file():
                raise RuntimeError("Найдена неполная пара ключей. Не перезаписываю; укажите другой --identity.")
            print(f"Использую существующую пару: {self.identity}", flush=True)
        else:
            self.identity.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                self.identity.parent.chmod(0o700)
            print("Создание ключа. Задайте парольную фразу в запросе ssh-keygen (ввод скрыт).", flush=True)
            self.call(["ssh-keygen", "-t", "ed25519", "-a", "64", "-f", str(self.identity),
                       "-C", f"srv-{self.args.host}-{self.args.port}"], "keygen")
        self.call(["ssh-keygen", "-l", "-f", str(public)], "key_fingerprint")
        self.event("keygen", "ok")

    def public_key(self) -> str:
        text = Path(str(self.identity) + ".pub").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?", text):
            raise RuntimeError("Ожидался публичный ключ Ed25519 в одной строке")
        return " ".join(text.split()[:2])

    def enroll(self) -> None:
        key = self.public_key()
        script = """set -euo pipefail
test "$(id -u)" = 0
umask 077
test ! -L "$HOME/.ssh"
install -d -m 700 "$HOME/.ssh"
file="$HOME/.ssh/authorized_keys"
test ! -L "$file"
if [ -f "$file" ]; then cp -p "$file" "$file.srv-backup-$(date +%s%N)"; fi
touch "$file"
chmod 600 "$file"
key=__KEY__
if ! awk -v wanted="$key" '$1 " " $2 == wanted {found=1} END {exit !found}' "$file"; then
    printf '\\n%s\\n' "$key" >> "$file"
fi
printf 'ROOT_PUBLIC_KEY_INSTALLED\\n'
""".replace("__KEY__", shlex.quote(key))
        print("Первый вход root: проверьте отпечаток хоста; затем введите выданный пароль.", flush=True)
        self.ssh_script("root", script, "enroll", password=True)
        self.ssh_script("root", "set -eu\ntest \"$(id -u)\" = 0\nprintf 'ROOT_KEY_LOGIN_OK\\n'\n", "root_key_verified")

    def doctor(self) -> None:
        print(f"Проверка {self.args.host}:{self.args.port}", flush=True)
        with socket.create_connection((self.args.host, self.args.port), timeout=10) as sock:
            print(sock.recv(256).decode("ascii", errors="replace").strip())
        script = """set -eu
. /etc/os-release
printf 'OS=%s VERSION=%s ARCH=%s\\n' "$ID" "$VERSION_ID" "$(uname -m)"
printf 'CPU='; getconf _NPROCESSORS_ONLN
free -m
df -h /
printf 'WG_INTERFACES='; if command -v wg >/dev/null; then wg show interfaces; else printf 'not-installed\\n'; fi
printf 'LOGIND_KILL_SETTING='; systemd-analyze cat-config systemd/logind.conf 2>/dev/null | sed -n '/^[[:space:]]*KillUserProcesses=/p'
"""
        self.ssh_script("root", script, "doctor")

    def bootstrap(self) -> None:
        key = self.public_key()
        template = (ROOT / "remote" / "bootstrap.sh").read_text(encoding="utf-8")
        script = template.replace("__AGENT_USER__", shlex.quote(self.args.user)).replace("__PUBLIC_KEY__", shlex.quote(key))
        self.ssh_script("root", script, "bootstrap")
        self.ssh_script(self.args.user, "set -eu\ntest \"$(id -u)\" != 0\ntest -w \"$HOME/workspaces\"\nprintf 'AGENT_KEY_LOGIN_OK\\n'\n", "agent_key_verified")
        self.git_key()
        self.git_identity()
        self.install_agent()

    def install_agent(self) -> None:
        install = (ROOT / "remote" / f"install-{self.args.agent}.sh").read_text(encoding="utf-8")
        self.ssh_script(self.args.user, install, f"install_{self.args.agent}")

    def git_key(self) -> None:
        script = """set -euo pipefail
umask 077
install -d -m 700 "$HOME/.ssh"
key="$HOME/.ssh/id_ed25519"
test ! -L "$key"
if [ -e "$key" ] || [ -e "$key.pub" ]; then
    test -f "$key" && test -f "$key.pub" || { printf 'Incomplete Git key pair; not overwriting.\\n' >&2; exit 1; }
else
    ssh-keygen -q -t ed25519 -N '' -f "$key" -C srv-git
fi
chmod 600 "$key"
ssh-keygen -l -f "$key.pub"
cat "$key.pub"
"""
        self.ssh_script(self.args.user, script, "git_key")

    def stage_wireguard(self) -> None:
        if not self.args.wg_config:
            raise RuntimeError("Нужен --wg-config с путём к готовой конфигурации")
        path = self.args.wg_config.expanduser().resolve()
        validate_wireguard(path.read_text(encoding="utf-8-sig"))
        directory = "/root/.local/share/srv/wireguard"
        upload = f"{directory}/upload-{uuid.uuid4().hex}.conf"
        self.ssh_script("root", f"umask 077\ninstall -d -m 700 {directory}\n", "vpn_stage_directory")
        host = f"[{self.args.host}]" if ":" in self.args.host else self.args.host
        self.call(["scp", "-P", str(self.args.port), "-i", str(self.identity),
                   "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=ask",
                   "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                   "-o", "NumberOfPasswordPrompts=1", str(path), f"root@{host}:{upload}"], "vpn_upload")
        script = """set -euo pipefail
umask 077
chmod 600 __UPLOAD__
if [ -f __TARGET__ ]; then cp -p __TARGET__ __BACKUP__; fi
python3 - <<'PY'
from pathlib import Path
source=Path(__UPLOAD_PY__)
target=Path(__TARGET_PY__)
target.write_text(source.read_text(encoding='utf-8-sig'),encoding='utf-8')
target.chmod(0o600)
PY
if ! wg-quick strip __TARGET__ >/dev/null 2>__ERROR_LOG__; then
    printf 'Конфигурация не прошла проверку; подробности сохранены только в root-каталоге.\\n' >&2
    exit 1
fi
printf 'WIREGUARD_CONFIG_STAGED; VPN_NOT_ACTIVE\\n'
"""
        target = f"{directory}/wg-srv.conf"
        replacements = {"__UPLOAD__": shlex.quote(upload), "__TARGET__": shlex.quote(target),
                        "__BACKUP__": shlex.quote(target + ".backup-" + uuid.uuid4().hex),
                        "__UPLOAD_PY__": repr(upload), "__TARGET_PY__": repr(target),
                        "__ERROR_LOG__": shlex.quote(directory + "/validation.log")}
        for token, value in replacements.items():
            script = script.replace(token, value)
        self.ssh_script("root", script, "vpn_validate")
        self.event("vpn-stage", "ok", updates={"wireguard": "staged_not_active", "wireguard_staged_path": target})
        print("VPN-конфиг подготовлен. Активация требует проверки реальных маршрутов и отката; текущая сеть не изменялась.", flush=True)

    def git_identity(self) -> None:
        name, email = self.args.git_name, self.args.git_email
        explicit = bool(name or email)
        if explicit and not (name and email):
            raise RuntimeError("Укажите --git-name и --git-email вместе")
        if not explicit and shutil.which("git"):
            def local_value(field):
                result = subprocess.run(["git", "config", "--global", "--get", field],
                                        capture_output=True, encoding="utf-8", errors="replace", check=False)
                return result.stdout.strip() if result.returncode == 0 else ""
            name, email = local_value("user.name"), local_value("user.email")
        if not (name and email):
            print("Git identity не задана: перед коммитами укажите --git-name/--git-email или настройте git config на сервере.", flush=True)
            return
        script = """set -eu
if [ __EXPLICIT__ = yes ] || [ -z "$(git config --global --get user.name || true)" ]; then git config --global user.name __NAME__; fi
if [ __EXPLICIT__ = yes ] || [ -z "$(git config --global --get user.email || true)" ]; then git config --global user.email __EMAIL__; fi
printf 'GIT_IDENTITY_READY\\n'
""".replace("__EXPLICIT__", "yes" if explicit else "no").replace("__NAME__", shlex.quote(name)).replace("__EMAIL__", shlex.quote(email))
        self.ssh_script(self.args.user, script, "git_identity")

    def connect(self) -> None:
        print(f"Открывается {self.args.agent} в tmux. Вход: личная подписка сотрудника.", flush=True)
        print("Отсоединиться, сохранив сессию: Ctrl+B, затем D. Повторный connect вернёт в неё.", flush=True)
        runner = "srv-session" if self.args.agent == "codex" else "srv-session-claude"
        command = f'export PATH="$HOME/.local/bin:$PATH"; exec "$HOME/.local/bin/{runner}"'
        self.call(self.ssh_args(self.args.user, tty=True) + [command], "connect")

    def verify(self) -> None:
        script = """set -eu
export PATH="$HOME/.local/bin:$PATH"
id
__AGENT__ --version
tmux -V
if __AUTH_COMMAND__ >/dev/null 2>&1; then printf '__AUTH_LABEL__=ok\\n'; else printf '__AUTH_LABEL__=pending\\n'; exit 2; fi
tmux has-session -t __AGENT__
printf 'TMUX_SESSION=ok\\n'
"""
        auth = "codex login status" if self.args.agent == "codex" else "claude auth status"
        script = script.replace("__AGENT__", self.args.agent).replace("__AUTH_COMMAND__", auth).replace("__AUTH_LABEL__", self.args.agent.upper() + "_AUTH")
        self.ssh_script(self.args.user, script, "verify")

    def run(self) -> None:
        if self.args.action == "guide":
            return
        self.local_requirements()
        if self.args.wg_config:
            path = self.args.wg_config.expanduser().resolve()
            if not path.is_file():
                raise RuntimeError("Файл --wg-config не найден; его содержимое не выводится.")
            if self.args.action not in ("setup", "vpn-stage"):
                raise RuntimeError("--wg-config поддерживается для setup и vpn-stage")
        self.trust_host()
        if self.args.action == "agent-install":
            self.install_agent()
            return
        if self.args.action == "git-key":
            self.git_key()
            return
        if self.args.action == "git-identity":
            self.git_identity()
            return
        if self.args.action == "vpn-stage":
            self.stage_wireguard()
            return
        if self.args.action in ("files-add", "files-sync", "files-stop"):
            from srv_files import Files
            Files(self).run()
            return
        if self.args.action == "setup":
            self.keygen()
            if self.state["steps"].get("root_key_verified", {}).get("status") != "ok":
                self.enroll()
            self.doctor()
            self.bootstrap()
            if self.args.wg_config:
                self.stage_wireguard()
            vpn_status = "VPN-конфиг подготовлен, но не активирован." if self.args.wg_config else "WireGuard ожидает готового конфига."
            print("Базовая подготовка завершена. " + vpn_status, flush=True)
            self.event(f"ready_for_{self.args.agent}", "ok")
            self.write_server_guide()
            self.connect()
            self.verify()
        else:
            getattr(self, self.args.action)()


def validate_wireguard(text: str) -> None:
    if "[Interface]" not in text or "[Peer]" not in text:
        raise RuntimeError("WireGuard: нужны секции Interface и Peer")
    for field in ("PrivateKey", "PublicKey", "PresharedKey"):
        values = re.findall(r"(?m)^\s*" + field + r"\s*=\s*(\S+)", text)
        if field != "PresharedKey" and not values:
            raise RuntimeError(f"WireGuard: отсутствует {field}")
        for value in values:
            try:
                if len(base64.b64decode(value, validate=True)) != 32:
                    raise ValueError()
            except ValueError:
                raise RuntimeError(f"WireGuard: некорректное поле {field}; содержимое не выводится") from None


def main() -> int:
    args = parser().parse_args()
    launcher = None
    try:
        launcher = Launcher(args)
        launcher.event(args.action, "running")
        print(f"Локальная памятка: {launcher.write_server_guide()}", flush=True)
        launcher.run()
        launcher.event(args.action, "ok")
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        if launcher:
            launcher.event(args.action, "failed")
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if launcher:
            launcher.event(args.action, "interrupted")
        print("Остановлено. Созданные ключи и завершённые шаги сохранены.", file=sys.stderr)
        return 130
    finally:
        if launcher:
            try:
                launcher.write_server_guide()
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"Не удалось обновить локальную памятку: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
