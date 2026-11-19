"""Файловый мост и синхронизация через проверенный rclone и OpenSSH."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.request
import zipfile

EXCLUDES = ("/.local/**", "/.scratch/**", "/.git/**", "/.venv/**", "**/__pycache__/**")
RCLONE_VERSION = "v1.76.0"


def download(url: str, target: Path):
    curl = shutil.which("curl")
    if curl:
        subprocess.run([curl, "--fail", "--location", "--proto", "=https", "--tlsv1.2",
                        "--connect-timeout", "15", "--max-time", "300", "--retry", "2",
                        "--output", str(target), url], check=True)
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "Srv-deployer/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)


def folder_id(folder: Path) -> str:
    digest = hashlib.sha256(os.path.normcase(str(folder.resolve())).encode()).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9_-]", "_", folder.name)[:30] or "folder"
    return f"{name}-{digest}"


def rclone_binary(state_dir: Path) -> Path:
    extension = ".exe" if os.name == "nt" else ""
    target = state_dir / "bin" / f"rclone{extension}"
    if target.is_file():
        return target
    machine = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system())
    if not arch or not system:
        raise RuntimeError("rclone: поддерживаются Windows/Linux amd64 и arm64")
    artifact = f"rclone-{RCLONE_VERSION}-{system}-{arch}.zip"
    base = f"https://downloads.rclone.org/{RCLONE_VERSION}"
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = state_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / artifact
    print(f"Загрузка rclone {RCLONE_VERSION} с проверкой SHA256...", flush=True)
    sums_file = cache / f"rclone-{RCLONE_VERSION}-SHA256SUMS"
    download(f"{base}/SHA256SUMS", sums_file)
    sums = sums_file.read_text(encoding="utf-8")
    expected = next((line.split()[0] for line in sums.splitlines()
                     if line.split() and line.split()[-1].lstrip("*") == artifact), None)
    if not expected:
        raise RuntimeError("Не найден SHA256 для дистрибутива rclone")
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected.lower():
        download(f"{base}/{artifact}", archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected.lower():
        raise RuntimeError("SHA256 rclone не совпал: бинарник не запускается")
    with zipfile.ZipFile(archive) as package:
        candidates = [name for name in package.namelist() if name.endswith("/rclone" + extension)]
        if len(candidates) != 1:
            raise RuntimeError("Неоднозначное содержимое архива rclone")
        target.write_bytes(package.read(candidates[0]))
    if os.name != "nt":
        target.chmod(0o700)
    return target


def filter_args() -> list[str]:
    result = []
    for pattern in EXCLUDES:
        result += ["--exclude", pattern]
    return result


class Files:
    def __init__(self, launcher):
        self.launcher = launcher
        self.args = launcher.args
        if not self.args.folder:
            raise RuntimeError("Нужен --folder с локальным каталогом")
        self.folder = self.args.folder.expanduser().resolve()
        if not self.folder.is_dir() or self.folder == Path(self.folder.anchor):
            raise RuntimeError("Нужен существующий выбранный каталог, не корень диска")
        if self.args.action != "files-stop" and not self.args.read_write and self.args.mode != "live":
            raise RuntimeError("Двусторонняя синхронизация требует --read-write; для чтения используйте --mode live")
        self.link_id = folder_id(self.folder)
        self.local = launcher.state_dir / "links" / self.link_id
        self.local.mkdir(parents=True, exist_ok=True)
        self.record = self.local / "link.json"
        self.info = json.loads(self.record.read_text(encoding="utf-8")) if self.record.exists() else {}
        if self.args.action == "files-stop" and not self.info:
            raise RuntimeError("Этот каталог ещё не зарегистрирован")
        if self.info and (self.info.get("host"), self.info.get("port"), self.info.get("user")) != (self.args.host, self.args.port, self.args.user):
            raise RuntimeError("Каталог уже привязан к другому серверу; нужен отдельный --state-dir")
        self.remote_home = f"/home/{self.args.user}"
        self.remote_meta = f"{self.remote_home}/.local/share/srv/links/{self.link_id}"
        self.remote_sync = f"{self.remote_home}/workspaces/sync/{self.link_id}"
        self.remote_live = f"{self.remote_home}/workspaces/live/{self.link_id}"
        self.rclone = rclone_binary(launcher.state_dir) if self.args.action != "files-stop" else None
        self.config = self.local / "rclone.conf"
        self.processes = []
        self.live_started = False

    def save(self):
        self.info.update(folder=str(self.folder), host=self.args.host, port=self.args.port,
                         user=self.args.user, sync=self.remote_sync, live=self.remote_live,
                         mode=self.args.mode, read_write=self.args.read_write)
        temp = self.record.with_suffix(".tmp")
        temp.write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.record)

    def rc(self, *args, capture=False):
        command = [str(self.rclone), "--config", str(self.config), *args]
        result = subprocess.run(command, capture_output=capture, check=False,
                                encoding="utf-8", errors="replace")
        if result.returncode:
            if capture and result.stderr:
                print(result.stderr, flush=True)
            raise RuntimeError(f"rclone завершился с кодом {result.returncode}; рабочие данные сохраняются")
        return result.stdout if capture else None

    def ssh(self, script: str) -> str:
        from srv import remote_command
        result = subprocess.run(self.launcher.ssh_args(self.args.user) + [remote_command(script)],
                                stdout=subprocess.PIPE, encoding="utf-8", errors="replace", check=False, timeout=90)
        if result.returncode:
            raise RuntimeError(f"Файловый SSH-шаг: код {result.returncode}")
        return result.stdout

    def generate_key(self, path: Path):
        if path.exists() and Path(str(path) + ".pub").is_file():
            return
        if path.exists() or Path(str(path) + ".pub").exists():
            raise RuntimeError("Неполная служебная пара ключей; существующие файлы не перезаписываются")
        self.launcher.call(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], "file_keygen")

    def prepare(self):
        key = self.launcher.state_dir / "file-transport-ed25519"
        self.generate_key(key)
        public = " ".join(Path(str(key) + ".pub").read_text(encoding="utf-8").split()[:2])
        script = """set -euo pipefail
