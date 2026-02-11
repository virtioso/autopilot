#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

START_RE = re.compile(r"DTB_DUMP_START\s+size=(\d+)")
END_RE = re.compile(r"DTB_DUMP_END")
B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")


@dataclass
class DumpRecord:
    source_file: Path
    index: int
    size_hint: int
    dtb_path: Optional[Path]
    dts_path: Optional[Path]
    status: str
    error: Optional[str]


@dataclass
class ExtractionSummary:
    result_dir: Path
    output_dir: Path
    dtc_available: bool
    scanned_files: List[Path]
    records: List[DumpRecord]

    def to_json(self) -> dict:
        return {
            "result_dir": str(self.result_dir),
            "output_dir": str(self.output_dir),
            "dtc_available": self.dtc_available,
            "scanned_files": [str(p) for p in self.scanned_files],
            "records": [
                {
                    "source_file": str(r.source_file),
                    "index": r.index,
                    "size_hint": r.size_hint,
                    "dtb_path": str(r.dtb_path) if r.dtb_path else None,
                    "dts_path": str(r.dts_path) if r.dts_path else None,
                    "status": r.status,
                    "error": r.error,
                }
                for r in self.records
            ],
            "generated_dtb": sum(1 for r in self.records if r.dtb_path),
            "generated_dts": sum(1 for r in self.records if r.dts_path),
            "issues": [
                {
                    "source_file": str(r.source_file),
                    "index": r.index,
                    "status": r.status,
                    "error": r.error,
                }
                for r in self.records
                if r.status != "ok"
            ],
        }


def iter_console_logs(console_dir: Path) -> List[Path]:
    candidates: List[Path] = []
    if not console_dir.exists():
        return candidates

    for pattern in ("*.raw", "*.log", "*.jsonl"):
        candidates.extend(sorted(console_dir.glob(pattern)))

    # Also include filtered logs produced by analyze steps.
    for extra in ("sel4.log", "vm.log"):
        path = console_dir / extra
        if path.exists() and path not in candidates:
            candidates.append(path)

    return candidates


def load_log_text(path: Path) -> str:
    if path.suffix != ".jsonl":
        return path.read_text(errors="ignore")

    chunks: List[str] = []
    with path.open("r", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                chunks.append(line)
                continue
            data = event.get("data")
            if isinstance(data, str):
                chunks.append(data)
            else:
                chunks.append(line)
    return "".join(chunks)


def extract_payloads(text: str) -> List[tuple[int, str]]:
    payloads: List[tuple[int, str]] = []
    in_dump = False
    expected_size = 0
    chunks: List[str] = []

    for line in text.splitlines():
        if not in_dump:
            start = START_RE.search(line)
            if not start:
                continue
            in_dump = True
            expected_size = int(start.group(1))
            chunks = []
            continue

        if END_RE.search(line):
            payloads.append((expected_size, "".join(chunks)))
            in_dump = False
            expected_size = 0
            chunks = []
            continue

        tokens = B64_TOKEN_RE.findall(line)
        if not tokens:
            continue
        # Pick the longest base64-like token to avoid prefixes such as logger tags.
        chunks.append(max(tokens, key=len))

    return payloads


def convert_dtb_to_dts(dtb_path: Path, dts_path: Path) -> None:
    subprocess.run(
        ["dtc", "-I", "dtb", "-O", "dts", str(dtb_path), "-o", str(dts_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def extract_guest_dtbs(result_dir: Path) -> ExtractionSummary:
    console_dir = result_dir / "console"
    output_dir = result_dir / "device-trees"
    output_dir.mkdir(parents=True, exist_ok=True)

    log_files = iter_console_logs(console_dir)
    dtc_available = shutil.which("dtc") is not None
    records: List[DumpRecord] = []
    seen_hashes = set()

    seq = 0
    for source_file in log_files:
        try:
            text = load_log_text(source_file)
        except Exception as exc:
            records.append(
                DumpRecord(
                    source_file=source_file,
                    index=-1,
                    size_hint=0,
                    dtb_path=None,
                    dts_path=None,
                    status="read_error",
                    error=str(exc),
                )
            )
            continue

        payloads = extract_payloads(text)
        for size_hint, payload in payloads:
            seq += 1
            stem = f"dtb-{seq:02d}-{source_file.stem}"
            dtb_path = output_dir / f"{stem}.dtb"
            dts_path = output_dir / f"{stem}.dts"
            try:
                blob = base64.b64decode(payload, validate=False)
                if size_hint > 0 and len(blob) >= size_hint:
                    blob = blob[:size_hint]
                blob_hash = hashlib.sha256(blob).hexdigest()
                if blob_hash in seen_hashes:
                    continue
                seen_hashes.add(blob_hash)
                dtb_path.write_bytes(blob)
            except Exception as exc:
                records.append(
                    DumpRecord(
                        source_file=source_file,
                        index=seq,
                        size_hint=size_hint,
                        dtb_path=None,
                        dts_path=None,
                        status="decode_error",
                        error=str(exc),
                    )
                )
                continue

            if not dtc_available:
                records.append(
                    DumpRecord(
                        source_file=source_file,
                        index=seq,
                        size_hint=size_hint,
                        dtb_path=dtb_path,
                        dts_path=None,
                        status="dtc_missing",
                        error="dtc not found on PATH",
                    )
                )
                continue

            try:
                convert_dtb_to_dts(dtb_path, dts_path)
                records.append(
                    DumpRecord(
                        source_file=source_file,
                        index=seq,
                        size_hint=size_hint,
                        dtb_path=dtb_path,
                        dts_path=dts_path,
                        status="ok",
                        error=None,
                    )
                )
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or "").strip() or str(exc)
                records.append(
                    DumpRecord(
                        source_file=source_file,
                        index=seq,
                        size_hint=size_hint,
                        dtb_path=dtb_path,
                        dts_path=None,
                        status="dtc_error",
                        error=err,
                    )
                )

    summary = ExtractionSummary(
        result_dir=result_dir,
        output_dir=output_dir,
        dtc_available=dtc_available,
        scanned_files=log_files,
        records=records,
    )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary.to_json(), indent=2))

    return summary


def _print_human_summary(summary: ExtractionSummary) -> None:
    generated_dtb = sum(1 for r in summary.records if r.dtb_path)
    generated_dts = sum(1 for r in summary.records if r.dts_path)
    issues = [r for r in summary.records if r.status != "ok"]

    print(
        f"DTB extraction: scanned={len(summary.scanned_files)} dumps={len(summary.records)} "
        f"dtb={generated_dtb} dts={generated_dts} issues={len(issues)}"
    )
    if issues:
        for issue in issues:
            print(
                f"  - {issue.status}: {issue.source_file.name}#{issue.index} {issue.error or ''}".rstrip()
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract guest DTBs from autopilot logs and emit DTS files")
    parser.add_argument("result_dir", type=Path, help="Autopilot results/<request_id> directory")
    args = parser.parse_args()

    summary = extract_guest_dtbs(args.result_dir)
    _print_human_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
