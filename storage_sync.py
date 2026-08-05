"""Post-commit remote-storage sync layer.

Independent of the crawler and of storage.py's PageStore: the crawler
ALWAYS writes locally first (LocalPageStore — see storage.py, which has
zero knowledge of this module or of Cloudflare). Once a domain's folder is
fully committed to local storage, exactly ONE call here (sync_folder)
uploads the finished folder to remote storage AS A SINGLE LOGICAL UNIT,
preserving its exact directory structure — never per-file writes scattered
across the crawl itself.

    StorageProvider (ABC)
            |
    R2StorageProvider   (Cloudflare R2, boto3 S3-compatible client)
    ... future: another cloud, a versioned backend, etc. — one new
        subclass + one line in PROVIDER_REGISTRY, nothing else changes.

Incremental sync: each domain gets one small companion manifest object at
`_sync_manifests/<domain>.json` (deliberately OUTSIDE the `<domain>/`
prefix, so the bucket's `<domain>/` folder stays a byte-for-byte mirror of
`storage/<domain>/` — no sync bookkeeping mixed into the content itself).
The manifest maps {relative_path: sha256}; a re-sync hashes each local
file, skips any whose hash is already in the manifest (already up to
date), uploads the rest, and rewrites the manifest. Sync only ever adds/
updates objects — it never deletes a remote file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ai_bdm.storage_sync")

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2


@dataclass
class SyncResult:
    """What happened when one domain's folder was synced."""

    domain: str
    uploaded: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (relative_path, error)

    @property
    def ok(self) -> bool:
        return not self.failed


class StorageProvider(ABC):
    """One remote-storage backend. The ONLY entry point the rest of AI-BDM
    calls: sync_folder(domain, local_dir). Everything about incremental
    diffing, retries, and logging is this provider's own concern — a
    caller never inspects file-by-file state itself, and never knows
    whether the active provider is R2 or something else."""

    name: str = "base"

    @abstractmethod
    def sync_folder(self, domain: str, local_dir: Path) -> SyncResult:
        """Upload local_dir's entire contents (recursively) to remote
        storage under a `<domain>/` prefix, preserving relative paths
        exactly as they exist locally. Incremental: a file whose content
        hash already matches what's recorded as synced is skipped, not
        re-uploaded. Never deletes a remote object. Best-effort per file —
        one file's failure (after retries) doesn't abort the rest of the
        folder."""


class R2StorageProvider(StorageProvider):
    """Cloudflare R2 (S3-compatible API via boto3)."""

    name = "r2"

    def __init__(
        self, *, account_id: str, access_key_id: str, secret_access_key: str,
        bucket: str, endpoint_url: str = "",
    ) -> None:
        import boto3  # local import: only paid for when this provider is actually built

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def _manifest_key(self, domain: str) -> str:
        return f"_sync_manifests/{domain}.json"

    def _get_manifest(self, domain: str) -> Dict[str, str]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._manifest_key(domain))
        except Exception:  # noqa: BLE001 - no manifest yet == first sync for this domain
            return {}
        try:
            return json.loads(obj["Body"].read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}  # a corrupt manifest just forces a full re-sync, never a crash

    def _put_manifest(self, domain: str, manifest: Dict[str, str]) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._manifest_key(domain),
            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        )

    def _upload_with_retry(self, key: str, data: bytes, relative_path: str) -> Optional[str]:
        """None on success; an error string after every retry is exhausted."""
        last_error = ""
        for attempt in range(_MAX_RETRIES):
            try:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
                return None
            except Exception as exc:  # noqa: BLE001 - retried below; final failure returned to caller
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF_S * (2 ** attempt)
                    logger.warning(
                        "[R2Sync] upload failed for %s (attempt %d/%d): %s — retrying in %ss",
                        relative_path, attempt + 1, _MAX_RETRIES, last_error, wait,
                    )
                    time.sleep(wait)
        return last_error

    def sync_folder(self, domain: str, local_dir: Path) -> SyncResult:
        result = SyncResult(domain=domain)
        if not local_dir.exists():
            logger.warning("[R2Sync] local_dir does not exist for %s: %s", domain, local_dir)
            return result

        files = sorted(p for p in local_dir.rglob("*") if p.is_file())
        logger.info("[R2Sync] Upload started — folder=%s (%d local file(s))", domain, len(files))

        manifest = self._get_manifest(domain)
        new_manifest = dict(manifest)

        for path in files:
            relative_path = str(path.relative_to(local_dir))
            data = path.read_bytes()
            file_hash = hashlib.sha256(data).hexdigest()

            if manifest.get(relative_path) == file_hash:
                result.skipped.append(relative_path)
                logger.debug("[R2Sync] skip (up to date): %s/%s", domain, relative_path)
                continue

            key = f"{domain}/{relative_path}"
            error = self._upload_with_retry(key, data, relative_path)
            if error:
                result.failed.append((relative_path, error))
                logger.error("[R2Sync] upload FAILED for %s/%s after %d attempt(s): %s",
                             domain, relative_path, _MAX_RETRIES, error)
                continue

            new_manifest[relative_path] = file_hash
            result.uploaded.append(relative_path)
            logger.debug("[R2Sync] uploaded: %s/%s", domain, relative_path)

        if result.uploaded:
            self._put_manifest(domain, new_manifest)

        logger.info(
            "[R2Sync] Upload completed — folder=%s uploaded=%d skipped=%d failed=%d",
            domain, len(result.uploaded), len(result.skipped), len(result.failed),
        )
        return result


