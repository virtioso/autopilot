"""
Robot Framework adapter: RobotFrameworkOracle and rf_xml output parser.

RobotFrameworkOracle runs `robot` (or a custom RF executable) as a subprocess,
waits for it to finish, parses output.xml for pass/fail verdict, and writes a
machine-readable verdict.json alongside the results.

This adapter wraps RunProcessOracle (process.py) for the subprocess management
and adds the RF-specific output.xml parsing.

The old system ran RF via a generic RunCommand step and then polled result files
with artifact_grep. The new design runs RF and reads structured results directly
— no polling, no regex on log output, no race with file writes.

Verdict mapping:
  - All suites PASS → Matched("rf_pass")
  - Any suite FAIL  → Matched("rf_fail")
  - Robot exits non-zero without output.xml → Error("rf_no_output")
  - Parse error on output.xml → Error("rf_parse_error:<reason>")

The oracle returns either Matched("rf_pass") or Matched("rf_fail") — both are
"matched" verdicts so the enclosing Sequence continues. A Sequence that should
stop on RF failure should use Choice to route on the label:
    Choice([..., case("rf_fail", fail_oracle), case("rf_pass", next_oracle)])
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from xml.etree import ElementTree

import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# RF output.xml parser
# ---------------------------------------------------------------------------

def parse_rf_output(output_xml: Path) -> tuple[str, dict]:
    """
    Parse a Robot Framework output.xml and return (verdict_label, summary_dict).

    verdict_label: "rf_pass" or "rf_fail"
    summary_dict:  machine-readable summary suitable for verdict.json

    Only the top-level <suite> status is checked — nested suite failures roll up.
    """
    try:
        tree = ElementTree.parse(output_xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"xml_parse_error: {exc}") from exc

    root = tree.getroot()
    if root.tag != "robot":
        raise ValueError(f"unexpected root tag: {root.tag!r}")

    stats_elem = root.find("statistics/total/stat")
    passed = failed = 0
    if stats_elem is not None:
        passed = int(stats_elem.get("pass", 0))
        failed = int(stats_elem.get("fail", 0))

    suite_elem = root.find("suite")
    suite_status = "FAIL"
    if suite_elem is not None:
        status_elem = suite_elem.find("status")
        if status_elem is not None:
            suite_status = status_elem.get("status", "FAIL")

    verdict_label = "rf_pass" if suite_status == "PASS" else "rf_fail"

    summary = {
        "verdict": verdict_label,
        "suite_status": suite_status,
        "tests_passed": passed,
        "tests_failed": failed,
        "output_xml": str(output_xml),
    }
    return verdict_label, summary


# ---------------------------------------------------------------------------
# RobotFrameworkOracle
# ---------------------------------------------------------------------------

class RobotFrameworkOracle:
    """
    Run Robot Framework and return a structured pass/fail verdict.

    Args:
        suite: path to the .robot file or directory of suites.
        outputdir: directory for RF results (output.xml, log.html, report.html).
            Defaults to "results/rf/<suite stem>" relative to cwd.
        robot_cmd: path to the `robot` executable (default: "robot").
        variables: dict of RF variables (--variable NAME:VALUE).
        extra_args: additional `robot` CLI arguments.
        verdict_key: ctx.metadata key to store the verdict summary dict.
        env_extra: extra environment variables for the RF process.
        cwd: working directory for the RF process.

    Returns Matched("rf_pass") or Matched("rf_fail") so that downstream oracles
    can route on the label. Both are Matched (not Error) — the test verdict is
    known. Error is returned only when RF fails to start or produce output.xml.
    """

    def __init__(
        self,
        suite: str | Path,
        *,
        outputdir: str | Path | None = None,
        robot_cmd: str = "robot",
        variables: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        verdict_key: str = "rf_verdict",
        env_extra: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self._suite = Path(suite)
        self._outputdir = Path(outputdir) if outputdir else None
        self._robot_cmd = robot_cmd
        self._variables = variables or {}
        self._extra_args = extra_args or []
        self._verdict_key = verdict_key
        self._env_extra = env_extra or {}
        self._cwd = Path(cwd) if cwd else None

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        import os as _os

        outputdir = self._outputdir or (
            Path("results") / "rf" / self._suite.stem
        )
        outputdir.mkdir(parents=True, exist_ok=True)

        cmd = [self._robot_cmd]
        for name, value in self._variables.items():
            cmd += ["--variable", f"{name}:{value}"]
        cmd += ["--outputdir", str(outputdir)]
        cmd += self._extra_args
        cmd += [str(self._suite)]

        env = {**_os.environ, **self._env_extra}
        cwd = str(self._cwd) if self._cwd else None

        log.info("rf.starting", suite=str(self._suite), outputdir=str(outputdir))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            log.warning("rf.timeout", suite=str(self._suite))
            return Error("rf_timeout"), ctx
        except OSError as exc:
            return Error(f"rf_spawn_failed: {exc}"), ctx

        exit_code = proc.returncode
        log.info("rf.finished", suite=str(self._suite), exit_code=exit_code)

        output_xml = outputdir / "output.xml"
        if not output_xml.exists():
            log.warning("rf.no_output_xml", path=str(output_xml))
            return Error("rf_no_output"), ctx

        try:
            verdict_label, summary = parse_rf_output(output_xml)
        except ValueError as exc:
            return Error(f"rf_parse_error:{exc}"), ctx

        # Write verdict.json alongside the RF results
        verdict_json = outputdir / "verdict.json"
        try:
            verdict_json.write_text(json.dumps(summary, indent=2) + "\n")
        except OSError as exc:
            log.warning("rf.verdict_json_write_failed", error=repr(exc))

        ctx.metadata[self._verdict_key] = summary

        log.info(
            "rf.verdict",
            verdict=verdict_label,
            passed=summary["tests_passed"],
            failed=summary["tests_failed"],
        )
        return Matched(verdict_label), ctx
