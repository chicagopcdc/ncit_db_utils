# NCIt Processing Pipeline

`process_ncit.py` builds a queryable database of the NCI Thesaurus (NCIt) along with derived hierarchy tables and mCODE cross-references. It is operational code: run it on a schedule (or on demand) and it will keep a target database current with the latest published NCIt release.

It can optionally merge one or more **local addendum files** into the thesaurus. Local concepts go through the same pipeline as NCIt concepts, so they appear in the hierarchy, synonym and closure tables. This is mainly for downstream consumers such as `ncit_tokenizer` that need to recognize terms of local interest, such as COG trial protocol numbers. See [Local addendum files](#local-addendum-files).

## What it does

On each run the script:

1. **Checks whether a rebuild is needed.** Queries the NCI EVS REST API for the version of a sentinel concept (`C2991`) to determine the latest published release. If the target database already holds that version (per the `ncit_version` table) *and* no local addendum file has been modified since the last processing run, it exits without doing any work.
2. **Obtains the NCIt flat file.** If `ncit_file_name` is set, reads that local `Thesaurus_<version>.FLAT.zip` archive; otherwise fetches `Thesaurus_<version>.FLAT.zip` from the EVS FTP endpoint. Either way it extracts `Thesaurus.txt`.
3. **Merges local addenda (optional).** If `local_ncit_files` is set, reads each addendum, validates it against NCIt and the other addenda, and appends its rows. Every row is tagged with a `source` (`NCIt`, or the addendum's file name). The combined set is loaded into the `ncit` table.
4. **Builds the parent/child edge table.** Explodes the pipe-delimited `parents` field into one row per edge in the `parents` table.
5. **Builds the synonyms table.** Explodes the pipe-delimited `synonyms` field into the `synonyms` table, dropping entries that duplicate a concept's preferred name.
6. **Enumerates all hierarchy paths.** Uses a recursive CTE over `parents` to build `ncit_tc_with_path`, which contains every ancestor→descendant path with its level and full pipe-delimited path string.
7. **Builds the transitive closure.** Derives `ncit_tc` (distinct `parent`, `descendant` pairs) from the path table, then adds reflexive (`code`, `code`) rows so SQL-based subsumption queries behave correctly.
8. **Adds mCODE associations from EVS.** Pulls inverse associations for the mCODE subset concept (`C193006`) and the `Has_Target` named association from the EVS API into the `associations` table, then builds a convenience `mcode_links` table mapping NCIt concepts to their mCODE target codes.
9. **Records the version.** Writes the processed version and timestamp to `ncit_version`.

## Requirements

- Python 3.12+ (the script uses f-strings containing quotes, which require 3.12+)
- Python packages: `pandas`, `sqlalchemy`, `requests`, `python-dotenv`, plus a driver for your target database:
  - PostgreSQL: `psycopg` (v3)
  - DuckDB: `duckdb` and `duckdb-engine` (the SQLAlchemy dialect behind `duckdb:///` URLs)
  - SQLite: bundled with Python (`sqlite3`)
- Network access to:
  - `https://api-evsrest.nci.nih.gov` (EVS REST API)
  - `https://evs.nci.nih.gov` (NCIt flat-file FTP endpoint)

## Database targets

The script writes to exactly one of three backends, selected by which environment variable is set. It picks the backend in this order of precedence: `sqlite_file` (SQLite), then `duckdb_file` (DuckDB), then `dbname` (PostgreSQL). If none is set it exits.

Configuration is read from the environment, loaded from a `.env` file in the working directory via [python-dotenv](https://pypi.org/project/python-dotenv/). Copy `.env.example` to `.env` and set the variables for the backend you want, then run:

```bash
python process_ncit.py
```

There are no command-line arguments.

### SQLite

```dotenv
# .env
sqlite_file=ncit.sqlite
```

### DuckDB

```dotenv
# .env
duckdb_file=ncit.duckdb
```

### PostgreSQL

```dotenv
# .env
dbname=ncit
host=db.example.org
port=5432
user=ncit_writer
schema=public
db_password=...
```

### Using a local flat file

By default the script downloads the flat file from EVS. To load from a local archive instead (for offline runs or to pin a specific export), set `ncit_file_name` to a `Thesaurus_<version>.FLAT.zip` file alongside your chosen backend:

```dotenv
# .env
sqlite_file=ncit.sqlite
ncit_file_name=/data/Thesaurus_24.12e.FLAT.zip
```

The version check still runs against EVS, so a local file is only loaded when a rebuild is needed: the target database is behind the current published version, or a local addendum has changed.

### Adding local addenda

```dotenv
# .env
sqlite_file=ncit.sqlite
local_ncit_files=/data/local/cog_trials_ncit.tsv
```

Multiple files are separated by `os.pathsep` (`:` on Linux/macOS, `;` on Windows).

## Configuration

All settings are read from environment variables (typically via a `.env` file).

| Variable | Backend | Description |
|----------|---------|-------------|
| `sqlite_file` | SQLite | Path to a SQLite database file. If set, SQLite is used. |
| `duckdb_file` | DuckDB | Path to a DuckDB database file. |
| `dbname` | PostgreSQL | Database name. |
| `host` | PostgreSQL | Database host. |
| `port` | PostgreSQL | Database port. |
| `user` | PostgreSQL | Database user. |
| `schema` | PostgreSQL | Schema name. |
| `db_password` | PostgreSQL | Database password. |
| `ncit_file_name` | (any) | Optional. Path to a local `Thesaurus_<version>.FLAT.zip` archive. If set, the flat file is read from here instead of downloading it from EVS. |
| `local_ncit_files` | (any) | Optional. One or more local addendum files to merge into NCIt, separated by `os.pathsep`. The script exits up front if any listed file doesn't exist. |

## Local addendum files

### Format

An addendum uses exactly the same layout as `Thesaurus.txt` from the NCIt flat file: tab-delimited, **no header row**, eight columns in this order:

| # | Column | Notes |
|---|--------|-------|
| 1 | `code` | Unique identifier. Use a prefix such as `local:` so it can never collide with NCIt `C` codes. |
| 2 | `url` | May be empty. |
| 3 | `parents` | Pipe-delimited parent codes. Each may be a local code or an NCIt code. Empty for a root. |
| 4 | `synonyms` | Pipe-delimited. **The first entry is the preferred name.** Required on every row. |
| 5 | `definition` | May be empty. |
| 6 | `display_name` | Display name. |
| 7 | `concept_status` | May be empty. |
| 8 | `semantic_type` | Free text, e.g. `Local Code`. |

As with NCIt, `pref_name` is derived from the first synonym; there is no separate preferred-name column. Example rows (tabs shown as `→`):

```
local:0000→→→COG Trials→→COG Trials→→Local Code
local:0003→→local:0000→AALL1732|NCT03959085→A Phase 3 Randomized Trial of ...→AALL1732→→Local Code
```

### Hierarchy

Local concepts can form their own hierarchy, for example a `COG Trials` root with protocols beneath it and sub-protocols beneath those. Because parents may also be NCIt codes, a local root can be attached under an existing NCIt concept by listing that concept's code in its `parents` column. Queries for descendants of that NCIt concept will then include the local concepts.

### Validation

Addenda are checked before anything is written to the database. The run stops and lists every problem found if:

- a row has an empty `synonyms` field
- a code appears more than once across the local files
- a local code already exists in NCIt
- a parent code matches neither a local nor an NCIt concept

Local terms that also appear as NCIt synonyms (compared case-insensitively) produce a warning but don't stop the run. These are worth reviewing, since a tokenizer will find them ambiguous.

### Querying local concepts

The `source` column on `ncit` separates local concepts from NCIt. For example, all terms for local concepts, as a tokenizer vocabulary might need them:

```sql
select code, pref_name as term from ncit where source <> 'NCIt'
union
select s.code, s.synonym from synonyms s
join ncit n on n.code = s.code
where n.source <> 'NCIt';
```

## Output tables

| Table | Contents |
|-------|----------|
| `ncit` | One row per concept: `code`, `url`, `parents`, `synonyms`, `definition`, `display_name`, `concept_status`, `semantic_type`, `pref_name`, `source` (`NCIt` or the addendum's file name). |
| `parents` | Parent→child edges: `concept`, `parent`, `path`, `level`. |
| `synonyms` | One row per synonym (`code`, `synonym`), excluding preferred names. |
| `ncit_tc_with_path` | Every ancestor→descendant path: `parent`, `descendant`, `level`, `path`. Includes reflexive level-0 rows. |
| `ncit_tc` | Transitive closure as distinct (`parent`, `descendant`) pairs, including reflexive rows. |
| `associations` | mCODE-related associations from EVS (`Has_Target` plus inverse associations of `C193006`). NCIt concepts only. |
| `mcode_links` | Convenience mapping of NCIt concepts to mCODE targets, with prefixed `ncit_code` and `full_target_code`. NCIt concepts only. |
| `ncit_version` | Single row recording the loaded NCIt version and processing timestamp. |

## Operational notes

- **Idempotent by version and addenda.** Re-running against an up-to-date database is a cheap no-op; it exits after the version check. A rebuild happens when the NCIt version changes or when any local addendum has a modification time later than the recorded processing timestamp. To force a reload, clear or drop the `ncit_version` table.
- **Removing an addendum.** Dropping a file from `local_ncit_files` doesn't trigger a rebuild by itself, since no file has changed. Its concepts stay in the database until the next rebuild; clear `ncit_version` to remove them right away.
- **Addendum-only changes.** A rebuild always reprocesses the full thesaurus, so editing an addendum still means re-reading NCIt. Pointing `ncit_file_name` at a cached flat file avoids re-downloading it.
- **Full refresh.** Content tables are written with `if_exists='replace'`, so each processed version fully replaces the prior one rather than accumulating.
- **EVS API resilience.** API calls retry with a backoff on HTTP and network errors; after the retry limit is exceeded the script exits rather than writing a partial database.
- **Reflexive rows.** `ncit_tc` and `ncit_tc_with_path` intentionally include `(code, code)` self-rows. They are not part of the strict transitive closure but make SQL subsumption filters (`where parent = :ancestor`) include the ancestor itself.
- **Runtime.** The script prints a total execution time on completion. The bulk of the time is the recursive path enumeration and the EVS association/synonym fetches.