SHELL := /bin/bash
.DEFAULT_GOAL := help

-include config.mk

DOCKER ?= docker
IMAGE ?= oss-hw-fuzz:dev
PUBLISHED_IMAGE ?= ghcr.io/kastnerrg/oss-hw-fuzz:latest
CONTAINER ?= oss-hw-fuzz-$(shell id -un)
BUILD_JOBS ?= 8
DURATION ?= 30
FUZZER ?= all
RESULTS ?= $(CURDIR)/results
SECCOMP_PROFILE ?= $(CURDIR)/docker/seccomp-hwfuzz.json

# Ordered, colon-separated lists, independent of vendor and installation layout.
# Existing roots are mounted read-only at the same absolute paths.
# TOOL_PATHS pins versions or adds binaries beyond the automatic EDA discovery.
TOOL_ROOTS ?= /tools:$(HOME)/install:$(HOME)/scratch/install
TOOL_PATHS ?=
export IMAGE CONTAINER SECCOMP_PROFILE TOOL_ROOTS TOOL_PATHS

# Inherit licenses by name so command output never includes their values.
LICENSE_ENV_VARS ?= LM_LICENSE_FILE XILINXD_LICENSE_FILE SNPSLMD_LICENSE_FILE CDS_LIC_FILE MGLS_LICENSE_FILE
export $(LICENSE_ENV_VARS)
export LICENSE_ENV_VARS
LICENSE_ARGS = $(foreach var,$(LICENSE_ENV_VARS),-e $(var))

.PHONY: help init scratch build publish image tools start enter kill restart run run_all run_all_oss_backend

help:
	@echo 'make scratch   Build the unified image from source (no cache)'
	@echo 'make build     Rebuild the unified image using the Docker cache'
	@echo 'make publish   Push IMAGE to PUBLISHED_IMAGE using your Docker login'
	@echo 'make image     Pull PUBLISHED_IMAGE and tag it as IMAGE'
	@echo 'make start     Start the container; make enter opens a shell'
	@echo 'make tools     Show mounted tool roots, selected tools, and container PATH'
	@echo 'make run_all   Run all examples; save logs and status under results/'
	@echo 'make run_all_oss_backend   Run examples without commercial EDA tools'
	@echo 'make run FUZZER=rfuzz DURATION=30   Run one example'
	@echo 'make kill / restart   Remove / recreate this container'

# Initialize only the nested repositories needed by these examples.
init:
	git submodule update --init
	git -C fuzzers/hyperfuzzer submodule update --init -- verilator
	git -C fuzzers/fuss submodule update --init -- difuzz-rtl

scratch: init
	$(DOCKER) build --pull --no-cache --build-arg BUILD_JOBS="$(BUILD_JOBS)" -t "$(IMAGE)" .

# Cached rebuilds are useful while working on this Dockerfile.
build: init
	$(DOCKER) build --build-arg BUILD_JOBS="$(BUILD_JOBS)" -t "$(IMAGE)" .

publish:
	$(DOCKER) tag "$(IMAGE)" "$(PUBLISHED_IMAGE)"
	$(DOCKER) push "$(PUBLISHED_IMAGE)"

image:
	$(DOCKER) pull "$(PUBLISHED_IMAGE)"
	$(DOCKER) tag "$(PUBLISHED_IMAGE)" "$(IMAGE)"

tools:
	@bash scripts/container-tools.sh tools $(DOCKER)

start:
	@bash scripts/container-tools.sh start $(DOCKER)

enter:
	$(DOCKER) exec -it $(LICENSE_ARGS) "$(CONTAINER)" bash

kill:
	@if $(DOCKER) container inspect "$(CONTAINER)" >/dev/null 2>&1; then \
		$(DOCKER) rm --force "$(CONTAINER)"; \
	fi

restart: kill
	$(MAKE) start

run_all:
	$(MAKE) run FUZZER=all

run_all_oss_backend:
	$(MAKE) run FUZZER=oss_backend

run: start
	@status=0; \
	$(DOCKER) exec -e DURATION="$(DURATION)" -e BUILD_JOBS="$(BUILD_JOBS)" \
		$(LICENSE_ARGS) \
		"$(CONTAINER)" /usr/local/bin/run-examples "$(FUZZER)" || status=$$?; \
	mkdir -p "$(RESULTS)" || exit $$?; \
	$(DOCKER) cp "$(CONTAINER):/results/." "$(RESULTS)/" || exit $$?; \
	exit $$status
