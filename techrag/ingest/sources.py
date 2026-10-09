"""Document folders ("roots"): finding the documents under a folder the user picked, and their buckets.

* The folders directly under the root are the buckets ("PCIe", "Ethernet", "DDR"); everything deeper stays in
  its top-level bucket ("DDR/DDR5/Specification.pdf" -> bucket "DDR", sub-path "DDR5"). Files directly in the
  root go to the explicit general bucket. Bucket names are folder names exactly as shown in Explorer
  (Turkish letters, spaces); a separate ASCII id is used internally. They never come from file names or a
  model guess, and they are collection information only - not evidence of a document's standard/revision.
* The walk never follows symbolic links or junctions out of the root and never loops; a sub-folder that
  cannot be listed is reported (its documents are "unreachable", not deleted).
* Nothing is written into the root: documents are read where they are.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from techrag.domains import fold_tr
from techrag.ingest.loaders import SUPPORTED_SUFFIXES

GENERAL_BUCKET_ID = "_root"
MAX_LIST = 50  # how many unsupported/link/empty entries a summary lists by name


def bucket_id_for(name: str) -> str:
    """Stable internal id for a bucket display name ('Haberleşme Arayüzleri' -> 'haberlesme-arayuzleri-1a2b3c')."""
    if not name:
        return GENERAL_BUCKET_ID
    slug = re.sub(r"[^a-z0-9]+", "-", fold_tr(name)).strip("-")[:40] or "bucket"
    return f"{slug}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"


def split_rel(rel: str) -> tuple[str, str]:
    """Posix path relative to the root -> (bucket display name, sub-path inside the bucket)."""
    if re.match(r"^(?:/|[A-Za-z]:[\\/]|\\\\)", rel or ""):
        return "", ""  # an absolute path (single files indexed from outside the library): general bucket
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if len(parts) <= 1:
        return "", ""
    return parts[0], "/".join(parts[1:-1])


@dataclass
class FoundFile:
    path: str          # absolute path
    rel: str           # posix path relative to the root
    bucket: str        # display name; "" = general bucket (files directly in the root)
    bucket_id: str
    subpath: str       # folder path inside the bucket ("" = directly in the bucket folder)
    size: int
    mtime_ns: int


@dataclass
class ScanResult:
    root: str
    reachable: bool = True
    error: str = ""
    files: list[FoundFile] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    unsupported_count: int = 0
    skipped_links: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)   # folders/files that could not be read
    empty_dirs: list[str] = field(default_factory=list)

    def buckets(self) -> list[dict]:
        out: dict[str, dict] = {}
        for f in self.files:
            b = out.setdefault(f.bucket_id, {"id": f.bucket_id, "name": f.bucket, "documents": 0,
                                             "general": f.bucket_id == GENERAL_BUCKET_ID, "folders": set()})
            b["documents"] += 1
            if f.subpath:
                b["folders"].add(f.subpath)
        res = []
        for b in sorted(out.values(), key=lambda x: (x["general"], x["name"].lower())):
            res.append(dict(b, folders=sorted(b["folders"])[:20]))
        return res

    def summary(self) -> dict:
        return {"root": self.root, "reachable": self.reachable, "error": self.error, "documents": len(self.files),
                "buckets": self.buckets(), "unsupported_count": self.unsupported_count,
                "unsupported": self.unsupported[:MAX_LIST], "skipped_links": self.skipped_links[:MAX_LIST],
                "unreadable": self.unreadable[:MAX_LIST], "empty_dirs": self.empty_dirs[:MAX_LIST]}

    def unreadable_prefixes(self) -> list[str]:
        return [u.rstrip("/") + "/" if u not in ("", ".") else "" for u in self.unreadable]


def _is_link(entry: os.DirEntry) -> bool:
    """Symbolic link, or a Windows junction / other reparse point."""
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)  # Python >= 3.12
        if is_junction is not None and is_junction():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives on Windows
        return False


def scan_root(root: str | Path, max_files: int = 200_000, exclude: tuple = ()) -> ScanResult:
    """Walk a document folder without following links/junctions and without leaving it. `exclude`: folders
    to skip (the library folder itself, when it happens to sit inside the picked folder)."""
    root = str(root)
    res = ScanResult(root=root)
    skip = {os.path.normcase(os.path.realpath(str(x))) for x in exclude}
    try:
        if not os.path.isdir(root):
            res.reachable, res.error = False, "folder not found or not reachable"
            return res
        root_real = os.path.realpath(root)
    except OSError as exc:
        res.reachable, res.error = False, f"{exc.__class__.__name__}: {exc}"
        return res
    visited = {os.path.normcase(root_real)}
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        folder, rel_dir = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as exc:
            if not rel_dir:
                res.reachable, res.error = False, f"{exc.__class__.__name__}: {exc}"
                return res
            res.unreadable.append(rel_dir)
            continue
        if not entries and rel_dir:
            res.empty_dirs.append(rel_dir)
        subdirs = []
        for e in entries:
            if e.name.startswith((".", "~$")) or e.name.lower() in ("thumbs.db", "desktop.ini"):
                continue
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            try:
                link = _is_link(e)
                if e.is_dir(follow_symlinks=False) or (link and os.path.isdir(e.path)):
                    if link:
                        res.skipped_links.append(rel)  # never follow linked folders (loops, other places)
                        continue
                    real = os.path.normcase(os.path.realpath(e.path))
                    if real in skip:
                        continue
                    if not _inside(real, root_real) or real in visited:
                        res.skipped_links.append(rel)
                        continue
                    visited.add(real)
                    subdirs.append((e.path, rel))
                    continue
                if link:
                    real = os.path.realpath(e.path)
                    if not (_inside(real, root_real) and os.path.isfile(real)):
                        res.skipped_links.append(rel)
                        continue
                elif not e.is_file(follow_symlinks=False):
                    continue
                if Path(e.name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    res.unsupported_count += 1
                    if len(res.unsupported) < MAX_LIST:
                        res.unsupported.append(rel)
                    continue
                st = e.stat()
                bucket, sub = split_rel(rel)
                res.files.append(FoundFile(e.path, rel, bucket, bucket_id_for(bucket), sub, st.st_size,
                                           st.st_mtime_ns))
                if len(res.files) >= max_files:
                    res.error = f"stopped after {max_files} files"
                    return res
            except OSError:
                res.unreadable.append(rel)
        stack.extend(reversed(subdirs))
    return res


@dataclass
class PlanItem:
    action: str            # new | changed | settings | moved | duplicate | touched | restored | unchanged |
    #                        missing | unreachable
    rel: str
    bucket: str = ""
    bucket_id: str = ""
    file: Optional[FoundFile] = None
    doc_id: Optional[int] = None
    from_doc: Optional[int] = None   # moved/duplicate: the indexed document whose content is reused
    sha256: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("file", None)
        return d


def plan_changes(scan: ScanResult, docs: list, fingerprint: str, root_id: Optional[int],
                 hash_fn=None, rebuild: bool = False) -> list[PlanItem]:
    """What a sync would do for one root. Without hash_fn (preview) only size/mtime are compared.

    docs: DocumentRow of this root. A changed size/mtime is confirmed by hashing (so a touched but identical
    file costs nothing); a document whose file is gone is 'missing' only when its folder was listed - a
    root or sub-folder that could not be read makes its documents 'unreachable' (kept as they are)."""
    by_rel = {d.rel_path: d for d in docs}
    found = {f.rel for f in scan.files}
    items: list[PlanItem] = []
    if not scan.reachable:
        return [PlanItem("unreachable", d.rel_path, d.bucket, d.bucket_id, doc_id=d.id, reason=scan.error)
                for d in docs]
    gone = [d for d in docs if d.rel_path not in found]
    prefixes = scan.unreadable_prefixes()
    gone_by_sha: dict[str, list] = {}
    for d in gone:
        gone_by_sha.setdefault(d.sha256, []).append(d)
    all_by_sha: dict[str, object] = {d.sha256: d for d in docs if not d.missing}
    reused: set[int] = set()
    for f in scan.files:
        d = by_rel.get(f.rel)
        it = PlanItem("unchanged", f.rel, f.bucket, f.bucket_id, f, d.id if d else None)
        if d is None:
            it.action = "new"
            if hash_fn:
                it.sha256 = hash_fn(f.path)
                prev = next((g for g in gone_by_sha.get(it.sha256, []) if g.id not in reused), None)
                if prev is not None and _fp_ok(prev, fingerprint) and not rebuild:
                    it.action, it.from_doc = "moved", prev.id
                    reused.add(prev.id)
                elif it.sha256 in all_by_sha and _fp_ok(all_by_sha[it.sha256], fingerprint) and not rebuild:
                    it.action, it.from_doc = "duplicate", all_by_sha[it.sha256].id
            items.append(it)
            continue
        same_stat = d.file_size == f.size and d.file_mtime == f.mtime_ns
        if rebuild:
            it.action, it.reason = "changed", "full re-index requested"
        elif not same_stat:
            if hash_fn:
                it.sha256 = hash_fn(f.path)
                it.action = "touched" if it.sha256 == d.sha256 else "changed"
            else:
                it.action = "changed"   # preview: may turn out identical when hashed
        if it.action in ("unchanged", "touched") and not _fp_ok(d, fingerprint):
            it.action, it.reason = "settings", "parsing / table-extraction settings changed"
        elif it.action == "unchanged" and d.missing:
            it.action = "restored"
        items.append(it)
    for d in gone:
        if d.id in reused:
            continue
        if any(p == "" or d.rel_path.startswith(p) for p in prefixes):
            items.append(PlanItem("unreachable", d.rel_path, d.bucket, d.bucket_id, doc_id=d.id,
                                  reason="its folder could not be read"))
        else:
            items.append(PlanItem("missing", d.rel_path, d.bucket, d.bucket_id, doc_id=d.id,
                                  reason="file not found in the folder"))
    return items


def _fp_ok(doc, fingerprint: str) -> bool:
    # Documents indexed before fingerprints existed (0.2.x) carry none: treat them as current.
    return not getattr(doc, "ingest_fp", "") or doc.ingest_fp == fingerprint


def plan_summary(items: list[PlanItem]) -> dict:
    counts: dict[str, int] = {}
    for it in items:
        counts[it.action] = counts.get(it.action, 0) + 1
    work = {"new", "changed", "settings"}
    return {"counts": counts, "to_process": sum(counts.get(a, 0) for a in work),
            "reused": counts.get("moved", 0) + counts.get("duplicate", 0),
            "missing": [it.rel for it in items if it.action == "missing"][:MAX_LIST],
            "unreachable": [it.rel for it in items if it.action == "unreachable"][:MAX_LIST]}
