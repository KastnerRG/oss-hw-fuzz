#!/usr/bin/env python3
"""Run the bundled examples sequentially and retain logs and execution evidence."""

import argparse
import fcntl
import json
import os
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager, nullcontext, suppress
from datetime import datetime, timezone
from pathlib import Path

NAMES = (
    "hw_like_sw", "hw_fuzzing_afl", "hyperfuzzer", "presifuzz", "symbfuzz", "profuzz",
    "rtl_fuzz_lab", "fast_hw_fuzz", "rfuzz", "directfuzz", "spinalfuzz", "fuss", "nocfuzzer",
)
COMMERCIAL_TOOL_NAMES = frozenset(("symbfuzz", "profuzz", "nocfuzzer"))
OSS_BACKEND_NAMES = tuple(name for name in NAMES if name not in COMMERCIAL_TOOL_NAMES)
FUZZERS = Path("/opt/fuzzers")
JAVA = ("java", "-XX:ActiveProcessorCount=8", "-cp",
        "target/scala-2.12/rtl-fuzz-lab-assembly-0.1.jar")
AFL_ENV = {"AFL_SKIP_CPUFREQ": "1", "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
           "AFL_NO_AFFINITY": "1"}


def signal_group(process, signum):
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signum)


@contextmanager
def child(*args, cwd=None, env=None, log=None, cleanup_grace=5):
    """Own a process group, including descendants left after its leader exits."""
    with (log.open("w") if log else nullcontext()) as output:
        process = subprocess.Popen(
            [str(arg) for arg in args], cwd=cwd, env=os.environ | (env or {}),
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            yield process
        finally:
            try:
                signal_group(process, signal.SIGTERM)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=cleanup_grace)
            finally:
                signal_group(process, signal.SIGKILL)
                process.wait()


def wait(process, seconds=None, stop_signal=signal.SIGINT, grace=15, timeout_ok=False):
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        signal_group(process, stop_signal)
        try:
            process.wait(timeout=grace)
            return 0 if timeout_ok else 124
        except subprocess.TimeoutExpired:
            signal_group(process, signal.SIGKILL)
            process.wait()
            return 137


def run(*args, seconds=None, timeout_ok=True, **kwargs):
    with child(*args, **kwargs) as process:
        code = wait(process, seconds, timeout_ok=timeout_ok)
    if code:
        raise subprocess.CalledProcessError(code, args)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def has_text(path, pattern):
    return re.search(pattern, path.read_text(errors="replace")) is not None


def afl_executed(directory):
    require(any(has_text(path, r"(?m)^execs_done\s*:\s*[1-9][0-9]*")
                for path in directory.rglob("fuzzer_stats")), "No AFL inputs executed")


def verify_firrtl_feedback(name, root, result):
    """Require hardware feedback and corpus selection, beyond execution counts."""
    if name == "rtl_fuzz_lab":
        bitmap = (result / "afl/fuzz_bitmap").read_bytes()
        # AFLProxy supplies a constant byte at zero even with feedback disabled.
        require(any(value != 255 for value in bitmap[1:]), "No RTL feedback reached AFL")
        stats = {key.strip(): value.strip() for key, value in
                 (line.split(":", 1) for line in (result / "afl/fuzzer_stats").read_text().splitlines() if ":" in line)}
        require(int(stats.get("paths_total", "0")) > 1,
                "RTL feedback produced no corpus discoveries")
    elif name == "fast_hw_fuzz":
        lines = (result / "fuzz/plot_data").read_text().splitlines()
        require(lines, "No FastHwFuzz statistics")
        stats = dict(re.findall(r"(trials|valid|saved_inputs|total_cov|valid_cov): ([0-9.]+)", lines[-1]))
        require(all(float(stats.get(key, 0)) > 0 for key in
                    ("trials", "valid", "saved_inputs", "total_cov", "valid_cov")),
                "FastHwFuzz lacks valid executions, hardware feedback, or corpus discoveries")
        require(any(path.stat().st_size for path in (result / "fuzz/corpus").glob("id_*")),
                "FastHwFuzz retained no inputs")
    else:
        import toml
        config = toml.load(root / "build/Sodor3Stage.toml")
        indices = {int(counter["index"]) for counter in config["counter"] if not counter.get("fail", False)}
        corpus = result / "output/corpus"
        stats = json.loads((corpus / "latest.json").read_text())
        bitmap = stats["bitmap"]
        require(any(index < len(bitmap) and bitmap[index] != 255 for index in indices),
                f"{name} recorded no hardware coverage")
        entries = [json.loads(path.read_text())["entry"] for path in corpus.glob("entry_*.json")]
        discoveries = [entry for entry in entries if entry.get("lineage") is not None
                       and entry.get("is_valid") is True and indices.intersection(entry.get("new_cov", []))]
        require(discoveries, f"{name} feedback selected no mutated inputs")
        if name == "directfuzz":
            targets = [config["coverage"][counter["signal"]] for counter in config["counter"]
                       if not counter.get("fail", False)]
            require(any(target.get("target") and target.get("module") == "CSRFile"
                        and target.get("distance") == 0 for target in targets),
                    "DirectFuzz lacks its CSRFile coverage target")
            require(any(entry.get("is_targeted") is True for entry in discoveries),
                    "DirectFuzz selected no targeted discoveries")
            energies = re.findall(r"Energy of active test entryEntryId\(\d+\) is ([0-9.eE+-]+)",
                                  (result / "fuzzer.log").read_text())
            require(any(abs(float(value) - 1) > 1e-6 for value in energies),
                    "DirectFuzz did not exercise distance-based mutation energy")


