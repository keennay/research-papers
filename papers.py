#!/usr/bin/env python3
"""Import, classify, save, and search research paper PDFs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


SCRIPT_PATH = Path(__file__).resolve()
ROOT = SCRIPT_PATH.parent


def reexec_with_local_venv() -> None:
    venv_python = ROOT / ".venv" / "bin" / "python"
    venv_root = ROOT / ".venv"
    if not venv_python.exists():
        return
    if Path(sys.prefix).resolve() == venv_root.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), str(SCRIPT_PATH), *sys.argv[1:]])


reexec_with_local_venv()


try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - exercised before deps exist
    reexec_with_local_venv()
    raise SystemExit(
        "Missing dependencies. Create the local environment and install them with:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/python -m pip install -r requirements.txt"
    ) from exc

try:
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF imports
    try:
        import fitz as pymupdf
    except ImportError as exc:
        reexec_with_local_venv()
        raise SystemExit(
            "Missing PyMuPDF. Install dependencies inside the local environment with:\n"
            "  .venv/bin/python -m pip install -r requirements.txt"
        ) from exc


DB_PATH = ROOT / ".paper_index.sqlite"
DEFAULT_CATEGORIES = ("archaeology", "biology", "machine-learning")
REQUEST_TIMEOUT = (12, 90)
MAX_EXTRACTED_CHARS = 450_000
MAX_EXTRACTED_PAGES = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 papers.py/1.0"
    ),
    "Accept": "text/html,application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DEFAULT_KEYWORDS = {
    "archaeology": [
        "archaeology",
        "archaeological",
        "ancient dna",
        "ancient genome",
        "ancient genomes",
        "ancient human",
        "paleogenomic",
        "palaeogenomic",
        "radiocarbon",
        "excavation",
        "artifact",
        "artefact",
        "neolithic",
        "paleolithic",
        "palaeolithic",
        "bronze age",
        "iron age",
        "hunter-gatherer",
        "burial",
        "osteological",
        "isotopes",
        "ceramics",
        "aDNA",
    ],
    "biology": [
        "biology",
        "biological",
        "genetics",
        "genome",
        "genomic",
        "genomes",
        "dna",
        "rna",
        "protein",
        "cell",
        "cells",
        "disease",
        "evolution",
        "natural selection",
        "phenotype",
        "microbiome",
        "species",
        "population genetics",
        "molecular",
        "immunology",
        "metabolism",
        "transcriptome",
    ],
    "machine-learning": [
        "machine learning",
        "deep learning",
        "neural network",
        "neural networks",
        "transformer",
        "transformers",
        "attention",
        "gradient",
        "model training",
        "dataset",
        "benchmark",
        "language model",
        "large language model",
        "computer vision",
        "reinforcement learning",
        "classification accuracy",
        "backpropagation",
        "embedding",
        "embeddings",
    ],
}


class PaperError(Exception):
    """Base error for user-facing failures."""


class AntiBotError(PaperError):
    """Raised when a site blocks scripted access with a bot challenge."""


class DownloadError(PaperError):
    """Raised when a PDF or page cannot be downloaded."""


@dataclass
class PaperMetadata:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    abstract: str = ""
    doi: str = ""
    provider: str = ""
    provider_id: str = ""
    source_url: str = ""
    pdf_url: str = ""


@dataclass
class SourceResolution:
    source: str
    provider: str = ""
    provider_id: str = ""
    source_url: str = ""
    pdf_url: str = ""
    local_path: Path | None = None
    web_metadata: PaperMetadata = field(default_factory=PaperMetadata)
    web_blocked: bool = False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print_main_help()
        return 0

    if argv[0] == "search":
        return run_search(argv[1:])

    if argv[0] == "all":
        return run_all(argv[1:])

    if argv[0] == "import":
        argv = argv[1:]

    return run_import(argv)


def print_main_help() -> None:
    print(
        textwrap.dedent(
            """\
            usage:
              ./papers.py <url-or-local-pdf> [--category CATEGORY] [--dry-run]
              ./papers.py import <url-or-local-pdf> [--category CATEGORY] [--dry-run]
              ./papers.py search "query" [--limit N]
              ./papers.py all

            Supported inputs:
              direct PDF URL, local PDF path, arXiv, bioRxiv, and science.org links.
            """
        )
    )


def run_import(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="./papers.py")
    parser.add_argument("source", help="PDF URL, local PDF path, or supported paper page URL")
    parser.add_argument("--category", help="Force a category and create it if needed")
    parser.add_argument("--dry-run", action="store_true", help="Analyze but do not save or index")
    args = parser.parse_args(argv)

    if not args.dry_run:
        ensure_project_dirs()
        con = connect_db()
        ensure_schema(con)
    else:
        con = None

    try:
        with tempfile.TemporaryDirectory(prefix="papers-") as tmp:
            temp_dir = Path(tmp)
            resolution = resolve_source(args.source, temp_dir)
            pdf_path, final_pdf_url = obtain_pdf(resolution, temp_dir)
            metadata, extracted_text = analyze_pdf(pdf_path, resolution, final_pdf_url)
            profiles = load_category_profiles(con)
            category, score_report, was_prompted = choose_category(
                metadata,
                extracted_text,
                profiles,
                con,
                override=args.category,
                dry_run=args.dry_run,
            )
            destination = build_destination_path(category, metadata)
            file_hash = sha256_file(pdf_path)

            if args.dry_run:
                print_import_summary(
                    metadata=metadata,
                    category=category,
                    saved_path=destination,
                    score_report=score_report,
                    was_prompted=was_prompted,
                    dry_run=True,
                )
                return 0

            existing = find_existing_by_hash(con, file_hash)
            if existing:
                print(f"Already imported: {existing['saved_path']}")
                print(f"Category: {existing['category']}")
                return 0

            saved_path = save_pdf(pdf_path, destination)
            index_paper(con, metadata, extracted_text, category, saved_path, file_hash)
            con.commit()
            print_import_summary(
                metadata=metadata,
                category=category,
                saved_path=saved_path,
                score_report=score_report,
                was_prompted=was_prompted,
                dry_run=False,
            )
            return 0
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130
    except PaperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if con is not None:
            con.close()


def run_search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="./papers.py search")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", type=int, default=10, help="Maximum results to show")
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        print("No index exists yet. Import a paper first.")
        return 0

    con = connect_db()
    try:
        rows = search_index(con, args.query, args.limit)
    finally:
        con.close()

    print_found_count(len(rows))
    if not rows:
        return 0

    blocks = [format_result_block(row, index, len(rows)) for index, row in enumerate(rows, start=1)]
    print("\n\n".join(blocks))
    return 0


def run_all(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="./papers.py all")
    parser.add_argument("--page-size", type=int, default=25, help="Number of files to show per page")
    args = parser.parse_args(argv)

    page_size = max(args.page_size, 1)
    if not DB_PATH.exists():
        print_found_count(0)
        return 0

    con = connect_db()
    try:
        rows = list_all_papers(con)
    finally:
        con.close()

    total = len(rows)
    print_found_count(total)
    if not rows:
        return 0

    start = 0
    while start < total:
        end = min(start + page_size, total)
        page = rows[start:end]
        blocks = [
            format_result_block(row, index, total)
            for index, row in enumerate(page, start=start + 1)
        ]
        print("\n\n".join(blocks))
        start = end
        if start >= total:
            break
        if not sys.stdin.isatty():
            print(f"\nShowing {start}/{total}. Re-run interactively to page through more results.")
            break
        next_count = min(page_size, total - start)
        answer = input(f"\nShow next {next_count} results? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            break
        print()
    return 0


def print_found_count(count: int) -> None:
    result_word = "Result" if count == 1 else "Results"
    print(f"\nFound {count} {result_word}\n")


def format_result_block(row: sqlite3.Row, index: int, total: int) -> str:
    title = row["title"] or "(untitled)"
    authors = format_authors(json.loads(row["authors"] or "[]"))
    source_bits = [bit for bit in (row["provider"], row["provider_id"], row["doi"]) if bit]
    source = " | ".join(source_bits)
    block = [f"Result {index}/{total}:", f"{row['saved_path']}", f"  {title}"]
    if authors:
        block.append(f"  {authors}")
    if source:
        block.append(f"  {source}")
    block.append(f"  category: {row['category']}")
    return "\n".join(block)


def ensure_project_dirs() -> None:
    for category in DEFAULT_CATEGORIES:
        (ROOT / category).mkdir(exist_ok=True)


def connect_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table if not exists papers (
            id integer primary key,
            title text,
            authors text,
            year text,
            abstract text,
            doi text,
            provider text,
            provider_id text,
            source_url text,
            pdf_url text,
            category text not null,
            saved_path text not null unique,
            file_sha256 text not null,
            imported_at text not null
        );

        create table if not exists category_keywords (
            category text primary key,
            keywords text not null
        );

        create virtual table if not exists papers_fts using fts5(
            title,
            authors,
            abstract,
            body_text,
            doi,
            provider,
            provider_id,
            category,
            saved_path
        );
        """
    )
    for category, keywords in DEFAULT_KEYWORDS.items():
        con.execute(
            """
            insert into category_keywords(category, keywords)
            values(?, ?)
            on conflict(category) do nothing
            """,
            (category, json.dumps(keywords)),
        )
    con.commit()


