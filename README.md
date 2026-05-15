# Papers

`papers.py` is a command-line tool for downloading, organizing, indexing, and searching research paper PDFs.

It accepts direct PDF URLs, local PDF files, and selected paper-page URLs, then extracts PDF text and metadata, chooses a category folder, saves the file with a readable name, and indexes it in SQLite for later search.

## Features

- Import research papers from direct PDF URLs.
- Import local PDF files.
- Resolve selected paper pages from arXiv, bioRxiv, and Science.org.
- Extract PDF text and metadata with PyMuPDF.
- Sort papers into category directories.
- Prompt to create a new category when classification is uncertain.
- Store searchable metadata and extracted text in SQLite.
- Search indexed papers from the command line.
- List all indexed papers with pagination.
- Use a local Python virtual environment.

## Default Categories

The script starts with these category directories:

```text
archaeology/
biology/
machine-learning/
```

Additional categories can be created interactively when importing a paper.

## Setup

Create a local virtual environment:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Make the script executable if needed:

```bash
chmod +x papers.py
```

The script auto-detects `.venv`, so activating the environment is optional after dependencies are installed.

## Usage

Import a direct PDF URL:

```bash
./papers.py "https://example.com/path/to/paper.pdf"
```

Import a local PDF:

```bash
./papers.py "~/Downloads/paper.pdf"
```

Import an arXiv paper:

```bash
./papers.py "https://arxiv.org/abs/<paper-id>"
```

Force a category:

```bash
./papers.py "~/Downloads/paper.pdf" --category biology
```

Preview an import without saving:

```bash
./papers.py "https://arxiv.org/abs/<paper-id>" --dry-run
```

Search indexed papers:

```bash
./papers.py search "search terms"
```

List all indexed papers:

```bash
./papers.py all
```

Use a custom page size when listing all papers:

```bash
./papers.py all --page-size 10
```

## Output

After importing a paper, the script prints a summary block:

```text

Saved paper.
Category: <category>
Path: <category>/<saved-file-name>.pdf
Title: <paper title>
Source: <provider> <provider-id>
Scores: <category>=<score>, <category>=<score>, <category>=<score>

```

Search and list results are printed as numbered blocks:

```text

Found <n> Results

Result 1/<n>:
<category>/<saved-file-name>.pdf
  <paper title>
  <authors>
  <provider> | <provider-id>
  category: <category>
```

## Filenames

Saved PDFs use readable, stable filenames derived from available metadata:

```text
<year>_<first-author>_<title-slug>.pdf
<year>_<first-author>_<title-slug>_<provider>_<provider-id>.pdf
```

If metadata is missing, the script uses fallback values such as `undated` or `unknown-author`.

## Metadata And Search

The script stores metadata and extracted text in:

```text
.paper_index.sqlite
```

SQLite FTS5 is used for full-text search across fields such as:

- title
- authors
- abstract
- extracted PDF text
- DOI or provider ID
- category
- saved path

## Supported Sources

### Direct PDF URLs

Direct PDF links are downloaded and validated before saving.

### Local PDFs

Local PDF files are analyzed, renamed, copied into a category directory, and indexed.

### arXiv

arXiv abstract URLs are converted to PDF URLs automatically.

### bioRxiv And Science.org

The script attempts to resolve the PDF URL and extract available page metadata.

Some publisher sites may block automated access with anti-bot challenges. The script does not bypass those protections. If blocked, run the command interactively and provide either:

- a direct PDF URL, or
- a local path to a manually downloaded PDF

## Limitations

- Classification uses deterministic local keyword scoring.
- No hosted AI model or external classification API is used.
- OCR is not included. Scanned or image-only PDFs may fail text extraction.
- Paywalls, CAPTCHAs, and anti-bot systems are not bypassed.
- Provider support is intentionally conservative and can be extended over time.

## Development

Run a syntax check:

```bash
.venv/bin/python -m py_compile papers.py
```

Run a dry import:

```bash
./papers.py "https://arxiv.org/abs/<paper-id>" --dry-run
```

Search the index:

```bash
./papers.py search "query"
```

## License

MIT License. See [LICENSE](LICENSE).
