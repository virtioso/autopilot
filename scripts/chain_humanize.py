#!/usr/bin/env python3
"""Produce human-readable chain artifacts with inline console snippets.

Default mode scans a results directory for chain*.json files and writes
*.human.json siblings.
"""

import argparse
import copy
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class FileIssue:
    kind: str
    message: str


@dataclass
class FileResult:
    input_path: Path
    output_path: Path
    ok: bool
    issues: List[FileIssue]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inline console snippets into chain JSON artifacts",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--result-dir",
        type=Path,
        help="Result directory containing chain*.json artifacts",
    )
    mode.add_argument(
        "--chain-file",
        type=Path,
        help="Single chain JSON file to transform",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write transformed files under this directory (defaults to sibling files)",
    )
    parser.add_argument(
        "--fallback-context-bytes",
        type=int,
        default=256,
        help="Context bytes on each side of log_offset when source_ranges are missing",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Text encoding for snippet decode (default: utf-8)",
    )
    return parser.parse_args()


def _humanized_name(path: Path) -> str:
    if path.suffix == ".json":
        return f"{path.stem}.human.json"
    return f"{path.name}.human.json"


def _collect_chain_files(result_dir: Path) -> List[Path]:
    if not result_dir.exists() or not result_dir.is_dir():
        raise FileNotFoundError(f"result dir not found: {result_dir}")
    files: List[Path] = []
    for p in sorted(result_dir.glob("chain*.json")):
        if p.name.endswith(".human.json"):
            continue
        files.append(p)
    if not files:
        raise FileNotFoundError(f"no chain*.json files found in {result_dir}")
    return files


def _find_result_root(chain_path: Path) -> Optional[Path]:
    for parent in [chain_path.parent, *chain_path.parents]:
        if (parent / "console").exists():
            return parent
    return None


def _resolve_log_path(
    raw_log_path: Optional[str],
    chain_path: Path,
    result_root: Optional[Path],
    source: Optional[str],
) -> Tuple[Optional[Path], str]:
    candidates: List[Path] = []

    if raw_log_path:
        p = Path(raw_log_path)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(chain_path.parent / p)
            if result_root is not None:
                candidates.append(result_root / p)

    if source:
        default_rel = Path("console") / f"{source}.jsonl"
        candidates.append(chain_path.parent / default_rel)
        if result_root is not None:
            candidates.append(result_root / default_rel)

    seen = set()
    unique_candidates: List[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(c)

    for c in unique_candidates:
        if c.exists() and c.is_file():
            return c, ""

    if unique_candidates:
        return None, "log file not found"
    return None, "no log path candidates"


def _decode_bytes(blob: bytes, encoding: str) -> Tuple[str, Dict[str, int]]:
    text = blob.decode(encoding, errors="replace")
    replacements = text.count("\ufffd")
    return text, {
        "bytes": len(blob),
        "replacement_chars": replacements,
    }


def _read_range(
    log_path: Path,
    start_offset: int,
    end_offset: int,
) -> bytes:
    if start_offset < 0 or end_offset < 0:
        raise ValueError("offsets must be non-negative")
    if end_offset < start_offset:
        raise ValueError("end_offset must be >= start_offset")

    size = log_path.stat().st_size
    if start_offset > size:
        raise ValueError(f"start_offset beyond file size ({size})")

    end = min(end_offset, size)
    count = max(0, end - start_offset)
    with log_path.open("rb") as f:
        f.seek(start_offset)
        return f.read(count)


def _make_source_snippet(
    *,
    chain_path: Path,
    result_root: Optional[Path],
    source: str,
    range_data: dict,
    encoding: str,
) -> dict:
    entry: dict = {
        "log_path": range_data.get("log_path"),
        "start_offset": range_data.get("start_offset"),
        "end_offset": range_data.get("end_offset"),
        "bytes": range_data.get("bytes"),
    }

    try:
        start = int(range_data.get("start_offset"))
        end = int(range_data.get("end_offset"))
    except Exception:
        entry["error"] = "invalid start_offset/end_offset"
        return entry

    log_path, err = _resolve_log_path(
        raw_log_path=range_data.get("log_path"),
        chain_path=chain_path,
        result_root=result_root,
        source=source,
    )
    if not log_path:
        entry["error"] = err
        return entry

    try:
        blob = _read_range(log_path, start, end)
    except Exception as exc:
        entry["error"] = str(exc)
        return entry

    text, decode_meta = _decode_bytes(blob, encoding)
    entry["resolved_log_path"] = str(log_path)
    entry["text"] = text
    entry["decode"] = {
        "encoding": encoding,
        **decode_meta,
    }
    entry["bytes"] = len(blob)
    return entry


def _make_match_snippet(
    *,
    chain_path: Path,
    result_root: Optional[Path],
    step: dict,
    fallback_context_bytes: int,
    encoding: str,
) -> Optional[dict]:
    source = step.get("source")
    log_offset = step.get("log_offset")
    if not source or log_offset is None:
        return None

    try:
        offset = int(log_offset)
    except Exception:
        return {
            "mode": "fallback_log_offset_window",
            "source": source,
            "log_offset": log_offset,
            "error": "invalid log_offset",
        }

    log_path, err = _resolve_log_path(
        raw_log_path=step.get("log_path"),
        chain_path=chain_path,
        result_root=result_root,
        source=source,
    )
    if not log_path:
        return {
            "mode": "fallback_log_offset_window",
            "source": source,
            "log_offset": offset,
            "error": err,
        }

    try:
        size = log_path.stat().st_size
        start = max(0, offset - fallback_context_bytes)
        end = min(size, offset + fallback_context_bytes)
        blob = _read_range(log_path, start, end)
    except Exception as exc:
        return {
            "mode": "fallback_log_offset_window",
            "source": source,
            "log_offset": offset,
            "resolved_log_path": str(log_path),
            "error": str(exc),
        }

    text, decode_meta = _decode_bytes(blob, encoding)
    return {
        "mode": "fallback_log_offset_window",
        "source": source,
        "log_offset": offset,
        "resolved_log_path": str(log_path),
        "window_start": start,
        "window_end": end,
        "text": text,
        "decode": {
            "encoding": encoding,
            **decode_meta,
        },
    }


def _transform_chain(
    chain: dict,
    chain_path: Path,
    fallback_context_bytes: int,
    encoding: str,
) -> Tuple[dict, List[FileIssue]]:
    transformed = copy.deepcopy(chain)
    issues: List[FileIssue] = []
    result_root = _find_result_root(chain_path)

    steps = transformed.get("steps")
    if not isinstance(steps, list):
        issues.append(FileIssue("schema", "steps is not a list"))
        transformed.setdefault("humanize_meta", {})
        transformed["humanize_meta"].update(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source_file": str(chain_path),
                "issues": [issue.__dict__ for issue in issues],
            }
        )
        return transformed, issues

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            issues.append(FileIssue("schema", f"step[{idx}] is not an object"))
            continue

        source_ranges = step.get("source_ranges")
        if isinstance(source_ranges, dict) and source_ranges:
            snippets = {}
            for source, range_data in source_ranges.items():
                if not isinstance(range_data, dict):
                    snippets[str(source)] = {
                        "error": "source range entry is not an object",
                    }
                    issues.append(FileIssue("range", f"step[{idx}] source_ranges[{source}] invalid"))
                    continue
                snippets[str(source)] = _make_source_snippet(
                    chain_path=chain_path,
                    result_root=result_root,
                    source=str(source),
                    range_data=range_data,
                    encoding=encoding,
                )
            step["source_snippets"] = snippets
            continue

        fallback = _make_match_snippet(
            chain_path=chain_path,
            result_root=result_root,
            step=step,
            fallback_context_bytes=fallback_context_bytes,
            encoding=encoding,
        )
        if fallback is not None:
            step["match_snippet"] = fallback

    transformed.setdefault("humanize_meta", {})
    transformed["humanize_meta"].update(
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_file": str(chain_path),
            "fallback_context_bytes": fallback_context_bytes,
            "issues": [issue.__dict__ for issue in issues],
        }
    )
    return transformed, issues


