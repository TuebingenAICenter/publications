"""Roundtrip test for the Zotero ⇄ store-entries bridge (``zotero_bridge``).

Manual developer harness, **not** wired into CI (consistent with the rest of
``tools/tests/``). Builds a tiny in-memory Zotero library — two items, two
collections, one ``mentionsAICenter`` tag — and asserts the two institute-specific
transforms in both directions:

* collection membership ⇄ ``custom.groups`` (names, not URIs; absent from the
  ``zotero`` half);
* a ``mentionsAICenter`` tag ⇄ ``custom.mentions_ai_center == True``.

Also checks the emitted sidecars validate the real schema, and that re-importing
reconstructs the collections and the tag.
"""

from __future__ import annotations

from conftest import REPO_ROOT
from publication_store import entry, zotero_bridge, sidecar
from zotero_rdf import JournalArticle, Tag, ZoteroCollection


def _library():
    """Two items; item A is in both groups and mentions the AI Center, B in neither."""
    a = JournalArticle(
        title="Predictive Coding",
        publicationTitle="Nature",
        date="2025",
        creators=[],
        tags=[Tag(tag="ml"), Tag(tag=zotero_bridge.MENTIONS_AI_CENTER_TAG)],
    )
    b = JournalArticle(
        title="A Quiet Result",
        publicationTitle="Science",
        date="2024",
        creators=[],
        tags=[Tag(tag="ml")],
    )
    bethge = ZoteroCollection(name="bethge")
    schoelkopf = ZoteroCollection(name="schoelkopf")
    bethge.add(a)
    schoelkopf.add(a)
    return [a, b], [bethge, schoelkopf]


def test_export_lifts_groups_and_tag_into_custom():
    items, collections = _library()
    entries = zotero_bridge.to_store_entries(items, collections)
    by_key = {e.citekey: e for e in entries}
    assert len(by_key) == 2

    # Every entry has the store's canonical both-keys shape.
    for e in by_key.values():
        assert set(e.sidecar) == {"zotero", "custom"}

    # Item A: both groups (sorted, by name) + mentions flag; tag is gone from the bib.
    (a,) = [e for e in by_key.values() if e.sidecar["custom"]]
    assert a.sidecar["custom"] == {
        "groups": ["bethge", "schoelkopf"],
        "mentions_ai_center": True,
    }
    assert zotero_bridge.MENTIONS_AI_CENTER_TAG not in a.bib
    # The zotero half never carries collections (groups own membership now).
    assert "collections" not in a.sidecar["zotero"]

    # Item B: an empty custom half.
    (b,) = [e for e in by_key.values() if not e.sidecar["custom"]]
    assert b.sidecar["custom"] == {}


def test_emitted_sidecars_validate_the_schema():
    items, collections = _library()
    for e in zotero_bridge.to_store_entries(items, collections):
        assert sidecar.validation_error(e.sidecar, "test", REPO_ROOT) is None


def test_input_items_are_not_mutated():
    items, collections = _library()
    zotero_bridge.to_store_entries(items, collections)
    # The mentionsAICenter tag is still on the caller's original item.
    assert any(t.tag == zotero_bridge.MENTIONS_AI_CENTER_TAG for t in items[0].tags)


def test_roundtrip_rebuilds_collections_and_tag():
    items, collections = _library()
    entries = zotero_bridge.to_store_entries(items, collections)

    back_items, back_collections = zotero_bridge.from_store_entries(entries)

    titles = {item.title for item in back_items}
    assert titles == {"Predictive Coding", "A Quiet Result"}

    # The two collections come back, each holding the right member.
    names = {c.name for c in back_collections}
    assert names == {"bethge", "schoelkopf"}
    a = next(i for i in back_items if i.title == "Predictive Coding")
    b = next(i for i in back_items if i.title == "A Quiet Result")
    for c in back_collections:
        assert a in c        # A is in both groups
        assert b not in c    # B is in neither

    # The mentions tag is re-added to A, and only A.
    assert any(t.tag == zotero_bridge.MENTIONS_AI_CENTER_TAG for t in a.tags)
    assert not any(t.tag == zotero_bridge.MENTIONS_AI_CENTER_TAG for t in b.tags)


