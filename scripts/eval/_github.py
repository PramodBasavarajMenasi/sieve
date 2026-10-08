"""Shared helpers for the eval-repo search scripts: GitHub API access and JUnit sampling."""

from __future__ import annotations

import contextlib
import fnmatch
import io
import os
import statistics
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.ElementTree import fromstring

Json = dict[str, Any]

# Artifact names that usually hold JUnit XML.
JUNIT_PATTERNS = ("*junit*", "*test-result*", "*test_result*", "*pytest*", "*test-report*")
BOT_HINTS = ("dependabot", "renovate", "pre-commit-ci", "github-actions", "[bot]", "copilot")
BUMP_PREFIXES = ("bump ", "chore(deps)", "update dependency", "build(deps)")


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "GITHUB_TOKEN" and value.strip():
                return value.strip().strip("\"'")
    raise SystemExit("set GITHUB_TOKEN (or put it in .env)")


def client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=60,
        follow_redirects=True,
    )


def since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def paged(gh: httpx.Client, url: str, key: str | None, pages: int, **params: Any) -> list[Json]:
    """Up to ``pages`` pages of 100 from a list endpoint (``key`` for wrapped responses)."""
    items: list[Json] = []
    for page in range(1, pages + 1):
        response = gh.get(url, params={**params, "per_page": 100, "page": page})
        if response.status_code != httpx.codes.OK:
            break
        data = response.json()
        batch: list[Json] = data[key] if key else data
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def is_junit_artifact(artifact: Json) -> bool:
    name = artifact["name"].lower()
    return (
        not artifact["expired"]
        and "mypy" not in name
        and any(fnmatch.fnmatchcase(name, p) for p in JUNIT_PATTERNS)
    )


def is_bot(pr: Json) -> bool:
    user = pr.get("user") or {}
    login = str(user.get("login", "")).lower()
    return user.get("type") == "Bot" or any(hint in login for hint in BOT_HINTS)


@dataclass(frozen=True)
class JunitSample:
    tests: int
    failures: int
    median_s: float | None
    integration_pct: float


def junit_sample(
    gh: httpx.Client, artifact: Json, max_bytes: int = 40_000_000
) -> JunitSample | None:
    """Count tests, failing tests and durations in one artifact's XML files."""
    if artifact["size_in_bytes"] > max_bytes:
        return None
    response = gh.get(artifact["archive_download_url"])
    if response.status_code != httpx.codes.OK:
        return None
    tests = failures = integration = 0
    durations: list[float] = []
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith(".xml") or info.file_size > 50_000_000:
                    continue
                try:
                    root = fromstring(zf.read(info))
                except (ParseError, ValueError):
                    continue
                for case in root.iter("testcase"):
                    tests += 1
                    name = f"{case.get('classname', '')}::{case.get('name', '')}".lower()
                    if any(word in name for word in ("integration", "e2e", "functional")):
                        integration += 1
                    with contextlib.suppress(ValueError):  # unparseable time: skip it
                        durations.append(float(case.get("time") or 0))
                    if case.find("failure") is not None or case.find("error") is not None:
                        failures += 1
    except zipfile.BadZipFile:
        return None
    if not tests:
        return None
    return JunitSample(
        tests=tests,
        failures=failures,
        median_s=statistics.median(durations) if durations else None,
        integration_pct=round(100 * integration / tests, 1),
    )
