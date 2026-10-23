#!/usr/bin/env python3
"""Exercise real counter simulation with BMC enabled and disabled.

Run with the SymbFuzz virtualenv and Yosys 0.40 on PATH inside the image.
Random stimulus holds en=0 deliberately, so any progress needs solver replay.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/opt/fuzzers/symbfuzz"))
    parser.add_argument("--quick", action="store_true", help="Check three real witnesses and a bounded negative")
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    from symfuzz.bmc_interface import BmcInterface
    from symfuzz.coverage_db import CoverageDB
    from symfuzz.design_parser import parse_design
    from symfuzz.orchestrator import Orchestrator
    from symfuzz.sim_driver import SimDriver
    from symfuzz.state_forcing import StateForcer
    from symfuzz.testbench_gen import generate_vivado_harness

    results = {}
    design = parse_design([str(args.root / "examples/counter.v")], top_module="counter")
    for mode in ("enabled", "disabled"):
        directory = args.output.resolve() / mode
        directory.mkdir(parents=True, exist_ok=True)
        tb = generate_vivado_harness(design, directory)
        driver = SimDriver(design, tb, verbose=True)
        driver.compile()
        driver.load()
        driver.random_step = lambda: driver.step({"en": 0, "rst": 0})
        db = CoverageDB(directory / "coverage.db", design)
        bmc = BmcInterface(args.root / "build/symbfuzz", design, verbose=True)
        if mode == "disabled":
            bmc.find_sequence = lambda target: None
            bmc.last_status = "bounded_unsat"
        try:
            if args.quick:
                if mode == "enabled":
                    witnesses = []
                    for target in (1, 2, 3):
                        witness = bmc.find_sequence({"count": target})
                        states = StateForcer(driver, design).replay_sequence(witness)
                        witnesses.append(dict(target=target, steps=witness.steps, states=states))
                        assert states[-1]["count"] == target, witnesses[-1]
                    results["witnesses"] = witnesses
                    bounded = BmcInterface(args.root / "build/symbfuzz", design, max_steps=2)
                    assert bounded.find_sequence({"count": 15}) is None
                    assert bounded.last_status == "bounded_unsat"
                    results["bounded_negative"] = bounded.last_status
                    try:
                        bmc.find_sequence({"audit_missing_register": 1})
                    except RuntimeError:
                        results["invalid_target"] = "error"
                    else:
                        raise AssertionError("Invalid target was treated as an unreachable state")
                    unknown_db = CoverageDB(directory / "unknown.db", design)

                    class RetriesObserved(Exception):
                        pass

                    attempts = []

                    def unknown_result(target):
                        attempts.append(dict(target))
                        if len(attempts) == 4:
                            raise RetriesObserved
                        return None

                    unknown = SimpleNamespace(last_status="unknown", find_sequence=unknown_result)
                    try:
                        try:
                            Orchestrator(design, driver, unknown_db, unknown,
                                StateForcer(driver, design), stall_cycles=1,
                                global_timeout=float("inf")).run()
                        except RetriesObserved:
                            pass
                        else:
                            raise AssertionError("Unknown solver result stopped exploration")
                        assert attempts == [{"count": 1}] * 4, "Unknown target was skipped"
                        assert not unknown_db._exhausted_targets
                        results["unknown_negative"] = dict(
                            status="retryable", attempts=attempts, deferred_targets=0)
                    finally:
                        unknown_db.close()
                    break
            result = Orchestrator(design, driver, db, bmc, StateForcer(driver, design),
                stall_cycles=1, global_timeout=30, verbose=True).run()
            results[mode] = asdict(result)
        finally:
            driver.close()
            db.close()
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    if args.quick:
        return
    assert results["enabled"]["bmc_successes"] > 0
    assert results["enabled"]["total_states"] > results["disabled"]["total_states"]
    assert results["disabled"]["total_states"] == 1


if __name__ == "__main__":
    main()