def resolve_source(source: str, temp_dir: Path) -> SourceResolution:
    source_path = Path(source).expanduser()
    if source_path.exists():
        if not source_path.is_file():
            raise PaperError(f"Local path is not a file: {source_path}")
        if source_path.suffix.lower() != ".pdf":
            raise PaperError(f"Local path does not look like a PDF: {source_path}")
        validate_pdf_file(source_path)
        return SourceResolution(source=source, local_path=source_path, source_url=str(source_path))

    if not is_url(source):
        raise PaperError(f"Input is neither an existing file nor an HTTP URL: {source}")

    parsed = urllib.parse.urlparse(source)
    host = parsed.netloc.lower()
    path = urllib.parse.unquote(parsed.path)

    if "arxiv.org" in host:
        return resolve_arxiv(source)
    if "biorxiv.org" in host:
        return resolve_biorxiv(source)
    if host.endswith("science.org") or host.endswith(".science.org"):
        return resolve_science(source)

    if path.lower().endswith(".pdf"):
        return SourceResolution(source=source, source_url=source, pdf_url=source)

    metadata = PaperMetadata(source_url=source)
    try:
        html = fetch_html(source)
        metadata = parse_web_metadata(html, source)
    except AntiBotError:
        raise
    except DownloadError:
        pass

    if metadata.pdf_url:
        return SourceResolution(
            source=source,
            source_url=source,
            pdf_url=metadata.pdf_url,
            web_metadata=metadata,
        )

    raise PaperError(
        "Could not find a PDF for this page. Provide a direct PDF URL or a local PDF path."
    )


