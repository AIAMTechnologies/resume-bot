"""Recognise which ATS a URL belongs to, so LinkedIn/Indeed jobs can pivot to the right adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


class Rerouted(Exception):
    """The job's real application lives elsewhere (company ATS)."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


@dataclass
class Route:
    source: str
    slug: str = ""
    job_id: str = ""
    url: str = ""


def route(url: str) -> Route:
    u = urlparse(url)
    host, path, qs = u.netloc.lower(), u.path, parse_qs(u.query)
    if "greenhouse.io" in host:
        m = re.search(r"/([^/]+)/jobs/(\d+)", path)
        if m:
            return Route("greenhouse", m.group(1), m.group(2), url)
        if "for" in qs and "token" in qs:
            return Route("greenhouse", qs["for"][0], qs["token"][0], url)
    if "gh_jid" in qs:
        return Route("greenhouse", "", qs["gh_jid"][0], url)
    if host.endswith("lever.co"):
        m = re.search(r"^/([^/]+)/([0-9a-f-]{36})", path)
        if m:
            return Route("lever", m.group(1), m.group(2), url)
    if host.endswith("ashbyhq.com"):
        m = re.search(r"^/([^/]+)/([0-9a-f-]{36})", path)
        if m:
            return Route("ashby", m.group(1), m.group(2), url)
    if "myworkdayjobs.com" in host or "workday" in host:
        return Route("workday", host.split(".")[0], path.rsplit("/", 1)[-1], url)
    return Route("external", host, "", url)
