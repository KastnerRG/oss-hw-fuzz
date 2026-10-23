#!/usr/bin/env python3
"""Start available PROFUZZ artifact stages without claiming a complete campaign.

The published I2C inputs are copied into a fresh output directory.
Commercial tools are resolved from PATH or explicit executable arguments.
Exit 77 means partial bootstrap; summary.json records each executed stage.
No missing testbench, target-selection algorithm, or coverage oracle is invented.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


def redact(text):
    for name in ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE",
                 "XILINXD_LICENSE_FILE", "MGLS_LICENSE_FILE"):
        value = os.environ.get(name)
        if value:
            entries = value.split(":")
            # Vendor summaries may print just the hostname from port@host.
            hosts = {entry.split("@", 1)[1] for entry in entries if "@" in entry}
            for part in sorted({value, *entries, *hosts}, key=len, reverse=True):
                if part:
                    text = text.replace(part, f"<redacted:{name}>")
    text = re.sub(r"(?m)(Cdslmd servers:\s*)[^\r\n]+", r"\1<redacted:license-servers>", text)
    return re.sub(r"\b\d+@[A-Za-z0-9_.-]+", "<redacted:license-endpoint>", text)


def run_stage(command, directory, timeout):
    started = time.monotonic()
    result = {"command": command, "cwd": str(directory), "status": "failed"}
    process = None
    try:
        process = subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   errors="replace", start_new_session=True)
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
        result["returncode"] = process.returncode
        result["status"] = "completed" if process.returncode == 0 and not result.get("timed_out") else "failed"
        (directory / "run.log").write_text(redact(output))
        if re.search(r"licenses? could not be acquired|checkout failed|Cannot obtain .* license|Can't find license file", output, re.I):
            result.update(status="blocked", reason="license_unavailable")
        elif result.get("timed_out"):
            result["reason"] = "tool_timeout"
        elif "error while loading shared libraries" in output:
            result["reason"] = "missing_runtime_library"
        elif "Fatal internal error" in output and "Checking out license:" in output:
            result["reason"] = "vendor_license_checkout_crash"
    except OSError as error:
        (directory / "run.log").write_text(redact(str(error)) + "\n")
        result["error"] = redact(str(error))
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["log"] = str(directory / "run.log")
    return result


def target_primitive(report, netlist, library, target):
    """Match TestMAX's cell/ID report to a real output driving the target net."""
    if re.search(r"^\s*Error\s*:", report, re.M | re.I):
        return None
    # report_primitives <net> prints the driving primitive, not the net name.
    for instance, identifier, cell in re.findall(
            r"^\s*(\S+)\s+\((\d+)\)\s+\S+\s+\((\S+)\)\s*$", report, re.M):
        binding = re.search(r"\b" + re.escape(cell) + r"\s+" + re.escape(instance)
                            + r"\s*\((.*?)\)\s*;", netlist, re.S)
        model = re.search(r"\bmodule\s+" + re.escape(cell) + r"\b(.*?)\bendmodule\b",
                          library, re.S)
        if binding is None or model is None:
            continue
        outputs = set()
        for declaration in re.findall(r"\boutput\s+([^;]+);", model.group(1)):
            outputs.update(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", declaration))
        for pin, net in re.findall(r"\.\s*(\w+)\s*\(\s*([^()]+?)\s*\)", binding.group(1)):
            if (net == target and pin in outputs
                    and re.search(r"^\s*" + re.escape(pin) + r"\s+O\s+", report, re.M)):
                return {"target_net": target, "instance": instance, "cell": cell,
                        "primitive_id": int(identifier), "driver_pin": pin}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--artifact", type=Path, default=Path("/opt/fuzzers/profuzz"))
    parser.add_argument("--tool-timeout", type=float, default=60)
    for tool in ("xrun", "genus", "tmax"):
        parser.add_argument("--" + tool, default=tool)
    args = parser.parse_args()
    if args.tool_timeout <= 0:
        parser.error("--tool-timeout must be positive")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    artifact = args.artifact.resolve()
    inputs = output / "inputs"
    inputs.mkdir()
    source_files = {
        "syn_flat_i2c.v": "random_selection_fulldesigns/i2c/syn_flat_i2c.v",
        "target.txt": "random_selection_fulldesigns/i2c/target.txt",
        "in_sedd.txt": "random_selection_fulldesigns/i2c/i2c_master_top_final_pattern.txt",
        "gscl45nm.v": "lib/gscl45nm/sim/gscl45nm.v",
        "gscl45nm.lib": "lib/gscl45nm/syn/gscl45nm.lib",
    }
    summary = {"status": "partial", "campaign_completed": False,
               "scope": "Published I2C artifact bootstrap; no coverage-guided campaign or bug oracle",
               "artifact": str(artifact), "inputs": {}, "stages": {}, "blockers": []}
    summary["license_environment_present"] = {
        name: bool(os.environ.get(name)) for name in ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE")}
    for name, source in source_files.items():
        path = artifact / source
        if not path.is_file():
            summary["blockers"].append("Missing published input: " + source)
            continue
        shutil.copyfile(path, inputs / name)
        summary["inputs"][name] = {"source": source,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if len(summary["inputs"]) != len(source_files):
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print("PARTIAL: PROFUZZ published inputs are missing; see", output / "summary.json")
        return 77

    stages = summary["stages"]
    tools = {name: shutil.which(getattr(args, name)) for name in ("xrun", "genus", "tmax")}
    tools["imc"] = shutil.which("imc")
    summary["tools"] = tools
    for name, tool in tools.items():
        if tool is None:
            summary["blockers"].append("Executable unavailable: " + name)

    # Inspect the actual script without importing its Colab/scientific dependencies.
    extraction = artifact / "Target Selection/node_extraction.py"
    try:
        ast.parse(extraction.read_text(), filename=str(extraction))
        stages["target_selection"] = {"status": "blocked", "reason": "Original script requires a Colab mount and design-specific configuration"}
    except SyntaxError as error:
        stages["target_selection"] = {"status": "blocked", "source": str(extraction),
                                      "reason": f"{type(error).__name__} at line {error.lineno}: {error.msg}"}
    summary["blockers"].append("Original target extraction is not runnable as supplied; consuming published target.txt")
    targets = (inputs / "target.txt").read_text().split()
    netlist = (inputs / "syn_flat_i2c.v").read_text()
    wires = set()
    for declaration in re.findall(r"\bwire\s+([^;]+);", netlist):
        wires.update(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", declaration))
    selected = next((target for target in targets if target in wires), None)
    if selected is None:
        raise RuntimeError("Published target list has no matching I2C wire")
    summary["target"] = {"selection": "First published target present in the bundled netlist",
                         "name": selected, "published_count": len(targets),
                         "matching_wire_count": sum(target in wires for target in targets)}

    directory = output / "seed_preparation"
    directory.mkdir()
    shutil.copyfile(inputs / "in_sedd.txt", directory / "in_sedd.txt")
    for name in ("atpg_flip.py",):
        shutil.copyfile(artifact / "Fuzz_Scripts" / name, directory / name)
    script = "from atpg_flip import bit_flip_sequence\nbit_flip_sequence('in_sedd.txt', 'flipped_atpg_pat.txt')\n"
    (directory / "prepare.py").write_text(script)
    stages["seed_preparation"] = run_stage([sys.executable, "prepare.py"], directory, args.tool_timeout)
    seed = (inputs / "in_sedd.txt").read_text().strip()
    flipped = directory / "flipped_atpg_pat.txt"
    expected = "".join("1" if char == "0" else "0" if char == "1" else char for char in seed)
    if not flipped.is_file() or flipped.read_text().strip() != expected:
        stages["seed_preparation"]["status"] = "failed"
    stages["seed_preparation"].update(seed_bits=len(seed), unknown_bits=seed.upper().count("X"),
                                      source="Fuzz_Scripts/atpg_flip.py:bit_flip_sequence")
    if "X" in seed.upper():
        summary["blockers"].append("Published ATPG seed contains X bits; arithmetic mutation requires a binary don't-care resolution policy that is not supplied")

    def commercial_stage(name, tool, command, files, marker=None):
        directory = output / name
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_text(content)
        if tools[tool] is None:
            stages[name] = {"status": "unavailable", "reason": tool + " not on PATH"}
            return
        stages[name] = run_stage([tools[tool], *command], directory, args.tool_timeout)
        if (stages[name]["status"] == "completed" and marker
                and not re.search(marker, (directory / "run.log").read_text(), re.M)):
            stages[name]["status"] = "failed"
            stages[name]["reason"] = "Expected completion evidence absent: " + marker
        if stages[name]["status"] != "completed":
            summary["blockers"].append(name + " did not complete; inspect its run.log")

    commercial_stage("xcelium_elaboration", "xrun", ["-64bit", "-elaborate",
        str(inputs / "syn_flat_i2c.v"), "-top", "i2c_master_top", "-v", str(inputs / "gscl45nm.v"),
        "-nospecify", "-timescale", "1ns/10ps"], {},
        r"Writing initial simulation snapshot: worklib\.i2c_master_top:v")
    stages["xcelium_elaboration"]["scope"] = "Original simulation command's netlist/library elaboration only; no absent tb_dut testbench or stimulus"

    genus_script = f"""set_db library {{{inputs / 'gscl45nm.lib'}}}
read_hdl {{{inputs / 'syn_flat_i2c.v'}}}
elaborate i2c_master_top
check_design -unresolved
set count [llength [get_db insts]]
if {{$count < 1}} {{error "No imported I2C instances"}}
set handle [open instances.txt w]
puts $handle [get_db insts .name]
close $handle
puts "PROFUZZ_GENUS_IMPORTED $count"
exit
"""
    commercial_stage("genus_import", "genus", ["-no_gui", "-batch", "-files", "import.tcl"],
                     {"import.tcl": genus_script}, r"^PROFUZZ_GENUS_IMPORTED [1-9][0-9]*$")
    stages["genus_import"]["scope"] = "Netlist import prerequisite for the published Genus submodule flow; no submodule selection or synthesis claim"

    tmax_template = (artifact / "atpg_seed_gen/get_id.tcl").read_text()
    substitutions = {"gscl45nm.v": "{" + str(inputs / "gscl45nm.v") + "}",
                     "<design.v>": "{" + str(inputs / "syn_flat_i2c.v") + "}",
                     "<top_module_name>": "i2c_master_top", "<clk_name>": "wb_clk_i",
                     "<targetnet_name>": "{" + selected + "}"}
    for old, new in substitutions.items():
        tmax_template = tmax_template.replace(old, new)
    commercial_stage("testmax_target_id", "tmax", ["-shell", "-tcl", "get_id.tcl"],
                     {"get_id.tcl": tmax_template})
    stages["testmax_target_id"]["substitutions"] = substitutions
    report = output / "testmax_target_id/tmax_data_nets.log"
    if stages["testmax_target_id"]["status"] == "completed":
        report_text = report.read_text(errors="replace") if report.is_file() else ""
        primitive = target_primitive(report_text, netlist, (inputs / "gscl45nm.v").read_text(), selected)
        if primitive is None:
            stages["testmax_target_id"]["status"] = "failed"
            stages["testmax_target_id"]["reason"] = "Reported primitive is not a verified library-cell output driving the selected target net"
            summary["blockers"].append("TestMAX target primitive evidence absent")
        else:
            stages["testmax_target_id"]["primitive"] = primitive

    for filename in ("tb_dut.vhd", "common.ccf", "targetsites.vRefine"):
        if not any(artifact.rglob(filename)):
            summary["blockers"].append("Missing full-flow input: " + filename)
    summary["blockers"].extend([
        "Fuzz_Scripts/base_coverage.py leaves the target-signal denominator as insts = x",
        "Conflict-PI and fan-in-cone analyses are unpublished; supplied patterns do not reproduce their generation",
    ])
    stages["directed_fuzzing"] = {"status": "blocked", "reason": "Full-flow prerequisites listed in blockers; no feedback or oracle result claimed"}
    summary["commercial_stages_completed"] = [name for name in (
        "xcelium_elaboration", "genus_import", "testmax_target_id") if stages[name]["status"] == "completed"]
    failed = [name for name, result in stages.items() if result["status"] == "failed"]
    if failed:
        summary["status"] = "failed"
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("PROFUZZ completed " + ", ".join(name for name, result in stages.items()
                                           if result["status"] == "completed"))
    if failed:
        print("FAIL: bootstrap stages failed: " + ", ".join(failed))
    print("Full directed fuzzing remains blocked; see", output / "summary.json")
    return 1 if failed else 77


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    raise SystemExit(main())
