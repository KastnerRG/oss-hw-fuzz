#!/usr/bin/env python3
"""Probe a generated GCD workspace without modifying its baseline artifacts.

The output corruption is an explicit negative oracle control, not a real bug.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--quick", action="store_true", help="Check feedback and encoded delay without recompiling")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"PATH": "/opt/spinalfuzz/bin:/opt/verilator/bin:" + os.environ["PATH"],
                        "AFL_LLVM_ALLOWLIST": "./allowlist", "AFL_NO_UI": "1"}
    payload = struct.pack("<IIBB", 48, 18, 1, 1) + struct.pack("<IIBB", 0, 0, 0, 32)
    evidence = {"workspace": str(args.workspace), "maps": {}, "objects": {}, "oracle": {}}
    for label, data in (("empty", b""), ("gcd48_18", payload)):
        target = args.workspace / "bin/VGCD_fuzz"
        result = subprocess.run(["afl-showmap", "-e", "-o", str(args.output.resolve() / (label + ".map")),
            "--", str(target)], input=data, cwd=args.workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        (args.output / (label + ".log")).write_bytes(result.stdout)
        assert result.returncode == 0, f"{label}: GCD feedback replay failed"
        tuples = (args.output / (label + ".map")).read_text().splitlines()
        evidence["maps"][label] = dict(returncode=result.returncode, edges=len(tuples))
    model_objects = sorted(p.name for p in (args.workspace / "src").glob("VGCD*.o"))
    for filename in ["main_afl_simple.o", "verilated.o", "verilated_cov.o", *model_objects]:
        output = subprocess.check_output(["nm", "-u", str(args.workspace / "src" / filename)], text=True)
        (args.output / (filename + ".nm")).write_text(output)
        evidence["objects"][filename] = [line.strip() for line in output.splitlines()
            if "__afl" in line or "__sanitizer_cov" in line]
    replay = subprocess.run([str(args.workspace / "bin/VGCD_fuzz"), "pp"], input=payload,
        cwd=args.workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    (args.output / "delay.log").write_bytes(replay.stdout)
    assert replay.returncode == 0, "GCD delay replay failed"
    cycles = [int(x) for x in re.findall(rb"Cycle: (\d+)", replay.stdout)]
    evidence["encoded_delay"] = dict(requested=1, measured=cycles[1] - cycles[0])
    assert cycles[1] - cycles[0] == 1, "Encoded sleep advanced more than once"
    assert evidence["maps"]["gcd48_18"]["edges"] > evidence["maps"]["empty"]["edges"]
    assert not any(evidence["objects"][name] for name in
        ("main_afl_simple.o", "verilated.o", "verilated_cov.o")), "Simulation infrastructure is instrumented"
    assert any(evidence["objects"][name] for name in model_objects), "DUT feedback is absent"
    if args.quick:
        (args.output / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
        return
    for mode in ("baseline", "corrupt_output"):
        work = args.output.resolve() / mode
        shutil.copytree(args.workspace, work)
        source = work / "src/main_afl_simple.cpp"
        text = source.read_text().replace("    top->final();",
            '    std::cerr << "AUDIT_RESULT=" << top->io_result << " READY=" << int(top->io_rdy) << "\\n";\n    top->final();')
        source.write_text(text)
        if mode == "corrupt_output":
            for source in (work / "src").glob("VGCD*.cpp"):
                source.write_text(source.read_text().replace(
                    "vlSelfRef.io_result = vlSelfRef.GCD__DOT__reg_b;", "vlSelfRef.io_result = 0U;"))
        with (work / "audit-build.log").open("w") as log:
            subprocess.run(["sh", "gppCompScript.sh"], cwd=work, env=env, stdout=log,
                stderr=subprocess.STDOUT, check=True, timeout=180)
        replay = subprocess.run([str(work / "bin/VGCD_fuzz"), "pp"], input=payload, cwd=work,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        (work / "audit-replay.log").write_bytes(replay.stdout)
        measured = re.search(rb"AUDIT_RESULT=(\d+) READY=(\d+)", replay.stdout)
        evidence["oracle"][mode] = dict(returncode=replay.returncode,
            result=int(measured[1]), ready=int(measured[2]), expected=6)
    (args.output / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    assert evidence["maps"]["gcd48_18"]["edges"] > evidence["maps"]["empty"]["edges"]
    assert not evidence["objects"]["main_afl_simple.o"], "Harness is instrumented"
    assert any(evidence["objects"][name] for name in model_objects), "DUT feedback is absent"
    assert evidence["oracle"]["baseline"]["result"] == 6
    assert evidence["oracle"]["baseline"]["returncode"] == 0
    assert all(result["ready"] == 1 for result in evidence["oracle"].values())
    assert evidence["oracle"]["corrupt_output"]["result"] == 0
    assert evidence["oracle"]["corrupt_output"]["returncode"] == 0, "Oracle behavior changed"


if __name__ == "__main__":
    main()
