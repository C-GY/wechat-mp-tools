"""Build the Windows Full installer using the project's fixed install policy."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "installer" / "windows_full_setup.iss"
REQUIRED_SETTINGS = {
    "AppId": "{{E956D31A-06A0-49BB-BA0B-3AE4DF127F6A}",
    "DefaultDirName": r"{localappdata}\Programs\SelfMediaContentCollector",
    "UsePreviousAppDir": "no",
}


def verify_install_policy() -> None:
    source = SETUP.read_text(encoding="utf-8-sig")
    for name, expected in REQUIRED_SETTINGS.items():
        values = re.findall(rf"^{name}\s*=(.*?)\s*$", source, re.MULTILINE)
        if values != [expected]:
            raise ValueError(f"Installation policy mismatch: {name} must be {expected!r}")


def find_compiler(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = [Path(found)] if (found := shutil.which("ISCC.exe")) else []
        for key, suffix in (
            ("LOCALAPPDATA", "Programs/Inno Setup 6/ISCC.exe"),
            ("ProgramFiles(x86)", "Inno Setup 6/ISCC.exe"),
            ("ProgramFiles", "Inno Setup 6/ISCC.exe"),
        ):
            if base := os.environ.get(key):
                candidates.append(Path(base) / suffix)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError("Inno Setup 6 ISCC.exe not found; pass --compiler PATH")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 2026.09.08.1")
    parser.add_argument("--source-dir", type=Path, required=True, help="PyInstaller Full bundle directory")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "installer")
    parser.add_argument("--compiler", type=Path)
    parser.add_argument("--check-only", action="store_true", help="Validate inputs without compiling")
    args = parser.parse_args()

    try:
        verify_install_policy()
        if not re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}(?:\.\d{1,5})?", args.version):
            raise ValueError("Version must contain three or four numeric components")
        parts = [int(part) for part in args.version.split(".")]
        if any(part > 65535 for part in parts):
            raise ValueError("Version components must be at most 65535")
        file_version = ".".join(map(str, parts + [0] * (4 - len(parts))))
        source_dir = args.source_dir.resolve()
        for required in ("WeChat MP Tools.exe", "_internal/ms-playwright"):
            if not (source_dir / required).exists():
                raise ValueError(f"Full bundle is missing {required}: {source_dir}")
        compiler = find_compiler(args.compiler)
    except ValueError as error:
        parser.error(str(error))

    release_name = "".join(args.version.split(".")[:3])
    if len(parts) == 4:
        release_name += f"_r{parts[3]}"
    basename = f"自媒体内容采集工具_Windows_x64_Full_Setup_{release_name}"
    output_dir = args.output_dir.resolve()
    output_file = output_dir / f"{basename}.exe"
    command = [
        str(compiler),
        f"/DAppVersion={args.version}",
        f"/DVersionInfoVersion={file_version}",
        f"/DSourceDir={source_dir}",
        f"/DOutputDir={output_dir}",
        f"/DOutputBaseFilename={basename}",
        str(SETUP),
    ]
    print(f"Default directory: {REQUIRED_SETTINGS['DefaultDirName']}", flush=True)
    print(f"Installer: {output_file}", flush=True)
    if args.check_only:
        print("Installation policy and build inputs verified; no files written.")
        return
    if output_file.exists():
        parser.error("Installer already exists; increment the version revision to keep releases distinct")
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, cwd=ROOT, check=True)
    checksum = hashlib.sha256()
    with output_file.open("rb") as bundle:
        for chunk in iter(lambda: bundle.read(1024 * 1024), b""):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    output_file.with_suffix(".exe.sha256").write_text(
        f"{digest}  {output_file.name}\n", encoding="utf-8"
    )
    print(f"SHA256: {digest}")


if __name__ == "__main__":
    main()
