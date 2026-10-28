# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Assembles the public repository folder from the project. No downloads, no git commands.

    uv run export_repo.py           (from inside 08_WEBSITE, after uv run build_site.py)

Writes ../XX_REPOSITORY with this layout:

    index.html, events/, data/, img/, assets/   the website (GitHub Pages serves the repo root)
    README.md, LICENSE, DATA_LICENSE.md         repository documents
    pipeline/01_EVENTS ... pipeline/08_WEBSITE  code, small inputs and outputs of every stage

Left out on purpose: .env (API key), download caches, full-size Sentinel-2 images (the site keeps
web copies), .venv, 03_QGIS, XX_GUIDELINES, Office lock files.

Re-running replaces everything in XX_REPOSITORY except its .git folder, so it can be committed and
pushed again after every rebuild.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = HERE / "site"
OUT = ROOT / "XX_REPOSITORY"

STAGES = ["01_EVENTS", "02_OSM_CHECK", "04_FIRMS", "05_PERSISTENT_HEAT", "06_VERDICTS", "07_SENTINEL2", "08_WEBSITE"]
SKIP = shutil.ignore_patterns(
    "cache", ".venv", "__pycache__", "*.pyc", ".DS_Store", "~$*", ".env",
    "panels", "blind", "compressed",      # large Sentinel-2 images (07_SENTINEL2/outputs)
    "site",                               # 08_WEBSITE/site is published at the repository root
)
PROJECT_FILES = ["README.md", "LICENSES_AND_ATTRIBUTION.md"]
GITIGNORE = """.DS_Store
.env
.venv/
__pycache__/
*/data/cache/
pipeline/*/data/cache/
~$*
"""


def clear(folder: Path):
    """Remove everything in the folder except .git."""
    for p in folder.iterdir():
        if p.name == ".git":
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def main():
    if not (SITE / "index.html").exists():
        raise SystemExit("site/ not built yet: run  uv run build_site.py  first")
    OUT.mkdir(exist_ok=True)
    clear(OUT)

    # 1. website at the root
    for p in SITE.iterdir():
        if p.is_dir():
            shutil.copytree(p, OUT / p.name, ignore=shutil.ignore_patterns(".DS_Store"))
        else:
            shutil.copy2(p, OUT / p.name)

    # 2. pipeline code and small data
    pipe = OUT / "pipeline"
    pipe.mkdir()
    for stage in STAGES:
        if (ROOT / stage).exists():
            shutil.copytree(ROOT / stage, pipe / stage, ignore=SKIP)
    for name in PROJECT_FILES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, pipe / name)
    (OUT / ".gitignore").write_text(GITIGNORE)

    # 3. safety checks: no secrets, no oversized files
    problems = []
    env = ROOT / ".env"
    key = ""
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line:
                key = line.split("=", 1)[1].strip().strip('"')
    files = [p for p in OUT.rglob("*") if p.is_file() and ".git" not in p.parts]
    for p in files:
        if p.name == ".env":
            problems.append(f"secret file copied: {p}")
        if p.stat().st_size > 50e6:
            problems.append(f"file over 50 MB: {p}")
        if key and len(key) > 8 and p.suffix in (".py", ".csv", ".json", ".md", ".txt", ".html", ".geojson"):
            if key in p.read_text(encoding="utf-8", errors="ignore"):
                problems.append(f"FIRMS key found in {p}")
    if problems:
        raise SystemExit("export stopped:\n  " + "\n  ".join(problems))
    size = sum(p.stat().st_size for p in files) / 1e6
    print(f"exported {len(files)} files, {size:.1f} MB, to {OUT}")


if __name__ == "__main__":
    main()