umask 077
file="$HOME/.ssh/authorized_keys"
test ! -L "$file"
key=__KEY__
if ! grep -Fq "$key" "$file"; then
    cp -p "$file" "$file.srv-files-$(date +%s%N)"
    printf '\\nrestrict,command="internal-sftp" %s\\n' "$key" >> "$file"
fi
mkdir -p __META__ __SYNC__
printf 'SFTP_TRANSPORT_READY\\n'
""".replace("__KEY__", shlex.quote(public)).replace("__META__", shlex.quote(self.remote_meta)).replace("__SYNC__", shlex.quote(self.remote_sync))
        print(self.ssh(script).strip())
        self.config.write_text("\n".join([
            "[srv]", "type = sftp", f"host = {self.args.host}", f"port = {self.args.port}",
            f"user = {self.args.user}", f"key_file = {key.as_posix()}",
            f"known_hosts_file = {(Path.home()/'.ssh/known_hosts').as_posix()}",
            "disable_hashcheck = true", "", ]), encoding="utf-8")
        if os.name != "nt":
            key.chmod(0o600)
            self.config.chmod(0o600)
        self.save()

    def estimate(self):
        summary = json.loads(self.rc("size", str(self.folder), "--json", *filter_args(), capture=True))
        volume = summary["bytes"]
        free = int(self.ssh(f"df --output=avail -B1 {shlex.quote(self.remote_meta)} | tail -1\n").strip())
        if not self.info.get("initialized") and volume > free * 0.9:
            raise RuntimeError("Недостаточно свободного места на сервере для первичной копии с запасом")
        speed = self.info.get("measured_bytes_per_second")
        if not speed:
            probe = self.local / "speed-probe.bin"
            if not probe.exists():
                probe.write_bytes(os.urandom(4 * 1024 * 1024))
            started = time.monotonic()
            self.rc("copyto", str(probe), f"srv:{self.remote_meta}/speed-probe.bin", "--ignore-times")
            duration = max(time.monotonic() - started, 0.1)
            speed = probe.stat().st_size / duration
            self.info["measured_bytes_per_second"] = speed
            self.save()
        # Conservative allowance for protocol and many small files.
        eta = volume / max(speed * 0.7, 1) + summary["count"] * 0.005
        print(f"Каталог: {summary['count']} файлов, {volume / 1048576:.1f} MiB; оценка передачи {eta:.0f} с.", flush=True)
        if eta > 120 and not self.args.allow_large:
            if not sys_stdin_tty() or input("Передача дольше 2 минут. Продолжить? [y/N]: ").strip().lower() not in ("y", "yes", "д", "да"):
                raise RuntimeError("Большая передача не запущена; можно выбрать --mode live или --allow-large")

    def sync(self):
        initial = not self.info.get("initialized")
        target = f"srv:{self.remote_sync}"
        if initial:
            existing = self.rc("lsf", target, "--files-only", "--recursive", capture=True)
            if existing.strip():
                raise RuntimeError("Первая синхронизация требует пустого серверного каталога; --resync автоматически не применяется к существующим данным")
        args = ["bisync", str(self.folder), target, "--workdir", str(self.local / "bisync"),
                "--conflict-resolve", "none", "--conflict-loser", "num", "--conflict-suffix", "srv-conflict",
                "--max-delete", "0", "--resilient", "--recover", "--ignore-listing-checksum",
                "--no-slow-hash", "--stats", "10s", "--stats-one-line", *filter_args()]
        if initial:
            args.append("--resync")
        else:
            self.preflight(args)
        self.rc(*args)
        self.info["initialized"] = True
        self.info.pop("sync_paused", None)
        self.save()
        print(f"Серверная копия: {self.remote_sync}", flush=True)

    def preflight(self, args):
        result = subprocess.run([str(self.rclone), "--config", str(self.config), *args,
                                 "--dry-run", "--use-json-log", "--verbose"],
                                capture_output=True, encoding="utf-8", errors="replace", check=False)
        messages = []
        for line in result.stderr.splitlines():
            try:
                message = json.loads(line).get("msg", "")
                messages.append(re.sub(r"\x1b\[[0-9;]*m", "", message))
            except (ValueError, AttributeError):
                pass
        conflicts = [message for message in messages if "New or changed in both paths" in message]
        if conflicts:
            raise RuntimeError("Правки с обеих сторон: синхронизация остановлена до перезаписи. " + "; ".join(conflicts))
        if result.returncode:
            errors = [message for message in messages if "abort" in message.lower() or "failed" in message.lower()]
            raise RuntimeError("Предварительная проверка синхронизации не прошла: " + "; ".join(errors[-3:]))

    def live(self):
        host_key = self.local / "sftp-host-ed25519"
        self.generate_key(host_key)
        host_public = " ".join(Path(str(host_key) + ".pub").read_text(encoding="utf-8").split()[:2])
        remote_port = self.args.remote_file_port
        script = """set -euo pipefail
