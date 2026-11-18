"""Adapt the image copy of FUZZItizer to current VCS XML compression."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
files = {
    "fuzz/parse_cov.py": 1,
    "ipfuzzer/fuzz/parse_cov.py": 2,
    "ipfuzzer/fuzz/FSMSanitizer.py": 1,
    "ipfuzzer/fuzz/createPerifSanitizersJg.py": 1,
}
for relative, expected in files.items():
    path = root / relative
    source = path.read_text()
    actual = source.count("gzip.open(")
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} gzip.open calls; found {actual}")
    source = source.replace("gzip.open(", "open_xml(")
    if relative == "ipfuzzer/fuzz/parse_cov.py":
        old = 'detailed_cov[child.attrib["name"]] = child.attrib["value"]'
        new = 'detailed_cov[child.attrib["name"]] = coverage_bits(child.attrib["value"], cov_type)'
        if source.count(old) != 1:
            raise RuntimeError(f"{path}: expected one IP coverage assignment")
        source = source.replace(old, new)
    elif relative == "fuzz/parse_cov.py":
        old = 'detailed_cov[child.attrib["name"]] = value'
        new = 'detailed_cov[child.attrib["name"]] = coverage_bits(value, cov_type)'
        if source.count(old) != 1:
            raise RuntimeError(f"{path}: expected one SoC coverage assignment")
        source = source.replace(old, new)
    imports = "from vcs_xml import open_xml"
    if relative.endswith("parse_cov.py"):
        imports += ", coverage_bits"
    path.write_text(imports + "\n" + source)

helper = Path(__file__).with_name("vcs_xml.py")
for directory in (root / "fuzz", root / "ipfuzzer/fuzz"):
    shutil.copy2(helper, directory / "vcs_xml.py")

triage = root / "ipfuzzer/fuzz/violation_triage.py"
source = triage.read_text()
old = "    register_widths = parse_register_widths(jg_log)\n"
new = (
    "    if Path(jg_log).is_file():\n"
    "        register_widths = parse_register_widths(jg_log)\n"
    "    else:\n"
    "        # Bundled sanitizers can be replayed without regenerating them in JG.\n"
    "        # Keep zeroization events; their target widths remain unknown.\n"
    "        print(f'JG width metadata unavailable: {jg_log}; retaining all zeroization events')\n"
    "        register_widths = {}\n"
)
if source.count(old) != 1:
    raise RuntimeError(f"{triage}: expected one JG width lookup")
triage.write_text(source.replace(old, new))
