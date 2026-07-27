# Contributing

Thanks for considering it! This project is young — bug reports from real setups are as valuable as code.

## Dev setup

```bash
git clone https://github.com/Vasanthdev2004/Game-Save-Genie
cd Game-Save-Genie
python -m venv .venv && .venv\Scripts\activate   # or source .venv/bin/activate
pip install -e ".[dev]"
```

## Before you open a PR

All three must pass — CI runs them on Windows and Linux:

```bash
pytest -q
ruff check src tests
mypy src tests        # strict mode
```

## Ground rules

- **Never risk save data.** Anything that touches live save files must verify first, take a safety backup, and fail closed (abort rather than half-apply). Look at `_apply_cloud_version` in `cli.py` for the pattern.
- **Version-id format is load-bearing.** Ids are UTC `%Y%m%d-%H%M%S-%f` timestamps; lexicographic order == chronological order is relied on in `sync.py`, `cloud.py`, and the restore gates. Don't change it.
- **Keep `_slugify` byte-stable.** Existing `games.yaml` files depend on it; a changed scheme re-adds every tracked game under a new id.
- New behavior needs a test. Pure logic (policies, parsers, remapping) should live in testable functions without I/O.

## The website

`docs/` is the landing page, served by GitHub Pages from `main`. It is one
self-contained HTML file with no build step and no external requests — no web
fonts, no analytics, no CDN. Please keep it that way.

Preview it locally with any static server:

```bash
python -m http.server 8765 --directory docs
```

`assets/icon.png` and `assets/social-card.png` under `docs/` are generated —
run `python packaging/make_icon.py` rather than editing them by hand.

## Where help is especially wanted

- **Linux / Steam Deck testing** — the backup/restore/pull path has Linux branches that need real-world exercise (autostart + toasts are Windows-only right now).
- **Launcher detection edge cases** — Steam library layouts, Epic manifests, Xbox/UWP quirks (`launcher.py`).
- **Emulator save presets** — RetroArch/PCSX2/Dolphin path recipes (planned `gsg add --path`).
- **Process-matching reports** — games whose executables the watcher misses or false-matches (`watcher.py`).
