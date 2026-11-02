"""Backblaze B2 state store for Prime reviewer.

Provides session persistence and immutable trace bundle storage in Backblaze B2 (S3-compatible API).
Supports safe tar.gz extraction, atomic pointer updates, and isolation across repositories and pull requests.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    Config = None  # type: ignore

    class ClientError(Exception):  # type: ignore
        def __init__(self, error_response: dict, operation_name: str = ""):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(str(error_response))


B2_ENV_VARS = (
    "B2_ENDPOINT_URL",
    "B2_BUCKET",
    "B2_REGION",
    "B2_ACCESS_KEY_ID",
    "B2_SECRET_ACCESS_KEY",
)


class StorageError(RuntimeError):
    """Generic storage error that does not leak sensitive endpoints, buckets, or keys."""
    pass


class SessionNotFoundError(StorageError):
    """Raised when a session was expected (e.g. instruct mode) but does not exist."""
    pass


class CorruptArchiveError(StorageError):
    """Raised when an archive fails integrity or safety checks (traversal, symlinks, etc.)."""
    pass


def is_b2_configured(env: dict[str, str] | None = None) -> bool:
    """Return True if all five required B2 environment variables are present and non-empty."""
    source = os.environ if env is None else env
    return all(bool(source.get(k, "").strip()) for k in B2_ENV_VARS)


def any_b2_configured(env: dict[str, str] | None = None) -> bool:
    """Return True if at least one B2 environment variable is present and non-empty."""
    source = os.environ if env is None else env
    return any(bool(source.get(k, "").strip()) for k in B2_ENV_VARS)


def validate_b2_config(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return dict of B2 configuration or raise StorageError if any required variable is missing."""
    source = os.environ if env is None else env
    missing = [k for k in B2_ENV_VARS if not source.get(k, "").strip()]
    if missing:
        raise StorageError(f"Missing required B2 configuration: {', '.join(missing)}")
    return {k: source[k].strip() for k in B2_ENV_VARS}


def is_not_found(exc: Exception) -> bool:
    """Return True if an exception represents a missing object (NoSuchKey / NotFound / 404).

    Inspects structured S3 error codes only.
    NoSuchBucket must fail and NOT be treated as a missing object.
    Generic exception strings containing '404' are never treated as not-found.
    """
    if hasattr(exc, "response") and isinstance(exc.response, dict):
        err = exc.response.get("Error", {})
        code = str(err.get("Code", "")).strip()
        if code in ("NoSuchBucket", "AccessDenied", "Forbidden", "InvalidBucketName"):
            return False
        if code in ("NoSuchKey", "NotFound", "404"):
            return True
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404 and (not code or code in ("404", "NotFound", "NoSuchKey")):
            return True
    return False


