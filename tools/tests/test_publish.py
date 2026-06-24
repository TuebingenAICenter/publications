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

    def __init__(self, in_store=(), existing_branches=(), fail_on=()):
        self._in_store = set(in_store)
        self._branches = set(existing_branches)
        self._fail_on = set(fail_on)
        self.created: list[tuple[str, object, str, str]] = []  # (branch, pair, title, body)

    def existing_citekeys(self) -> set[str]:
        return set(self._in_store)

    def branch_exists(self, branch: str) -> bool:
        return branch in self._branches

    def create_entry_pr(self, branch, pair, *, title, body, commit_message):
        if branch in self._fail_on:
            raise RuntimeError("boom")
        self.created.append((branch, pair, title, body))
        self._branches.add(branch)
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


def test_creates_one_pr_per_new_item():
    entries = _entries([("A", "2025", ["bethge"]), ("B", "2024", ["schoelkopf"])])
    pub = FakePublisher()
    summary = publish.publish_entries(pub, entries)

    assert len(summary.created) == 2
    assert len(pub.created) == 2
    assert not summary.skipped_in_store and not summary.skipped_branch_exists


def test_skips_citekey_already_in_store():
    entries = _entries([("A", "2025", ["bethge"])])
    in_store = {entries[0].citekey}
    pub = FakePublisher(in_store=in_store)
    summary = publish.publish_entries(pub, entries)

    assert summary.skipped_in_store == [entries[0].citekey]
    assert pub.created == []


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
    assert branch == se.citekey and title == se.citekey
    # Both groups ride in the single PR body and in the entry's custom.groups.
    assert "bethge" in body and "schoelkopf" in body
    assert se.sidecar["custom"]["groups"] == ["bethge", "schoelkopf"]


def test_limit_caps_prs_created_not_items_seen():
    # First item is already in store (a skip), then three fresh ones with limit=2:
    # the skip must not consume the budget, so exactly two of the three are created.
    entries = _entries(
        [("A", "2025", ["g"]), ("B", "2024", ["g"]), ("C", "2023", ["g"]), ("D", "2022", ["g"])]
    )
    pub = FakePublisher(in_store={entries[0].citekey})
    summary = publish.publish_entries(pub, entries, limit=2)

    assert summary.skipped_in_store == [entries[0].citekey]
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
