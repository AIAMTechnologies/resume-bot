"""Pull public (and, with a token, private) repos for the master list."""
from __future__ import annotations

import base64

import httpx

from ..config import env

API = "https://api.github.com"


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if env().github_token:
        h["Authorization"] = f"Bearer {env().github_token}"
    return h


async def list_repos(username: str | None = None) -> list[dict]:
    username = username or env().github_username
    url = f"{API}/user/repos?affiliation=owner&per_page=100" if env().github_token else \
        f"{API}/users/{username}/repos?per_page=100&type=owner"
    repos: list[dict] = []
    async with httpx.AsyncClient(headers=_headers(), timeout=30) as c:
        while url:
            r = await c.get(url)
            r.raise_for_status()
            repos.extend(r.json())
            url = r.links.get("next", {}).get("url")
    return [r for r in repos if not r.get("fork") and not r.get("archived")]


async def repo_dossier(repo: dict) -> str:
    full = repo["full_name"]
    async with httpx.AsyncClient(headers=_headers(), timeout=30) as c:
        langs = (await c.get(f"{API}/repos/{full}/languages")).json()
        readme = ""
        r = await c.get(f"{API}/repos/{full}/readme")
        if r.status_code == 200:
            readme = base64.b64decode(r.json().get("content", "")).decode(errors="ignore")
    return "\n\n".join([
        f"# GitHub repo: {full}",
        f"URL: {repo.get('html_url')}",
        f"Description: {repo.get('description') or ''}",
        f"Topics: {', '.join(repo.get('topics') or [])}",
        f"Created {repo.get('created_at', '')[:10]}, last push {repo.get('pushed_at', '')[:10]}, "
        f"stars {repo.get('stargazers_count', 0)}",
        "Languages (bytes): " + ", ".join(f"{k} {v}" for k, v in langs.items()),
        "## README\n" + readme[:12000],
    ])
