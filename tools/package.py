"""Собрать передаваемый комплект без ключей и локального состояния."""
from pathlib import Path
import hashlib
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "LICENSE", ".gitignore", "srv.py", "srv_files.py", "README.md", "START-HERE.md", "docs/RUNBOOK.md", "tools/package.py",
    "remote/bootstrap.sh", "remote/install-codex.sh", "remote/install-claude.sh",
    "remote/enable-codex-network.sh",
    "tests/test_srv.py", "tests/test_files.py",
)


def main():
    for name in FILES:
        source = ROOT / name
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Исходник должен находиться внутри проекта без ссылки: {name}")
        if not source.is_file():
            raise RuntimeError(f"Нет обязательного файла: {name}")
    output = ROOT / "dist"
    if not output.resolve().is_relative_to(ROOT):
        raise RuntimeError("Каталог dist должен находиться внутри проекта")
    output.mkdir(exist_ok=True)
    target = output / "srv-deployer.zip"
    if target.is_symlink():
        raise RuntimeError("Не перезаписываю ссылку вместо архива")
    scratch = ROOT / ".local" / "package-build"
    if not scratch.resolve().is_relative_to(ROOT):
        raise RuntimeError("Временный каталог пакета должен находиться внутри проекта")
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=scratch, suffix=".tmp", delete=False) as handle:
        temp = Path(handle.name)
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, arcname=name)
    with zipfile.ZipFile(temp) as archive:
        assert set(archive.namelist()) == set(FILES)
        assert archive.testzip() is None
        marker = b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----"
        assert all(marker not in archive.read(name) for name in FILES)
    temp.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "srv-deployer.zip.sha256").write_text(digest + "  srv-deployer.zip\n", encoding="ascii")
    print(str(target))
    print(digest)


if __name__ == "__main__":
    main()
