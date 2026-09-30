# Open-source hardware fuzzers

This repository builds pinned KastnerRG fuzzer forks in one Docker image and runs their bundled examples.
The [integration audit](docs/integration-audit.md) records feedback checks, known limits, and differences from the papers.

## Status

`Working` means the bundled example passed its execution and applicable feedback, replay, and oracle checks.
These results do not establish that every upstream target or paper evaluation was reproduced.
`Need tool` means a full campaign is blocked; `Will get` and `No response` describe artifact requests.
The table covers IP, SoC, and NoC fuzzers from [SoK: You Find What You Seek](https://arxiv.org/abs/2609.27300), plus the other bundled fuzzers.

| Fuzzer | Abs | Language | Venue | Status | Bundled example / support |
| --- | --- | --- | --- | --- | --- |
| [DirectFuzz](https://github.com/KastnerRG/fuzz_directfuzz) | IP | FIRRTL | [DAC'21](https://doi.org/10.1109/DAC18074.2021.9586289) | `Working` | Directed Sodor3Stage campaign. |
| [RFUZZ](https://github.com/KastnerRG/fuzz_rfuzz) | IP | FIRRTL | [ICCAD'18](https://doi.org/10.1145/3240765.3240842) | `Working` | Sodor3Stage simulation. |
| [RTLFuzzLab](https://github.com/KastnerRG/fuzz_rtl_fuzz_lab) | IP | FIRRTL | [WOSET'21](https://woset-workshop.github.io/PDFs/2021/a10.pdf) | `Working` | TLI2C. |
| [Fuzzing HW like SW](https://github.com/KastnerRG/fuzz_hw_like_sw) | IP | SV | [USENIX Security'22](https://www.usenix.org/conference/usenixsecurity22/presentation/trippel) | `Working` | Lock RTL and AFL++. |
| [SpinalFuzz](https://github.com/KastnerRG/fuzz_spinalfuzz) | IP | Scala | [ETS'22](https://doi.org/10.1109/ETS54262.2022.9810421) | `Working` | GCD. |
| [HyperFuzzing](https://github.com/KastnerRG/fuzz_hyperfuzzer) | SoC | SV | [ICCAD'20](https://doi.org/10.1145/3400302.3415709) | `Working` | AES. |
| [SymbFuzz](https://github.com/KastnerRG/fuzz_symbfuzz) | SoC | SV | [MICRO'25](https://doi.org/10.1145/3725843.3756131) | `Working` | Counter campaign; licensed Vivado. |
| [FuSS](https://github.com/KastnerRG/fuzz_fuss) | SoC | FIRRTL | [ACM TECS'25](https://doi.org/10.1145/3760529) | `Working` | RocketTile with Spike/RTL replay. |
| [PreSiFuzz](https://github.com/KastnerRG/fuzz_presifuzz) | IP | SV | — | `Working` | [Intel Labs toolkit](https://github.com/IntelLabs/PreSiFuzz); OpenTitan AES. |
| [FastHwFuzz](https://github.com/KastnerRG/fuzz_fast_hw_fuzz) | IP | FIRRTL | [GLSVLSI'25](https://doi.org/10.1145/3716368.3735190) | `Working` | TLI2C. |
| [HWFuzzing](https://github.com/KastnerRG/fuzz_hw_fuzzing_afl) | RTL | SV | — | `Working` | RTL taint-chain example. |
| [NoCFuzzer](https://github.com/KastnerRG/fuzz_noc-fuzzer-src) | NoC | SV/UVM | [IEEE TCAD'25](https://doi.org/10.1109/TCAD.2024.3430195) | `Working` | Router with DUT coverage and scoreboard; licensed VCS/UVM. |
| [ProFuzz](https://github.com/KastnerRG/fuzz_profuzz) | IP | SV | [ICCAD'25](https://doi.org/10.1109/ICCAD66269.2025.11240782) | `Need tool` | I2C preparation passes; full campaign blocked by missing inputs and Genus failure. |
| BugsBunny | IP | | [SILM'22](https://silm-workshop.github.io/2022-papers/silm2022-bugsbunny.pdf) | `Will get` | |
| TargetFuzz | IP | | [ASP-DAC'26](https://doi.org/10.1109/ASP-DAC66049.2026.11420533) | `Will get` | |
| SynFuzz | Netlist | | [arXiv'25](https://arxiv.org/abs/2504.18812) | `Will get` | |
| Fuzzitizer | SoC | | [ASIA CCS'26](https://doi.org/10.1145/3779208.3806078) | `Will get` | |
| InterConFuzz | NoC | | [DAC'25](https://doi.org/10.1109/DAC63849.2025.11133216) | `Will get` | |
| VGF | IP | | [arXiv'23](https://arxiv.org/abs/2312.06580) | `No response` | |
| FuzzWiz | IP | | [ISETC'24](https://doi.org/10.1109/ISETC63109.2024.10797245) | `No response` | |
| FMTC | IP | | [ISCAS'21](https://doi.org/10.1109/ISCAS51556.2021.9401267) | `No response` | |
| FormalFuzzer | SoC | | [ASP-DAC'24](https://doi.org/10.1109/ASP-DAC58780.2024.10473911) | `No response` | |
| SoCFuzzer | SoC | | [DATE'23](https://doi.org/10.23919/DATE56975.2023.10137024) | `No response` | |
| TaintFuzzer | SoC | | [ICCAD'23](https://doi.org/10.1109/ICCAD57390.2023.10323726) | `No response` | |

## Run

Use Git, GNU Make, and Docker with BuildKit on Linux x86_64.
A source build takes substantial time and disk space.

```sh
git clone https://github.com/KastnerRG/oss-hw-fuzz.git
cd oss-hw-fuzz
make scratch BUILD_JOBS=8
make run_all_oss_backend DURATION=30
```

`make scratch` initializes the required submodules and builds without cached layers; use `make build` for a cached build.
To use the published image instead, run:

```sh
make image
make restart
make run_all_oss_backend DURATION=30
```

`make run_all` runs every example, including licensed flows.
`make run_all_oss_backend` runs the ten examples that need no commercial EDA tools.
Run one example with `make run FUZZER=nocfuzzer DURATION=30`, replacing the fuzzer name as needed.
`DURATION` is the fuzzing budget in seconds; compilation and setup take additional time.
After building or pulling a new image, use `make restart` so the container uses it.

Results are copied to `results/<timestamp>/`, including `summary.tsv`, logs, corpora, and statistics.
Set `RESULTS=/absolute/path` to change the host destination.
`PASS` means the example met its checks, `SKIP` means a required tool or artifact is unavailable, and `FAIL` means a check failed.

## Commercial tools

Copy [config.example.mk](config.example.mk) to the ignored `config.mk`, or pass the same settings to Make.
`TOOL_ROOTS` lists read-only host directories to mount and defaults to `/tools:$(HOME)/install:$(HOME)/scratch/install`.
`TOOL_PATHS` selects additional or preferred tool binaries.
`LICENSE_ENV_VARS` names the license variables forwarded from the host; export the applicable values, such as `SNPSLMD_LICENSE_FILE` for VCS.
Run `make tools` to inspect discovered installations and `make restart` after changing mounts or an image.
Licensed NoCFuzzer validates the router example; the mesh flow remains outside the bundled check.
ProFuzz's full campaign remains blocked as detailed in the [audit](docs/integration-audit.md).

## GitHub Actions

The [workflow](.github/workflows/run-examples.yml) pulls `ghcr.io/kastnerrg/oss-hw-fuzz:latest` and runs `make run_all_oss_backend`.
It uses GitHub's built-in token for GHCR and requires every selected example to pass.
It compares the published image's results with the checked-out fuzzer list, so publish an updated image after changing the runner.
Logs and a compressed results archive are retained for 14 days; the archive preserves AFL filenames containing colons.
Build and push the image with `make build` and `make publish` after logging in to GHCR.
Manual workflow runs can choose another published image and fuzzing duration.
