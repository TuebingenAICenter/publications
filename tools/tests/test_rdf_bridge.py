"""Roundtrip test for the Zotero-RDF ⇄ store-pairs bridge (``rdf_bridge``).

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
from publication_store import entry, rdf_bridge, sidecar
from zotero_rdf import JournalArticle, Tag, ZoteroCollection


def _library():
    """Two items; item A is in both groups and mentions the AI Center, B in neither."""
    a = JournalArticle(
        title="Predictive Coding",
        publicationTitle="Nature",
        date="2025",
        creators=[],
        tags=[Tag(tag="ml"), Tag(tag=rdf_bridge.MENTIONS_AI_CENTER_TAG)],
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
    pairs = rdf_bridge.items_to_pairs(items, collections)
    by_key = {citekey: (text, sc) for citekey, text, sc in pairs}
    assert len(by_key) == 2

    # Every pair has the store's canonical both-keys shape.
    for _, sc in by_key.values():
        assert set(sc) == {"zotero", "custom"}

    # Item A: both groups (sorted, by name) + mentions flag; tag is gone from the bib.
    (a_key, a_sc), = [
        (k, sc) for k, (_, sc) in by_key.items() if sc["custom"]
    ]
    assert a_sc["custom"] == {
        "groups": ["bethge", "schoelkopf"],
        "mentions_ai_center": True,
    }
    a_text = by_key[a_key][0]
    assert rdf_bridge.MENTIONS_AI_CENTER_TAG not in a_text
    # The zotero half never carries collections (groups own membership now).
    assert "collections" not in a_sc["zotero"]

    # Item B: an empty custom half.
    (b_sc,) = [sc for _, (_, sc) in by_key.items() if not sc["custom"]]
    assert b_sc["custom"] == {}


def test_emitted_sidecars_validate_the_schema():
    items, collections = _library()
    for _, _, sc in rdf_bridge.items_to_pairs(items, collections):
        assert sidecar.validation_error(sc, "test", REPO_ROOT) is None


def test_input_items_are_not_mutated():
    items, collections = _library()
    rdf_bridge.items_to_pairs(items, collections)
    # The mentionsAICenter tag is still on the caller's original item.
    assert any(t.tag == rdf_bridge.MENTIONS_AI_CENTER_TAG for t in items[0].tags)


def test_roundtrip_rebuilds_collections_and_tag():
    items, collections = _library()
    pairs = rdf_bridge.items_to_pairs(items, collections)

    back_items, back_collections = rdf_bridge.pairs_to_items(
        [(text, sc) for _, text, sc in pairs]
    )

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
    assert any(t.tag == rdf_bridge.MENTIONS_AI_CENTER_TAG for t in a.tags)
    assert not any(t.tag == rdf_bridge.MENTIONS_AI_CENTER_TAG for t in b.tags)


BASE_URL = "https://github.com/TuebingenAICenter/publications/blob/main"


def test_sidecar_base_url_attaches_blob_link_per_entry():
    items, collections = _library()
    pairs = rdf_bridge.items_to_pairs(items, collections, sidecar_base_url=BASE_URL)

    for citekey, _, sc in pairs:
        atts = sc["zotero"]["attachments"]
        link = next(a for a in atts if a["title"] == rdf_bridge.SIDECAR_ATTACHMENT_TITLE)
        assert link["linkMode"] == "linked_url"
        # blob URL points at the entry's derived sidecar path (year from the bib).
        year = "2025" if "predictive" in citekey else "2024"
        assert link["url"] == f"{BASE_URL}/meta/{year}/{citekey}.json"

    # A trailing slash on the base is tolerated (no doubled slash).
    (_, _, sc), = [t for t in rdf_bridge.items_to_pairs(
        [items[1]], collections, sidecar_base_url=BASE_URL + "/")]
    assert "//meta" not in sc["zotero"]["attachments"][0]["url"]


def test_sidecar_link_validates_schema_and_is_stripped_on_import():
    items, collections = _library()
    pairs = rdf_bridge.items_to_pairs(items, collections, sidecar_base_url=BASE_URL)

    for _, _, sc in pairs:
        assert sidecar.validation_error(sc, "test", REPO_ROOT) is None

    # Round-trip drops the derived link (it is a pointer, never store data).
    back_items, _ = rdf_bridge.pairs_to_items([(text, sc) for _, text, sc in pairs])
    for item in back_items:
        assert all(
            a.title != rdf_bridge.SIDECAR_ATTACHMENT_TITLE for a in item.attachments
        )


def test_no_base_url_means_no_attachment():
    items, collections = _library()
    for _, _, sc in rdf_bridge.items_to_pairs(items, collections):
        assert "attachments" not in sc["zotero"]


def test_multi_paragraph_abstract_does_not_break_the_split():
    # Regression: a blank line *inside* a field value (a multi-paragraph abstract,
    # emitted verbatim into the .bib) must not be mistaken for an entry boundary.
    abstract = "First paragraph.\n\n2. A numbered point.\n\n3. Another."
    items = [
        JournalArticle(title="A", date="2025", creators=[], abstractNote=abstract),
        JournalArticle(title="B", date="2024", creators=[]),
    ]
    pairs = rdf_bridge.items_to_pairs(items)
    assert [ck for ck, _, _ in pairs] == [
        "noauthor_notitle_2025",
        "noauthor_b_2024",
    ]
    # split_entries itself yields exactly the two entries, not five blank-line shards.
    a_text = next(text for ck, text, _ in pairs if "2025" in ck)
    assert len(entry.split_entries(a_text)) == 1
