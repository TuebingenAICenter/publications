# Publications Database

A repository serving as a database of publications of the Tübingen AI Center.
Each entry is represented by a BibTeX record in a `tueai_publications.bib` file plus a companion JSON file containing additional metadata that doesn't fit naturally into BibTeX.

## Structure

- A `tueai_publications.bib` file containing the BibTeX entries.
- `tueai_publications.json` JSON file providing supplementary metadata for those entries.

## Usage

Clone the repository and parse the `.bib` and JSON files with the tools of your choice.

```bash
git clone git@github.com:TuebingenAICenter/publications.git
```

## Contributing

Add new publications by creating both the BibTeX entry and its corresponding JSON metadata file, then open a pull request.