def safe_extract_tar(archive_source: Path | bytes | io.BytesIO, dest_dir: Path) -> list[str]:
    """Extract tar.gz safely into dest_dir.

    Allows only regular files and directories. Rejects symlinks, hardlinks, devices,
    FIFOs, directory traversal, absolute paths, and escaping members before any extraction.
    Catches malformed/corrupted archives with CorruptArchiveError.
    Returns list of extracted member names.
    """
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        if isinstance(archive_source, (bytes, bytearray)):
            fileobj = io.BytesIO(archive_source)
            tar_context = tarfile.open(fileobj=fileobj, mode="r:gz")
        elif isinstance(archive_source, (io.BytesIO, io.BufferedReader)):
            tar_context = tarfile.open(fileobj=archive_source, mode="r:gz")
        else:
            tar_context = tarfile.open(archive_source, mode="r:gz")
    except Exception:
        raise CorruptArchiveError("Malformed or corrupt archive: failed to read archive") from None

    extracted_members: list[str] = []
    try:
        with tar_context as tar:
            members = tar.getmembers()
            for member in members:
                # 1. Allow only regular files and directories (reject symlinks, hardlinks, devices, FIFOs, specials)
                if not (member.isfile() or member.isdir()):
                    raise CorruptArchiveError(
                        f"Archive member is not a regular file or directory: safe extraction rejected"
                    )

                # 2. Reject absolute paths
                if member.name.startswith("/") or os.path.isabs(member.name) or Path(member.name).is_absolute():
                    raise CorruptArchiveError(
                        f"Archive member has an absolute path: safe extraction rejected"
                    )

                # 3. Reject directory traversal
                parts = Path(member.name).parts
                if ".." in parts:
                    raise CorruptArchiveError(
                        f"Archive member contains path traversal (..): safe extraction rejected"
                    )

                resolved_target = (dest_dir / member.name).resolve()
                if not (resolved_target == dest_dir or resolved_target.is_relative_to(dest_dir)):
                    raise CorruptArchiveError(
                        f"Archive member escapes destination directory: safe extraction rejected"
                    )

            # Extraction after all members are verified
            for member in members:
                if hasattr(tarfile, "data_filter"):
                    tar.extract(member, path=dest_dir, filter="data")
                else:
                    tar.extract(member, path=dest_dir)
                extracted_members.append(member.name)
    except CorruptArchiveError:
        raise
    except Exception:
        raise CorruptArchiveError("Malformed or corrupt archive: safe extraction failed") from None

    return extracted_members


def create_session_tar(session_dir: Path) -> bytes:
    """Compress the entire session directory into a tar.gz byte stream."""
    session_dir = session_dir.resolve()
    buf = io.BytesIO()
    if session_dir.is_dir():
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for root, _, files in os.walk(session_dir):
                for file_name in sorted(files):
                    full_path = Path(root) / file_name
                    rel_path = full_path.relative_to(session_dir)
                    tar.add(full_path, arcname=str(rel_path))
    return buf.getvalue()


