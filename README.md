# Valhalla Slice

A high-performance Python microservice designed to stream Valhalla planetary routing tiles as TAR archives based on MapLibre Z, X, Y coordinates.

## HTTP endpoints

### `/valhalla+{z}-{x}-{y}.tgz`
Stream a GZIP-compressed Valhalla TAR archive containing all the Valhalla slices required to cover the OSM Z/X/Y tile.

### `/valhalla.tgz`
Stream a GZIP-compressed Valhalla TAR archive containing the Valhalla slices that overlap a polygon.

* `GET /valhalla.tgz?polygon=<polygon>`: the polygon URL-encoded in the `polygon` parameter.
* `POST /valhalla.tgz`: the polygon as the raw request body, up to 64 MiB. The `polygon` parameter is rejected.

The polygon is GeoJSON when its first non-space character is `{`, otherwise an Osmosis `.poly` file:

* **GeoJSON:** a FeatureCollection, a Feature or a bare geometry. Polygon and MultiPolygon geometries are used, others are ignored.
* **Osmosis `.poly`:** a name line, then rings, each a header line, `lon lat` lines and `END`, then a final `END`. A ring whose header starts with `!` is a hole in each preceding ring that contains it.

Longitudes must be within -180..180 and latitudes within -90..90. Each ring is closed automatically and needs at least 3 distinct points.

At each Valhalla level, a slice is selected when it overlaps the polygon by a positive area; slices that only touch the polygon along an edge or at a corner are not selected. This is the same rule the Z/X/Y endpoint applies to the tile bounds.

Invalid, missing or repeated polygons return 400.

```bash
curl -o area.tgz "http://valhalla-slice:8000/valhalla.tgz?polygon=$(jq -sRr @uri area.geojson)"
curl -o area.tgz --data-binary @area.poly http://valhalla-slice:8000/valhalla.tgz
```

### `/valhalla+{z}-{x}-{y}.tar` and `/valhalla.tar`
Stream the same Valhalla TAR archive as the matching `.tgz` endpoint, uncompressed. They take the same arguments and return the same errors. The `Content-Length` header is computed from the index, so it is sent before any tile data is read.

### `/list`
List the contents of the source Valhalla TAR archive. Mainly for debug purposes.

## Architecture & Features

* **High-Performance Streaming:** Extracts and serves routing tiles directly from an uncompressed Valhalla `.tar` archive.
* **Thread-Safe I/O:** Utilizes `os.pread` to prevent shared file offset race conditions during concurrent tile requests, maintaining a strict single-process execution model.
* **Zero-Downtime Hot-Reloading:** Employs `os.stat` to poll for inode changes. Detected updates trigger an atomic state swap via `swap_lock` and an `ArchiveState` reference-counted file management system.
* **Coordinate Precision:** Uses `Decimal` and `math.ceil` to accurately resolve grid boundary conditions across Valhalla's specific 4°, 1°, and 0.25° hierarchy levels.
* **Valhalla Indexing:** Efficiently parses the 16-byte `index.bin` records (8-byte offset, 4-byte ID, 4-byte length) without silent failures.

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

## Tests

```bash
pip install docopt fastapi uvicorn mercantile shapely httpx2 pytest
pytest test_valhalla_slice.py
```
