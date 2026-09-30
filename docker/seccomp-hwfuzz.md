`seccomp-hwfuzz.json` derives from the [Moby default seccomp profile](https://github.com/moby/profiles/blob/a21872828a8e5745d79e2fc1a07ee6dad8aedffa/seccomp/default.json), pinned at commit `a21872828a8e5745d79e2fc1a07ee6dad8aedffa`.
The upstream file's SHA-256 is `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6`.
The Moby Authors license it under Apache-2.0; the unchanged license is included in [LICENSE.moby-profiles](LICENSE.moby-profiles).

The only modification prepends an allow rule for `personality(0x40000)`, exactly `ADDR_NO_RANDOMIZE`, when Docker identifies the native architecture as `amd64`.
HWFuzzing's modified MemorySanitizer uses this call to restart its process with a compatible shadow-memory layout when address randomization places a mapping in its reserved range.
Other personality arguments and all upstream rules remain unchanged, including the default deny action and capability conditions.
Docker's architecture condition selects the rule by native host architecture; the upstream compatibility-architecture map is preserved.

`make start` uses this profile by default when creating a container.
Override the path with `make start SECCOMP_PROFILE=/absolute/path/to/profile.json` if needed.
Existing containers retain the profile selected when they were created, including after `make start` starts them again.
To apply this profile to an existing container, save any needed container files or results first, then use `make restart` to remove and recreate the container.

For a direct Docker invocation, use `docker run --security-opt "seccomp=/absolute/path/to/docker/seccomp-hwfuzz.json" ...`.
The profile requires no additional capabilities or host kernel setting changes.
