# NCIt Processing Pipeline

`process_ncit.py` builds a queryable database of the NCI Thesaurus (NCIt) along with derived hierarchy tables and mCODE cross-references. It is operational code: run it on a schedule (or on demand) and it will keep a target database current with the latest published NCIt release.

## What it does

On each run the script:

1. **Checks the current NCIt version.** Queries the NCI EVS REST API for the version of a sentinel concept (`C2991`) to determine the latest published release. If the target database already holds that version (per the `ncit_version` table), it exits without doing any work.
2. **Obtains the NCIt flat file.** If `ncit_file_name` is set, reads that local `Thesaurus_<version>.FLAT.zip` archive; otherwise fetches `Thesaurus_<version>.FLAT.zip` from the EVS FTP endpoint. Either way it extracts `Thesaurus.txt` and loads it into the `ncit` table.
3. **Builds the parent/child edge table.** Explodes the pipe-delimited `parents` field into one row per edge in the `parents` table.
4. **Builds the synonyms table.** Explodes the pipe-delimited `synonyms` field into the `synonyms` table, dropping entries that duplicate a concept's preferred name.
5. **Enumerates all hierarchy paths.** Uses a recursive CTE over `parents` to build `ncit_tc_with_path`, which contains every ancestor→descendant path with its level and full pipe-delimited path string.
6. **Builds the transitive closure.** Derives `ncit_tc` (distinct `parent`, `descendant` pairs) from the path table, then adds reflexive (`code`, `code`) rows so SQL-based subsumption queries behave correctly.
7. **Adds mCODE associations from EVS.** Pulls inverse associations for the mCODE subset concept (`C193006`) and the `Has_Target` named association from the EVS API into the `associations` table, then builds a convenience `mcode_links` table mapping NCIt concepts to their mCODE target codes.
8. **Records the version.** Writes the processed version and timestamp to `ncit_version`.

## Requirements

- Python 3.12+ (the script uses f-strings containing quotes, which require 3.12+)
- Python packages: `pandas`, `sqlalchemy`, `requests`, `python-dotenv`, plus a driver for your target database:
  - PostgreSQL: `psycopg` (v3)
  - DuckDB: `duckdb`
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

The version check still runs against EVS, so a local file is only loaded when the target database is behind the current published version.

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

## Output tables

| Table | Contents |
|-------|----------|
| `ncit` | One row per NCIt concept: `code`, `url`, `parents`, `synonyms`, `definition`, `display_name`, `concept_status`, `semantic_type`, `pref_name`. |
| `parents` | Parent→child edges: `concept`, `parent`, `path`, `level`. |
| `synonyms` | One row per synonym (`code`, `synonym`), excluding preferred names. |
| `ncit_tc_with_path` | Every ancestor→descendant path: `parent`, `descendant`, `level`, `path`. Includes reflexive level-0 rows. |
| `ncit_tc` | Transitive closure as distinct (`parent`, `descendant`) pairs, including reflexive rows. |
| `associations` | mCODE-related associations from EVS (`Has_Target` plus inverse associations of `C193006`). |
| `mcode_links` | Convenience mapping of NCIt concepts to mCODE targets, with prefixed `ncit_code` and `full_target_code`. |
| `ncit_version` | Single row recording the loaded NCIt version and processing timestamp. |

## Operational notes

- **Idempotent by version.** Re-running against an up-to-date database is a cheap no-op; it exits after the version check. To force a reload, clear or drop the `ncit_version` table.
- **Full refresh.** Content tables are written with `if_exists='replace'`, so each processed version fully replaces the prior one rather than accumulating.
- **EVS API resilience.** API calls retry with a backoff on HTTP and network errors; after the retry limit is exceeded the script exits rather than writing a partial database.
- **Reflexive rows.** `ncit_tc` and `ncit_tc_with_path` intentionally include `(code, code)` self-rows. They are not part of the strict transitive closure but make SQL subsumption filters (`where parent = :ancestor`) include the ancestor itself.
- **Runtime.** The script prints a total execution time on completion. The bulk of the time is the recursive path enumeration and the EVS association/synonym fetches.