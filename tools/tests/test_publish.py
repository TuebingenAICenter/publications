"""Tests for the per-item PR publisher loop (``publication_store.publish``).

Drives :func:`publish_entries` against an in-memory :class:`FakePublisher` — no
network, no PyGithub — to pin the behaviours that make the monthly cron safe to
re-run:

- **idempotency** — skip a citekey already in the store, and skip one whose branch
  already exists (an open/abandoned PR);
- **one PR per item, even multi-group** — a single PR carries all of an item's group
  associations in its body and its ``custom.groups``;
- **``--limit``** caps PRs *created*, not items seen (skips don't consume the budget);
- **``--dry-run``** computes everything but opens nothing.

Manual developer harness, **not** wired into CI.
"""

from __future__ import annotations

from publication_store import publish
from publication_store.zotero_bridge import to_store_entries
from zotero_rdf import JournalArticle, Tag, ZoteroCollection


class FakePublisher:
    """In-memory :class:`publish.StorePublisher`: records PRs, simulates skips."""

    def __init__(self, existing_branches=(), fail_on=()):
        self._stored: dict[str, tuple[str, str]] = {}  # citekey -> (bib_text, meta_text)
        self._branches = set(existing_branches)
        self._fail_on = set(fail_on)
        self.created: list[tuple[str, object, str, str]] = []  # (branch, pair, title, body)
        self.renamed_from: list[tuple[str, str]] = []  # (branch, old_citekey)

    def seed(self, store_entry, *, changed=False):
        """Register ``store_entry`` as already in the store.

        ``changed=False`` stores the byte-identical emitted pair (so a re-publish is a
        no-op skip); ``changed=True`` stores a differing pair (so it becomes an update).
        """
        pair = publish.emit.emit_pair(store_entry)
        self._stored[store_entry.citekey] = (
            ("STALE BIB", "STALE META") if changed else (pair.bib_text, pair.meta_text)
        )
        return self

    def existing_citekeys(self) -> set[str]:
        return set(self._stored)

    def stored_pair(self, citekey):
        return self._stored.get(citekey)

    def branch_exists(self, branch: str) -> bool:
        return branch in self._branches

    def create_entry_pr(self, branch, pair, *, title, body, commit_message):
        if branch in self._fail_on:
            raise RuntimeError("boom")
        self.created.append((branch, pair, title, body))
        self._branches.add(branch)
        return f"https://example.test/pr/{branch}"

    def create_rename_pr(self, branch, pair, *, old_citekey, title, body, commit_message):
        if branch in self._fail_on:
            raise RuntimeError("boom")
        self.created.append((branch, pair, title, body))
        self.renamed_from.append((branch, old_citekey))
        self._branches.add(branch)
        self._stored.pop(old_citekey, None)  # the old pair is deleted by the same commit
        return f"https://example.test/pr/{branch}"


def _entries(specs):
    """Build store entries from ``[(title, date, [groups]), ...]``."""
    items, collections = [], {}
    for title, date, groups in specs:
        item = JournalArticle(
            title=title, publicationTitle="Nature", date=date, creators=[],
            tags=[Tag(tag="mentionsAICenter")],
        )
        items.append(item)
        for g in groups:
            collections.setdefault(g, ZoteroCollection(name=g)).add(item)
    return to_store_entries(items, list(collections.values()))


def _entries_with_creators(title, creators):
    """A single store entry carrying ``title`` and ``creators`` (for title/byline tests)."""
    item = JournalArticle(
        title=title, publicationTitle="Nature", date="2025", creators=creators,
        tags=[Tag(tag="mentionsAICenter")],
    )
    col = ZoteroCollection(name="bethge")
    col.add(item)
    (se,) = to_store_entries([item], [col])
    return se


def test_creates_one_pr_per_new_item():
    entries = _entries([("A", "2025", ["bethge"]), ("B", "2024", ["schoelkopf"])])
    pub = FakePublisher()
    summary = publish.publish_entries(pub, entries)

    assert len(summary.created) == 2
    assert len(pub.created) == 2
    assert not summary.skipped_unchanged and not summary.skipped_branch_exists


def test_skips_unchanged_item_already_in_store():
    (se,) = _entries([("A", "2025", ["bethge"])])
    pub = FakePublisher().seed(se)  # stored pair is byte-identical to the emitted one
    summary = publish.publish_entries(pub, [se])

    assert summary.skipped_unchanged == [se.citekey]
    assert pub.created == [] and summary.updated == []


