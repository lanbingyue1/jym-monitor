"""Persistent run state and the stores that read/write it.

The state is a plain JSON object shared by the pipeline (Task 7) and the CLI
(Task 8).  Two stores are provided: a local JSON file and a file kept in a
GitHub repository through the Contents API.  Corrupt state is never silently
reset to an empty state -- it raises :class:`StateError` so the caller can
surface the problem.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from monitor.errors import StateError
from monitor.models import Listing

STATE_VERSION = 1
_GITHUB_API = "https://api.github.com"
_GITHUB_TIMEOUT_SECONDS = 30
_SAVE_COMMIT_MESSAGE = "chore: update monitor state"


def new_state() -> dict[str, Any]:
    """Return an empty state document."""
    return {
        "version": STATE_VERSION,
        "last_commit_at": None,
        "sites": {},
        "notifiers": {},
    }


def _to_utc(value: datetime) -> datetime:
    """Return *value* in UTC; naive datetimes are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_ts(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    return _to_utc(value).isoformat()


def _parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 string; a trailing ``Z`` and naive strings are UTC."""
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _to_utc(datetime.fromisoformat(text))


def _state_ts(mapping: dict[str, Any], key: str) -> datetime:
    """Read and parse a required timestamp from a state mapping."""
    try:
        raw = mapping[key]
    except KeyError as exc:
        raise StateError(f"state is missing {key!r}") from exc
    try:
        return _parse_ts(raw)
    except (TypeError, ValueError) as exc:
        raise StateError(f"state has invalid {key!r}: {raw!r}") from exc


def ensure_site(state: dict[str, Any], site_id: str, now: datetime) -> dict[str, Any]:
    """Return the state block for *site_id*, filling defaults when absent.

    An existing ``anchor_at`` is never overwritten: it marks the first run and
    decides which listings are considered new.
    """
    site = state["sites"].setdefault(site_id, {})
    site.setdefault("anchor_at", _format_ts(now))
    site.setdefault("seen", {})
    site.setdefault("last_run", None)
    site.setdefault("consecutive_failures", 0)
    site.setdefault("last_alert_at", None)
    return site


def ensure_notifier(state: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the state block for notifier *name*, filling defaults when absent."""
    notifier = state["notifiers"].setdefault(name, {})
    notifier.setdefault("consecutive_failures", 0)
    notifier.setdefault("last_alert_at", None)
    return notifier


def is_new(listing: Listing, site_state: dict[str, Any]) -> bool:
    """Return True when *listing* has not been seen and is at/after the anchor.

    A listing without a publication timestamp is treated conservatively: it is
    reported unless its ID was already recorded.
    """
    if listing.item_id in site_state["seen"]:
        return False
    if listing.published_at is None:
        return True
    anchor = _state_ts(site_state, "anchor_at")
    return _to_utc(listing.published_at) >= anchor


def mark_seen(
    site_state: dict[str, Any], listings: list[Listing], now: datetime
) -> None:
    """Record *listings* as seen at *now*."""
    seen = site_state["seen"]
    stamp = _format_ts(now)
    for listing in listings:
        seen[listing.item_id] = stamp


def prune_seen(
    site_state: dict[str, Any],
    now: datetime,
    days: int = 60,
    max_entries: int = 5000,
) -> None:
    """Drop stale seen entries and cap the mapping to the newest entries.

    Entries whose timestamp cannot be parsed are dropped as well.
    """
    seen = site_state["seen"]
    cutoff = _to_utc(now) - timedelta(days=days)
    parsed: dict[str, datetime] = {}
    for item_id, raw in seen.items():
        try:
            timestamp = _parse_ts(raw)
        except (TypeError, ValueError):
            continue
        if timestamp < cutoff:
            continue
        parsed[item_id] = timestamp

    if len(parsed) > max_entries:
        newest = sorted(parsed, key=parsed.__getitem__, reverse=True)[:max_entries]
        keep = set(newest)
        parsed = {item_id: ts for item_id, ts in parsed.items() if item_id in keep}

    site_state["seen"] = {item_id: seen[item_id] for item_id in parsed}


def should_commit(
    state: dict[str, Any], now: datetime, heartbeat_hours: int = 24
) -> bool:
    """Return True when state must be committed (heartbeat elapsed or never)."""
    raw = state.get("last_commit_at")
    if raw is None:
        return True
    last_commit = _state_ts(state, "last_commit_at")
    return _to_utc(now) - last_commit > timedelta(hours=heartbeat_hours)


class FileStateStore:
    """Read/write state as a local JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return new_state()
        except OSError as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"invalid JSON in state file {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"state file {self.path} must contain a JSON object")
        return data

    def save(self, state: dict[str, Any]) -> None:
        text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = None
        except OSError as exc:
            raise StateError(f"cannot write state file {self.path}: {exc}") from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


class GitHubStateStore:
    """Read/write state through the GitHub Contents API."""

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "state",
        path: str = "state.json",
    ):
        self.repo = repo
        self.branch = branch
        self.path = path
        self._token = token
        self._sha: str | None = None
        self._url = f"{_GITHUB_API}/repos/{repo}/contents/{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def _request_json(self, response: Any, action: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise StateError(f"GitHub state {action} response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise StateError(f"GitHub state {action} response must be a JSON object")
        return data

    def load(self) -> dict[str, Any]:
        url = f"{self._url}?ref={self.branch}"
        try:
            response = requests.get(
                url, headers=self._headers(), timeout=_GITHUB_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise StateError(f"cannot read GitHub state: {exc}") from exc
        if response.status_code == 404:
            self._sha = None
            return new_state()
        if response.status_code != 200:
            raise StateError(
                f"GitHub state read failed with status {response.status_code}"
            )
        payload = self._request_json(response, "read")
        sha = payload.get("sha")
        self._sha = sha if isinstance(sha, str) else None
        content = payload.get("content")
        if not content:
            if self._sha:
                # A file record with a sha but no inline content (for example a
                # file larger than 1 MB, or ``encoding: "none"``) is not the
                # same as a missing file: resetting state here would lose the
                # anchor and every seen ID, so surface it loudly instead.
                raise StateError(
                    "GitHub state file is present but has no inline content"
                )
            return new_state()
        try:
            raw = base64.b64decode(content).decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise StateError("GitHub state content is not valid base64/UTF-8") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError("GitHub state content is not valid JSON") from exc
        if not isinstance(data, dict):
            raise StateError("GitHub state content must be a JSON object")
        return data

    def _put(self, payload: dict[str, Any]) -> Any:
        try:
            return requests.put(
                self._url,
                json=payload,
                headers=self._headers(),
                timeout=_GITHUB_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise StateError(f"cannot write GitHub state: {exc}") from exc

    def _body(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        body: dict[str, Any] = {
            "message": _SAVE_COMMIT_MESSAGE,
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha:
            body["sha"] = self._sha
        return body

    def save(self, state: dict[str, Any]) -> None:
        response = self._put(self._body(state))
        if response.status_code == 409:
            # Someone else committed in between: refresh the sha and retry once.
            self.load()
            response = self._put(self._body(state))
        if response.status_code not in (200, 201):
            raise StateError(
                f"GitHub state save failed with status {response.status_code}"
            )
        payload = self._request_json(response, "write")
        content = payload.get("content")
        if isinstance(content, dict) and isinstance(content.get("sha"), str):
            self._sha = content["sha"]