def resolve_arxiv(url: str) -> SourceResolution:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/")
    arxiv_id = ""

    if path.startswith("abs/"):
        arxiv_id = path.removeprefix("abs/").removesuffix(".pdf")
    elif path.startswith("pdf/"):
        arxiv_id = path.removeprefix("pdf/").removesuffix(".pdf")
    elif path:
        arxiv_id = path.rsplit("/", 1)[-1].removesuffix(".pdf")

    if not arxiv_id:
        raise PaperError(f"Could not parse arXiv ID from URL: {url}")

    source_url = f"https://arxiv.org/abs/{arxiv_id}"
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    metadata = PaperMetadata(provider="arxiv", provider_id=arxiv_id, source_url=source_url, pdf_url=pdf_url)

    try:
        html = fetch_html(source_url)
        metadata = merge_metadata(metadata, parse_web_metadata(html, source_url))
    except PaperError:
        pass

    if not metadata.title or not metadata.abstract:
        metadata = merge_metadata(metadata, fetch_arxiv_api_metadata(arxiv_id))

    metadata.provider = "arxiv"
    metadata.provider_id = arxiv_id
    metadata.source_url = source_url
    metadata.pdf_url = metadata.pdf_url or pdf_url
    return SourceResolution(
        source=url,
        provider="arxiv",
        provider_id=arxiv_id,
        source_url=source_url,
        pdf_url=metadata.pdf_url,
        web_metadata=metadata,
    )


def resolve_biorxiv(url: str) -> SourceResolution:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    provider_id = ""
    match = re.search(r"/content/(.+?)(?:\.full\.pdf)?/?$", path)
    if match:
        provider_id = match.group(1).strip("/")
    else:
        provider_id = path.strip("/")

    source_url = url
    if ".full.pdf" in source_url:
        source_url = source_url.replace(".full.pdf", "")
    pdf_url = source_url.rstrip("/") + ".full.pdf"
    metadata = PaperMetadata(
        provider="biorxiv",
        provider_id=provider_id,
        source_url=source_url,
        pdf_url=pdf_url,
    )

    if path.endswith(".full.pdf"):
        return SourceResolution(
            source=url,
            provider="biorxiv",
            provider_id=provider_id,
            source_url=source_url,
            pdf_url=pdf_url,
            web_metadata=metadata,
        )

    try:
        html = fetch_html(source_url)
        metadata = merge_metadata(metadata, parse_web_metadata(html, source_url))
    except AntiBotError:
        return SourceResolution(
            source=url,
            provider="biorxiv",
            provider_id=provider_id,
            source_url=source_url,
            pdf_url=pdf_url,
            web_metadata=metadata,
            web_blocked=True,
        )

    metadata.provider = "biorxiv"
    metadata.provider_id = provider_id or metadata.provider_id
    metadata.source_url = source_url
    metadata.pdf_url = metadata.pdf_url or pdf_url
    return SourceResolution(
        source=url,
        provider="biorxiv",
        provider_id=metadata.provider_id,
        source_url=source_url,
        pdf_url=metadata.pdf_url,
        web_metadata=metadata,
    )