def test_changed_item_in_store_opens_update_pr():
    (se,) = _entries([("A", "2025", ["bethge"])])
    pub = FakePublisher().seed(se, changed=True)  # stored pair differs → update
    summary = publish.publish_entries(pub, [se])

    assert summary.skipped_unchanged == [] and summary.created == []
    assert [k for k, _ in summary.updated] == [se.citekey]
    branch, _pair, title, _body = pub.created[0]
    assert branch == f"update/{se.citekey}"
    assert title.startswith("Update Publication:")


def test_skips_when_branch_already_exists():
    entries = _entries([("A", "2025", ["bethge"])])
    pub = FakePublisher(existing_branches={entries[0].citekey})
    summary = publish.publish_entries(pub, entries)

    assert summary.skipped_branch_exists == [entries[0].citekey]
    assert pub.created == []


def test_multi_group_item_is_a_single_pr_listing_all_groups():
    (se,) = _entries([("A", "2025", ["bethge", "schoelkopf"])])
    pub = FakePublisher()
    summary = publish.publish_entries(pub, [se])

    # One PR for the item even though it belongs to two PIs.
    assert len(pub.created) == 1
    branch, pair, title, body = pub.created[0]
    assert branch == se.citekey and title == 'New Publication: "A"'
    # Both groups ride in the single PR body and in the entry's custom.groups.
    assert "bethge" in body and "schoelkopf" in body
    assert se.sidecar["custom"]["groups"] == ["bethge", "schoelkopf"]


def test_limit_caps_prs_created_not_items_seen():
    # First item is already in store (a skip), then three fresh ones with limit=2:
    # the skip must not consume the budget, so exactly two of the three are created.
    entries = _entries(
        [("A", "2025", ["g"]), ("B", "2024", ["g"]), ("C", "2023", ["g"]), ("D", "2022", ["g"])]
    )
    pub = FakePublisher().seed(entries[0])  # first item unchanged in store → a skip
    summary = publish.publish_entries(pub, entries, limit=2)

    assert summary.skipped_unchanged == [entries[0].citekey]
    assert len(summary.created) == 2
    assert [c[0] for c in pub.created] == [entries[1].citekey, entries[2].citekey]


def test_dry_run_computes_but_opens_nothing():
    entries = _entries([("A", "2025", ["bethge"]), ("B", "2024", ["schoelkopf"])])
    pub = FakePublisher()
    summary = publish.publish_entries(pub, entries, dry_run=True)

    assert len(summary.created) == 2
    assert all(url == "(dry-run)" for _, url in summary.created)
    assert pub.created == []  # no PR was actually opened


def test_one_bad_item_does_not_sink_the_run():
    entries = _entries([("A", "2025", ["g"]), ("B", "2024", ["g"])])
    pub = FakePublisher(fail_on={entries[0].citekey})
    summary = publish.publish_entries(pub, entries)

    assert [k for k, _ in summary.errors] == [entries[0].citekey]
    assert [c[0] for c in pub.created] == [entries[1].citekey]


def test_pr_body_surfaces_title_authors_groups():
    from zotero_rdf import Creator

    item = JournalArticle(
        title="Predictive Coding", publicationTitle="Nature", date="2025",
        creators=[Creator(creatorType="author", firstName="Jane", lastName="Smith")],
    )
    col = ZoteroCollection(name="bethge")
    col.add(item)
    (se,) = to_store_entries([item], [col])
    body = publish.build_pr_body(se, publish.emit.emit_pair(se).bib_text)

    assert "Predictive Coding" in body
    assert "Jane Smith" in body
    assert "bethge" in body


def test_pr_title_reads_new_publication_title_by_author():
    from zotero_rdf import Creator

    bib = publish.emit.emit_pair(
        _entries_with_creators(
            "Predictive Coding",
            [Creator(creatorType="author", firstName="Jane", lastName="Smith"),
             Creator(creatorType="author", firstName="John", lastName="Doe")],
        )
    ).bib_text
    assert publish.build_pr_title(bib, "fallback") == 'New Publication: "Predictive Coding" by Smith et al.'


