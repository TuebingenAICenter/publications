"""Bridge a Zotero RDF library to/from the store's ``.bib`` + sidecar pairs.

``ZoteroItem``\\ s + ``ZoteroCollection``\\ s  ⇄  per-entry ``(.bib, sidecar)`` pairs,
where the sidecar is the full store shape ``{"zotero": …, "custom": …}``.

This sits one layer above zotero-rdf-python's lossless ``to_bibtex`` / ``from_bibtex``
round-trip — which only ever carries the **``zotero``** half — and adds the two
institute-specific transforms that produce the **``custom``** half:

* **Collections ⇄ ``custom.groups``.** Group ownership is recorded explicitly in
  ``custom.groups`` as collection *names* (PI-table slugs), decoupled from Zotero
  collection semantics — the store plan's stance, where the ``zotero`` half's own
  ``collections`` overlay may even be dropped later. So on export we map each item's
  collection membership to ``custom.groups`` and omit ``collections`` from the
  ``zotero`` half; on import we rebuild a ``ZoteroCollection`` per group name and
  attach the item.
* **``mentionsAICenter`` tag ⇄ ``custom.mentions_ai_center == True``.** The tag is
  the Zotero-native representation; the flag is the store representation. On export a
  ``mentionsAICenter`` tag is lifted out of ``tags`` into the flag. On import the
  reverse is for human review in the Zotero GUI, so ``True`` re-adds the tag;
  ``False`` and absent both map to "no tag" (the store JSON stays authoritative for
  the tri-state — Zotero has no native way to carry it).

Everything else flows through the ``zotero`` half unchanged. The functions are pure:
:func:`items_to_pairs` deep-copies before stripping the tag, so the caller's items are
untouched.
"""

from __future__ import annotations

import copy
from typing import Iterable

from zotero_rdf import (
    Attachment,
    Tag,
    ZoteroCollection,
    ZoteroItem,
    from_bibtex,
    to_bibtex,
)

from .entry import citekey_of, split_entries, year_of

MENTIONS_AI_CENTER_TAG = "mentionsAICenter"

#: Title of the derived linked-URL attachment that points at the entry's store
#: sidecar on the web. Used as the marker to strip it again on import — it is a
#: pointer to the source of truth, never store data.
SIDECAR_ATTACHMENT_TITLE = "Publication store record (custom metadata)"


def items_to_pairs(
    items: Iterable[ZoteroItem],
    collections: Iterable[ZoteroCollection] | None = None,
    *,
    sidecar_base_url: str | None = None,
) -> list[tuple[str, str, dict]]:
    """Zotero library → ``[(citekey, bib_text, sidecar)]``, one triple per item.

    ``sidecar`` is the store's canonical shape — ``{"zotero": …, "custom": …}`` with
    both keys always present (either may be ``{}``), matching what the diff job
    writes. The ``zotero`` half is the lossless overlay; ``custom.groups`` holds the
    names of the collections each item belongs to (resolved via ``collections``) and
    a ``mentionsAICenter`` tag becomes ``custom.mentions_ai_center = True``. The
    ``zotero`` half omits ``collections`` (that membership now lives in
    ``custom.groups``).

    ``sidecar_base_url`` (optional, host-agnostic by default) is the web base under
    which the store's tree is browsable — e.g.
    ``"https://github.com/TuebingenAICenter/publications/blob/main"``. When given,
    each item gets a linked-URL attachment pointing at its sidecar
    (``<base>/meta/<year>/<citekey>.json``) so a reviewer in the Zotero GUI can jump
    to the full custom record. It is a *pointer*, not a carrier: it only resolves
    once the entry has landed at that path, and :func:`pairs_to_items` strips it back
    out (it is never store data). The attachment rides in the ``zotero`` half like
    any other.

    The input items are not mutated. ``bib_text`` is a single canonical entry.
    """
    items = [copy.deepcopy(item) for item in items]
    # Membership is authoritative on the collection side (``collection.items``, the
    # hasPart links the serializer writes) — ``item.collections`` is only populated
    # when parsing existing RDF, not when building via ``collection.add(item)``. Map
    # each member URI to the group names it belongs to.
    uri_to_names: dict[str, list[str]] = {}
    for collection in collections or []:
        if not collection.name:
            continue
        for uri in collection.items:
            uri_to_names.setdefault(uri, []).append(collection.name)

    customs: list[dict] = []
    for item in items:
        groups = sorted(set(uri_to_names.get(item._rdf_uri, [])))
        mentions = any(tag.tag == MENTIONS_AI_CENTER_TAG for tag in item.tags)
        item.tags = [tag for tag in item.tags if tag.tag != MENTIONS_AI_CENTER_TAG]

        custom: dict = {}
        if groups:
            custom["groups"] = groups
        if mentions:
            custom["mentions_ai_center"] = True
        customs.append(custom)

    bib_string, zotero = to_bibtex(items, export_collections=False)

    # The sidecar link needs each item's citekey + year, which only exist after the
    # first serialization; attaching it can't change either (the citekey is a pure
    # function of creators/title/date, and attachments live in the zotero half, not
    # the bib), so a second pass just folds the attachment into the zotero half.
    if sidecar_base_url is not None:
        base = sidecar_base_url.rstrip("/")
        for item, (citekey, entry_text) in zip(items, split_entries(bib_string)):
            url = f"{base}/meta/{year_of(entry_text)}/{citekey}.json"
            item.attachments.append(
                Attachment(
                    title=SIDECAR_ATTACHMENT_TITLE,
                    url=url,
                    linkMode="linked_url",
                    mimeType="application/json",
                )
            )
        bib_string, zotero = to_bibtex(items, export_collections=False)

    pairs: list[tuple[str, str, dict]] = []
    for (citekey, entry_text), custom in zip(split_entries(bib_string), customs):
        sidecar = {"zotero": zotero.get(citekey, {}), "custom": custom}
        pairs.append((citekey, entry_text, sidecar))
    return pairs


