#!/usr/bin/env python3
"""Check HyperFuzzer's high-level feedback and HyperPLTL oracle in the image."""

import argparse
import json
import os
from pathlib import Path
import resource
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/fuzzers/hyperfuzzer"))
    parser.add_argument("--target", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cwd = args.root.resolve() / "fuzztest"
    target = str((args.target or cwd / "aes_test").resolve())
    probes = output / "inputs"
    probes.mkdir()
    samples = {"seed": bytes.fromhex("003000"), "zeros": bytes(24),
               "seed-repeat": bytes.fromhex("003000")}
    for name, data in samples.items():
        (probes / name).write_bytes(data)
    violation = bytes.fromhex("801c00")
    (output / "property-violation.input").write_bytes(violation)
    commands = []

    def run(label, command, data=None):
        with (output / f"{label}.log").open("wb") as log:
            result = subprocess.run(command, cwd=cwd, input=data, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=30,
                                    env=os.environ | {"AFL_MAP_SIZE": "65536",
                                                     "AFL_SKIP_CPUFREQ": "1",
                                                     "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1"})
        commands.append({"label": label, "command": command, "cwd": str(cwd),
                         "input_hex": data.hex() if data is not None else None,
                         "exit_code": result.returncode})
        (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        return result.returncode

    assert run("benign", [target], samples["seed"]) == 0, "Benign RTL replay failed"
    code = run("property-violation", [target], violation)
    log = (output / "property-violation.log").read_text(errors="replace")
    assert code == -signal.SIGABRT and "Assertion `sim.evaluate(f)' failed" in log, \
        "Known input did not reach the HyperPLTL assertion"
    maps = output / "maps"
    assert run("feedback", ["afl-showmap", "-r", "-t", "10000", "-i", str(probes),
                            "-o", str(maps), "--", target]) == 0, "Feedback collection failed"
    values = {}
    for name in samples:
        values[name] = {int(line.split(":")[0]): int(line.split(":")[1])
                        for line in (maps / name).read_text().splitlines()}
        assert all(any(start <= index < end for index in values[name])
                   for start, end in ((0, 21841), (21841, 43682), (43682, 65523))), \
            f"{name} lacks one of the opcode, PC, or memory feedback partitions"
    assert values["seed"] != values["zeros"], "Tampering did not change high-level feedback"
    assert values["seed"] == values["seed-repeat"], "Feedback changed across interleaved replays"
    result = {"status": "passed", "oracle": "HyperPLTL assertion",
              "metric": "instruction and memory bigrams",
              "map_entries": {name: len(entries) for name, entries in values.items()}}
    (output / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