def resolve_science(url: str) -> SourceResolution:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    doi = ""
    match = re.search(r"/doi/(?:pdf/)?(.+)$", path)
    if match:
        doi = match.group(1).strip("/")

    if not doi:
        raise PaperError(f"Could not parse Science DOI from URL: {url}")

    source_url = f"https://www.science.org/doi/{doi}"
    pdf_url = f"https://www.science.org/doi/pdf/{doi}"
    metadata = PaperMetadata(provider="science", provider_id=doi, doi=doi, source_url=source_url, pdf_url=pdf_url)

    if "/doi/pdf/" in path:
        return SourceResolution(
            source=url,
            provider="science",
            provider_id=doi,
            source_url=source_url,
            pdf_url=pdf_url,
            web_metadata=metadata,
        )

    try:
        html = fetch_html(source_url)
        metadata = merge_metadata(metadata, parse_web_metadata(html, source_url))
    except AntiBotError:
        return SourceResolution(
            source=url,
            provider="science",
            provider_id=doi,
            source_url=source_url,
            pdf_url=pdf_url,
            web_metadata=metadata,
            web_blocked=True,
        )

    metadata.provider = "science"
    metadata.provider_id = doi
    metadata.doi = metadata.doi or doi
    metadata.source_url = source_url
    metadata.pdf_url = metadata.pdf_url or pdf_url
    return SourceResolution(
        source=url,
        provider="science",
        provider_id=doi,
        source_url=source_url,
        pdf_url=metadata.pdf_url,
        web_metadata=metadata,
    )


def obtain_pdf(resolution: SourceResolution, temp_dir: Path) -> tuple[Path, str]:
    if resolution.local_path is not None:
        return resolution.local_path, resolution.pdf_url

    if resolution.web_blocked:
        return prompt_for_pdf_after_block(resolution, temp_dir)

    if not resolution.pdf_url:
        raise PaperError("No PDF URL was found for this input.")

    target = temp_dir / "download.pdf"
    try:
        download_pdf(resolution.pdf_url, target)
        return target, resolution.pdf_url
    except AntiBotError:
        return prompt_for_pdf_after_block(resolution, temp_dir)


def prompt_for_pdf_after_block(resolution: SourceResolution, temp_dir: Path) -> tuple[Path, str]:
    if not sys.stdin.isatty():
        raise AntiBotError(
            "Anti-bot challenge detected. Re-run interactively to provide a direct PDF URL "
            "or a local path to a manually downloaded PDF."
        )

    print("Anti-bot challenge detected for this source.")
    direct_url = input(
        "Please enter a direct PDF URL, or press Enter to provide a local PDF path: "
    ).strip()
    if direct_url:
        target = temp_dir / "manual-url.pdf"
        try:
            download_pdf(direct_url, target)
            return target, direct_url
        except AntiBotError:
            print("That PDF URL also hit an anti-bot challenge.")
        except PaperError as exc:
            print(f"That PDF URL could not be used: {exc}")

    while True:
        local = input("Path to manually downloaded PDF: ").strip()
        if not local:
            raise PaperError("No PDF path provided.")
        path = Path(local).expanduser()
        if path.exists() and path.is_file():
            validate_pdf_file(path)
            return path, direct_url
        print("That path does not exist or is not a file.")


def fetch_html(url: str) -> str:
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise DownloadError(f"Could not fetch page {url}: {exc}") from exc

    body = response.content[:8192]
    if is_anti_bot_response(response, body):
        raise AntiBotError(f"Anti-bot challenge detected at {url}")
    if response.status_code >= 400:
        raise DownloadError(f"Could not fetch page {url}: HTTP {response.status_code}")

    return response.text


def download_pdf(url: str, target: Path) -> None:
    try:
        with requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as response:
            first_chunks: list[bytes] = []
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    first_chunks.append(chunk)
                    break
            first = b"".join(first_chunks)

            if is_anti_bot_response(response, first):
                raise AntiBotError(f"Anti-bot challenge detected while downloading {url}")
            if response.status_code >= 400:
                raise DownloadError(f"Could not download PDF {url}: HTTP {response.status_code}")
            if not first.startswith(b"%PDF"):
                content_type = response.headers.get("content-type", "")
                snippet = first[:120].decode("utf-8", errors="ignore").replace("\n", " ")
                raise DownloadError(
                    f"URL did not return a PDF. content-type={content_type!r} snippet={snippet!r}"
                )

            with target.open("wb") as fh:
                fh.write(first)
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
    except requests.RequestException as exc:
        raise DownloadError(f"Could not download PDF {url}: {exc}") from exc

    validate_pdf_file(target)


