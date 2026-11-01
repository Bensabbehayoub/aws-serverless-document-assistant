"""Build the Lambda packages into .build/ before running Terraform.

    python scripts/build.py

Terraform zips .build/pdf_processor (code + pypdf). The API Lambda has no
dependencies besides boto3, which the Lambda runtime already provides.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
PROCESSOR_SRC = ROOT / "backend" / "pdf_processor"
PROCESSOR_OUT = BUILD / "pdf_processor"


def build_processor():
    if PROCESSOR_OUT.exists():
        shutil.rmtree(PROCESSOR_OUT)
    PROCESSOR_OUT.mkdir(parents=True)

    # Install for the Lambda platform (Linux, Python 3.12), whatever this machine is.
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--requirement", str(PROCESSOR_SRC / "requirements.txt"),
            "--target", str(PROCESSOR_OUT),
            "--platform", "manylinux2014_x86_64",
            "--python-version", "3.12",
            "--implementation", "cp",
            "--only-binary=:all:",
            "--upgrade",
            "--quiet",
        ],
        check=True,
    )
    shutil.copy2(PROCESSOR_SRC / "lambda_function.py", PROCESSOR_OUT / "lambda_function.py")

    for cache in PROCESSOR_OUT.rglob("__pycache__"):
        shutil.rmtree(cache)

    print(f"Built {PROCESSOR_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    build_processor()
