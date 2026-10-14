"""Restore, retain, and incrementally update trusted GitHub replay snapshots."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile
import zlib

if __package__:
    from . import fetch_replays as fetch
    from . import replay_snapshot
else:
    import fetch_replays as fetch
    import replay_snapshot

ARTIFACT_PREFIX = "battlecode-replay-state"
SNAPSHOT_MEMBER = "replay-state.tar.gz"
TIMEOUT = 30
RETRIES = 2
MAX_JSON_BYTES = 16 * 1024 * 1024
TRUSTED_EVENTS = {"schedule", "push", "workflow_dispatch"}


class StateError(fetch.FetchError):
    """Unsafe state provenance, invalid artifact, or failed persistence request."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _identifier(value, label):
    if type(value) is not int or value <= 0:
        raise StateError(f"Invalid {label}")
    return value


def _repository(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
            or any(part in (".", "..") for part in value.split("/"))):
        raise StateError("Expected an owner/repository GitHub repository")
    return value


def _created_at(value):
    if not isinstance(value, str):
        raise StateError("Artifact is missing its creation timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("missing timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise StateError("Invalid artifact creation timestamp") from error


def _https_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port == 0):
            raise ValueError("not a credential-free HTTPS URL")
    except (TypeError, ValueError) as error:
        raise StateError("Expected an HTTPS download URL without credentials") from error
    return value


def _content_length(response):
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise StateError("Invalid download Content-Length")
    return int(value)


