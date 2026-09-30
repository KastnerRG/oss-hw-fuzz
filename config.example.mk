# Copy to config.mk for persistent machine-specific settings.
# Lists use colons; paths may contain spaces, but do not add shell quotes.
TOOL_ROOTS := /tools:$(HOME)/install:$(HOME)/scratch/install

# Optional explicit binary directories, searched before auto-discovered tools.
# The image's own binaries always remain first on PATH.
TOOL_PATHS :=
# TOOL_PATHS := /tools/cadence/GENUS211/bin:$(HOME)/install/custom-tool/bin
