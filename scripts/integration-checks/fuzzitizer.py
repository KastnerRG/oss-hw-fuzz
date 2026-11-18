#!/usr/bin/env python3
"""Build and exercise FUZZItizer's bundled Caliptra UART peripheral."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path("/opt/fuzzers/sethlab_hwfuzzing/fuzzitizer")
IP_ROOT = ROOT / "ipfuzzer"
PYTHON = Path("/opt/fuzzitizer/venv/bin/python")
PERIPHERAL = "cal_uart"


def run(log, *args, cwd=None, env=None, timeout=300):
    with log.open("w") as output:
        subprocess.run(args, cwd=cwd, env=env, stdout=output,
                       stderr=subprocess.STDOUT, timeout=timeout, check=True)


def main():
    result = Path(sys.argv[1]).resolve()
    result.mkdir(parents=True, exist_ok=True)
    duration = int(sys.argv[2])
    if not shutil.which("vcs") or not shutil.which("vlogan"):
        print("FUZZItizer requires mounted Synopsys VCS and vlogan")
        return 77

    env = os.environ | {
        "FUZZITIZER_ROOT": str(ROOT),
        "IPFUZZ_ROOT": str(IP_ROOT),
        "CALIPTRA_ROOT": str(IP_ROOT / "benchmarks/cal"),
        "PATH": f"{PYTHON.parent}:{os.environ['PATH']}",
    }
    fuzz_dir = IP_ROOT / "fuzz"
    build_dir = IP_ROOT / "benchmarks/cal/tools/scripts"
    sanitizer = IP_ROOT / "benchmarks/cal_uart/tb/sanitizers.sva"
    soc_sanitizer = IP_ROOT / "benchmarks/calAsn/tb/sanitizers_cal_uart.sva"
    if not sanitizer.is_file() or not soc_sanitizer.is_file():
        raise RuntimeError("Bundled Caliptra UART sanitizers are missing")

    (IP_ROOT / "input_seeds").mkdir(exist_ok=True)
    run(result / "manifest.log", PYTHON, IP_ROOT / "utils/export_soc_input_manifest.py",
        cwd=fuzz_dir, env=env)
    run(result / "testbench.log", PYTHON, "createPerifTb.py", "-co", PERIPHERAL,
        cwd=fuzz_dir, env=env)

    original = {path: path.read_bytes() for path in (sanitizer, soc_sanitizer)}
    if b"Sanitizer:" not in original[sanitizer]:
        raise RuntimeError("The bundled UART sanitizer file is empty")
    try:
        # The shipped SVA avoids a JasperGold regeneration step. Build the
        # exploration simulator with upstream's dummy assertion, then replay
        # with its bundled full sanitizer set.
        run(result / "empty_sanitizers.log", PYTHON, "-c",
            "import createPerifSanitizersJg as s; s.main(empty_only=True)",
            "-co", PERIPHERAL, cwd=fuzz_dir, env=env)
        if b"a_dummy" not in sanitizer.read_bytes():
            raise RuntimeError("Empty-sanitizer generation failed")
        run(result / "exploration_clean.log", "make", "clean", cwd=build_dir, env=env)
        run(result / "exploration_build.log", "make", "vcs-build",
            "TOP_LEVEL_MODULE=cal_uart_tb", "V=cal_uart", cwd=build_dir, env=env)
        exploration = IP_ROOT / "sim/cal_uart/vcs_0"
        if exploration.exists():
            shutil.rmtree(exploration)
        exploration.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(build_dir, exploration)
    finally:
        for path, data in original.items():
            path.write_bytes(data)

    run(result / "replay_clean.log", "make", "clean", cwd=build_dir, env=env)
    run(result / "replay_build.log", "make", "vcs-build",
        "TOP_LEVEL_MODULE=cal_uart_tb", "V=cal_uart", cwd=build_dir, env=env)
    replay = IP_ROOT / "sim/cal_uart_san/vcs_0"
    if replay.exists():
        shutil.rmtree(replay)
    replay.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build_dir, replay)

    run_id = f"oss_{int(time.time())}_{os.getpid()}"
    run(result / "fuzzer.log", PYTHON, "fuzz.py", "-co", PERIPHERAL,
        "-id", run_id, "-mp", "6", "-mt", str(duration), "-sj", "2",
        "-j", "1", "-rs", "4242", "-fd", "1",
        cwd=fuzz_dir, env=env, timeout=duration + 180)
    output_name = f"{PERIPHERAL}_fuzz_{run_id}"
    output = IP_ROOT / "outputs" / output_name
    detail = IP_ROOT / "outputs_all" / output_name
    selected = json.loads((output / "selected_inputs/selected_inputs.json").read_text())
    coverage = json.loads((output / "merged_cov.json").read_text())
    triage = json.loads((output / "triage/summary.json").read_text())
    if selected["selected_count"] < 1 or selected["covered_points"] < 1:
        raise RuntimeError("FUZZItizer selected no coverage-increasing UART input")
    if not any("1" in bits for kind, bits in coverage.items() if kind != "assert"):
        raise RuntimeError("FUZZItizer recorded no structural hardware coverage")
    if "violations total" not in triage:
        raise RuntimeError("FUZZItizer did not complete sanitizer triage")
    shutil.copytree(output, result / "run")
    shutil.copytree(detail, result / "replay")
    handoff = IP_ROOT / "ipfuzzer_output"
    for name in ("soc_input_manifest.json", "peripheral_runs.json", "Flist.cal.fuzz"):
        shutil.copy2(handoff / name, result / name)
    print(f"FUZZItizer UART: {selected['selected_count']} selected inputs, "
          f"{selected['covered_points']} coverage points, "
          f"{triage['violations total']} triaged violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
