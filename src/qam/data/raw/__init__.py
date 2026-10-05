"""Raw archive: immutable, as-received data with integrity manifests (DESIGN.md §11.2).

Layout under the archive root::

    raw/<venue>/<stream>/<YYYY-MM-DD>/<venue>.<stream>.<YYYYMMDDTHH>.<start_ns>.jsonl.zst[.part]
    manifest/<venue>/<YYYY-MM-DD>.jsonl

Each data file is a sequence of zstd frames containing JSON lines (one envelope per line,
see ``envelope.py``). Files are written as ``.part`` and renamed once closed; only closed files
appear in the manifest, and closed files are never modified.
"""

from qam.data.raw.envelope import ENVELOPE_VERSION, make_message, make_meta
from qam.data.raw.reader import iter_file, iter_records, verify_archive
from qam.data.raw.writer import RawArchive, RawStreamWriter

__all__ = [
    "ENVELOPE_VERSION",
    "RawArchive",
    "RawStreamWriter",
    "iter_file",
    "iter_records",
    "make_message",
    "make_meta",
    "verify_archive",
]