def _process_file(
    chain_path: Path,
    output_dir: Optional[Path],
    fallback_context_bytes: int,
    encoding: str,
) -> FileResult:
    issues: List[FileIssue] = []
    try:
        chain = json.loads(chain_path.read_text())
    except Exception as exc:
        return FileResult(
            input_path=chain_path,
            output_path=Path(""),
            ok=False,
            issues=[FileIssue("parse", str(exc))],
        )

    transformed, transform_issues = _transform_chain(
        chain=chain,
        chain_path=chain_path,
        fallback_context_bytes=fallback_context_bytes,
        encoding=encoding,
    )
    issues.extend(transform_issues)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / _humanized_name(chain_path)
    else:
        output_path = chain_path.with_name(_humanized_name(chain_path))

    try:
        output_path.write_text(json.dumps(transformed, indent=2))
    except Exception as exc:
        issues.append(FileIssue("write", str(exc)))
        return FileResult(
            input_path=chain_path,
            output_path=output_path,
            ok=False,
            issues=issues,
        )

    return FileResult(
        input_path=chain_path,
        output_path=output_path,
        ok=True,
        issues=issues,
    )


def main() -> int:
    args = _parse_args()

    if args.fallback_context_bytes < 0:
        print("error: --fallback-context-bytes must be >= 0", file=sys.stderr)
        return 1

    try:
        if args.chain_file:
            chain_files = [args.chain_file]
        else:
            chain_files = _collect_chain_files(args.result_dir)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results: List[FileResult] = []
    for chain_path in chain_files:
        result = _process_file(
            chain_path=chain_path,
            output_dir=args.output_dir,
            fallback_context_bytes=args.fallback_context_bytes,
            encoding=args.encoding,
        )
        results.append(result)

    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count
    issue_count = sum(len(r.issues) for r in results)

    for r in results:
        if r.ok:
            print(f"ok: {r.input_path} -> {r.output_path}")
        else:
            print(f"fail: {r.input_path}", file=sys.stderr)
        for issue in r.issues:
            stream = sys.stderr if not r.ok else sys.stdout
            print(f"  issue[{issue.kind}]: {issue.message}", file=stream)

    print(
        f"summary: total={len(results)} ok={ok_count} failed={fail_count} issues={issue_count}",
        file=sys.stderr if fail_count else sys.stdout,
    )
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