def is_anti_bot_response(response: requests.Response, body: bytes = b"") -> bool:
    headers = {key.lower(): value.lower() for key, value in response.headers.items()}
    server = headers.get("server", "")
    if headers.get("cf-mitigated") == "challenge":
        return True
    if "cloudflare" in server and response.status_code in {403, 429, 503}:
        return True
    text = body[:8192].decode("utf-8", errors="ignore").lower()
    challenge_markers = (
        "cf-challenge",
        "cloudflare",
        "just a moment",
        "attention required",
        "captcha",
        "challenge-platform",
    )
    return response.status_code in {403, 429, 503} and any(marker in text for marker in challenge_markers)


def validate_pdf_file(path: Path) -> None:
    with path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise PaperError(f"File is not a valid PDF: {path}")


def parse_web_metadata(html: str, url: str) -> PaperMetadata:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if not key or not content:
            continue
        meta.setdefault(key.lower(), []).append(clean_spaces(content))

    def first(*keys: str) -> str:
        for key in keys:
            values = meta.get(key.lower())
            if values:
                return values[0]
        return ""

    authors = meta.get("citation_author", []) or meta.get("dc.creator", [])
    pdf_url = first("citation_pdf_url")
    if pdf_url:
        pdf_url = urllib.parse.urljoin(url, pdf_url)

    title = first("citation_title", "dc.title", "og:title", "twitter:title")
    abstract = first("citation_abstract", "dc.description", "description", "og:description")
    year = parse_year(first("citation_publication_date", "citation_online_date", "article:published_time", "dc.date"))
    doi = first("citation_doi", "dc.identifier")
    doi = doi.removeprefix("doi:").strip()

    return PaperMetadata(
        title=title,
        authors=dedupe(authors),
        year=year,
        abstract=abstract,
        doi=doi,
        source_url=url,
        pdf_url=pdf_url,
    )


