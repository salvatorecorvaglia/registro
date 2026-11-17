#!/usr/bin/env python3
"""Build a native Python 3.13 wheelhouse from the frozen runtime dependency lock."""

import argparse
import hashlib
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from homebrew import CONFIG, asset_names, stable_version


def build(tag, target, output):
    version = stable_version(tag)
    operating_system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if target != f"{operating_system}_{architecture}" or sys.version_info[:2] != (3, 13):
        raise ValueError("Build wheelhouses natively for the requested target using Python 3.13")
    repository = "salvatorecorvaglia/" + CONFIG["name"]
    with tempfile.TemporaryDirectory(prefix="homebrew-wheelhouse-") as temporary:
        root = Path(temporary)
        wheels = root / "wheels"
        wheels.mkdir()
        filename = f"{CONFIG['wheel']}-{version}-py3-none-any.whl"
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--pattern",
                filename,
                "--pattern",
                "SHA256SUMS",
                "--dir",
                str(wheels),
            ],
            check=True,
        )
        matches = [
            line.split()[0]
            for line in (wheels / "SHA256SUMS").read_text().splitlines()
            if len(line.split()) == 2 and line.split()[1] == filename
        ]
        if matches != [hashlib.sha256((wheels / filename).read_bytes()).hexdigest()]:
            raise ValueError("Project wheel checksum mismatch")
        requirements = root / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--all-extras",
                "--no-emit-project",
                "--quiet",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            check=True,
        )
        subprocess.run(
            [
                "python",
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                str(requirements),
                "--dest",
                str(wheels),
            ],
            check=True,
        )
        # Exercise exactly the offline install that the formula will perform.
        venv = root / "venv"
        subprocess.run(["python", "-m", "venv", str(venv)], check=True)
        python = str(venv / "bin/python")
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                *map(str, sorted(wheels.glob("*.whl"))),
            ],
            check=True,
        )
        subprocess.run([python, "-m", "pip", "check"], check=True)
        command = [str(venv / "bin" / CONFIG["name"]), CONFIG["test"]]
        subprocess.run(command, check=True)
        output.mkdir(parents=True, exist_ok=True)
        name = f"{CONFIG['name']}_{version}_{target}_python313.tar.gz"
        if name not in asset_names(tag):
            raise ValueError("Unsupported wheelhouse target")
        with tarfile.open(output / name, "w:gz") as archive:
            archive.add("LICENSE", arcname=f"{CONFIG['name']}-{version}/LICENSE")
            for wheel in sorted(wheels.glob("*.whl")):
                archive.add(wheel, arcname=f"{CONFIG['name']}-{version}/wheels/{wheel.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.tag, args.target, args.output)
