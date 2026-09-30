# syntax=docker/dockerfile:1
FROM eclipse-temurin:8-jdk-jammy AS java8

FROM ubuntu:22.04 AS base
ARG DEBIAN_FRONTEND=noninteractive
ARG BUILD_JOBS=8
ARG VERILATOR_VERSION=v5.038
ARG SBT_EXTRAS_REV=93cd2a6225473e8485d1688b13c7a8c9e831efaf

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git build-essential clang llvm llvm-14-dev lld \
    cmake ninja-build meson pkg-config autoconf automake libtool flex bison \
    perl help2man libfl-dev zlib1g-dev libboost-dev libgtest-dev libffi-dev \
    libssl-dev libz3-dev yosys z3 verilator afl++ openjdk-17-jdk-headless \
    cargo rustc python3 python3-pip python3-venv python3-toml \
    python3-matplotlib python3-scipy ccache bsdextrautils bc time unzip \
 && rm -rf /var/lib/apt/lists/*
COPY --from=java8 /opt/java/openjdk /opt/java8
RUN curl -fsSL "https://raw.githubusercontent.com/dwijnand/sbt-extras/${SBT_EXTRAS_REV}/sbt" \
      -o /usr/local/bin/sbt \
 && chmod +x /usr/local/bin/sbt \
 && ln -s /usr/bin/make /usr/local/bin/gmake

# Keep Ubuntu's Verilator 4 for the old FIRRTL harnesses; newer SV/Scala
# examples opt into this installation without replacing the system tool.
RUN git clone --depth 1 --branch "${VERILATOR_VERSION}" \
      https://github.com/verilator/verilator.git /tmp/verilator \
 && cd /tmp/verilator && autoconf && ./configure --prefix=/opt/verilator \
 && make -j"${BUILD_JOBS}" && make install \
 && rm -rf /tmp/verilator

RUN useradd --create-home --uid 1000 --shell /bin/bash fuzz \
 && mkdir -p /opt/fuzzers /results \
 && chown fuzz:fuzz /opt/fuzzers /results
ENV LANG=C.UTF-8 \
    JAVA8_HOME=/opt/java8 \
    JVM_OPTS="-Xms256m -Xmx4g -Xss4m -XX:ActiveProcessorCount=8" \
    SBT_OPTS="-Dsbt.supershell=false" \
    AFL_SKIP_CPUFREQ=1 \
    AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
    AFL_NO_AFFINITY=1 \
    AFL_NO_UI=1
WORKDIR /opt/fuzzers

# NoCFuzzer's AFL and native proxy build without commercial tools.
# The router's VCS/UVM model is compiled at run time using mounted installations.
FROM base AS nocfuzzer
COPY --chown=fuzz:fuzz fuzzers/nocfuzzer/ /opt/fuzzers/nocfuzzer/
USER fuzz
RUN make -C /opt/fuzzers/nocfuzzer/noc_router_fuzz/AFL_hardware afl-fuzz afl-showmap \
 && make -C /opt/fuzzers/nocfuzzer/noc_router_fuzz/router_simple -f Makefile.native native-proxy \
 && python3 /opt/fuzzers/nocfuzzer/noc_router_fuzz/integration/test_native.py \
 && python3 /opt/fuzzers/nocfuzzer/noc_router_fuzz/integration/test_runner.py

# HWFuzzing's bit-level taint instrumentation requires its modified LLVM 15.
# Keep this compiler-only stage independent of example and AFL source changes.
FROM base AS hwfuzz_tools
ARG BUILD_JOBS=8
ARG HWFUZZ_LLVM_REV=2e28165c7904f3b6cdf8ba08f4a34c247db5bf01
USER root
RUN git init /tmp/hw-fuzzing-llvm \
 && git -C /tmp/hw-fuzzing-llvm remote add origin https://github.com/vusec/hw-fuzzing-llvm.git \
 && git -C /tmp/hw-fuzzing-llvm fetch --depth 1 origin "${HWFUZZ_LLVM_REV}" \
 && git -C /tmp/hw-fuzzing-llvm checkout --detach FETCH_HEAD \
 && test "$(git -C /tmp/hw-fuzzing-llvm rev-parse HEAD)" = "${HWFUZZ_LLVM_REV}" \
 && cmake -S /tmp/hw-fuzzing-llvm/llvm -B /tmp/hw-fuzzing-llvm-build -G Ninja \
      -DLLVM_ENABLE_PROJECTS=clang \
      '-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;compiler-rt' \
      -DCMAKE_BUILD_TYPE=Release -DLLVM_USE_LINKER=lld \
      -DLLVM_PARALLEL_LINK_JOBS=1 \
      -DCMAKE_INSTALL_PREFIX=/opt/hw_fuzzing_afl/llvm \
      -DLIBCXX_CXX_FLAGS=-fsanitize=memory \
      -DLLVM_TARGETS_TO_BUILD=X86 \
      -DCLANG_ENABLE_STATIC_ANALYZER=OFF -DCLANG_ENABLE_ARCMT=OFF \
 && cmake --build /tmp/hw-fuzzing-llvm-build -j "${BUILD_JOBS}" \
 && cmake --install /tmp/hw-fuzzing-llvm-build \
 && install -m644 /tmp/hw-fuzzing-llvm/compiler-rt/include/tainting.h \
      "$(/opt/hw_fuzzing_afl/llvm/bin/clang -print-resource-dir)/include/tainting.h" \
 && cmake -S /tmp/hw-fuzzing-llvm/runtimes -B /tmp/hw-fuzzing-libcxx-build -G Ninja \
      -DCMAKE_C_COMPILER=/opt/hw_fuzzing_afl/llvm/bin/clang \
      -DCMAKE_CXX_COMPILER=/opt/hw_fuzzing_afl/llvm/bin/clang++ \
      -DLLVM_USE_SANITIZER=Memory \
      -DCMAKE_INSTALL_PREFIX=/opt/hw_fuzzing_afl/libcxx \
      '-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi' \
 && cmake --build /tmp/hw-fuzzing-libcxx-build -j "${BUILD_JOBS}" \
 && cmake --install /tmp/hw-fuzzing-libcxx-build \
 && rm -rf /tmp/hw-fuzzing-llvm /tmp/hw-fuzzing-llvm-build /tmp/hw-fuzzing-libcxx-build

FROM hwfuzz_tools AS hwfuzz
COPY --chown=fuzz:fuzz fuzzers/hw_fuzzing_afl/ /opt/fuzzers/hw_fuzzing_afl/
USER fuzz
RUN export PATH=/opt/hw_fuzzing_afl/llvm/bin:$PATH \
      LLVM_CONFIG=/opt/hw_fuzzing_afl/llvm/bin/llvm-config \
      HWFUZZ_NO_DFSAN=1 HWFUZZ_COVERAGE=Edge \
 && cd /opt/fuzzers/hw_fuzzing_afl \
 && LD=/usr/bin/ld.lld CFLAGS=-fuse-ld=lld CXXFLAGS=-fuse-ld=lld \
      make -f GNUmakefile -j "${BUILD_JOBS}" nproc="${BUILD_JOBS}" all \
 && LD=/usr/bin/ld.lld CFLAGS=-fuse-ld=lld CXXFLAGS=-fuse-ld=lld \
      make -f GNUmakefile.llvm -j "${BUILD_JOBS}" \
 && test -x afl-fuzz && test -s SanitizerCoveragePCGUARD.so \
 && hw-fuzz-tests/rtl/build.sh

FROM base AS sv
COPY --chown=fuzz:fuzz fuzzers/hw_like_sw/ /opt/fuzzers/hw_like_sw/
COPY --chown=fuzz:fuzz fuzzers/hyperfuzzer/ /opt/fuzzers/hyperfuzzer/
COPY --chown=fuzz:fuzz fuzzers/profuzz/ /opt/fuzzers/profuzz/
COPY --chown=fuzz:fuzz fuzzers/symbfuzz/ /opt/fuzzers/symbfuzz/
# All fuzzer source trees live in /opt/fuzzers/<name>.
# Build as the unprivileged fuzz user after /opt/hw_like_sw is made writable.
USER root
RUN mkdir -p /opt/hw_like_sw && chown fuzz:fuzz /opt/hw_like_sw
USER fuzz

# The original lock RTL, C++ harness, and seeds, without the nested Docker pipeline.
# Regenerate its testbench wrapper to match the checked-in four-state, eight-bit lock.
# Restrict AFL feedback to translated RTL and avoid stale instrumented ccache objects.
# Use the original non-deferred harness: a fork after Verilator initializes
# threads can deadlock, and this tiny design needs only one simulation thread.
RUN mkdir -p /opt/hw_like_sw/hw \
 && cp -a /opt/fuzzers/hw_like_sw/hw/other/lock /opt/hw_like_sw/hw/lock \
 && cp -a /opt/fuzzers/hw_like_sw/infra/base-sim/tb /opt/hw_like_sw/hw/tb \
 && cd /opt/hw_like_sw/hw/lock \
 && LOCK_COMP_WIDTH=8 NUM_LOCK_STATES=4 python3 hdl_generator/generate_lock_tb.py hdl_generator/lock_tb_template.sv > hdl/lock_tb.sv \
 && mkdir -p corpus && cp seeds/afl_seed.0.hwf corpus/ \
 && sed -i 's/return tb->get_main_time();/return tb ? tb->get_main_time() : 0;/; /tb = new LockTb/i\  Verilated::threadContextp()->threads(1);' tb/cpp/afl/main.cpp \
 && printf 'src:*Vtop*\n' > /opt/hw_like_sw/allowlist.txt \
 && AFL_LLVM_ALLOWLIST=/opt/hw_like_sw/allowlist.txt CCACHE_DISABLE=1 \
    /opt/verilator/bin/verilator --cc --exe --build -j ${BUILD_JOBS} \
      --prefix Vtop --top-module lock_tb --Mdir model --assert -Wno-fatal \
      -CFLAGS '-I/opt/hw_like_sw -std=c++17' \
      hdl/lock.sv hdl/lock_tb.sv \
      /opt/hw_like_sw/hw/lock/tb/cpp/afl/main.cpp \
      /opt/hw_like_sw/hw/lock/tb/cpp/afl/lock_tb.cpp \
      /opt/hw_like_sw/hw/tb/cpp/src/verilator_tb.cpp \
      /opt/hw_like_sw/hw/tb/cpp/src/stdin_fuzz_tb.cpp \
      -MAKEFLAGS 'CXX=afl-clang-fast++ LINK=afl-clang-fast++' -o lock

# HyperFuzzer must use its own Verilator checkout: its simulator accesses the
# fork's generated model internals and custom toggle-coverage representation.
RUN cd /opt/fuzzers/hyperfuzzer/verilator \
 && mkdir -p src/obj_opt src/obj_dbg \
 && ln -sf V3ParseBison.h src/obj_opt/verilog.h \
 && ln -sf V3ParseBison.h src/obj_dbg/verilog.h \
 && autoconf \
 && VERILATOR_ROOT=$PWD ./configure --prefix=/opt/hyperfuzzer \
 && VERILATOR_ROOT=$PWD make -j ${BUILD_JOBS} \
 && cd ../fuzztest \
 && cmake -S libprop -B libprop/build -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_FLAGS_RELEASE='-O3 -DNDEBUG -Wno-error' \
 && cmake --build libprop/build --target libprop -j ${BUILD_JOBS} \
 && make aes_test -j ${BUILD_JOBS}

# SymbFuzz's open-source BMC component builds independently of the Vivado driver.
RUN cd /opt/fuzzers/symbfuzz \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j ${BUILD_JOBS} \
 && g++ -std=c++20 -O2 -Wall -Wextra -Iinclude src/solver/z3_solver.cpp \
      tests/z3_solver_test.cpp -lz3 -o build/z3_solver_test \
 && timeout 10s build/z3_solver_test \
 && python3 -m venv .venv \
 && .venv/bin/pip install --no-cache-dir -e . \
 && build/symbfuzz --help >/dev/null \
 && .venv/bin/symfuzz --help >/dev/null

# PROFUZZ is a partial script artifact; no compilation is required.

# PreSiFuzz's bundled OpenTitan AES example uses Verilator and hardware coverage.
# Its newer Rust and Python dependencies stay private to this stage and prefix.
FROM base AS presifuzz
USER root
RUN mkdir -p /opt/presifuzz && chown fuzz:fuzz /opt/presifuzz
USER fuzz
ENV RUSTUP_HOME=/opt/presifuzz/rustup CARGO_HOME=/opt/presifuzz/cargo
ENV PATH=/opt/presifuzz/cargo/bin:/opt/presifuzz/venv/bin:/opt/verilator/bin:${PATH}
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
      sh -s -- -y --no-modify-path --profile minimal --default-toolchain 1.90.0 \
 && python3 -m venv /opt/presifuzz/venv \
 && pip install --no-cache-dir setuptools==68.2.2 setuptools-scm==7.1.0 wheel \
 && pip install --no-cache-dir --no-build-isolation \
      git+https://github.com/lowRISC/fusesoc.git@14dfc825ced58fe1fb343662fa80fc4fbd0fdc50 \
      git+https://github.com/lowRISC/edalize.git@5ae2c3e1ca306e27d81ce5fcc769f62cb7ac42d0 \
      hjson==3.1.0 Mako==1.4.3
COPY --chown=fuzz:fuzz fuzzers/presifuzz/ /opt/fuzzers/presifuzz/
RUN cd /opt/fuzzers/presifuzz/fuzzers/opentitan-fuzzer-verilator-hw-cov \
 && ./build.sh

FROM base AS firrtl
COPY --chown=fuzz:fuzz fuzzers/rtl_fuzz_lab/ /opt/fuzzers/rtl_fuzz_lab/
USER root
RUN git clone https://github.com/ekiwi/AFL /opt/afl-rtl-fuzz-lab \
 && git -C /opt/afl-rtl-fuzz-lab checkout e11e38dd15ffe4647975e13c3514f25de5c2ad3e \
 && make -C /opt/afl-rtl-fuzz-lab -j"${BUILD_JOBS}"
USER fuzz
RUN cd /opt/fuzzers/rtl_fuzz_lab \
 && ./setup.sh \
 && sbt -batch assembly

COPY --chown=fuzz:fuzz fuzzers/fast_hw_fuzz/ /opt/fuzzers/fast_hw_fuzz/
# Bound the fixed iteration loops by DURATION and drain the final pipeline batch.
# Only the image copy changes; the pinned submodule remains untouched.
RUN python3 - <<'PY'
from pathlib import Path
import re
p = Path('/opt/fuzzers/fast_hw_fuzz/fuzz/src/fuzzing/fast_driver/FastDriver.scala')
s = p.read_text()
old = 'Duration.ofHours(1)'
assert s.count(old) == 1
s = s.replace(old, 'Duration.ofSeconds(sys.env.getOrElse("DURATION", "30").toLong)')
old = 'val startTime = System.nanoTime()'
assert s.count(old) == 1
s = s.replace(old, old + '\n        val maxDurationNanos = Duration.ofSeconds(sys.env.getOrElse("DURATION", "30").toLong).toNanos\n        var completedIterations = 0')
old = 'for(iter <- 1 to iterNum)'
assert s.count(old) == 3
s = s.replace(old, 'for(iter <- (1 to iterNum).iterator.takeWhile(_ => System.nanoTime() - startTime < maxDurationNanos))')
old = '                    if(iter == iterNum) {'
assert s.count(old) == 1
s = s.replace(old, '                } //end iteration\n                if(completedIterations > 0) {')
old = '                    }\n                } //end iteration\n                val time ='
assert s.count(old) == 1
s = s.replace(old, '                }\n                val time =')
s, count = re.subn(r'(?m)^( +)} //end iteration$', r'\1    completedIterations += 1\n\1} //end iteration', s)
assert count == 3
assert s.count('/ iterNum') == 4
s = s.replace('/ iterNum', '/ math.max(completedIterations, 1)')
p.write_text(s)
PY
RUN cd /opt/fuzzers/fast_hw_fuzz/chiseltest \
 && sbt -batch publishLocal \
 && cd /opt/fuzzers/fast_hw_fuzz/verilator \
 && autoconf \
 && VERILATOR_ROOT="$PWD" OBJCACHE= ./configure \
 && VERILATOR_ROOT="$PWD" make -C src optimize OBJCACHE= -j"${BUILD_JOBS}" \
 && cd /opt/fuzzers/fast_hw_fuzz/fuzz \
 && sbt -batch assembly

COPY --chown=fuzz:fuzz fuzzers/rfuzz/ /opt/fuzzers/rfuzz/
RUN cd /opt/fuzzers/rfuzz \
 && sed -i "s/&& ninja$/\&\& ninja -j${BUILD_JOBS}/" Makefile \
 && CXXFLAGS=-DVL_THREADED make FIR=Sodor3Stage.fir DUT=Sodor3Stage /opt/fuzzers/rfuzz/build/Sodor3Stage_server \
 && cd fuzzer \
 && cargo build --release -j"${BUILD_JOBS}"

COPY --chown=fuzz:fuzz fuzzers/directfuzz/ /opt/fuzzers/directfuzz/
RUN cd /opt/fuzzers/directfuzz \
 && sed -i "s/&& ninja$/\&\& ninja -j${BUILD_JOBS}/" Makefile \
 && CXXFLAGS=-DVL_THREADED make FIR=Sodor3Stage.fir DUT=Sodor3Stage /opt/fuzzers/directfuzz/build/Sodor3Stage_server \
 && cd fuzzer \
 && cargo build --release -j"${BUILD_JOBS}"

FROM base AS fuss_tools
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf device-tree-compiler \
      libboost-regex-dev libboost-system-dev libexpat1-dev libelf-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/fuss-tools/venv \
 && /opt/fuss-tools/venv/bin/pip install --no-cache-dir cocotb==1.5.2 psutil==7.2.2 sysv_ipc==1.2.0
RUN git clone --depth 1 --branch v4.106 https://github.com/verilator/verilator.git /tmp/fuss-verilator \
 && cd /tmp/fuss-verilator && autoconf \
 && ./configure --prefix=/opt/fuss-tools/verilator \
 && make -j"${BUILD_JOBS}" && make install \
 && sed -i '/#include <list>/a #include <limits>' /opt/fuss-tools/verilator/share/verilator/include/verilated.cpp \
 && rm -rf /tmp/fuss-verilator
RUN git init /tmp/fuss-elf2hex \
 && git -C /tmp/fuss-elf2hex remote add origin https://github.com/sifive/elf2hex.git \
 && git -C /tmp/fuss-elf2hex fetch --depth=1 origin f28a3103c06131ed3895052b1341daf4ca0b1c9c \
 && git -C /tmp/fuss-elf2hex checkout --detach FETCH_HEAD \
 && cd /tmp/fuss-elf2hex && autoreconf -i \
 && ./configure --prefix=/opt/fuss-tools/riscv --target=riscv64-unknown-elf \
 && make -j"${BUILD_JOBS}" && make install \
 && rm -rf /tmp/fuss-elf2hex
COPY fuzzers/fuss/difuzz-rtl/Fuzzer/ISASim/riscv-isa-sim/ /tmp/fuss-spike/
RUN mkdir -p /tmp/fuss-spike-build /opt/fuss-tools/bin \
 && cd /tmp/fuss-spike-build \
 && /tmp/fuss-spike/configure --prefix=/opt/fuss-tools CXXFLAGS='-O2 -g0' \
 && make -j"${BUILD_JOBS}" spike \
 && install -m 0755 spike /opt/fuss-tools/bin/spike \
 && rm -rf /tmp/fuss-spike /tmp/fuss-spike-build

FROM fuss_tools AS fuss
COPY --chown=fuzz:fuzz fuzzers/fuss/ /opt/fuzzers/fuss/
USER fuzz
RUN cd /opt/fuzzers/fuss && ./fuss setup
RUN export PATH=/opt/fuss-tools/venv/bin:/opt/fuss-tools/verilator/bin:$PATH \
 && cd /opt/fuzzers/fuss/difuzz-rtl/Fuzzer \
 && make -j"${BUILD_JOBS}" SIM_BUILD=build/rocket VFILE=RocketTile_state TOPLEVEL=RocketTile build/rocket/RocketTile

FROM base AS spinal
# SpinalFuzz uses Java 8 and the stock Verilator/AFL++ toolchain.
# Keep the headless launch/compiler compatibility names private to this fuzzer.
USER root
COPY --chown=fuzz:fuzz fuzzers/spinalfuzz/ /opt/fuzzers/spinalfuzz/
RUN mkdir -p /opt/spinalfuzz/bin \
 && printf '%s\n' '#!/bin/bash' 'set -e' 'if [[ "${1:-}" == -e ]]; then shift; fi' 'exec "$@"' > /opt/spinalfuzz/bin/x-terminal-emulator \
 && printf '%s\n' '#!/bin/bash' 'exec /usr/bin/afl-clang-fast++ "$@"' > /opt/spinalfuzz/bin/afl-clang-lto++ \
 && printf '%s\n' '#!/bin/bash' 'if [[ "${1:-}" == -v ]]; then shift; fi' 'exec hexdump -v -C "$@"' > /opt/spinalfuzz/bin/hd \
 && chmod +x /opt/spinalfuzz/bin/*
# These fixes affect only the image copy, leaving the pinned fork unchanged.
# Respect the isolated Verilator, avoid host-wide tuning, and support absolute results paths.
RUN python3 - <<'PY'
from pathlib import Path
path = Path('/opt/fuzzers/spinalfuzz/sim/src/main/scala/spinal/sim/fuzz/FuzzBackend.scala')
text = path.read_text()
replacements = {
    '"/usr/local/share/verilator/include"': '"/opt/verilator/share/verilator/include"',
    'var useSysChange = config.withSysChange': 'val requestedSysChange = config.withSysChange && sys.env.get("SPINALFUZZ_SYS_CHANGE").contains("1")\n    var useSysChange = requestedSysChange',
    'if (config.withSysChange) {': 'if (requestedSysChange) {',
    's"cat ../../${file} | bin/V${config.toplevelName}_fuzz pp"': 's"cat ${file.getAbsolutePath} | bin/V${config.toplevelName}_fuzz pp"',
}
for old, new in replacements.items():
    assert text.count(old) == 1, f'SpinalFuzz compatibility patch no longer matches: {old}'
    text = text.replace(old, new)
path.write_text(text)
PY
USER fuzz
RUN cd /opt/fuzzers/spinalfuzz \
 && JAVA_HOME=/opt/java8 PATH="/opt/java8/bin:/opt/spinalfuzz/bin:/opt/verilator/bin:${PATH}" \
    sbt -batch 'project tester' compile 'runMain mylib.GCDVerilog' \
 && test -s tester/GCD.v
USER root

# SymbFuzz's state parser needs the sampled-register names from newer Yosys.
# Retain Ubuntu's version for other tools; select this prefix only for SymbFuzz.
FROM base AS symbfuzz_tools
RUN git clone --depth 1 --branch yosys-0.40 https://github.com/YosysHQ/yosys.git /tmp/yosys \
 && test "$(git -C /tmp/yosys rev-parse HEAD)" = a1bb0255d654ffd0d8503577274f24b349a42995 \
 && make -C /tmp/yosys config-gcc \
 && make -C /tmp/yosys -j"${BUILD_JOBS}" PREFIX=/opt/symbfuzz/yosys ENABLE_ABC=0 ENABLE_TCL=0 ENABLE_READLINE=0 \
 && make -C /tmp/yosys install PREFIX=/opt/symbfuzz/yosys ENABLE_ABC=0 ENABLE_TCL=0 ENABLE_READLINE=0 \
 && rm -rf /tmp/yosys

FROM base AS final
LABEL org.opencontainers.image.source="https://github.com/KastnerRG/oss-hw-fuzz"
# Runtime dependencies for optional, read-only host EDA tool mounts.
# Verdi's /bin/sh launcher uses Bash syntax, as in the reference setup.
# FuSS's freestanding RISC-V templates need Newlib headers in GCC's target include path.
RUN apt-get update && apt-get install -y --no-install-recommends \
      locales tcsh ksh dc lsb-release libgl1 libelf1 libtinfo5 libncurses5 libnuma1 libxtst6 \
      libx11-xcb1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
      libxcb-render-util0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 \
      libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 \
      gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf device-tree-compiler \
      libboost-regex1.74.0 libboost-system1.74.0 libnewlib-dev \
 && locale-gen en_US.UTF-8 \
 && ln -sf /bin/bash /bin/sh \
 && mkdir -p /usr/riscv64-unknown-elf \
 && ln -s /usr/include/newlib /usr/riscv64-unknown-elf/include \
 && rm -rf /var/lib/apt/lists/*
# TestMAX needs the older PNG ABI, absent from Ubuntu 22.04.
# Extract only that library, avoiding the old package's pre-usrmerge symlinks.
RUN curl -fLsS --retry 3 -o /tmp/libpng12.deb \
      https://archive.ubuntu.com/ubuntu/pool/main/libp/libpng/libpng12-0_1.2.54-1ubuntu1.1_amd64.deb \
 && echo '9d938a376dafd1668827ceeb742f18b164c390e53774b06eaaf644713b6ffb25  /tmp/libpng12.deb' | sha256sum -c - \
 && dpkg-deb -x /tmp/libpng12.deb /tmp/libpng12 \
 && install -m 0644 /tmp/libpng12/lib/x86_64-linux-gnu/libpng12.so.0.54.0 /usr/local/lib/ \
 && ln -s libpng12.so.0.54.0 /usr/local/lib/libpng12.so.0 \
 && install -D -m 0644 /tmp/libpng12/usr/share/doc/libpng12-0/copyright /usr/local/share/doc/libpng12/copyright \
 && ldconfig \
 && rm -rf /tmp/libpng12 /tmp/libpng12.deb
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
COPY --from=symbfuzz_tools /opt/symbfuzz/ /opt/symbfuzz/
COPY --from=sv /opt/fuzzers/ /opt/fuzzers/
COPY --from=sv /opt/hw_like_sw/ /opt/hw_like_sw/
COPY --from=hwfuzz /opt/hw_fuzzing_afl/ /opt/hw_fuzzing_afl/
COPY --from=hwfuzz /opt/fuzzers/hw_fuzzing_afl/ /opt/fuzzers/hw_fuzzing_afl/
COPY --from=presifuzz /opt/fuzzers/presifuzz/ /opt/fuzzers/presifuzz/
COPY --from=presifuzz /opt/presifuzz/ /opt/presifuzz/
COPY --from=firrtl /opt/fuzzers/ /opt/fuzzers/
COPY --from=firrtl /opt/afl-rtl-fuzz-lab/ /opt/afl-rtl-fuzz-lab/
COPY --from=firrtl /home/fuzz/ /home/fuzz/
COPY --from=fuss /opt/fuzzers/fuss/ /opt/fuzzers/fuss/
COPY --from=fuss /opt/fuss-tools/ /opt/fuss-tools/
COPY --from=spinal /opt/fuzzers/ /opt/fuzzers/
COPY --from=spinal /opt/spinalfuzz/ /opt/spinalfuzz/
COPY --from=spinal /home/fuzz/ /home/fuzz/
COPY --from=nocfuzzer /opt/fuzzers/nocfuzzer/ /opt/fuzzers/nocfuzzer/
COPY --chmod=755 scripts/run-examples.py /usr/local/bin/run-examples
COPY --chmod=755 scripts/tool-env.py /usr/local/bin/tool-env
COPY scripts/integration-checks/ /opt/integration-checks/
USER fuzz
WORKDIR /opt/fuzzers
CMD ["bash"]
