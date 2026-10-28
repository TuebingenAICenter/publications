"""Store BibTeX export extensions, shared by normalization and compilation.

Zotero has no shortbooktitle attribute. Its importer carries the field through
Extra as Shortbooktitle; export it back into the BibTeX instead of the sidecar.
"""
from __future__ import annotations

import re
from zotero_rdf import Document, to_bibtex as _to_bibtex


def to_bibtex(items, **kwargs):
    """Like zotero_rdf.to_bibtex, preserving the shortbooktitle extension.

    Use the upstream title renderer for TeX escaping and case protection. No
    input items are mutated, and unrelated Extra values stay in the sidecar.
    """
    text, sidecar = _to_bibtex(items, **kwargs)
    if not text:
        return text, sidecar
    chunks = re.split(r"\n\s*\n(?=@\w+\{)", text.strip())
    for index, chunk in enumerate(chunks):
        key = re.match(r"@\w+\{([^,]+),", chunk).group(1)
        spill = sidecar.get(key, {})
        extra = spill.get("extra", {})
        labels = [label for label in extra if label.casefold() == "shortbooktitle"]
        if not labels:
            continue
        values = {str(extra[label]) for label in labels}
        if len(values) != 1:
            raise ValueError(f"{key}: conflicting shortbooktitle values")
        value = values.pop()
        if value:
            rendered, _ = _to_bibtex([Document(title=value, citationKey="shortbooktitle")])
            prefix = "@misc{shortbooktitle,\n\ttitle = "
            suffix = ",\n}\n"
            if not rendered.startswith(prefix) or not rendered.endswith(suffix):
                raise ValueError("Unexpected upstream title rendering")
            field_value = rendered[len(prefix):-len(suffix)]
            chunks[index] = chunk[:-1] + f"\tshortbooktitle = {field_value},\n}}"
        for label in labels:
            del extra[label]
        if not extra:
            spill.pop("extra", None)
    return "\n\n".join(chunks) + "\n", sidecar
