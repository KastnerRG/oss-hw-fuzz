#!/usr/bin/env python3
"""Probe the bundled PreSiFuzz binary, including controlled target ablations.

Run inside the unified image; all generated targets and evidence stay in OUTPUT.
The synthetic targets test the fuzzer plumbing, not AES correctness.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from contextlib import suppress


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def stop_group(process):
    """Reap a control fuzzer and terminate descendants even on interruption."""
    handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=Path(
        "/opt/fuzzers/presifuzz/fuzzers/opentitan-fuzzer-verilator-hw-cov"))
    parser.add_argument("--expect-fixed", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    binary = args.root / "target/debug/opentitan-fuzzer"
    target = args.root / "build/Vaes_tb"
    evidence = {"root": str(args.root), "hardware_replays": [], "controls": {}}

    for label, data in [("empty", b"")] + [(p.stem, p.read_bytes())
            for p in sorted((args.root / "seeds").iterdir())]:
        work = out / "hardware" / label
        (work / "logs").mkdir(parents=True, exist_ok=True)
        replay = subprocess.run([str(target)], input=data, cwd=work,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        (work / "replay.log").write_bytes(replay.stdout)
        assert replay.returncode == 0, f"Known AES replay {label} failed: {replay.returncode}"
        counts = [int(line.rsplit(" ", 1)[1]) for line in
                  (work / "logs/coverage.dat").read_text().splitlines() if line.startswith("C")]
        evidence["hardware_replays"].append(dict(input=label, returncode=replay.returncode,
            counters=len(counts), nonzero=sum(c > 0 for c in counts), total=sum(counts)))

    for mode in ("constant", "alternating", "crash", "crash_no_coverage", "timeout", "missing_coverage"):
        work = out / mode
        for directory in ("logs", "build", "seeds"):
            (work / directory).mkdir(parents=True, exist_ok=True)
        (work / "seeds/input").write_bytes(b"audit")
        source = '''#!/usr/bin/python3
import os, pathlib, signal, sys, time
sys.stdin.buffer.read()
p = pathlib.Path('calls')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
mode = MODE
if mode not in ('missing_coverage', 'crash_no_coverage', 'timeout') or n == 1:
    counts = [int(n % 2 == 1), int(n % 2 == 0)] if mode == 'alternating' else [1, 0]
    pathlib.Path('logs/coverage.dat').write_text("C 'a' %d\\nC 'b' %d\\n" % tuple(counts))
if mode in ('crash', 'crash_no_coverage') and n > 1:
    os.kill(os.getpid(), signal.SIGABRT)
if mode == 'timeout' and n > 1:
    time.sleep(3)
'''.replace("MODE", repr(mode))
        shim = work / "build/Vaes_tb"
        shim.write_text(source)
        shim.chmod(0o755)
        process = subprocess.Popen([str(binary), "seeds"], cwd=work,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            try:
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                output, _ = process.communicate(timeout=5)
        finally:
            stop_group(process)
        (work / "fuzzer.log").write_bytes(output)
        log = output.decode(errors="replace")
        corpus = [int(x) for x in re.findall(r"corpus: (\d+)", log)]
        solutions = [p for p in (work / "solutions").glob("*") if not p.name.startswith(".")]
        evidence["controls"][mode] = dict(returncode=process.returncode,
            calls=int((work / "calls").read_text()) if (work / "calls").exists() else 0,
            max_corpus=max(corpus, default=0), solution_files=[p.name for p in solutions])
    (out / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if args.expect_fixed:
        controls = evidence["controls"]
        assert len({x["nonzero"] for x in evidence["hardware_replays"]}) > 1
        assert controls["alternating"]["calls"] > 4
        assert controls["alternating"]["max_corpus"] <= 2, "Coverage history forgot prior maxima"
        assert controls["crash"]["solution_files"], "Crashing targets were not saved"
        assert controls["crash_no_coverage"]["solution_files"], "Crash without coverage was not saved"
        assert controls["timeout"]["solution_files"], "Timed out target was not saved"
        assert controls["missing_coverage"]["calls"] == 2, "Stale coverage was reused"


if __name__ == "__main__":
    main()
