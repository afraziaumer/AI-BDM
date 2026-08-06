"""Offline tests for two distinct, real Windows-path bugs fixed this
session: final_reasoning.py's crawl_index.csv basename lookup, and
storage_sync.py's R2 object-key building. Both are "backslash vs forward
slash" bugs, but in opposite directions -- the first needed OS-native
separators handled correctly when reading a LOCAL path off Windows, the
second needed them normalized to "/" when building a REMOTE (S3/R2) key
regardless of host OS.

No network call -- storage_sync's R2 client is replaced with an in-memory
fake that never touches a real socket.

Run: pytest -q tests/unit
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- #
# final_reasoning._txt_path_by_filename() -- basename extraction from an
# OS-native crawl_index.csv path.
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def read_index(self):
        return self._rows


def test_windows_backslash_path_resolves_to_correct_basename(monkeypatch):
    """The exact real bug: on Windows, crawl_index.csv's txt_path is
    backslash-separated. A forward-slash-only split used to return the
    WHOLE path instead of just the filename, so this lookup silently
    matched nothing and every answer_query() call failed with
    "no_page_text_available" -- on every business, every run, on Windows,
    with no visible error."""
    import final_reasoning

    fake_rows = [
        {"domain": "example.org", "txt_path": "storage\\example.org\\home.txt"},
        {"domain": "example.org", "txt_path": "storage\\example.org\\contact.txt"},
    ]
    monkeypatch.setattr(final_reasoning, "get_store", lambda: _FakeStore(fake_rows))

    lookup = final_reasoning._txt_path_by_filename("example.org")
    assert lookup["home.txt"] == "storage\\example.org\\home.txt"
    assert lookup["contact.txt"] == "storage\\example.org\\contact.txt"


def test_forward_slash_path_also_resolves_correctly(monkeypatch):
    """A path stored with forward slashes (e.g. if this ever runs on Linux)
    must resolve the same way -- the fix splits on either separator, it
    doesn't just swap which one it expects."""
    import final_reasoning

    fake_rows = [{"domain": "example.org", "txt_path": "storage/example.org/home.txt"}]
    monkeypatch.setattr(final_reasoning, "get_store", lambda: _FakeStore(fake_rows))

    lookup = final_reasoning._txt_path_by_filename("example.org")
    assert lookup["home.txt"] == "storage/example.org/home.txt"


def test_domain_filter_excludes_other_businesses(monkeypatch):
    import final_reasoning

    fake_rows = [
        {"domain": "example.org", "txt_path": "storage\\example.org\\home.txt"},
        {"domain": "other.org", "txt_path": "storage\\other.org\\home.txt"},
    ]
    monkeypatch.setattr(final_reasoning, "get_store", lambda: _FakeStore(fake_rows))

    lookup = final_reasoning._txt_path_by_filename("example.org")
    assert list(lookup.values()) == ["storage\\example.org\\home.txt"]


# --------------------------------------------------------------------------- #
# storage_sync.R2StorageProvider.sync_folder() -- object-key hierarchy
# --------------------------------------------------------------------------- #
class _FakeS3Client:
    """In-memory stand-in for boto3's S3 client -- records every key it
    would have uploaded, never touches a real socket."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(f"no such key: {Key}")  # provider treats any exception as "no manifest yet"
        class _Body:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body=b""):
        self.objects[Key] = Body


def _make_provider():
    from storage_sync import R2StorageProvider
    provider = R2StorageProvider(
        account_id="fake", access_key_id="fake", secret_access_key="fake",
        bucket="fake-bucket", endpoint_url="https://fake.example",
    )
    provider.client = _FakeS3Client()
    return provider


def test_sync_folder_preserves_hierarchy_with_posix_keys(tmp_path):
    """The exact real bug: Path.relative_to() returns OS-native separators.
    On Windows, a key built from that directly is a single flat file
    literally named "reviews\\file.json" instead of nesting under a
    "reviews/" folder -- confirmed live against a real R2 bucket before and
    after this fix. Object keys must always use "/" regardless of host OS."""
    domain_dir = tmp_path / "example.org"
    (domain_dir / "reviews").mkdir(parents=True)
    (domain_dir / "home.txt").write_text("home page text", encoding="utf-8")
    (domain_dir / "reviews" / "google_reviews.json").write_text("{}", encoding="utf-8")

    provider = _make_provider()
    result = provider.sync_folder("example.org", domain_dir)

    assert result.ok
    assert sorted(result.uploaded) == ["home.txt", "reviews/google_reviews.json"]
    uploaded_keys = set(provider.client.objects.keys())
    assert "example.org/home.txt" in uploaded_keys
    assert "example.org/reviews/google_reviews.json" in uploaded_keys
    # The literal bug this test guards against: a backslash must never
    # appear in an uploaded object key.
    assert not any("\\" in key for key in uploaded_keys)


def test_sync_folder_skips_unchanged_files_on_second_run(tmp_path):
    domain_dir = tmp_path / "example.org"
    domain_dir.mkdir()
    (domain_dir / "home.txt").write_text("home page text", encoding="utf-8")

    provider = _make_provider()
    first = provider.sync_folder("example.org", domain_dir)
    assert first.uploaded == ["home.txt"]
    assert first.skipped == []

    second = provider.sync_folder("example.org", domain_dir)
    assert second.uploaded == []
    assert second.skipped == ["home.txt"]