def pairs_to_items(
    pairs: Iterable[tuple[str, dict]],
) -> tuple[list[ZoteroItem], list[ZoteroCollection]]:
    """``[(bib_text, sidecar)]`` → ``(items, collections)`` — inverse of export.

    The ``zotero`` half is overlaid back via ``from_bibtex``; ``custom.groups``
    rebuilds a :class:`ZoteroCollection` per group name (de-duplicated across the
    batch, in first-seen order) with the item attached; ``custom.mentions_ai_center
    is True`` re-adds the ``mentionsAICenter`` tag. ``False``/absent add nothing.
    The derived sidecar linked-URL attachment (:data:`SIDECAR_ATTACHMENT_TITLE`, if
    :func:`items_to_pairs` added one) is stripped back out — it is a pointer, never
    store data, so it must not survive into a re-exported ``zotero`` half.

    Accepts ``(bib_text, sidecar)`` pairs; any extra tuple elements (e.g. the
    ``citekey`` from :func:`items_to_pairs`) are ignored, so its output can be fed
    straight back in.
    """
    bib_texts: list[str] = []
    zotero_map: dict[str, dict] = {}
    custom_map: dict[str, dict] = {}
    for pair in pairs:
        bib_text, data = pair[0], pair[1]
        key = citekey_of(bib_text)
        if key is None:
            raise ValueError(f"could not find citekey in entry:\n{bib_text[:200]}")
        bib_texts.append(bib_text.rstrip("\n"))
        zotero_half = data.get("zotero", {})
        if zotero_half:
            zotero_map[key] = zotero_half
        custom_map[key] = data.get("custom", {})

    items = from_bibtex("\n\n".join(bib_texts), sidecar=zotero_map or None)

    collections_by_name: dict[str, ZoteroCollection] = {}
    for item in items:
        item.attachments = [
            att
            for att in item.attachments
            if not (
                att.linkMode == "linked_url"
                and att.title == SIDECAR_ATTACHMENT_TITLE
            )
        ]
        custom = custom_map.get(item.citationKey, {})
        if custom.get("mentions_ai_center") is True:
            item.tags.append(Tag(tag=MENTIONS_AI_CENTER_TAG))
        for name in custom.get("groups", []):
            collection = collections_by_name.get(name)
            if collection is None:
                collection = ZoteroCollection(name=name)
                collections_by_name[name] = collection
            collection.add(item)

    return items, list(collections_by_name.values())
