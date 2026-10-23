#!/usr/bin/env python3
"""Probe native FuSS feedback and signature-oracle wiring in the common image.

Run in an isolated container: python3 fuss.py --output /results/audit-fuss
Each control replays the same deterministic native input on real RocketTile RTL.
The controls inject faults at Python adapter boundaries, without changing sources.
This checks the native adapter, not the paper's hardware-CFG symbolic algorithm.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    backend = Path("/opt/fuzzers/fuss/difuzz-rtl/Fuzzer")
    sys.path.insert(0, str(backend / "src"))
    from mutator import simInput, P_M
    from word import Word, PREFIX, MAIN, SUFFIX

    def word(label, instruction, part):
        value = Word(label, [instruction])
        value.populate({}, part)
        return value

    seed = output / "seed.si"
    instructions = ["addi x5, x0, 1", "addi x6, x0, 1", "beq x5, x6, _l4",
                    "addi x7, x0, 2", "addi x7, x0, 3"]
    simInput([word(0, "addi x20, x0, 1", PREFIX)],
             [word(i, instruction, MAIN) for i, instruction in enumerate(instructions)],
             [word(0, "addi x21, x0, 0", SUFFIX)], [0] * len(instructions), 0, P_M).save(
                 str(seed), list(range(384)))

    inject_fault = '''
from pathlib import Path
from RTLSim.host import rvRTLhost
_save_signature = rvRTLhost.save_signature
def _corrupt_signature(self, *args, **kwargs):
    _save_signature(self, *args, **kwargs)
    path = Path(self.rtl_sig_file)
    lines = path.read_text().splitlines()
    lines[0] = f"{int(lines[0], 16) ^ 1:032x}"
    path.write_text("\\n".join(lines) + "\\n")
rvRTLhost.save_signature = _corrupt_signature
'''
    variants = {
        "baseline": "",
        "coverage-disabled": "from RTLSim.host import rvRTLhost\nrvRTLhost.get_covsum = lambda self: 0\n",
        "signature-fault": inject_fault,
        "oracle-disabled-under-fault": inject_fault +
            "from src.signature_checker import sigChecker\nsigChecker.check = lambda self, symbols: True\n",
    }
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "FUZZ_DURATION", "FUSS_QUEUE"):
        env.pop(key, None)
    env.update(VIRTUAL_ENV="/opt/fuss-tools/venv", PYTHONNOUSERSITE="1",
               PYTHON_BIN="/opt/fuss-tools/venv/bin/python", SPIKE="/opt/fuss-tools/bin/spike")
    env["PATH"] = "/opt/fuss-tools/venv/bin:/opt/fuss-tools/verilator/bin:/opt/fuss-tools/riscv/bin:/opt/fuss-tools/bin:" + env["PATH"]
    outcomes = {}
    for name, prefix in variants.items():
        directory = output / name
        directory.mkdir()
        # TestFactory discovers the generated tests in this separate module.
        (directory / "FuSSProbe.py").write_text(prefix + "\n" + (backend / "DifuzzRTL.py").read_text())
        campaign = directory / "campaign"
        command = ["make", "SIM_BUILD=build/rocket", "VFILE=RocketTile_state", "TOPLEVEL=RocketTile",
                   "MODULE=FuSSProbe", f"VPATH={directory}", "NUM_ITER=1", "RECORD=1", f"IN_FILE={seed}", f"OUT={campaign}",
                   f"COCOTB_RESULTS_FILE={directory / 'results.xml'}",
                   "PYTHONPATH=" + ":".join(str(p) for p in (
                       directory, backend, backend / "src", backend / "RTLSim/src"))]
        with (directory / "run.log").open("w") as log:
            # These controls inspect FuSS execution reports, so enable its hooks.
            probe_env = env | {"FUSS_QUEUE": str(campaign / "symbolic_queue")}
            result = subprocess.run(command, cwd=backend, env=probe_env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=120)
        assert result.returncode == 0, f"{name}: simulator command failed; inspect run.log"
        junit = ET.parse(directory / "results.xml").getroot()
        assert list(junit.iter("testcase")) and not list(junit.iter("failure")) and not list(junit.iter("error"))
        stats = json.loads((campaign / "campaign.json").read_text())
        assert stats["finished"] and stats["backend_status"] == 0 and stats["rtl_executions"] == 1
        outcomes[name] = dict(stats=stats, corpus=len(list((campaign / "corpus").glob("*.si"))),
                              mismatches=len(list((campaign / "mismatch/sim_input").glob("*.si"))))
    baseline, no_cov, fault, bypass = (outcomes[key] for key in variants)
    assert baseline["stats"]["isa_matches"] == 1 and baseline["mismatches"] == 0
    assert baseline["stats"]["coverage"] > 0 and baseline["corpus"] == 1
    assert no_cov["stats"]["coverage"] == 0 and no_cov["corpus"] == 0
    assert no_cov["stats"]["isa_matches"] == 1
    assert fault["stats"]["isa_matches"] == 0 and fault["mismatches"] == 1
    assert bypass["stats"]["isa_matches"] == 1 and bypass["mismatches"] == 0
    (output / "summary.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    print(json.dumps(outcomes, indent=2))


if __name__ == "__main__":
    main()