umask 077
mkdir -p __META__ __LIVE__
if [ ! -f __CLIENT__ ]; then ssh-keygen -q -t ed25519 -N '' -f __CLIENT__; fi
printf '%s\\n' __HOST__ > __KNOWN__
cat __CLIENT_PUB__
""".replace("__META__", shlex.quote(self.remote_meta)).replace("__LIVE__", shlex.quote(self.remote_live))
        script = script.replace("__CLIENT_PUB__", shlex.quote(self.remote_meta + "/live-client.pub"))
        script = script.replace("__CLIENT__", shlex.quote(self.remote_meta + "/live-client"))
        script = script.replace("__KNOWN__", shlex.quote(self.remote_meta + "/known_hosts"))
        script = script.replace("__HOST__", shlex.quote(f"[127.0.0.1]:{remote_port} {host_public}"))
        client_public = self.ssh(script).strip()
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", client_public):
            raise RuntimeError("Не получен публичный ключ локального SFTP-клиента")
        authorized = self.local / "authorized_keys"
        authorized.write_text(client_public + "\n", encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            local_port = sock.getsockname()[1]
        command = [str(self.rclone), "--config", str(self.config), "serve", "sftp", str(self.folder),
                   "--addr", f"127.0.0.1:{local_port}", "--authorized-keys", str(authorized),
                   "--key", str(host_key), "--dir-cache-time", "1s", "--vfs-cache-mode", "writes",
                   "--vfs-write-back", "0s", "--cache-dir", str(self.local / "vfs-cache"), *filter_args()]
        if not self.args.read_write:
            command.append("--read-only")
        server = subprocess.Popen(command)
        self.processes.append(server)
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Локальный SFTP-сервис не запустился")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Локальный SFTP-сервис не отвечает")
        ssh_args = self.launcher.ssh_args(self.args.user)
        ssh_args[-1:-1] = ["-N", "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}"]
        tunnel = subprocess.Popen(ssh_args)
        self.processes.append(tunnel)
        mounted = False
        for _ in range(20):
            if tunnel.poll() is not None:
                raise RuntimeError("Обратный SSH-туннель не создан; проверьте занятость --remote-file-port")
            connect_code = f"import socket; s=socket.create_connection(('127.0.0.1', {remote_port}),timeout=1); s.close()"
            try:
                self.ssh(f"python3 -c {shlex.quote(connect_code)}\n")
            except RuntimeError:
                time.sleep(0.5)
                continue
            mounted = True
            break
        if not mounted:
            raise RuntimeError("Не удалось проверить обратный туннель")
        options = f"IdentityFile={self.remote_meta}/live-client,UserKnownHostsFile={self.remote_meta}/known_hosts,StrictHostKeyChecking=yes,ServerAliveInterval=5,ServerAliveCountMax=2,cache=no"
        script = f"""set -eu
