"""Summarize a local project folder into a text dossier the LLM can read."""
from __future__ import annotations

import collections
import json
import subprocess
from pathlib import Path

from .extract import file_text

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
             "target", ".idea", ".vscode", "vendor", "Pods", ".cache", "coverage"}
MANIFESTS = ["package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod",
             "Gemfile", "pom.xml", "build.gradle", "composer.json", "Package.swift",
             "docker-compose.yml", "Dockerfile", "CLAUDE.md"]
LANG_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript/React", ".js": "JavaScript",
    ".jsx": "JavaScript/React", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".c": "C",
    ".sql": "SQL", ".sh": "Shell", ".tf": "Terraform", ".vue": "Vue", ".svelte": "Svelte",
    ".ipynb": "Jupyter", ".dart": "Dart", ".scala": "Scala", ".r": "R",
}


def is_project_dir(path: Path) -> bool:
    return any((path / m).exists() for m in MANIFESTS + ["README.md", ".git"])


def find_projects(root: Path, max_depth: int = 3) -> list[Path]:
    """A folder of projects → each project folder. A single project → itself."""
    if is_project_dir(root):
        return [root]
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
            if is_project_dir(child):
                found.append(child)
            elif max_depth > 1:
                found.extend(find_projects(child, max_depth - 1))
    return found


def _git(path: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:
        return ""


def dossier(path: Path, max_chars: int = 30_000) -> str:
    langs: collections.Counter[str] = collections.Counter()
    tree: list[str] = []
    for p in path.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(path).parts):
            continue
        if p.is_file():
            lang = LANG_BY_EXT.get(p.suffix.lower())
            if lang:
                langs[lang] += 1
            if len(p.relative_to(path).parts) <= 2 and len(tree) < 80:
                tree.append(str(p.relative_to(path)))

    parts = [f"# Project folder: {path.name}", f"Path: {path}"]
    if langs:
        parts.append("Languages (file counts): " + ", ".join(f"{k} {v}" for k, v in langs.most_common(8)))
    if (path / ".git").exists():
        first = _git(path, "log", "--reverse", "--format=%ad", "--date=short", "-1")
        last = _git(path, "log", "--format=%ad", "--date=short", "-1")
        count = _git(path, "rev-list", "--count", "HEAD")
        remote = _git(path, "config", "--get", "remote.origin.url")
        parts.append(f"Git: {count} commits, {first} → {last}" + (f", remote {remote}" if remote else ""))
        subjects = _git(path, "log", "--format=%s", "-60")
        if subjects:
            parts.append("Recent commit messages:\n" + subjects)
    parts.append("Files:\n" + "\n".join(tree))
    for name in ["README.md", "README.rst", "README.txt", "CLAUDE.md", *MANIFESTS]:
        f = path / name
        if f.exists() and f.is_file():
            text = file_text(f, 8000) if f.suffix else f.read_text(errors="ignore")[:8000]
            if name == "package.json":
                try:
                    pkg = json.loads(text)
                    text = json.dumps({k: pkg.get(k) for k in ("name", "description", "dependencies",
                                                               "devDependencies")}, indent=1)
                except Exception:
                    pass
            parts.append(f"## {name}\n{text}")
    return "\n\n".join(parts)[:max_chars]
