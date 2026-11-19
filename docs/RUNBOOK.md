# Ручная подготовка персонального сервера

Статус: SSH, рабочий пользователь, Codex и tmux проверены в пилотной проверке;
пользователь лично вошёл и получил ответ. Прямой файловый доступ и двусторонняя
синхронизация проверены с Windows. Адаптер Claude проверен синтаксически;
реальный WireGuard-конфиг ещё не предоставлен.
Сценарий: пустой Ubuntu Server 24.04, root-пароль, доступ только SSH.
В примерах замените `SERVER_IP` IP-адресом или DNS-именем вашего сервера;
порт `22`, пользователя `ai-agent` и каталог `C:\git\Srv` — своими значениями.
`FOLDER_ID` — идентификатор выбранной папки, который выводит `files-add`.
Для готовых команд с конкретными параметрами используйте
`python srv.py guide --host SERVER_IP` (при необходимости с `--port`, `--user`,
`--identity` и `--folder`). Памятки и личные данные сохраняются локально
в `.local/server-guides/`, исключены из Git и передаваемого комплекта.

## 1. Клиентский ключ

Проверить наличие OpenSSH Client и выбрать новый путь, не занятый существующим ключом.
На Windows:

```powershell
ssh-keygen -t ed25519 -a 64 -f "$HOME\.ssh\srv_SERVER_IP_22_ed25519" -C srv-SERVER_IP-22
```

