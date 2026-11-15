"""Conference acronyms survive all store export paths, independently of titles."""
import json
import pytest
from conftest import install_schema
from zotero_rdf import from_bibtex, parse_zotero_rdf
from publication_store.entry import canonical_entries, canonical_entry
from publication_store.bibtex import to_bibtex
from publication_store import artifacts
from publication_store.zotero_bridge import StoreEntry, from_store_entries, to_store_entries


@pytest.mark.parametrize('acronym', ['CVPR', 'NeurIPS', 'ECML-PKDD', 'A & B', 'École'])
def test_canonical_roundtrip(acronym):
    text = f'@inproceedings{{paper, title={{A paper}}, shorttitle={{Paper}}, booktitle={{Full conference}}, shortbooktitle={{{acronym}}}, year={{2025}}}}'
    key, year, bib, spill = canonical_entry(text)
    assert 'shortbooktitle =' in bib
    assert 'shorttitle = {Paper}' in bib
    assert not any(k.lower() == 'shortbooktitle' for k in spill.get('extra', {}))
    assert canonical_entry(bib)[2] == bib
    assert canonical_entries(bib, {key: spill})[0][2] == bib


def test_multiple_entries_and_legacy_sidecar():
    text = '@inproceedings{a,title={One},year={2025}}\n@inproceedings{b,title={Two},shortbooktitle={ICML},year={2025}}'
    result = canonical_entries(text, {'a': {'extra': {'Shortbooktitle': 'CVPR', 'Source': 'existing'}}})
    assert 'CVPR' in result[0][2] and 'ICML' not in result[0][2]
    assert 'ICML' in result[1][2] and 'CVPR' not in result[1][2]
    assert result[0][3]['extra'] == {'Source': 'existing'}


def test_no_extension_is_unchanged():
    from zotero_rdf import to_bibtex as upstream
    items = from_bibtex('@inproceedings{a,title={One},year={2025}}')
    assert to_bibtex(items) == upstream(items)


def test_compile_and_rdf_roundtrip(tmp_path):
    install_schema(tmp_path)
    key, _, bib, spill = canonical_entry('@inproceedings{paper,title={Paper},shortbooktitle={CVPR},year={2025}}')
    sidecar = {'zotero': spill, 'custom': {'groups': ['hennig'], 'mentions_ai_center': False}}
    for directory in ['entries/2025', 'meta/2025']:
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / 'entries/2025/paper.bib').write_text(bib)
    (tmp_path / 'meta/2025/paper.json').write_text(json.dumps(sidecar))
    all_bib, warnings = artifacts.compile_all_bib(tmp_path)
    assert not warnings and 'shortbooktitle = {{CVPR}}' in all_bib
    groups, warnings = artifacts.compile_group_bibs(tmp_path)
    assert not warnings and 'shortbooktitle = {{CVPR}}' in groups['hennig']
    items, collections = from_store_entries([StoreEntry(key, bib, sidecar)])
    assert to_store_entries(items, collections)[0].bib == bib
    rdf, warnings = artifacts.compile_rdf(tmp_path)
    assert not warnings
    parsed = parse_zotero_rdf(rdf)
    # RDF preserves the acronym in Zotero Extra for subsequent BibTeX export.
    assert b'CVPR' in rdf and b'Shortbooktitle' in rdf