def _extract_snapshot(archive_path, destination):
    """Write the sole allowed regular member without generic ZIP extraction."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != SNAPSHOT_MEMBER:
                raise StateError(f"Artifact must contain exactly {SNAPSHOT_MEMBER}")
            member = members[0]
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or mode not in (0, stat.S_IFREG) or member.external_attr & 0x10:
                raise StateError("Artifact snapshot member must be a regular file")
            with archive.open(member) as source, Path(destination).open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                if output.tell() != member.file_size:
                    raise StateError("Truncated artifact snapshot member")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        if isinstance(error, StateError):
            raise
        raise StateError("Corrupt or unsupported GitHub artifact ZIP") from error


class GitHubApi:
    def __init__(self, token, api_url="https://api.github.com"):
        if not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n"):
            raise StateError("A GitHub token is required")
        _https_url(api_url)
        if urlsplit(api_url).query:
            raise StateError("GitHub API base URL cannot have a query")
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self.opener = build_opener(_NoRedirect())

    def _api_path(self, path):
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or urlsplit(path).scheme or urlsplit(path).netloc or urlsplit(path).fragment):
            raise StateError("Expected a relative GitHub API path")
        return self.api_url + path

    def _request(self, method, url, consume, *, authenticated, allow_302=False):
        for attempt in range(RETRIES + 1):
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "cambc-replay-state/1.0"}
            if authenticated:
                headers.update(Authorization=f"Bearer {self.token}", **{"X-GitHub-Api-Version": "2022-11-28"})
            try:
                with self.opener.open(Request(url, method=method, headers=headers), timeout=TIMEOUT) as response:
                    return consume(response)
            except HTTPError as error:
                code = error.code
                retry_after = error.headers.get("Retry-After")
                location = error.headers.get("Location") if code == 302 and allow_302 else None
                error.close()
                if code == 302 and allow_302:
                    if not location:
                        raise StateError("GitHub artifact redirect is missing its destination") from None
                    return _https_url(location)
                if code not in (429, 500, 502, 503, 504) or attempt == RETRIES:
                    raise StateError(f"GitHub request returned HTTP {code}") from None
                wait = min(2 ** attempt, 5)
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        raise StateError("Invalid GitHub retry interval") from None
                    if not math.isfinite(wait) or wait < 0 or wait > 30:
                        raise StateError("GitHub retry interval exceeds the request budget") from None
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                if attempt == RETRIES:
                    raise StateError("GitHub request failed after bounded retries") from None
                wait = min(2 ** attempt, 5)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def json(self, method, path):
        method = method.upper()
        if method not in ("GET", "DELETE"):
            raise StateError("GitHub state requests support only GET and DELETE")

        def consume(response):
            if getattr(response, "status", None) == 204:
                return None
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise StateError("GitHub JSON response is too large")
            if not raw and method == "DELETE":
                return None
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise StateError("Expected a GitHub JSON response") from None

        return self._request(method, self._api_path(path), consume, authenticated=True)

    def download_artifact(self, repository, artifact, destination):
        """Verify the GitHub ZIP, then publish its extracted snapshot to destination."""
        repository = _repository(repository)
        if not isinstance(artifact, dict):
            raise StateError("Expected GitHub artifact metadata")
        artifact_id = _identifier(artifact.get("id"), "artifact ID")
        expected_digest = artifact.get("digest")
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", expected_digest):
                raise StateError("Invalid or unsupported artifact digest")
            expected_digest = expected_digest.split(":", 1)[1].lower()
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise StateError("Artifact snapshot destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="github_replay_artifact_", dir=destination.parent) as temporary:
            archive_path = Path(temporary) / "artifact.zip"
            observed = {}

            def consume(response):
                expected_size = _content_length(response)
                digest = hashlib.sha256()
                size = 0
                with archive_path.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                if expected_size is not None and size != expected_size:
                    raise http.client.IncompleteRead(b"")
                observed.update(sha256=digest.hexdigest(), bytes=size)
                return None

            redirect = self._request("GET", self._api_path(f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"),
                                     consume, authenticated=True, allow_302=True)
            if redirect is not None:
                # A fresh request carries no GitHub token to the signed URL.
                self._request("GET", redirect, consume, authenticated=False)
            if expected_digest is not None and observed["sha256"] != expected_digest:
                raise StateError("GitHub artifact SHA-256 digest mismatch")
            extracted = Path(temporary) / SNAPSHOT_MEMBER
            _extract_snapshot(archive_path, extracted)
            fetch.publish_new(extracted, destination)
            return {**observed, "digest_verified": expected_digest is not None}


def trusted_artifacts(api, repository, workflow_filename, branch):
    repository = _repository(repository)
    if (not isinstance(workflow_filename, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow_filename)):
        raise StateError("Expected a workflow YAML filename")
    if not isinstance(branch, str) or not branch or any(char in branch for char in "\r\n"):
        raise StateError("The trusted workflow branch is required")
    workflow = api.json("GET", f"/repos/{repository}/actions/workflows/{quote(workflow_filename, safe='')}")
    if not isinstance(workflow, dict):
        raise StateError("Invalid GitHub workflow response")
    workflow_id = _identifier(workflow.get("id"), "workflow ID")
    pattern = re.compile(re.escape(ARTIFACT_PREFIX) + r"-([1-9][0-9]*)-([1-9][0-9]*)\Z")
    trusted = []
    runs = {}
    seen = set()
    page = 1
    while True:
        data = api.json("GET", f"/repos/{repository}/actions/artifacts?" + urlencode({"per_page": 100, "page": page}))
        if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
            raise StateError("Invalid GitHub artifact listing")
        added = 0
        for artifact in data["artifacts"]:
            if not isinstance(artifact, dict):
                raise StateError("Invalid artifact listing entry")
            try:
                artifact_id = _identifier(artifact.get("id"), "artifact ID")
            except StateError:
                continue
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            added += 1
            name = artifact.get("name")
            named = pattern.fullmatch(name) if isinstance(name, str) else None
            source = artifact.get("workflow_run")
            if (named is None or artifact.get("expired") is not False or not isinstance(source, dict)
                    or source.get("head_branch") != branch):
                continue
            try:
                run_id = _identifier(source.get("id"), "workflow run ID")
                _created_at(artifact.get("created_at"))
            except StateError:
                continue
            if run_id != int(named.group(1)):
                continue
            if run_id not in runs:
                runs[run_id] = api.json("GET", f"/repos/{repository}/actions/runs/{run_id}")
            run = runs[run_id]
            if not isinstance(run, dict):
                raise StateError("Invalid GitHub workflow run response")
            head = run.get("head_repository")
            if (type(run.get("id")) is not int or run.get("id") != run_id or type(run.get("workflow_id")) is not int
                    or run.get("workflow_id") != workflow_id or run.get("head_branch") != branch
                    or run.get("event") not in TRUSTED_EVENTS or not isinstance(head, dict)
                    or not isinstance(head.get("full_name"), str)
                    or head["full_name"].casefold() != repository.casefold()):
                continue
            trusted.append(artifact)
        if len(data["artifacts"]) < 100:
            break
        if not added:
            raise StateError("GitHub artifact pagination made no progress")
        page += 1
    return sorted(trusted, key=lambda item: (_created_at(item["created_at"]), item["id"]), reverse=True)


def _empty_directory(outdir):
    outdir = Path(outdir)
    if outdir.exists() or outdir.is_symlink():
        info = outdir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise StateError("Bootstrap archive must be a real directory")
        if any(outdir.iterdir()):
            raise StateError("Bootstrap requires an empty replay archive directory")
    else:
        outdir.mkdir(parents=True)


def restore_state(api, repository, workflow_filename, branch, outdir, snapshot, *, bootstrap=False, seed=None):
    artifacts = trusted_artifacts(api, repository, workflow_filename, branch)
    if not artifacts:
        if not bootstrap:
            raise StateError("No live trusted snapshot exists; explicit bootstrap is required")
        _empty_directory(outdir)
        if seed is not None:
            seed = Path(seed)
            if not seed.is_file():
                raise StateError("Supplied bootstrap seed snapshot does not exist")
            report = replay_snapshot.restore_snapshot(seed, outdir)
            return {"restored": True, "bootstrap": True, "source": "seed", **report}
        return {"restored": False, "bootstrap": True, "source": "empty"}
    newest = artifacts[0]
    snapshot = Path(snapshot)
    if snapshot.resolve().is_relative_to(Path(outdir).resolve()):
        raise StateError("Restored snapshot must be outside the replay archive")
    api.download_artifact(repository, newest, snapshot)
    report = replay_snapshot.restore_snapshot(snapshot, outdir)
    return {"restored": True, "bootstrap": False, "artifact_id": newest["id"],
            "artifact_name": newest["name"], **report}


def prune_state(api, repository, workflow_filename, branch, uploaded_id, keep=2):
    _identifier(uploaded_id, "uploaded artifact ID")
    if type(keep) is not int or keep < 2:
        raise StateError("Retain at least two trusted snapshots")
    artifacts = trusted_artifacts(api, repository, workflow_filename, branch)
    current = next((artifact for artifact in artifacts if artifact["id"] == uploaded_id), None)
    if current is None:
        raise StateError("Uploaded artifact is not a live trusted snapshot")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id is not None:
        if not re.fullmatch(r"[1-9][0-9]*", run_id) or int(run_id) != current["workflow_run"]["id"]:
            raise StateError("Uploaded snapshot does not belong to the current workflow run")
    if artifacts[0]["id"] != uploaded_id:
        raise StateError("A newer trusted snapshot exists; refusing stale retention cleanup")
    kept = [artifact["id"] for artifact in artifacts[:keep]]
    deleted = []
    for artifact in artifacts[keep:]:
        api.json("DELETE", f"/repos/{_repository(repository)}/actions/artifacts/{artifact['id']}")
        deleted.append(artifact["id"])
    return {"uploaded_id": uploaded_id, "kept": kept, "deleted": deleted}


def run_sync(outdir, seconds=180, max_pages=20, max_details=40, max_downloads=100):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise StateError("Sync work budget must be a positive finite number")
    for value, label, minimum in ((max_pages, "pages", 1), (max_details, "details", 0),
                                  (max_downloads, "downloads", 0)):
        if type(value) is not int or value < minimum:
            raise StateError(f"Invalid sync {label} bound")
    script = Path(__file__).with_name("sync_replays.py")
    command = [sys.executable, str(script), "--outdir", str(Path(outdir)), "--max-pages", str(max_pages),
               "--max-details", str(max_details), "--max-downloads", str(max_downloads),
               "--timeout", "15", "--delay", "0.25", "--retries", "1"]
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen(command, **options)
    reaped = False
    try:
        try:
            result = child.wait(timeout=seconds)
            reaped = True
            return result if result >= 0 else 1
        except subprocess.TimeoutExpired:
            print("Sync work budget reached; requesting a checkpointed stop.", file=sys.stderr)
        try:
            child.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        except OSError:
            if child.poll() is None:
                raise
            # The child can finish between wait's timeout and the interrupt.
        try:
            result = child.wait(timeout=20)
            reaped = True
            return 0 if result in (0, 130) else result if result > 0 else 124
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            reaped = True
            return 124
    finally:
        if not reaped:
            if child.poll() is None:
                child.kill()
            child.wait()


def _positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _nonnegative(value):
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    prune = commands.add_parser("prune")
    for operation in (restore, prune):
        operation.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
        operation.add_argument("--branch", required=True)
        operation.add_argument("--workflow", default="replay-sync.yml")
    restore.add_argument("--outdir", type=Path, required=True)
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--bootstrap", action="store_true")
    restore.add_argument("--seed", type=Path)
    prune.add_argument("--uploaded-id", type=_positive, required=True)
    prune.add_argument("--keep", type=_positive, default=2)
    run = commands.add_parser("run")
    run.add_argument("--outdir", type=Path, required=True)
    run.add_argument("--seconds", type=fetch.nonnegative_float, default=180)
    run.add_argument("--max-pages", type=_positive, default=20)
    run.add_argument("--max-details", type=_nonnegative, default=40)
    run.add_argument("--max-downloads", type=_nonnegative, default=100)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_sync(args.outdir, args.seconds, args.max_pages, args.max_details, args.max_downloads)
        api = GitHubApi(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        if args.command == "restore":
            report = restore_state(api, args.repository, args.workflow, args.branch, args.outdir, args.snapshot,
                                   bootstrap=args.bootstrap, seed=args.seed)
        else:
            report = prune_state(api, args.repository, args.workflow, args.branch, args.uploaded_id, args.keep)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (fetch.FetchError, OSError, sqlite3.Error) as error:
        print(f"State persistence failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
