# Copy to config.mk for persistent machine-specific settings.
# Lists use colons; paths may contain spaces, but do not add shell quotes.
TOOL_ROOTS := /tools:$(HOME)/install:$(HOME)/scratch/install

# Optional explicit binary directories, searched before auto-discovered tools.
# The image's own binaries always remain first on PATH.
TOOL_PATHS :=
# TOOL_PATHS := /tools/cadence/GENUS211/bin:$(HOME)/install/custom-tool/bin

# Optional site-specific license servers inherited by the container.
# SNPSLMD_LICENSE_FILE := port@server
# CDS_LIC_FILE := port@server
# XILINXD_LICENSE_FILE := port@server

# Mount this project or a parent containing multiple projects at /workspace.
WORKSPACE := $(CURDIR)
SHM_SIZE := 1g
