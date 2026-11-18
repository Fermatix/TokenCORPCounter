## Quick start

```bash
make count path="/path/to/files"
```

The first run creates `.venv` and installs the dependencies (Python 3.10+). The report is written to `token_report.xlsx` in the current folder; override it with `out=`, pass extra flags with `args=`:

```bash
make count path="/path/to/files" out="/path/to/report.csv" args="--base64 --exclude '*.log'"
make test
```

## Running directly

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python token_counter --path "/path/to/files" --out "/path/to/report.xlsx"
```

Quote paths that contain spaces or non-ASCII characters. The report format is taken from the `--out` extension.

| Flag | What it does |
|---|---|
| `--path` | folder (walked recursively) or a single file |
| `--out` | report: `.xlsx`, `.csv`, `.json`, `.md`; a folder means `token_report.xlsx` inside it; without the flag — console only |
| `--image-detail high\|low` | image token mode, `high` by default |
| `--base64` | add a `base64_tokens` column: each whole file as base64, as in a raw API request; not included in the clean or full totals |
| `--exclude GLOB` | skip files and folders matching a glob, repeatable |
| `--workers N` | number of threads, up to 8 by default |
| `--max-member-mb N` | do not extract archive members larger than N MB, 2048 by default |
| `--top N` | how many of the heaviest files and sources to show in the console |

## Report

The `.xlsx` report has three sheets:

1. `sources` — totals per source. A source is each folder or file directly inside `--path`; an archive in the root counts as one source with everything inside it.
2. `files` — every file, including archive members (`archive.zip!/dir/file.txt`) and e-mail attachments.
3. `categories` — totals per file type: text, pdf, image, media and so on.

`.json` and `.md` contain the same three tables; `.csv` holds a single table, so it gets the files.

| Column | Meaning |
|---|---|
| `clean_tokens` | main content without export service fields or markup; excludes attachments |
| `dirty_tokens` | full main text, including service fields and markup; excludes attachments |
| `clean_attachment_text_tokens` | clean text extracted from attachment files |
| `attachment_text_tokens` | full text extracted from attachment files |
| `image_tokens` | estimated vision tokens of standalone and embedded images |
| `attachment_tokens` | `attachment_text_tokens + image_tokens` |
| `clean_with_attachments` | `clean_tokens + clean_attachment_text_tokens + image_tokens` |
| `dirty_with_attachments` | `dirty_tokens + attachment_tokens`, including duplicates |
| `unique_clean_with_attachments` | clean total after excluding byte-identical copies within each source, content type and main/attachment role |
| `duplicate_tokens` | full tokens of those copies, already included in the full total; do not add again |
| `duplicate_of` | path of the first identical file; available on the `files` sheet |
| `errors` | files whose contents could not be read |
| `clean_errors` | files with a full count but a failed clean-content extraction |
| `not_counted` | all unsupported, skipped or unreadable files, including `errors` |

The summaries also include `attachment_files` and `duplicate_files`; the files table has an `error` flag. Summary columns `text_tokens` and `total_tokens` are compatibility totals: all extracted text including attachments, and `dirty_with_attachments`, respectively. Column definitions are also in XLSX header comments, JSON `definitions` and the Markdown report.

### What clean content means

The full count of JSON/JSONL and other text files is their verbatim decoded text. For documents it is extracted text, tables, notes and metadata, rather than their binary bytes or internal XML. `--base64` remains a separate transport estimate and is never added to either total.

Clean extraction is a heuristic, not an estimate of a deduplicated knowledge base:

- JSON/JSONL is parsed as a stream. Object keys, identifiers, timestamps, API link fields, author metadata, statistics and attachment descriptors are excluded; message bodies, descriptions, titles and business values remain. HTML in text fields is removed. URLs quoted in messages remain. For paths containing a `slack` directory, each message uses `text` when present and falls back to rich-text `blocks`, to avoid counting both representations.
- Markdown loses front matter, common export metadata lines and formatting. Link labels and code content remain. HTML/XML loses markup and technical branches. Plain text is kept verbatim; `.state` directories and `.log`, `.lock`, `.done` export artifacts contribute only to the full count.
- CSV/TSV and Excel retain business values and remove service columns identified from the first non-empty row (the first row for CSV/TSV). This includes columns named `id`, `created_at` and similar; identifiers quoted in an actual message remain. CSV comma, semicolon and tab separators are recognized.
- Documents and PDF retain their visible text while document metadata contributes only to the full count.

Attachment directories and archives named `attachments`/`attachment` (including names such as `attachments-2026.zip`), e-mail attachments, and their nested contents are marked as attachments. Standalone images, media and opaque binaries are also attachments; other documents are main content. Classification is visible in `is_attachment`. Only local files and embedded images are counted; attachment URLs are never fetched. Images contribute vision tokens once; OCR is not performed.

All rows remain in the report. Full and ordinary clean totals include repeated files, so they describe processing the entire input. The unique clean column removes exact file copies only. JSON and Markdown exports of the same messages, repeated records and similar document versions can still overlap; semantic deduplication is not performed.
