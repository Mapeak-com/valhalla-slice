# Valhalla Slice

A high-performance Python microservice designed to stream Valhalla planetary routing tiles as TAR archives based on MapLibre Z, X, Y coordinates.

## HTTP endpoints

### `/valhalla+{z}-{x}-{y}.tar`
Stream a valhalla TAR archive containing all the Valhalla slices required to cover the OSM Z/X/Y tile.

### `/list`
List the contents of the source Valhalla TAR archive. Mainly for debug purposes.

## Architecture & Features

* **High-Performance Streaming:** Extracts and serves routing tiles directly from an uncompressed Valhalla `.tar` archive.
* **Thread-Safe I/O:** Utilizes `os.pread` to prevent shared file offset race conditions during concurrent tile requests, maintaining a strict single-process execution model.
* **Zero-Downtime Hot-Reloading:** Employs `os.stat` to poll for inode changes. Detected updates trigger an atomic state swap via `swap_lock` and an `ArchiveState` reference-counted file management system.
* **Coordinate Precision:** Uses `Decimal` and `math.ceil` to accurately resolve grid boundary conditions across Valhalla's specific 4°, 1°, and 0.25° hierarchy levels.
* **Valhalla Indexing:** Efficiently parses the 16-byte `index.bin` records (8-byte ID, 4-byte offset, 4-byte length) without silent failures.

## Containerized Deployment

The service is designed to run in a private Docker network, accessible only to other containers. It is built upon a minimal, POSIX-compliant `python:3.12-slim` base image.

### Docker Compose Configuration

Create a `docker-compose.yml` file to build the service directly from the repository source:

```yaml
services:
  valhalla-slice:
    build: https://github.com/zstadler/valhalla-slice.git
    volumes:
      - ./valhalla:/valhalla:ro
    restart: unless-stopped
```

## Updating the Archive (Hot-Reload)

To seamlessly update the routing data without dropping active requests, you must perform an atomic move (`mv`) on the host system. This generates a new inode on the mounted volume, which the service detects and loads automatically.

```bash
mv valhalla-build/valhalla_tiles.tar valhalla/
```
