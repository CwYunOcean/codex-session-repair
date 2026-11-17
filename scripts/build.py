"""Build deterministic zip/zipapp releases with the Python standard library."""

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_session_repair import __version__


def write_zip(output, files):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def main():
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    package = {str(p.relative_to(ROOT)).replace("\\", "/"): p.read_bytes() for p in (ROOT / "codex_session_repair").glob("*.py")}
    application = {**package, "__main__.py": b"import runpy\nrunpy.run_module('codex_session_repair', run_name='__main__')\n"}
    write_zip(destination / "codex-session-repair.pyz", application)
    files = dict(package)
    for name in ("README.md", "LICENSE", "pyproject.toml", "codex-session-repair.cmd", ".gitignore", ".gitattributes", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        files[name] = (ROOT / name).read_bytes()
    # Explicit resources keep ignored/private files out of Release archives.
    for name in (
        "docs/root-cause.zh-CN.md", "scripts/build.py", "scripts/reproduce.py",
        "tests/__init__.py", "tests/fixtures.py", "tests/test_repair.py",
        ".github/workflows/ci.yml", ".github/ISSUE_TEMPLATE/bug_report.yml",
    ):
        files[name] = (ROOT / name).read_bytes()
    source_archive = destination / f"codex-session-repair-{__version__}.zip"
    write_zip(source_archive, files)
    hashes = []
    for path in sorted([source_archive, destination / "codex-session-repair.pyz"]):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(digest + "  " + path.name)
        print(path)
    (destination / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