На Ubuntu:

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -a 64 -f "$HOME/.ssh/srv_SERVER_IP_22_ed25519" -C srv-SERVER_IP-22
```

Парольную фразу вводит сотрудник в скрытом запросе. Если ssh-keygen предлагает
перезаписать существующий ключ, отказаться и выбрать другой путь.

## 2. Первый вход root и установка публичного ключа

```text
ssh -p 22 -o NumberOfPasswordPrompts=1 root@SERVER_IP
```

Сверить отпечаток хоста с предоставленным администратором, если он доступен.
При отсутствии независимого источника первое подтверждение означает доверие
этому соединению; дальнейшая проверка ключа остаётся включённой.
Ввести выданный пароль root.

Если OpenSSH сообщает `REMOTE HOST IDENTIFICATION HAS CHANGED`, вход остановить.
Уточнить переустановку/повторное назначение IP и сверить новый отпечаток.
Не отключать проверку и не очищать весь known_hosts. Только после подтверждения
смены ключа сохранить резервную копию known_hosts и удалить запись именно этого
хоста/порта, затем повторить вход и подтвердить новый ключ.

Внутри root-сессии:

```bash
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
cp -p /root/.ssh/authorized_keys /root/.ssh/authorized_keys.before-srv
cat >> /root/.ssh/authorized_keys
```

Вставить одну строку из локального файла `.pub`, затем Enter и Ctrl+D.
Вставляется публичная часть, не приватный ключ.
Не закрывая эту сессию, в другом терминале Windows проверить:

```powershell
ssh -p 22 -i "$HOME\.ssh\srv_SERVER_IP_22_ed25519" -o IdentitiesOnly=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no root@SERVER_IP id
```

Ожидается `uid=0(root)`. Ввод парольной фразы ключа допустим;
запрос root-пароля в этой проверке отключён. При ошибке исправлять установку
ключа из первой сессии. Настройки SSH и парольный доступ пока не изменять.

## 3. Диагностика и рабочий пользователь

В root-сессии проверить ОС, архитектуру, память и диск:

```bash
cat /etc/os-release
uname -m
free -m
df -h /
```

Далее показано создание нового пользователя. Если `ai-agent` уже существует,
сначала проверить его домашний каталог и группы, а не пересоздавать.
Выбрать свободный UID, учитывая процессы контейнеров, отсутствующие в passwd:

```bash
getent passwd 20000
ps -eo uid= | sort -nu
```

В примере используется 20000; если он занят учётной записью или процессом,
выбрать другой свободный UID из диапазона 20000–20999.

```bash
useradd --uid 20000 --create-home --shell /bin/bash ai-agent
chmod 700 /home/ai-agent
install -d -m 700 -o ai-agent -g ai-agent /home/ai-agent/.ssh
touch /home/ai-agent/.ssh/authorized_keys
chmod 600 /home/ai-agent/.ssh/authorized_keys
cat >> /home/ai-agent/.ssh/authorized_keys
```

Вставить ту же публичную строку и завершить Enter, Ctrl+D. Затем:

```bash
chown ai-agent:ai-agent /home/ai-agent/.ssh/authorized_keys
install -d -m 700 -o ai-agent -g ai-agent /home/ai-agent/workspaces
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git tmux python3 wireguard-tools rsync sshfs
```

Проверить вход `ai-agent` отдельным SSH-соединением с тем же ключом.
Пользователю не выдавать sudo для запуска Codex. Административный доступ root
по ключу сохраняется, поскольку другого канала восстановления пока нет.

## 4. Codex и личная подписка

Далее команды выполняются как `ai-agent`:

```bash
umask 077
mkdir -p "$HOME/.local/share/srv/setup" "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
curl --fail --show-error --location --proto '=https' --tlsv1.2 https://chatgpt.com/codex/install.sh -o "$HOME/.local/share/srv/setup/codex-install.sh"
sha256sum "$HOME/.local/share/srv/setup/codex-install.sh"
CODEX_NON_INTERACTIVE=1 sh "$HOME/.local/share/srv/setup/codex-install.sh"
codex --version
export TERM=xterm-256color
tmux new-session -A -s codex -c "$HOME/workspaces"
```

Уже внутри tmux:

```bash
export PATH="$HOME/.local/bin:$PATH"
codex login --device-auth
codex
```

Открыть показанный адрес на своём компьютере и завершить авторизацию личной
подпиской. Device code может потребовать включения в настройках аккаунта.
Файл авторизации не выводить, не пересылать и не добавлять в Git.

Если device code недоступен, открыть другое SSH-подключение:

```text
ssh -p 22 -i "$HOME/.ssh/srv_SERVER_IP_22_ed25519" -o IdentitiesOnly=yes -L 127.0.0.1:1455:127.0.0.1:1455 ai-agent@SERVER_IP
```

При другом пути ключа заменить значение `-i`. В той сессии выполнить
`codex login`, следовать ссылке в локальном браузере. Порт 1455 должен быть свободен.
После авторизации вернуться в tmux и запустить Codex.

## 5. Приёмка

Сотрудник лично входит в Codex и даёт короткое задание, например создать
`session-check.txt` в рабочей папке с заданной строкой, после чего проверяет файл.
Это проверяет реальную работу агента, а не только наличие бинарника.

Отсоединиться: Ctrl+B, затем D. Снова подключиться по SSH как `ai-agent` и выполнить:

```bash
tmux attach-session -t codex
```

Должен открыться тот же агент с прежней историей. Дополнительно проверить
разрыв SSH во время работы и последующее подключение; не завершать сам Codex.

## 6. Локальные файлы: компоненты и ручное восстановление

Первый запуск моста и синхронизации:

```powershell
python C:\git\Srv\srv.py files-add --host SERVER_IP --folder C:\git\Srv --mode both --read-write --watch
```

Весь файловый процесс выполняется на компьютере сотрудника. Его состояние —
`C:\git\Srv\.local\links\FOLDER_ID\link.json`;
идентификатор зависит от выбранного пути. Собственный rclone с проверенным SHA256:
`C:\git\Srv\.local\bin\rclone.exe`. Для Ubuntu бинарник расположен в `.local/bin/rclone`.

После первого запуска конфигурация транспорта находится в
`C:\git\Srv\.local\links\FOLDER_ID\rclone.conf`.
Она содержит адрес и пути к ключам, не копируется в Git/экспорт.
Служебный клиентский ключ — `C:\git\Srv\.local\file-transport-ed25519`;
его публичная часть зарегистрирована на сервере с `restrict,command="internal-sftp"`.
Личный SSH-ключ не передаётся на сервер.

При отказе launcher сначала вручную проверить доступ к копии:

```powershell
& C:\git\Srv\.local\bin\rclone.exe --config C:\git\Srv\.local\links\FOLDER_ID\rclone.conf lsf srv:/home/ai-agent/workspaces/sync/FOLDER_ID
```

Прямой доступ состоит из трёх независимых компонентов:

1. Локальный `rclone serve sftp` выбранной папки на 127.0.0.1.
2. Обратный SSH-туннель с серверного 127.0.0.1:22022 на этот локальный порт.
3. Серверный `sshfs`, смонтированный рабочим пользователем без allow_other.

Для ручного восстановления выбрать фиксированный локальный порт, например 2022,
и держать два клиентских терминала открытыми. Запуск локального сервиса:

```powershell
& C:\git\Srv\.local\bin\rclone.exe --config C:\git\Srv\.local\links\FOLDER_ID\rclone.conf serve sftp C:\git\Srv --addr 127.0.0.1:2022 --authorized-keys C:\git\Srv\.local\links\FOLDER_ID\authorized_keys --key C:\git\Srv\.local\links\FOLDER_ID\sftp-host-ed25519 --vfs-cache-mode writes --vfs-write-back 0s --cache-dir C:\git\Srv\.local\links\FOLDER_ID\vfs-cache --dir-cache-time 1s --exclude '/.local/**' --exclude '/.scratch/**' --exclude '/.git/**' --exclude '/.venv/**' --exclude '**/__pycache__/**'
```

В другом клиентском терминале:

```powershell
ssh -p 22 -i "$HOME\.ssh\srv_SERVER_IP_22_ed25519" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -N -R 127.0.0.1:22022:127.0.0.1:2022 ai-agent@SERVER_IP
```

На сервере как ai-agent проверить, что каталог ещё не смонтирован, затем:

```bash
mountpoint /home/ai-agent/workspaces/live/FOLDER_ID
sshfs -p 22022 -o IdentityFile=/home/ai-agent/.local/share/srv/links/FOLDER_ID/live-client,UserKnownHostsFile=/home/ai-agent/.local/share/srv/links/FOLDER_ID/known_hosts,StrictHostKeyChecking=yes,ServerAliveInterval=5,ServerAliveCountMax=2,cache=no srv@127.0.0.1:/ /home/ai-agent/workspaces/live/FOLDER_ID
```

Эти пути ключей/known_hosts создаёт первый файловый запуск. Если он остановился
раньше, создать локальную SFTP host-пару и отдельную клиентскую пару на сервере
через ssh-keygen; публичный серверный ключ записать в локальный authorized_keys,
а локальный host public key — в серверный known_hosts как `[127.0.0.1]:22022 KEY`.
Служебные пары предназначены только этому мосту и хранятся с правами владельца.
Для работы без интерактивного ввода фразы служебные ключи создаются без неё;
личный входной ключ остаётся отдельным.

Режим VFS writes нужен для открытия существующего файла как read/write (`r+`).
Без него простое создание работает, а произвольное изменение может вернуть I/O error.
Чистое завершение:

```powershell
python C:\git\Srv\srv.py files-stop --host SERVER_IP --folder C:\git\Srv
```

Вручную сначала снять mount на сервере как ai-agent:

```bash
fusermount3 -u /home/ai-agent/workspaces/live/FOLDER_ID
```

Затем завершить клиентские сервис и туннель. Если есть открытые файлы, сначала
закрыть их; не подменять live-путь серверной копией. Копия находится отдельно:
`/home/ai-agent/workspaces/sync/FOLDER_ID`.

Синхронизацию выполняет rclone bisync, её workdir хранится рядом с link.json.
Для ручного прогона использовать те же пути, фильтры и workdir с `--dry-run` перед
применением, `--max-delete 0` и `--conflict-resolve none`. Не делать `--resync`
поверх имеющихся данных ради обхода ошибки: это может выбрать одну версию.
При конфликте сохранить обе версии, объединить изменения и только затем
восстановить согласованное состояние. Проверенные команды и полный набор флагов —
в методах `sync`/`preflight` файла `C:\git\Srv\srv_files.py`.

## 7. Git и выбранный агент

Git-ключ создаётся отдельной операцией `git-key`, либо автоматически при bootstrap.
Публичную часть `/home/ai-agent/.ssh/id_ed25519.pub` зарегистрировать в личных
Git-сервисах. Имя/email автора берутся из глобальной конфигурации локального Git;
если её нет, передать вместе `--git-name`/`--git-email` или настроить на сервере.

Для Claude: `setup --agent claude`, либо `agent-install --agent claude` на уже
подготовленном сервере. Вручную установить CLI из официального installer под
ai-agent, проверить `claude --version`, открыть отдельную tmux-сессию и запустить
`claude`. Вход выполняет сотрудник личной подпиской. Пилот авторизации Claude
не проводился; установлен и проверен Codex.

## 8. WireGuard и оставшиеся шаги

- Готовая конфигурация WireGuard будет предоставлена позднее. Инструменты
  установлены, VPN пока не активирован. Маршруты и firewall не меняются.
- Готовый файл можно передать через `vpn-stage --wg-config ПУТЬ`; он будет сохранён
  в root-каталоге и предварительно проверен, но сеть не изменится. Логи ошибок
  конфигурации остаются только в закрытом root-каталоге, значения ключей не печатаются.
- Активация VPN требует реального файла, проверки DNS/маршрутов и автоматического
  отката, поскольку доступ к серверу только SSH. Этот этап пока не выполнялся.
- Внешний Git проверен на AI-Memory: clone, тестовый push, git revert и push отката;
  тестовая ветка удалена, основная ветка осталась прежней. Внутренний Git — после VPN.

Источники: [установка Codex](https://learn.chatgpt.com/docs/codex/cli),
[вход на сервере без браузера](https://learn.chatgpt.com/docs/auth?surface=cli).