def create_trace_bundle(
    meta: dict,
    result: dict | None,
    session_dir: Path,
    child_dirs: list[Path],
    manifest: dict,
) -> bytes:
    """Pack manifest, meta, result, session transcript, and subagents into a tar.gz bundle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # manifest.json
        manifest_data = json.dumps(manifest, indent=2).encode("utf-8")
        ti = tarfile.TarInfo(name="manifest.json")
        ti.size = len(manifest_data)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(manifest_data))

        # meta.json
        meta_data = json.dumps(meta, indent=2).encode("utf-8")
        ti = tarfile.TarInfo(name="meta.json")
        ti.size = len(meta_data)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(meta_data))

        # result.json
        if result is not None:
            result_data = json.dumps(result, indent=2).encode("utf-8")
            ti = tarfile.TarInfo(name="result.json")
            ti.size = len(result_data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(result_data))

        # session.jsonl
        root_session = (
            next(iter(sorted(session_dir.glob("*.jsonl"))), None)
            if session_dir.is_dir()
            else None
        )
        if root_session and root_session.is_file():
            tar.add(root_session, arcname="session.jsonl")

        # subagents
        for cdir in child_dirs:
            if cdir.is_dir():
                for sf in sorted(cdir.rglob("*.jsonl")):
                    rel = sf.relative_to(cdir)
                    tar.add(sf, arcname=f"subagents/{rel}")

    return buf.getvalue()


class B2StateStore:
    """Manages session persistence and immutable run bundles in B2."""

    def __init__(
        self,
        bucket: str,
        owner: str,
        repo: str,
        pr: str | int,
        client: Any = None,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        self.bucket = bucket
        self.owner = owner
        self.repo = repo
        self.repo_name = repo.split("/", 1)[1] if "/" in repo else repo
        self.pr = str(pr)
        self.endpoint_url = endpoint_url
        self.region = region

        if client is not None:
            self._client = client
        else:
            if boto3 is None:
                raise StorageError("boto3 is required for B2 storage mode")
            try:
                self._client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=region,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    config=Config(signature_version="s3v4", request_checksum_calculation="when_required",
                                  response_checksum_validation="when_required") if Config else None,
                )
            except Exception as e:
                raise StorageError(f"Failed to initialize B2 client: {type(e).__name__}") from None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, client: Any = None) -> B2StateStore:
        """Instantiate B2StateStore from environment variables."""
        source = os.environ if env is None else env
        cfg = validate_b2_config(source)
        owner = source["OWNER"].strip()
        repo = source["REPO"].strip()
        pr = source["PR_NUMBER"].strip()
        return cls(
            bucket=cfg["B2_BUCKET"],
            owner=owner,
            repo=repo,
            pr=pr,
            client=client,
            endpoint_url=cfg["B2_ENDPOINT_URL"],
            region=cfg["B2_REGION"],
            access_key_id=cfg["B2_ACCESS_KEY_ID"],
            secret_access_key=cfg["B2_SECRET_ACCESS_KEY"],
        )

    @property
    def latest_key(self) -> str:
        """Mutable latest pointer key for this PR."""
        return f"sessions/{self.owner}/{self.repo_name}/{self.pr}/latest.json"

    def session_key(self, run_id: str) -> str:
        """Immutable session snapshot key for this run."""
        return f"sessions/{self.owner}/{self.repo_name}/{self.pr}/{run_id}/session.tar.gz"

    def session_meta_key(self, run_id: str) -> str:
        """Metadata key stored alongside immutable session snapshot."""
        return f"sessions/{self.owner}/{self.repo_name}/{self.pr}/{run_id}/meta.json"

    def trace_key(self, run_id: str) -> str:
        """Immutable trace bundle key for this run."""
        return f"traces/{self.owner}/{self.repo_name}/{self.pr}/{run_id}.tar.gz"

    def _get_object(self, key: str) -> bytes | None:
        """Read object from bucket; returns None on 404/NoSuchKey, raises StorageError on errors."""
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            body = resp["Body"]
            return body.read() if hasattr(body, "read") else body
        except Exception as e:
            if is_not_found(e):
                return None
            raise StorageError(f"B2 storage read error: {type(e).__name__}") from None

    def get_latest_metadata(self) -> dict | None:
        """Fetch the latest metadata dictionary for this PR, or None if absent."""
        data = self._get_object(self.latest_key)
        if data is None:
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
            return payload.get("meta") or payload
        except Exception as e:
            raise StorageError(f"Corrupt B2 latest pointer payload: {type(e).__name__}") from None

    def has_saved_session(self) -> bool:
        """Return True if a saved session exists for this PR."""
        return self.get_latest_metadata() is not None

    def restore_session(self, dest_dir: Path) -> dict | None:
        """Restore this PR's session into dest_dir from the latest pointer.

        Returns latest metadata dict or None if absent.
        Raises StorageError on storage/network/corrupt/safety issues.
        """
        data = self._get_object(self.latest_key)
        if data is None:
            return None

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as e:
            raise StorageError(f"Corrupt B2 latest pointer payload: {type(e).__name__}") from None

        session_key = payload.get("session_key")
        if not session_key:
            raise StorageError("Latest pointer missing session_key")

        session_tar_bytes = self._get_object(session_key)
        if session_tar_bytes is None:
            raise StorageError("Referenced session archive missing from storage")

        safe_extract_tar(session_tar_bytes, dest_dir)
        return payload.get("meta") or payload

    def save_state(
        self,
        run_id: str,
        meta: dict,
        result: dict | None,
        session_dir: Path,
        child_dirs: list[Path],
        config_commit: str = "",
        session_lines_before: int = 0,
    ) -> dict:
        """Upload immutable run bundle and session snapshot, then commit mutable latest pointer.

        Ordering:
          1. Upload immutable trace bundle
          2. Upload immutable session snapshot and meta
          3. Upload mutable latest pointer (only after 1 & 2 succeed)
        Returns committed latest pointer dictionary.
        """
        trace_key = self.trace_key(run_id)
        session_key = self.session_key(run_id)
        session_meta_key = self.session_meta_key(run_id)
        latest_key = self.latest_key

        manifest = {
            "schema_version": 1,
            "source": "prime-reviewer",
            "trace_id": f"{self.owner}/{self.repo_name}#{self.pr}/{run_id}",
            "repo": self.repo,
            "repo_name": self.repo_name,
            "owner": self.owner,
            "pr": int(self.pr),
            "run_id": run_id,
            "config_commit": config_commit,
            "session_lines_before": session_lines_before,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": ["manifest.json", "meta.json"],
        }
        if result is not None:
            manifest["files"].append("result.json")
        if session_dir.is_dir() and any(session_dir.glob("*.jsonl")):
            manifest["files"].append("session.jsonl")
        for cdir in child_dirs:
            if cdir.is_dir():
                for sf in sorted(cdir.rglob("*.jsonl")):
                    rel = sf.relative_to(cdir)
                    manifest["files"].append(f"subagents/{rel}")

        # Ensure meta includes boundary / provenance fields
        meta_to_store = dict(meta)
        if config_commit:
            meta_to_store["config_commit"] = config_commit
        if session_lines_before:
            meta_to_store["session_lines_before"] = session_lines_before
        meta_to_store["trace_id"] = manifest["trace_id"]
        meta_to_store["schema_version"] = 1

        trace_bundle_bytes = create_trace_bundle(
            meta=meta_to_store,
            result=result,
            session_dir=session_dir,
            child_dirs=child_dirs,
            manifest=manifest,
        )
        session_tar_bytes = create_session_tar(session_dir)
        session_meta_bytes = json.dumps(meta_to_store, indent=2).encode("utf-8")

        latest_payload = {
            "schema_version": 1,
            "source": "prime-reviewer",
            "trace_id": manifest["trace_id"],
            "run_id": run_id,
            "repo": self.repo,
            "repo_name": self.repo_name,
            "owner": self.owner,
            "pr": int(self.pr),
            "config_commit": config_commit,
            "session_lines_before": session_lines_before,
            "session_key": session_key,
            "session_meta_key": session_meta_key,
            "trace_key": trace_key,
            "head_after": meta_to_store.get("head_after"),
            "gate_blockers": meta_to_store.get("gate_blockers", []),
            "meta": meta_to_store,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        latest_bytes = json.dumps(latest_payload, indent=2).encode("utf-8")

        try:
            # 1. Upload immutable trace bundle
            self._client.put_object(
                Bucket=self.bucket,
                Key=trace_key,
                Body=trace_bundle_bytes,
                ContentType="application/gzip",
            )

            # 2. Upload immutable session snapshot and meta alongside it
            self._client.put_object(
                Bucket=self.bucket,
                Key=session_key,
                Body=session_tar_bytes,
                ContentType="application/gzip",
            )
            self._client.put_object(
                Bucket=self.bucket,
                Key=session_meta_key,
                Body=session_meta_bytes,
                ContentType="application/json",
            )

            # 3. Commit mutable latest pointer (only after 1 & 2 succeed)
            self._client.put_object(
                Bucket=self.bucket,
                Key=latest_key,
                Body=latest_bytes,
                ContentType="application/json",
            )
        except Exception as e:
            raise StorageError(f"B2 storage write error: {type(e).__name__}") from None

        return latest_payload

    def list_traces(self, prefix: str | None = None) -> list[str]:
        """List trace keys under the given prefix or this repo's trace prefix."""
        search_prefix = prefix if prefix is not None else f"traces/{self.owner}/{self.repo_name}/"
        try:
            resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=search_prefix)
            contents = resp.get("Contents", [])
            return [obj["Key"] for obj in contents if "Key" in obj]
        except Exception as e:
            raise StorageError(f"B2 storage list error: {type(e).__name__}") from None