BASE_URL = "https://github.com/TuebingenAICenter/publications/blob/main"


def test_sidecar_base_url_attaches_blob_link_per_entry():
    items, collections = _library()
    entries = zotero_bridge.to_store_entries(items, collections, sidecar_base_url=BASE_URL)

    for e in entries:
        atts = e.sidecar["zotero"]["attachments"]
        link = next(a for a in atts if a["title"] == zotero_bridge.SIDECAR_ATTACHMENT_TITLE)
        assert link["linkMode"] == "linked_url"
        # blob URL points at the entry's derived sidecar path (year from the bib).
        year = "2025" if "predictive" in e.citekey else "2024"
        assert link["url"] == f"{BASE_URL}/meta/{year}/{e.citekey}.json"

    # A trailing slash on the base is tolerated (no doubled slash).
    (e,) = zotero_bridge.to_store_entries(
        [items[1]], collections, sidecar_base_url=BASE_URL + "/")
    assert "//meta" not in e.sidecar["zotero"]["attachments"][0]["url"]


def test_sidecar_link_validates_schema_and_is_stripped_on_import():
    items, collections = _library()
    entries = zotero_bridge.to_store_entries(items, collections, sidecar_base_url=BASE_URL)

    for e in entries:
        assert sidecar.validation_error(e.sidecar, "test", REPO_ROOT) is None

    # Round-trip drops the derived link (it is a pointer, never store data).
    back_items, _ = zotero_bridge.from_store_entries(entries)
    for item in back_items:
        assert all(
            a.title != zotero_bridge.SIDECAR_ATTACHMENT_TITLE for a in item.attachments
        )


def test_no_base_url_means_no_attachment():
    items, collections = _library()
    for e in zotero_bridge.to_store_entries(items, collections):
        assert "attachments" not in e.sidecar["zotero"]


def test_multi_paragraph_abstract_does_not_break_the_split():
    # Regression: a blank line *inside* a field value (a multi-paragraph abstract,
    # emitted verbatim into the .bib) must not be mistaken for an entry boundary.
    abstract = "First paragraph.\n\n2. A numbered point.\n\n3. Another."
    items = [
        JournalArticle(title="A", date="2025", creators=[], abstractNote=abstract),
        JournalArticle(title="B", date="2024", creators=[]),
    ]
    entries = zotero_bridge.to_store_entries(items)
    assert [e.citekey for e in entries] == [
        "noauthor_notitle_2025",
        "noauthor_b_2024",
    ]
    # split_entries itself yields exactly the two entries, not five blank-line shards.
    a_text = next(e.bib for e in entries if "2025" in e.citekey)
    assert len(entry.split_entries(a_text)) == 1


def test_roundtrip_tolerates_colliding_citekeys():
    # Regression: two distinct publications can carry the same explicit citationKey
    # (e.g. both came from a prior import). to_bibtex honors explicit keys verbatim
    # (no -N disambiguation), so the exported entries collide. Per-file uniqueness is
    # the diff job's placement concern, not the bridge's — from_store_entries must
    # still round-trip them (a single combined parse would reject the duplicate keys).
    items = [
        JournalArticle(title="First", date="2023", creators=[], citationKey="dup2023"),
        JournalArticle(title="Second", date="2023", creators=[], citationKey="dup2023"),
    ]
    entries = zotero_bridge.to_store_entries(items)
    assert [e.citekey for e in entries] == ["dup2023", "dup2023"]  # they collide

    back_items, _ = zotero_bridge.from_store_entries(entries)
    assert {i.title for i in back_items} == {"First", "Second"}
