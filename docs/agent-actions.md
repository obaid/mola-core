# Agent Actions Log

## 2026-11-10: Fix architecture mismatch bug

### Problem
On Linux x86_64, `npx mola-core` downloads the default Omarchy image which is aarch64. This causes the guest to hang silently with no console output or error message when booted under QEMU/KVM. The user gets stuck with no explanation.

### Root Cause Analysis
1. **Image download** (`src/image.js`): The `downloadImage()` function fetches a manifest from a URL and downloads all artifacts without validating that the image architecture matches the host architecture.

2. **Architecture detection** (`src/preflight.js`, `src/runtime.js`): The host architecture is correctly detected:
   - macOS → `aarch64`
   - Linux x86_64 → `x86_64`

3. **Late validation** (`runtime/native/host.py` lines 124-126): The Python runtime validates architecture when booting a machine, but by then the wrong image is already installed. When an aarch64 image runs on x86_64 under QEMU without proper error handling, it hangs silently.

### Solution
1. **Early architecture validation**: Add architecture checking to `downloadImage()` to fail fast with a clear error message before downloading an incompatible image.

2. **Manifest architecture field**: Parse the `architecture` field from the manifest (if present) and validate it against the host architecture.

3. **Clear error messages**: When there's a mismatch, inform the user of:
   - Host architecture
   - Image architecture  
   - Available alternatives (e.g., "use --image ubuntu" or specify MOLA_IMAGE_REF)

4. **Test coverage**: Add tests for architecture validation and mismatch scenarios.

### Implementation
- Modified `src/image.js` to validate architecture during download
- Added architecture parameter to `downloadImage()` 
- Updated `bin/mola.js` to pass host architecture to download function
- Added test cases in `test/image-architecture.test.js`
- Updated README if needed to clarify supported architectures per image

### Testing
- Test that x86_64 host rejects aarch64 manifest
- Test that aarch64 host rejects x86_64 manifest  
- Test that matching architectures succeed
- Test that manifests without architecture field still work (backward compatibility)
