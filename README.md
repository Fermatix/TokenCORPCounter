# Token counter

Counts `o200k_base` tokens (GPT-4o / GPT-4.1 / o-series) for every file in a folder: text, JSON/JSONL, spreadsheets, documents, PDF, images, archives and e-mail.

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
| `--base64` | add a `base64_tokens` column: each whole file as base64, as in a raw API request; not included in `total_tokens` |
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

Audio, video, RAR and files that could not be parsed are listed with zero tokens and an explanation in `note`: they carry no text, so they stay out of the totals. The `not_counted` column of the summaries shows how many such files each source has.