# === Registry + config =======================================================
# Add a future backend by writing one StorageProvider subclass and
# registering it here — nothing else in AI-BDM changes.
PROVIDER_REGISTRY: Dict[str, type] = {
    "r2": R2StorageProvider,
}


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# Which registered provider is active, and whether syncing runs at all.
# Sync is considered "configured" purely by whether its credentials are
# present — the same convention this codebase already uses for every other
# optional integration (Scrape.do/ZenRows/GROQ/...): no key -> feature is a
# silent no-op, never an error, never a required setup step.
STORAGE_PROVIDER = os.getenv("STORAGE_PROVIDER", "r2").strip().lower()
R2_SYNC_ENABLED = _env_bool("R2_SYNC_ENABLED", True)

_provider: Optional[StorageProvider] = None
_provider_checked = False


def get_provider() -> Optional[StorageProvider]:
    """The active StorageProvider, or None if remote sync isn't configured
    (missing credentials, unrecognized STORAGE_PROVIDER, or explicitly
    disabled via R2_SYNC_ENABLED=false). Callers treat None as "sync is a
    no-op" — never an error. Built once and reused; the reason sync is
    unavailable is logged ONCE, not on every crawl."""
    global _provider, _provider_checked
    if _provider_checked:
        return _provider
    _provider_checked = True

    if not R2_SYNC_ENABLED:
        logger.info("[R2Sync] disabled via R2_SYNC_ENABLED=false — folders stay local-only.")
        return None

    provider_cls = PROVIDER_REGISTRY.get(STORAGE_PROVIDER)
    if provider_cls is None:
        logger.warning("[R2Sync] STORAGE_PROVIDER=%r is not registered (known: %s) — "
                       "folders stay local-only.", STORAGE_PROVIDER, sorted(PROVIDER_REGISTRY))
        return None

    if provider_cls is R2StorageProvider:
        account_id = os.getenv("R2_ACCOUNT_ID", "")
        access_key_id = os.getenv("R2_ACCESS_KEY_ID", "")
        secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY", "")
        bucket = os.getenv("R2_BUCKET_NAME", "")
        endpoint_url = os.getenv("R2_ENDPOINT", "")
        if not (access_key_id and secret_access_key and bucket and (account_id or endpoint_url)):
            logger.info("[R2Sync] R2 credentials not fully configured — folders stay local-only.")
            return None
        _provider = R2StorageProvider(
            account_id=account_id, access_key_id=access_key_id,
            secret_access_key=secret_access_key, bucket=bucket, endpoint_url=endpoint_url,
        )
        logger.info("[R2Sync] R2StorageProvider active (bucket=%s).", bucket)

    return _provider