def wait_ready(process, log, pattern):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        require(process.poll() is None, f"Simulator exited before becoming ready; see {log}")
        if has_text(log, pattern):
            return
        time.sleep(1)
    raise RuntimeError(f"Simulator was not ready within 300 seconds; see {log}")


def run_example(name, result, duration):
    root = FUZZERS / name

    def afl(cwd, seeds, target, binary="afl-fuzz", options=(), env=None, target_args=()):
        run(binary, "-V", duration, "-i", seeds, "-o", result / "afl",
            *options, "--", target, *target_args, cwd=cwd, env=env)
        afl_executed(result / "afl")

    match name:
        case "hw_like_sw":
            run("python3", "/opt/integration-checks/hw_like_sw.py", "--output", result / "checks",
                seconds=120, timeout_ok=False, log=result / "checks.log")
            afl("/opt/hw_like_sw/hw/lock", "corpus", "./model/lock")
        case "hw_fuzzing_afl":
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            rtl = root / "hw-fuzz-tests/rtl"
            target = rtl / "build/taint-chain"
            env = AFL_ENV | {"HWFUZZ_COVERAGE": "Taint"}
            print("Fuzzing the RTL taint-chain example with bit-level taint feedback.",
                  flush=True)
            # Check the sanitizer and deliberate RTL assertion before fuzzing.
            for label, data, code, pattern in (
                ("untainted", b"abcdef", 0, r"RTL result=48 tagged=0"),
                ("tainted", b"a457;a", -signal.SIGABRT, r"RTL result=49 tagged=1"),
            ):
                seed = result / f"{label}.input"
                seed.write_bytes(data)
                log = result / f"{label}.log"
                with child(target, seed, env=env, log=log) as process:
                    actual = wait(process, 30)
                require(actual == code and has_text(log, pattern),
                        f"HWFuzzing {label} replay failed; see {log}. "
                        "Recreate older containers with make restart to apply the seccomp profile.")
            # Preserve raw taint bits: showmap's hit-count display drops label 255.
            probes = result / "feedback-inputs"
            probes.mkdir()
            for label, data in (("none", b"abcdef"), ("first", b"a4cdef"),
                                ("second", b"a45def"), ("third", b"a457ef")):
                (probes / label).write_bytes(data)
            maps = result / "feedback-maps"
            run(root / "afl-showmap", "-r", "-i", probes, "-o", maps, "--", target, "@@",
                env=env, log=result / "feedback.log", seconds=30)
            coverage = [{int(line.split(":")[0]) for line in (maps / label).read_text().splitlines()}
                        for label in ("none", "first", "second", "third")]
            require(not coverage[0] and coverage[0] < coverage[1] < coverage[2] < coverage[3],
                    "HWFuzzing did not record increasing RTL taint feedback")
            afl(rtl, "seeds", target, binary=root / "afl-fuzz", env=env,
                options=("-g", "6", "-G", "6"), target_args=("@@",))
            bitmap = (result / "afl/default/fuzz_bitmap").read_bytes()
            require(all(offset < len(bitmap) and bitmap[offset] != 255 for offset in coverage[1]),
                    "HWFuzzing campaign did not observe the seed's RTL taint feedback")
            print("Verified untainted/tainted replays and increasing RTL taint feedback.")
        case "hyperfuzzer":
            run("python3", "/opt/integration-checks/hyperfuzzer.py", "--output", result / "checks",
                seconds=120, timeout_ok=False, log=result / "checks.log")
            afl(root / "fuzztest", "afl-in/aes_test", "./aes_test", options=("-t", "10000"),
                env=AFL_ENV | {"AFL_MAP_SIZE": "65536"})
        case "presifuzz":
            root /= "fuzzers/opentitan-fuzzer-verilator-hw-cov"
            run("python3", "/opt/integration-checks/presifuzz.py", result / "checks",
                "--expect-fixed", seconds=120, timeout_ok=False, log=result / "checks.log")
            (result / "build").symlink_to(root / "build")
            (result / "logs").mkdir()
            run(root / "target/debug/opentitan-fuzzer", root / "seeds", cwd=result,
                seconds=duration, log=result / "fuzzer.log")
            require(has_text(result / "fuzzer.log", r"executions: [1-9][0-9]*"),
                    "No PreSiFuzz inputs executed")
            # Stopping between children can leave no current dump: pre_exec
            # deliberately removes it to prevent stale feedback reuse.
            require(has_text(result / "fuzzer.log", r"corpus: [1-9][0-9]*"),
                    "PreSiFuzz hardware feedback selected no inputs")
        case "symbfuzz":
            for tool in ("xsim", "xvlog", "xelab"):
                if not shutil.which(tool):
                    print(f"SKIP: full SymbFuzz campaigns require Vivado ({tool} missing).")
                    return 77
            run(root / ".venv/bin/python", "/opt/integration-checks/symbfuzz.py",
                result / "checks", "--quick", cwd=root, seconds=120, timeout_ok=False,
                log=result / "checks.log",
                env={"PATH": f"/opt/symbfuzz/yosys/bin:{os.environ['PATH']}"})
            run(".venv/bin/symfuzz", "examples/counter.v", "--top", "counter",
                "--timeout", duration, "--stall-cycles", "3", "--output-dir", result / "counter",
                cwd=root, env={"PATH": f"/opt/symbfuzz/yosys/bin:{os.environ['PATH']}"})
            with sqlite3.connect(f"file:{result}/counter/coverage.db?mode=ro", uri=True) as db:
                rows = db.execute("SELECT state_json, cycle_count FROM visited_states").fetchall()
            counts = {json.loads(state).get("count") for state, _ in rows}
            cycles = max((cycle for _, cycle in rows), default=0)
            require(None not in counts and len(counts) > 1 and cycles > 0,
                    "No counter state exploration recorded")
            print(f"Verified {len(counts)} counter states through cycle {cycles}.")
        case "profuzz":
            with child("python3", "/opt/integration-checks/profuzz.py", result / "bootstrap",
                       log=result / "bootstrap.log") as process:
                code = wait(process, 240, timeout_ok=False)
            report = result / "bootstrap/summary.json"
            require(report.is_file(), f"ProFuzz bootstrap produced no report; see {result / 'bootstrap.log'}")
            summary = json.loads(report.read_text())
            completed = [stage for stage, outcome in summary.get("stages", {}).items()
                         if outcome["status"] == "completed"]
            print("ProFuzz bootstrap completed: " + (", ".join(completed) or "no stages"))
            for stage, outcome in summary.get("stages", {}).items():
                if outcome["status"] == "failed":
                    print(f"ProFuzz {stage}: {outcome.get('reason', 'failed')}")
            require(code in (0, 77), f"ProFuzz bootstrap failed; see {result / 'bootstrap.log'}")
            if not summary.get("campaign_completed"):
                print("SKIP: full directed campaign remains unavailable; "
                      f"stage results and blockers: {result / 'bootstrap/summary.json'}")
                return 77
            require(code == 0, "ProFuzz reported a completed campaign with a nonzero exit status")
        case "nocfuzzer":
            with child("python3", "/opt/fuzzers/nocfuzzer/noc_router_fuzz/integration/run.py", result,
                       "--duration", duration, log=result / "checks.log") as process:
                code = wait(process, duration + 540, timeout_ok=False)
            if code == 77:
                print((result / "checks.log").read_text(errors="replace"))
                return 77
            require(code == 0, f"NoCFuzzer feedback/oracle checks failed; see {result / 'checks.log'}")
            print("Verified NoCFuzzer DUT feedback, replay, scoreboard, and AFL campaign.")
        case "rtl_fuzz_lab":
            (root / "seeds").mkdir(exist_ok=True)
            shutil.copy(root / "src/fuzzing/template_seeds/binary/TLI2C_longSeed.hwf", root / "seeds")
            for fifo in (root / "a2j", root / "j2a"):
                if not fifo.is_fifo():
                    os.mkfifo(fifo)
            log = result / "driver.log"
            with child(*JAVA, "fuzzing.afl.AFLDriver", "--FIRRTL", "test/resources/fuzzing/TLI2C.fir",
                       "--Harness", "tlul", "--Directed", "--MuxToggleCoverage", "false",
                       "--Feedback", "255", "--Folder", result / "afl", cwd=root, log=log) as driver:
                wait_ready(driver, log, "Ready to fuzz!")
                run("/opt/afl-rtl-fuzz-lab/afl-fuzz", "-d", "-i", "seeds", "-o", result / "afl",
                    "-f", "input", "--", "./fuzzing/afl-proxy", "a2j", "j2a", "log",
                    cwd=root, env=AFL_ENV, seconds=duration)
                afl_executed(result / "afl")
                verify_firrtl_feedback(name, root, result)
        case "fast_hw_fuzz":
            verilator = root / "verilator"
            run(*JAVA, "fuzzing.fast.FastDriver", "--FIRRTL", "test/resources/fuzzing/TLI2C.fir",
                "--Harness", "tlul", "--line-coverage", "--Feedback", "255",
                "--SeedInputFolder", "seeds", "--ThreadNum", "2", "--OutputFolder", result / "fuzz",
                cwd=root / "fuzz", log=result / "driver.log", seconds=duration + 300, timeout_ok=False,
                env={"VERILATOR_ROOT": str(verilator), "PATH": f"{verilator}/bin:{os.environ['PATH']}",
                     "VM_DEFAULT_RULES": "0"})
            # FastDriver writes final stats only after completed hardware simulation.
            require(has_text(result / "fuzz/plot_data", r"trials: [1-9][0-9]*"),
                    "No FastHwFuzz inputs executed")
            if (root / "fuzz/cov.log").exists():
                shutil.copy(root / "fuzz/cov.log", result / "cov.log")
            verify_firrtl_feedback(name, root, result)
        case "rfuzz" | "directfuzz":
            endpoint = Path("/tmp/fpga/0")
            endpoint.parent.mkdir(exist_ok=True)
            shutil.rmtree(endpoint, ignore_errors=True)
            (result / "output").mkdir()
            log = result / "server.log"
            try:
                with child(root / "build/Sodor3Stage_server", "0", log=log) as server:
                    wait_ready(server, log, "created rx fifo")
                    options = ("--targeted_mode", "2") if name == "directfuzz" else ()
                    # DirectFuzz also writes to CWD/basename(output-directory).
                    run(root / "fuzzer/target/release/kfuzz", "--server-id", "0", *options,
                        "--output-directory", result / "output/corpus", root / "build/Sodor3Stage.toml",
                        cwd=result, log=result / "fuzzer.log", seconds=duration)
                    require((result / "output/corpus/entry_0000.json").stat().st_size, "Empty corpus")
                    stats = json.loads((result / "output/corpus/latest.json").read_text())
                    require(stats["tests_per_second"]["global_numerator"] > 0, "No test inputs executed")
                    verify_firrtl_feedback(name, root, result)
            finally:
                shutil.rmtree(endpoint, ignore_errors=True)
        case "spinalfuzz":
            workspace = result / "spinalfuzz"
            workspace.mkdir()
            run("sbt", "-batch", "project tester", "runMain mylib.GCDFuzz", cwd=root,
                env=AFL_ENV | {"JAVA_HOME": "/opt/java8", "EXAMPLE_TIME_SECS": str(duration),
                              "PATH": f"/opt/java8/bin:/opt/spinalfuzz/bin:/opt/verilator/bin:{os.environ['PATH']}",
                              "SPINALFUZZ_WORKSPACE": str(workspace), "SPINALFUZZ_SYS_CHANGE": "0"})
            afl_executed(workspace)
            run("python3", "/opt/integration-checks/spinalfuzz.py", workspace / "GCD",
                result / "checks", "--quick", seconds=120, timeout_ok=False,
                log=result / "checks.log")
        case "fuss":
            run("python3", "/opt/integration-checks/fuss.py", "--output", result / "checks",
                seconds=120, timeout_ok=False, log=result / "checks.log")
            workspace = result / "campaign"
            config = result / "rocket_example.yml"
            shutil.copy(root / "symbolic_fuzzing/config/rocket_example.yml", config)
            run("./fuss", "integrated", "--workspace", workspace, "--start-fuzzer",
                "--duration", duration, "--interval", "1", "--config", config,
                cwd=root, seconds=duration + 300, timeout_ok=False, cleanup_grace=20,
                env={"FUSS_SIM_PYTHON": "/opt/fuss-tools/venv/bin/python",
                     "SPIKE": "/opt/fuss-tools/bin/spike",
                     "PATH": f"/opt/fuss-tools/verilator/bin:/opt/fuss-tools/riscv/bin:"
                             f"/opt/fuss-tools/bin:{os.environ['PATH']}"})
            stats = json.loads((workspace / "campaign.json").read_text())
            require(stats.get("finished") is True and stats.get("backend_status") == 0,
                    "FuSS native campaign did not finish successfully")
            for key in ("rtl_executions", "rtl_successes", "isa_matches", "coverage",
                        "symbolic_executions", "symbolic_successes", "symbolic_matches"):
                require(stats.get(key, 0) > 0, f"FuSS produced no {key}; see {workspace}")
            manifests = list((workspace / "runs").glob("*/run.json"))
            require(len(manifests) == 1 and
                    json.loads(manifests[0].read_text()).get("status") == "passed",
                    "FuSS did not validate the current simulator results")
            validated = 0
            for replay in (workspace / "symbolic_replays").glob("*.si"):
                outcome = replay.with_suffix(".result.json")
                if not outcome.is_file():
                    continue
                outcome = json.loads(outcome.read_text())
                if outcome.get("rtl_status") != 0 or outcome.get("isa_match") is not True:
                    continue
                metadata = json.loads(replay.with_suffix(".json").read_text())
                generated = list((workspace / "symbolic_results").glob(f"run_*/{replay.name}"))
                require(len(generated) == 1 and generated[0].read_bytes() == replay.read_bytes(),
                        "FuSS replay does not match its generated symbolic input")
                require(metadata.get("symbolic_model", {}).get("kind") == "RV64 conditional branch"
                        and metadata.get("coverage_at_generation", {}).get("iteration", 0) >= 3,
                        "FuSS replay lacks symbolic model and measured coverage evidence")
                validated += 1
            require(validated > 0, "No generated FuSS candidate passed RTL replay and ISA comparison")
            print(f"Verified {stats['rtl_executions']} RTL executions and {validated} "
                  "symbolic candidates with matching Spike/RTL results.")
        case _:
            raise ValueError(f"Unknown fuzzer: {name}")
    return 0