def test_pr_title_single_author_has_no_et_al():
    from zotero_rdf import Creator

    bib = publish.emit.emit_pair(
        _entries_with_creators(
            "Solo Work", [Creator(creatorType="author", firstName="Ada", lastName="Lovelace")]
        )
    ).bib_text
    assert publish.build_pr_title(bib, "fallback") == 'New Publication: "Solo Work" by Lovelace'


# --------------------------------------------------------------------------- #
# Renames (explicit `Replaces:` marker) and the pin guard
# --------------------------------------------------------------------------- #
def _article(title, date, *, citation_key=None, extra=None):
    from zotero_rdf import Creator

    return JournalArticle(
        title=title, publicationTitle="Nature", date=date,
        creators=[Creator(creatorType="author", firstName="Sam", lastName="Six")],
        tags=[Tag(tag="mentionsAICenter")], citationKey=citation_key, extra=extra,
    )


def test_extract_and_strip_replaces_helpers():
    assert publish._extract_replaces("Replaces: foo_2020") == "foo_2020"
    assert publish._extract_replaces("Citation Key: x\nReplaces:  bar ") == "bar"
    assert publish._extract_replaces("nothing here") is None
    assert publish._extract_replaces(None) is None

    stripped = publish._strip_replaces("Citation Key: x\nReplaces: bar\nfoo: y")
    assert "Replaces" not in stripped
    assert "Citation Key: x" in stripped and "foo: y" in stripped


def test_build_publish_items_reads_pin_status_and_replaces():
    pinned_field = _article("P", "2025", citation_key="six_p_2025")
    pinned_extra = _article("Q", "2025", extra="Citation Key: six_q_2025")
    unpinned = _article("R", "2025")
    with_replaces = _article("S", "2025", citation_key="six_s_2025", extra="Replaces: six_s_2024")

    items = publish.build_publish_items([pinned_field, pinned_extra, unpinned, with_replaces], [])

    assert [pi.pinned for pi in items] == [True, True, False, True]
    assert [pi.replaces for pi in items] == [None, None, None, "six_s_2024"]
    # The Replaces marker is operational, not store data — it must not leak into the entry.
    assert "Replaces" not in items[3].entry.bib


def test_replaces_marker_supersedes_old_entry_via_rename_pr():
    (old,) = publish.build_publish_items([_article("Paper", "2025")], [])
    old_key = old.entry.citekey
    (new,) = publish.build_publish_items(
        [_article("Paper", "2026", extra=f"Replaces: {old_key}")], []
    )
    new_key = new.entry.citekey
    assert new.replaces == old_key and new_key != old_key  # a year edit moved the citekey

    pub = FakePublisher().seed(old.entry)  # the old entry is already in the store
    summary = publish.publish_entries(pub, [new])

    assert [k for k, _ in summary.renamed] == [new_key]
    assert summary.created == [] and summary.updated == []
    branch, _pair, title, body = pub.created[0]
    assert branch == f"rename/{new_key}"
    assert title.startswith("Update Publication:")
    assert pub.renamed_from == [(f"rename/{new_key}", old_key)]
    assert old_key not in pub.existing_citekeys()  # old pair removed by the same commit
    assert f"`{old_key}`" in body  # the Supersedes line names what it replaced


def test_replaces_marker_with_unknown_target_falls_through_to_add():
    (pi,) = publish.build_publish_items(
        [_article("Ghosted", "2026", extra="Replaces: never_existed_2000")], []
    )
    pub = FakePublisher()  # target not in store
    summary = publish.publish_entries(pub, [pi])

    assert summary.rename_target_missing == [(pi.entry.citekey, "never_existed_2000")]
    assert [k for k, _ in summary.created] == [pi.entry.citekey]  # added as a normal new item
    assert summary.renamed == [] and pub.renamed_from == []


def test_limit_counts_renames_alongside_adds_and_updates():
    (old,) = publish.build_publish_items([_article("Paper", "2025")], [])
    (renamed,) = publish.build_publish_items(
        [_article("Paper", "2026", extra=f"Replaces: {old.entry.citekey}")], []
    )
    adds = _entries([("A", "2025", ["g"]), ("B", "2024", ["g"])])
    pub = FakePublisher().seed(old.entry)

    summary = publish.publish_entries(pub, [renamed, *adds], limit=1)

    assert summary.opened() == 1  # the rename alone fills the budget
    assert len(summary.renamed) == 1 and summary.created == []
