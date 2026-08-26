# Project Summary: Valhalla TAR Slicing Service (`valhalla-slice`)

## Overview
`valhalla-slice` is a high-performance Python microservice (built with FastAPI) designed to stream Valhalla planetary routing tiles as dynamic, uncompressed TAR archives based on MapLibre Z, X, Y coordinates. It performs direct thread-safe disk I/O (`os.pread`) to extract specific `.gph` files and their `index.bin` from a massive Valhalla `valhalla_tiles.tar` archive.

## Core Architecture
*   **API Endpoints:** 
    *   `/valhalla-{z}-{x}-{y}.tar`: Streams a valid, on-the-fly TAR archive containing the requested tiles and a dynamically generated `index.bin`.
    *   `/list`: Outputs a `tar -tvf` style text list of the archive's contents.
*   **State Management:** Uses an `ArchiveState` class with reference counting and a `StateManager` that polls `os.stat` for inode changes, allowing zero-downtime hot-reloads of the underlying tarball.
*   **File I/O:** Bypasses `tarfile` extraction in favor of direct `os.pread()` for highly concurrent, lock-free tile reads.

## Recent Fixes & Modifications
During the latest development session, several critical bugs were resolved to ensure alignment with standard Valhalla archive structures and POSIX TAR specifications:

1.  **Binary Struct Unpacking:** Corrected the byte parsing of the Valhalla `index.bin` file. The correct sequence is an 8-byte offset, a 4-byte GraphId (Tile ID), and a 4-byte length. 
2.  **GraphId Bit Alignment:** Fixed the bitwise decoding of Valhalla's Graph IDs. The script originally assumed 64-bit alignment, but `index.bin` stores a 32-bit packed ID where:
    *   `level` = bits 0-2 (extracted via `tile_id & 7`)
    *   `tile_index` = bits 3-24 (extracted via `(tile_id >> 3) & 0x3FFFFF`)
3.  **Path Generation (`get_tile_path`):** Added a helper to correctly format internal `.gph` tar paths. Level 2 tiles are formatted as `2/XXX/YYY/ZZZ.gph`, while levels 0 and 1 use `L/XXX/YYY.gph`.
4.  **TAR Header Re-use:** 
    *   The dynamic TAR stream now fetches and re-uses the original 512-byte POSIX headers for individual `.gph` tiles directly from the source archive, preserving accurate `mtime` and permissions.
    *   The `/list` endpoint was updated to decode the octal `mtime` from these original headers to perfectly mirror `tar -tvf` outputs.
5.  **Dynamic `index.bin` Generation:** When streaming a coordinate slice, the service now generates a custom `index.bin` specifically for the streamed tiles. It re-uses the source `index.bin` header but patches the file size, modification time (to current time), and recalculates the TAR header checksum.
6.  **Timezone Handling:** Removed hardcoded Python `TZ` environment variables. The service relies on Docker volume mounts (`/etc/localtime` and `/etc/timezone`) to sync container time with the host.
7.  **CLI Updates:** Swapped the default logging behavior. The service is now verbose by default, and the `--verbose` (`-v`) flag was replaced with a `--quiet` (`-q`) flag.

## Current State
The service is fully functional, capable of intercepting XYZ tile requests, identifying intersecting Valhalla tiles across all 3 levels (4.0°, 1.0°, 0.25°), and assembling a perfectly valid, on-the-fly TAR stream that MapLibre or Valhalla workers can consume directly.