def interrupted(signum, _frame):
    # Let active process groups finish cleanup even if another signal arrives.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", nargs="?", default="all",
                        choices=("all", "oss_backend", *NAMES))
    for key, default in (("DURATION", 30), ("BUILD_JOBS", 8)):
        value = os.environ.get(key) or str(default)
        if not re.fullmatch(r"[1-9][0-9]*", value):
            parser.error(f"{key} must be a positive integer")
        os.environ[key] = value
    duration = int(os.environ["DURATION"])
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    if sys.argv[1:2] == ["--one"]:
        os.environ["RESULTS"] = sys.argv[3]
        return run_example(sys.argv[2], Path(sys.argv[3]), duration)

    selection = parser.parse_args().selection
    # The hardware servers share fixed FIFO endpoints; allow only one suite.
    with open("/tmp/oss-hw-fuzz-examples.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another example suite is running.", file=sys.stderr)
            return 1
        prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        run_dir = Path(tempfile.mkdtemp(prefix=prefix, dir="/results"))
        failed = skipped = False
        with (run_dir / "summary.tsv").open("w", buffering=1) as summary:
            summary.write("fuzzer\tstatus\texit_code\n")
            if selection == "all":
                names = NAMES
            elif selection == "oss_backend":
                names = OSS_BACKEND_NAMES
            else:
                names = (selection,)
            for name in names:
                result = run_dir / name
                result.mkdir()
                print(f"Running {name} (fuzzing budget: {duration}s)...", flush=True)
                with child(sys.executable, __file__, "--one", name, result,
                           log=result / "run.log", cleanup_grace=20) as worker:
                    code = wait(worker, duration + 600, signal.SIGTERM, 20)
                code = 128 - code if code < 0 else code
                status = "PASS"
                if code:
                    status = "SKIP" if code == 77 else "FAIL"
                    skipped |= code == 77
                    failed |= code != 77
                    with (result / "run.log").open(errors="replace") as log:
                        print("".join(deque(log, maxlen=25)), end="", flush=True)
                row = f"{name}\t{status}\t{code}\n"
                summary.write(row)
                print(row, end="", flush=True)
        print(f"\nResults: {run_dir}\n{(run_dir / 'summary.tsv').read_text()}", end="")
        return 1 if failed else 77 if skipped and selection != "all" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        print(error, file=sys.stderr)
        sys.exit(128 - error.returncode if error.returncode < 0 else error.returncode)
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