def fetch_arxiv_api_metadata(arxiv_id: str) -> PaperMetadata:
    clean_id = re.sub(r"v\d+$", "", arxiv_id)
    api_url = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(clean_id)
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return PaperMetadata()
    if response.status_code >= 400:
        return PaperMetadata()

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return PaperMetadata()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        return PaperMetadata()

    def atom_text(name: str) -> str:
        node = entry.find(f"atom:{name}", ns)
        return clean_spaces(node.text or "") if node is not None else ""

    authors = [
        clean_spaces(node.text or "")
        for node in entry.findall("atom:author/atom:name", ns)
        if clean_spaces(node.text or "")
    ]
    return PaperMetadata(
        title=atom_text("title"),
        authors=authors,
        abstract=atom_text("summary"),
        year=parse_year(atom_text("published") or atom_text("updated")),
        provider="arxiv",
        provider_id=arxiv_id,
        source_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def analyze_pdf(
    pdf_path: Path,
    resolution: SourceResolution,
    final_pdf_url: str,
) -> tuple[PaperMetadata, str]:
    pdf_metadata, extracted_text = extract_pdf(pdf_path)
    metadata = merge_metadata(PaperMetadata(), resolution.web_metadata)
    metadata.provider = metadata.provider or resolution.provider
    metadata.provider_id = metadata.provider_id or resolution.provider_id
    metadata.source_url = metadata.source_url or resolution.source_url or resolution.source
    metadata.pdf_url = final_pdf_url or metadata.pdf_url or resolution.pdf_url

    if not useful_title(metadata.title):
        metadata.title = clean_spaces(pdf_metadata.get("title", ""))
    if not useful_title(metadata.title):
        metadata.title = guess_title_from_text(extracted_text)

    if not metadata.authors:
        metadata.authors = split_authors(pdf_metadata.get("author", ""))

    if not metadata.year:
        metadata.year = parse_year(pdf_metadata.get("creationDate", "") or pdf_metadata.get("modDate", ""))
    if not metadata.year:
        metadata.year = parse_year(extracted_text[:8000])

    if not metadata.abstract:
        metadata.abstract = extract_abstract(extracted_text)

    if not metadata.title:
        metadata.title = Path(pdf_path).stem

    return metadata, extracted_text


def extract_pdf(pdf_path: Path) -> tuple[dict[str, str], str]:
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise PaperError(f"Could not open PDF: {pdf_path}: {exc}") from exc

    try:
        if getattr(doc, "needs_pass", False):
            raise PaperError("Encrypted PDFs are not supported.")

        metadata = dict(doc.metadata or {})
        chunks: list[str] = []
        total = 0
        page_count = min(len(doc), MAX_EXTRACTED_PAGES)
        for page_index in range(page_count):
            page = doc.load_page(page_index)
            text = page.get_text("text", sort=True) or ""
            if not text:
                continue
            remaining = MAX_EXTRACTED_CHARS - total
            if remaining <= 0:
                break
            chunks.append(text[:remaining])
            total += min(len(text), remaining)
        extracted = "\n".join(chunks)
    finally:
        doc.close()

    if len(extracted.strip()) < 100:
        raise PaperError(
            "Could not extract enough text from this PDF. It may be scanned; OCR is not included in v1."
        )
    return metadata, extracted


def merge_metadata(base: PaperMetadata, incoming: PaperMetadata) -> PaperMetadata:
    if incoming.title and not base.title:
        base.title = incoming.title
    if incoming.authors and not base.authors:
        base.authors = incoming.authors
    if incoming.year and not base.year:
        base.year = incoming.year
    if incoming.abstract and not base.abstract:
        base.abstract = incoming.abstract
    if incoming.doi and not base.doi:
        base.doi = incoming.doi
    if incoming.provider and not base.provider:
        base.provider = incoming.provider
    if incoming.provider_id and not base.provider_id:
        base.provider_id = incoming.provider_id
    if incoming.source_url and not base.source_url:
        base.source_url = incoming.source_url
    if incoming.pdf_url and not base.pdf_url:
        base.pdf_url = incoming.pdf_url
    return base


def load_category_profiles(con: sqlite3.Connection | None) -> dict[str, list[str]]:
    profiles = {category: list(keywords) for category, keywords in DEFAULT_KEYWORDS.items()}
    for directory in visible_category_dirs():
        profiles.setdefault(directory, [])

    if con is None:
        return profiles

    for row in con.execute("select category, keywords from category_keywords"):
        try:
            keywords = json.loads(row["keywords"])
        except json.JSONDecodeError:
            keywords = []
        profiles.setdefault(row["category"], [])
        profiles[row["category"]] = dedupe(profiles[row["category"]] + list(keywords))
    return profiles


def visible_category_dirs() -> list[str]:
    ignored = {"__pycache__"}
    categories = []
    for path in ROOT.iterdir():
        if not path.is_dir():
            continue
        if path.name.startswith(".") or path.name in ignored:
            continue
        categories.append(path.name)
    return sorted(categories)


def choose_category(
    metadata: PaperMetadata,
    extracted_text: str,
    profiles: dict[str, list[str]],
    con: sqlite3.Connection | None,
    override: str | None,
    dry_run: bool,
) -> tuple[str, list[tuple[str, int]], bool]:
    if override:
        category = normalize_category_name(override)
        if not dry_run:
            (ROOT / category).mkdir(exist_ok=True)
            ensure_category_exists(con, category, [])
        return category, score_categories(metadata, extracted_text, profiles), False

    scores = score_categories(metadata, extracted_text, profiles)
    if category_is_confident(scores):
        return scores[0][0], scores, False

    if not sys.stdin.isatty():
        best = scores[0] if scores else ("none", 0)
        raise PaperError(
            "Could not classify confidently in non-interactive mode. "
            f"Best guess was {best[0]!r} with score {best[1]}. "
            "Re-run with --category CATEGORY."
        )

    print("Could not classify confidently.")
    if metadata.title:
        print(f"Title: {metadata.title}")
    print("Category scores:")
    for category, score in scores[:8]:
        print(f"  {category}: {score}")

    categories = sorted(profiles)
    while True:
        answer = input(
            "Choose category "
            f"[{', '.join(categories)}] or enter a new category name: "
        ).strip()
        if not answer:
            continue
        category = normalize_category_name(answer)
        if category in profiles:
            return category, scores, True

        confirm = input(f"Create new category '{category}'? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            continue

        keywords: list[str] = []
        raw_keywords = input(
            "Optional keywords for future auto-classification, comma-separated: "
        ).strip()
        if raw_keywords:
            keywords = [clean_spaces(part) for part in raw_keywords.split(",") if clean_spaces(part)]

        if not dry_run:
            (ROOT / category).mkdir(exist_ok=True)
            ensure_category_exists(con, category, keywords)
        return category, scores, True


def ensure_category_exists(
    con: sqlite3.Connection | None,
    category: str,
    keywords: list[str],
) -> None:
    if con is None:
        return
    con.execute(
        """
        insert into category_keywords(category, keywords)
        values(?, ?)
        on conflict(category) do update set keywords=excluded.keywords
        """,
        (category, json.dumps(keywords)),
    )
    con.commit()


def score_categories(
    metadata: PaperMetadata,
    extracted_text: str,
    profiles: dict[str, list[str]],
) -> list[tuple[str, int]]:
    segments = [
        (metadata.title, 6),
        (metadata.abstract, 4),
        (" ".join(metadata.authors), 2),
        (extracted_text[:100_000], 1),
    ]
    scores: dict[str, int] = {}
    for category, keywords in profiles.items():
        category_words = category.replace("-", " ")
        terms = dedupe([category_words, category] + keywords)
        score = 0
        for text, weight in segments:
            if not text:
                continue
            lower = text.lower()
            for term in terms:
                count = phrase_count(lower, term.lower())
                if count:
                    score += min(count, 5) * weight
        scores[category] = score
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def phrase_count(text: str, phrase: str) -> int:
    if not phrase:
        return 0
    escaped = re.escape(phrase)
    escaped = escaped.replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
    pattern = re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)
    return len(pattern.findall(text))


def category_is_confident(scores: list[tuple[str, int]]) -> bool:
    if not scores:
        return False
    best_score = scores[0][1]
    second_score = scores[1][1] if len(scores) > 1 else 0
    return best_score >= 8 and best_score >= second_score + 4


def build_destination_path(category: str, metadata: PaperMetadata) -> Path:
    year = metadata.year or "undated"
    author = first_author_slug(metadata.authors)
    title = slugify(metadata.title, max_len=80) or "untitled"
    base = f"{year}_{author}_{title}"
    if metadata.provider and metadata.provider_id:
        base += f"_{safe_filename_id(metadata.provider)}_{safe_filename_id(metadata.provider_id)}"
    filename = base + ".pdf"
    return unique_path(ROOT / category, filename)


def save_pdf(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(exist_ok=True)
    if source.resolve() == destination.resolve():
        return destination
    shutil.copy2(source, destination)
    return destination


def index_paper(
    con: sqlite3.Connection,
    metadata: PaperMetadata,
    extracted_text: str,
    category: str,
    saved_path: Path,
    file_hash: str,
) -> None:
    rel_path = str(saved_path.relative_to(ROOT))
    authors_json = json.dumps(metadata.authors)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    cursor = con.execute(
        """
        insert into papers(
            title, authors, year, abstract, doi, provider, provider_id,
            source_url, pdf_url, category, saved_path, file_sha256, imported_at
        )
        values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metadata.title,
            authors_json,
            metadata.year,
            metadata.abstract,
            metadata.doi,
            metadata.provider,
            metadata.provider_id,
            metadata.source_url,
            metadata.pdf_url,
            category,
            rel_path,
            file_hash,
            now,
        ),
    )
    rowid = cursor.lastrowid
    con.execute(
        """
        insert into papers_fts(
            rowid, title, authors, abstract, body_text, doi, provider,
            provider_id, category, saved_path
        )
        values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rowid,
            metadata.title,
            " ".join(metadata.authors),
            metadata.abstract,
            extracted_text,
            metadata.doi,
            metadata.provider,
            metadata.provider_id,
            category,
            rel_path,
        ),
    )


def find_existing_by_hash(con: sqlite3.Connection, file_hash: str) -> sqlite3.Row | None:
    return con.execute(
        "select saved_path, category from papers where file_sha256 = ? limit 1",
        (file_hash,),
    ).fetchone()


def search_index(con: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    fts_query = build_fts_query(query)
    rows: list[sqlite3.Row] = []
    if fts_query:
        try:
            rows = list(
                con.execute(
                    """
                    select p.*
                    from papers_fts f
                    join papers p on p.id = f.rowid
                    where papers_fts match ?
                    order by bm25(papers_fts)
                    limit ?
                    """,
                    (fts_query, limit),
                )
            )
        except sqlite3.OperationalError:
            rows = []

    if rows:
        return rows

    like = f"%{query}%"
    return list(
        con.execute(
            """
            select *
            from papers
            where title like ?
               or authors like ?
               or abstract like ?
               or doi like ?
               or provider_id like ?
               or category like ?
               or saved_path like ?
            order by imported_at desc
            limit ?
            """,
            (like, like, like, like, like, like, like, limit),
        )
    )


def list_all_papers(con: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = list(con.execute("select * from papers"))
    rows.sort(key=lambda row: natural_sort_key(row["saved_path"] or ""))
    return rows


def natural_sort_key(value: str) -> list[object]:
    parts = re.split(r"([0-9]+)", value.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def build_fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.-]+", query)
    return " ".join(f'"{token}"' for token in tokens[:12])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 10_000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise PaperError(f"Could not create a unique filename in {directory}")


def normalize_category_name(name: str) -> str:
    category = slugify(name, max_len=80)
    if not category:
        raise PaperError("Category name cannot be empty.")
    return category


def slugify(value: str, max_len: int = 120) -> str:
    value = ascii_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    if len(value) > max_len:
        value = value[:max_len].rstrip("-")
    return value


def safe_filename_id(value: str) -> str:
    value = ascii_text(value).lower().replace("/", "-")
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("._-")
    return value[:100] or "unknown"


def ascii_text(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii", "ignore")
    )


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def useful_title(title: str) -> bool:
    cleaned = clean_spaces(title)
    if len(cleaned) < 8:
        return False
    lower = cleaned.lower()
    bad = ("untitled", "microsoft word", ".doc", ".pdf", "arxiv:")
    return not any(token in lower for token in bad)


def guess_title_from_text(text: str) -> str:
    lines = [clean_spaces(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    stop_words = {"abstract", "introduction", "references"}
    skipped_prefixes = (
        "downloaded from",
        "bioRxiv preprint",
        "medRxiv preprint",
        "arxiv:",
        "copyright",
    )

    candidates: list[str] = []
    for line in lines[:80]:
        lower = line.lower()
        if lower in stop_words:
            break
        if any(lower.startswith(prefix.lower()) for prefix in skipped_prefixes):
            continue
        if "@" in line or "doi.org" in lower:
            continue
        if len(line) < 8 or len(line) > 180:
            continue
        candidates.append(line)

    for index, line in enumerate(candidates[:30]):
        words = line.split()
        if not (3 <= len(words) <= 24):
            continue
        title = line
        if index + 1 < len(candidates):
            next_line = candidates[index + 1]
            if should_join_title_line(title, next_line):
                title = f"{title} {next_line}"
        return clean_spaces(title)
    return ""


def should_join_title_line(title: str, next_line: str) -> bool:
    if len(title) + len(next_line) > 180:
        return False
    lower = next_line.lower()
    if "," in next_line or "university" in lower or "department" in lower:
        return False
    if re.search(r"\b\d{4}\b", next_line):
        return False
    return 2 <= len(next_line.split()) <= 14


def split_authors(value: str) -> list[str]:
    value = clean_spaces(value)
    if not value:
        return []
    parts = re.split(r"\s*(?:;|\band\b|,)\s*", value)
    return dedupe(part for part in parts if len(part.strip()) > 1)


def first_author_slug(authors: list[str]) -> str:
    if not authors:
        return "unknown-author"
    author = clean_spaces(authors[0])
    if not author:
        return "unknown-author"
    if "," in author:
        author = author.split(",", 1)[0]
    else:
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", ascii_text(author))
        if tokens:
            author = tokens[-1]
    return slugify(author, max_len=40) or "unknown-author"


def format_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) <= 3:
        return ", ".join(authors)
    return f"{', '.join(authors[:3])}, et al."


def parse_year(value: str) -> str:
    current = dt.datetime.now().year
    for match in re.findall(r"\b(19\d{2}|20\d{2})\b", value or ""):
        year = int(match)
        if 1900 <= year <= current + 1:
            return str(year)
    return ""


def extract_abstract(text: str) -> str:
    match = re.search(
        r"\bAbstract\b\s*(.*?)(?:\n\s*(?:1\s+)?Introduction\b|\n\s*INTRODUCTION\b|\n\s*Keywords?\b)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    abstract = clean_spaces(match.group(1))
    return abstract[:3000]


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = clean_spaces(str(value))
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def print_import_summary(
    metadata: PaperMetadata,
    category: str,
    saved_path: Path,
    score_report: list[tuple[str, int]],
    was_prompted: bool,
    dry_run: bool,
) -> None:
    rel_path = saved_path
    try:
        rel_path = saved_path.relative_to(ROOT)
    except ValueError:
        pass

    prefix = "Dry run complete." if dry_run else "Saved paper."
    print()
    print(prefix)
    print(f"Category: {category}")
    print(f"Path: {rel_path}")
    if metadata.title:
        print(f"Title: {metadata.title}")
    if metadata.provider and metadata.provider_id:
        print(f"Source: {metadata.provider} {metadata.provider_id}")
    if score_report:
        top_scores = ", ".join(f"{category}={score}" for category, score in score_report[:3])
    print(f"Scores: {top_scores}")
    if was_prompted:
        print("Category was selected interactively.")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
