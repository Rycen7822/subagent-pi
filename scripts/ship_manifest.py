"""One ship-file rule shared by scripts/package.py and scripts/install.py.

Both paths must ship exactly the same set: everything the plugin needs at
runtime, plus the tests/CI that document it — and nothing that is local,
generated or private. Keeping one rule prevents the two lists from drifting
(a local-only draft once reached both the ZIP and the installed plugin).
"""
import re
from pathlib import Path

# Directory names never shipped: VCS/dev tooling, caches, runtime state, local notes.
EXCLUDED_DIRS = {
    '.git', '.github-cache', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    '__pycache__', '.venv', 'venv', 'node_modules', 'build', 'dist',
    '.work', '.pi', '.codex', '.claude', '.cursor', '.windsurf', '.continue',
    '.copilot', 'htmlcov', '.idea', '.vscode',
}
# Local agent-instruction files are gitignored but carry no runtime value.
EXCLUDED_FILES = {
    'CLAUDE.md', 'GEMINI.md', 'AGENTS.local.md', 'TODO.md', 'NOTES.md',
    'daemon.log', 'daemon.previous.log', 'FILES.sha256',
}
EXCLUDED_SUFFIXES = {'.pyc', '.zip', '.sqlite', '.sqlite-wal', '.sqlite-shm'}
# Local-only notes by convention (*.local.md, scratch.sqlite-journal, *.egg-info).
EXCLUDED_PATTERNS = (re.compile(r'\.local\.md$'), re.compile(r'\.egg-info$'))

def excluded(path: Path, root: Path) -> bool:
    """True when `path` (a file under `root`) must not be shipped."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if any(part in EXCLUDED_DIRS or EXCLUDED_PATTERNS[1].search(part) for part in relative.parts[:-1]):
        return True
    name = relative.name
    if name in EXCLUDED_FILES or path.suffix in EXCLUDED_SUFFIXES:
        return True
    return any(pattern.search(name) for pattern in EXCLUDED_PATTERNS)

def ship_files(root: Path):
    """Every file under `root` that belongs in a release, sorted and deterministic."""
    return [f for f in sorted(root.rglob('*')) if f.is_file() and not excluded(f, root)]
