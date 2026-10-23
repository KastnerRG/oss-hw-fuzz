#!/usr/bin/env python3
"""Check the lock RTL state feedback and deliberately reachable SVA."""

import argparse
import json
import os
from pathlib import Path
import resource
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path,
                        default=Path("/opt/hw_like_sw/hw/lock/model/lock"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    target = str(args.target.resolve())
    probes = output / "inputs"
    probes.mkdir()
    samples = {"zero": bytes(4), "one": bytes.fromhex("aa000000"),
               "two": bytes.fromhex("aabb0000")}
    for name, data in samples.items():
        (probes / name).write_bytes(data)
    unlock = bytes.fromhex("aabbcc00")
    (output / "unlock.input").write_bytes(unlock)
    commands = []

    def run(label, command, data=None):
        with (output / f"{label}.log").open("wb") as log:
            result = subprocess.run(command, input=data, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=30, env=os.environ | {"AFL_SKIP_CPUFREQ": "1",
                                        "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1"})
        commands.append({"label": label, "command": command,
                         "input_hex": data.hex() if data is not None else None,
                         "exit_code": result.returncode})
        (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        return result.returncode

    for state, (name, data) in enumerate(samples.items()):
        assert run(name, [target], data) == 0, f"Lock state {state} replay failed"
        assert f"state = 0x{state}" in (output / f"{name}.log").read_text(), \
            f"Expected lock state {state} was not observed"
    code = run("unlock", [target], unlock)
    log = (output / "unlock.log").read_text(errors="replace")
    assert code == -signal.SIGABRT and "Assertion failed" in log and \
        "SUCCESS: unlocked state has been reached" in log, "Known input did not trigger the RTL assertion"
    maps = output / "maps"
    assert run("feedback", ["afl-showmap", "-r", "-i", str(probes), "-o", str(maps),
                            "--", target]) == 0, "Feedback collection failed"
    values = [(maps / name).read_text() for name in samples]
    assert all(values) and len(set(values)) == len(samples), \
        "Distinct RTL states did not produce distinct feedback"
    result = {"status": "passed", "oracle": "lock_tb RTL assertion",
              "map_entries": {name: len(value.splitlines())
                              for name, value in zip(samples, values)}}
    (output / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
