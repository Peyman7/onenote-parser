"""Incremental state and change classification."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from onenote_parser.config import StateConfig
from onenote_parser.errors import StateError
from onenote_parser.models import ChangeStatus, PageMetadata, PageRef, ParsedPage
from onenote_parser.state import STATE_VERSION, StateStore

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)


def make_page(page_id="pg-1", *, content_hash="h1", title="Title", path=("NB", "Sec"),
              modified=T0) -> ParsedPage:
    return ParsedPage(
        id=page_id,
        title=title,
        path=list(path),
        content_hash=content_hash,
        metadata=PageMetadata(modified_at=modified),
    )


def make_ref(page_id="pg-1", *, title="Title", path=("NB", "Sec"), modified=T0) -> PageRef:
    return PageRef(id=page_id, title=title, path=list(path), modified_at=modified)


def test_first_sight_of_a_page_is_new(tmp_path):
    store = StateStore(tmp_path / "state.json").load()
    assert store.record_parsed(make_page()).status is ChangeStatus.NEW


def test_same_hash_is_unchanged_and_different_hash_is_modified(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    assert store.record_parsed(make_page()).status is ChangeStatus.UNCHANGED
    change = store.record_parsed(make_page(content_hash="h2"))
    assert change.status is ChangeStatus.MODIFIED
    assert change.previous_hash == "h1" and change.current_hash == "h2"
    assert change.reason == "content_changed"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"title": "Renamed"}, "title_changed"),
        ({"path": ("NB", "Other")}, "moved"),
    ],
)
def test_rename_and_move_are_reported_with_a_reason(tmp_path, kwargs, reason):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    change = store.record_parsed(make_page(content_hash="h2", **kwargs))
    assert change.status is ChangeStatus.MODIFIED and change.reason == reason


def test_should_fetch_skips_pages_whose_timestamp_did_not_move(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    fetch, presumed = store.should_fetch(make_ref())
    assert fetch is False and presumed is ChangeStatus.UNCHANGED


def test_should_fetch_returns_true_when_the_page_was_edited(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    assert store.should_fetch(make_ref(modified=T1))[0] is True


def test_should_fetch_returns_true_for_a_rename_even_with_the_same_timestamp(tmp_path):
    """A rename does not move lastModifiedDateTime but must still be re-exported."""
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    assert store.should_fetch(make_ref(title="Renamed"))[0] is True
    assert store.should_fetch(make_ref(path=("NB", "Moved")))[0] is True


def test_full_mode_always_fetches(tmp_path):
    store = StateStore(tmp_path / "state.json", StateConfig(mode="full"))
    store.record_parsed(make_page())
    assert store.should_fetch(make_ref())[0] is True


def test_pages_absent_from_the_crawl_are_reported_deleted(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page("pg-1"))
    store.record_parsed(make_page("pg-2"))
    store.save()

    reloaded = StateStore(tmp_path / "state.json").load()
    reloaded.record_parsed(make_page("pg-1"))
    deletions = reloaded.detect_deletions()
    assert [c.page_id for c in deletions] == ["pg-2"]
    assert deletions[0].status is ChangeStatus.DELETED
    assert "pg-2" not in reloaded.known_page_ids()


def test_inaccessible_pages_are_not_downgraded_to_deleted(tmp_path):
    """Losing permission must never be reported as a deletion."""
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page("pg-1"))
    store.record_inaccessible(make_ref("pg-1"), "inaccessible")
    store.save()

    reloaded = StateStore(tmp_path / "state.json").load()
    assert reloaded.detect_deletions() == []
    assert reloaded.get("pg-1").status == ChangeStatus.INACCESSIBLE.value


def test_inaccessible_new_page_is_retained_when_configured(tmp_path):
    store = StateStore(tmp_path / "state.json", StateConfig(retain_inaccessible=True))
    change = store.record_inaccessible(make_ref("pg-9"), "inaccessible")
    assert change.status is ChangeStatus.INACCESSIBLE
    assert store.get("pg-9") is not None

    forgetful = StateStore(tmp_path / "s2.json", StateConfig(retain_inaccessible=False))
    forgetful.record_inaccessible(make_ref("pg-9"), "inaccessible")
    assert forgetful.get("pg-9") is None


def test_deletion_detection_can_be_disabled(tmp_path):
    store = StateStore(tmp_path / "state.json", StateConfig(detect_deletions=False))
    store.record_parsed(make_page("pg-1"))
    store.save()
    reloaded = StateStore(tmp_path / "state.json", StateConfig(detect_deletions=False)).load()
    assert reloaded.detect_deletions() == []


def test_save_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)
    store.record_parsed(make_page())
    store.save(run_id="run-1")

    payload = json.loads(path.read_text())
    assert payload["version"] == STATE_VERSION and payload["run_id"] == "run-1"
    assert not list(path.parent.glob("*.tmp"))

    reloaded = StateStore(path).load()
    assert reloaded.previous_run_id == "run-1"
    assert reloaded.get("pg-1").content_hash == "h1"


def test_corrupt_state_file_raises_an_actionable_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(StateError) as excinfo:
        StateStore(path).load()
    assert "Delete it to force a full run" in str(excinfo.value)


def test_state_written_by_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": STATE_VERSION + 1, "pages": {}}))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_missing_state_file_is_not_an_error(tmp_path):
    store = StateStore(tmp_path / "absent.json").load()
    assert len(store) == 0


def test_disabled_state_store_never_writes(tmp_path):
    store = StateStore(None)
    store.record_parsed(make_page())
    store.save()  # no path: silently does nothing
    assert list(tmp_path.iterdir()) == []


def test_record_skipped_marks_the_page_seen_and_unchanged(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.record_parsed(make_page())
    change = store.record_skipped(make_ref())
    assert change.status is ChangeStatus.UNCHANGED
    assert change.reason == "not_modified_since_last_run"
    assert store.detect_deletions() == []
