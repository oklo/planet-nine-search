"""Check local worker permissions before claiming a field."""

import argparse
import shutil
import tempfile
from pathlib import Path

import psutil
from astropy.config.paths import get_cache_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="intended work directory")
    args = parser.parse_args()
    try:
        psutil.Process().children(recursive=True)
    except (OSError, psutil.AccessDenied) as error:
        parser.exit(1, f"Worker cannot monitor child memory in this session: {error}\n")
    try:
        args.directory.mkdir(parents=True, exist_ok=True)
        for path in (args.directory, Path(get_cache_dir("lkspacecraft")) / "download" / "url"):
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
    except OSError as error:
        parser.exit(1, f"Worker needs write access: {error}\n")
    gib = 2**30
    print(
        f"{psutil.virtual_memory().total / gib:.1f} GiB RAM, "
        f"{shutil.disk_usage(args.directory).free / gib:.1f} GiB free on the work volume."
    )


if __name__ == "__main__":
    main()