if mountpoint -q {shlex.quote(self.remote_live)}; then printf 'Каталог уже смонтирован: остановите предыдущее files-add\\n' >&2; exit 1; fi
sshfs -p {remote_port} -o {shlex.quote(options)} srv@127.0.0.1:/ {shlex.quote(self.remote_live)}
mountpoint -q {shlex.quote(self.remote_live)}
"""
        self.ssh(script)
        self.info["live_mounted"] = True
        self.live_started = True
        self.save()
        print(f"Прямой доступ: {self.remote_live}", flush=True)

    def run(self):
        stop_file = self.local / "stop.request"
        if self.args.action == "files-stop":
            stop_file.write_text("stop", encoding="ascii")
            print("Запрошена остановка после текущей операции; серверная копия сохраняется.", flush=True)
            return
        # OS locks are released on process exit, including crashes. Keep the file.
        lock = (self.local / "run.lock").open("a+b")
        try:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Этот каталог уже обслуживается другим файловым процессом") from exc
            stop_file.unlink(missing_ok=True)
            self.run_locked()
        finally:
            lock.close()

    def run_locked(self):
        self.prepare()
        print("Исключены из передачи: .local, .scratch, .git, .venv, __pycache__.", flush=True)
        if self.args.action == "files-sync":
            if not self.args.read_write:
                raise RuntimeError("Для синхронизации нужен --read-write")
            self.estimate()
            self.sync()
            return
        if self.args.mode in ("sync", "both"):
            self.estimate()
            self.sync()
        try:
            if self.args.mode in ("live", "both"):
                self.live()
            if self.processes or self.args.watch:
                print("Файловый мост работает. Ctrl+C завершает мост; серверная копия сохраняется.", flush=True)
                while True:
                    time.sleep(self.args.sync_interval)
                    if (self.local / "stop.request").exists():
                        print("Получен запрос остановки.", flush=True)
                        break
                    if any(process.poll() is not None for process in self.processes):
                        raise RuntimeError("Локальный SFTP или SSH-туннель отключился")
                    if self.args.watch and self.args.mode in ("sync", "both") and not self.info.get("sync_paused"):
                        try:
                            self.sync()
                        except RuntimeError as exc:
                            self.info["sync_paused"] = str(exc)
                            self.save()
                            print(f"Автосинхронизация приостановлена: {exc}. Прямой доступ сохраняется.", flush=True)
        finally:
            if self.live_started:
                try:
                    self.ssh(f"fusermount3 -u {shlex.quote(self.remote_live)}\n")
                    self.info["live_mounted"] = False
                except Exception:
                    self.info["live_mounted"] = "cleanup_pending"
                    print(f"Не удалось снять mount; вручную: fusermount3 -u {self.remote_live}", flush=True)
                self.save()
            for process in reversed(self.processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()


def sys_stdin_tty():
    import sys
    return sys.stdin.isatty()
