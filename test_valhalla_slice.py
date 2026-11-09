"""Tests for the valhalla-slice service."""
import gzip
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import tarfile
import threading
import time
import pytest

sys.dont_write_bytecode = True

def load_service():
  """Loads the valhalla-slice script as a module."""
  path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valhalla-slice")
  loader = importlib.machinery.SourceFileLoader("valhalla_slice", path)
  spec = importlib.util.spec_from_loader("valhalla_slice", loader)
  module = importlib.util.module_from_spec(spec)
  loader.exec_module(module)
  return module

vs = load_service()
vs.ensure_package("shapely")
vs.ensure_package("httpx2")
from shapely.geometry import Polygon, box
from fastapi.testclient import TestClient

LEVELS = [(0, 4.0), (1, 1.0), (2, 0.25)]
ARCHIVE_AREA = (34.0, 29.0, 36.0, 33.5)
SQUARE = [[34.5, 30.5], [35.5, 30.5], [35.5, 31.5], [34.5, 31.5], [34.5, 30.5]]
TRIANGLE = [(34.1, 29.3), (35.9, 30.2), (34.6, 33.2)]

def square_poly():
  """Returns SQUARE as an Osmosis .poly file."""
  lines = ["square", "1"] + [f"  {lon} {lat}" for lon, lat in SQUARE] + ["END", "END"]
  return "\n".join(lines) + "\n"

def triangle_poly():
  """Returns TRIANGLE as an unclosed-ring Osmosis .poly file."""
  lines = ["triangle", "area"] + [f"\t{lon:.6E}\t{lat:.6E}" for lon, lat in TRIANGLE] + ["END", "END"]
  return "\n".join(lines) + "\n"

def triangle_geojson():
  """Returns TRIANGLE as a GeoJSON FeatureCollection."""
  ring = [list(p) for p in TRIANGLE] + [list(TRIANGLE[0])]
  return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]})

def cell_box(level, tile_id):
  """Returns the lon/lat box of a level's cell given its GraphID."""
  size = LEVELS[level][1]
  cols = int(360 / size)
  tile_index = tile_id >> 3
  col = tile_index % cols
  row = tile_index // cols
  return box(col * size - 180, row * size - 90, (col + 1) * size - 180, (row + 1) * size - 90)

def oracle_tile_ids(shape):
  """Returns the GraphIDs of the cells whose intersection with a shapely shape has a positive area."""
  tile_ids = []
  for level, size in LEVELS:
    cols = int(360 / size)
    rows = int(180 / size)
    bounds = shape.bounds
    first_col = max(0, math.floor((bounds[0] + 180) / size))
    last_col = min(cols - 1, math.ceil((bounds[2] + 180) / size))
    first_row = max(0, math.floor((bounds[1] + 90) / size))
    last_row = min(rows - 1, math.ceil((bounds[3] + 90) / size))
    for row in range(first_row, last_row + 1):
      for col in range(first_col, last_col + 1):
        tile_id = (((row * cols) + col) << 3) | level
        if shape.intersection(cell_box(level, tile_id)).area > 1e-9 * size * size:
          tile_ids.append(tile_id)
  return tile_ids

def to_polygons(shape):
  """Converts a shapely Polygon into the parser's list of polygons."""
  rings = [list(shape.exterior.coords)[:-1]] + [list(hole.coords)[:-1] for hole in shape.interiors]
  return [[[(p[0], p[1]) for p in ring] for ring in rings]]

def random_star(rng, center, radius, count, step=None):
  """Returns a star-shaped polygon, with vertices optionally rounded to a grid step."""
  points = []
  for i in range(count):
    angle = 2 * math.pi * (i + rng.random() * 0.8) / count
    r = radius * (0.3 + 0.7 * rng.random())
    lon = center[0] + r * math.cos(angle)
    lat = center[1] + r * math.sin(angle)
    if step:
      lon = round(lon / step) * step
      lat = round(lat / step) * step
    points.append((min(180.0, max(-180.0, lon)), min(90.0, max(-90.0, lat))))
  return Polygon(points)

def archive_tile_ids():
  """Returns the GraphIDs of every cell overlapping ARCHIVE_AREA at each level."""
  tile_ids = []
  for level, size in LEVELS:
    cols = int(360 / size)
    for row in range(math.floor((ARCHIVE_AREA[1] + 90) / size), math.ceil((ARCHIVE_AREA[3] + 90) / size)):
      for col in range(math.floor((ARCHIVE_AREA[0] + 180) / size), math.ceil((ARCHIVE_AREA[2] + 180) / size)):
        tile_ids.append((((row * cols) + col) << 3) | level)
  return tile_ids

TILE_MTIME = 1700000000
LARGE_MTIME = 2 ** 34

def add_member(tar, name, data, mtime=TILE_MTIME):
  """Adds a regular file member to a tar archive."""
  info = tarfile.TarInfo(name)
  info.size = len(data)
  info.mtime = mtime
  tar.addfile(info, io.BytesIO(data))

def build_archive(path, tile_ids, with_index=True, large_mtime_tile=None):
  """Writes a Valhalla tile archive: index.bin followed by one .gph member per tile; returns the tile contents."""
  contents = {}
  for tile_id in tile_ids:
    contents[tile_id] = tile_id.to_bytes(4, "little") * ((tile_id % 300) + 1)
  paths = {vs.get_tile_path(t): t for t in tile_ids}
  offsets = {t: 0 for t in tile_ids}
  tar_format = tarfile.GNU_FORMAT if large_mtime_tile else tarfile.USTAR_FORMAT
  for _ in range(2):
    index = b"".join(struct.pack("<QLL", offsets[t], t, len(contents[t])) for t in tile_ids)
    with tarfile.open(path, "w", format=tar_format) as tar:
      if with_index:
        add_member(tar, "index.bin", index)
      for tile_id in tile_ids:
        add_member(tar, vs.get_tile_path(tile_id), contents[tile_id], LARGE_MTIME if tile_id == large_mtime_tile else TILE_MTIME)
    with tarfile.open(path) as tar:
      for member in tar.getmembers():
        if member.name in paths:
          offsets[paths[member.name]] = member.offset_data
  return contents

def read_tar(raw):
  """Returns the members of a .tar response, checking that index.bin points at each tile's data."""
  with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
    members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
  index = members["index.bin"]
  for i in range(0, len(index), 16):
    record = struct.unpack("<QLL", index[i:i + 16])
    assert raw[record[0]:record[0] + record[2]] == members[vs.get_tile_path(record[1])]
  return members

def read_tgz(data):
  """Returns the members of a .tgz response, checking that index.bin points at each tile's data."""
  return read_tar(gzip.decompress(data))

@pytest.fixture(scope="module")
def service(tmp_path_factory):
  """Starts the app over a synthetic archive and returns the test client and tile contents."""
  path = tmp_path_factory.mktemp("archive") / "valhalla_tiles.tar"
  contents = build_archive(str(path), archive_tile_ids())
  vs.state_manager = vs.StateManager(str(path), verbose=False)
  return (TestClient(vs.app), contents)

@pytest.mark.parametrize("text", [
  json.dumps({"type": "Polygon", "coordinates": [SQUARE]}),
  json.dumps({"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [SQUARE]}}),
  json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}},
    {"type": "Feature", "geometry": None},
    {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [[SQUARE]]}}]}),
  "  \n" + json.dumps({"type": "Polygon", "coordinates": [SQUARE[:-1]]}),
  square_poly(),
])
def test_parse_square(text):
  """Every supported form of the same square parses to one ring without the closing point."""
  assert vs.parse_polygon(text) == [[[tuple(p) for p in SQUARE[:-1]]]]

def test_parse_poly_blank_lines():
  """Blank lines between and inside rings of a .poly file are ignored."""
  text = "name\n\n1\n0 0\n\n1 0\n1 1\nEND\n\nEND\n\n"
  assert vs.parse_polygon(text) == [[[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]]]

def test_parse_poly_holes():
  """A '!' ring becomes a hole of each preceding ring that contains it, and only those."""
  text = "\n".join([
    "name", "outer", "0 0", "10 0", "10 10", "0 10", "END",
    "other", "20 0", "30 0", "30 10", "END",
    "!hole", "2 2", "4 2", "4 4", "END",
    "END", ""])
  polygons = vs.parse_polygon(text)
  assert len(polygons) == 2
  assert polygons[0][1] == [(2.0, 2.0), (4.0, 2.0), (4.0, 4.0)]
  assert len(polygons[1]) == 1

@pytest.mark.parametrize("text", [
  "name\n1\n0 0\n1 0\n1 1\n",
  "name\n1\n0 0\n1 0\n1 1\nEND\n",
  "name\n1\n0 0\n1 0\n1 1\nEND\nEND\nextra\n",
  json.dumps({"type": "Polygon", "coordinates": [{}]}),
  json.dumps({"type": "Polygon", "coordinates": [[[0, 0], 5, [1, 1]]]}),
  json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1], [1, 1]]]}),
  json.dumps({"type": "Feature", "geometry": []}),
  json.dumps({"type": "MultiPolygon", "coordinates": {}}),
  json.dumps({"type": "Polygon", "coordinates": [[[0, 0], ["a", 0], [1, 1]]]}),
  "name\n1\n0 0\n1 x\n1 1\nEND\nEND\n",
  "name\n1\n0 0\n1 0 0\n1 1\nEND\nEND\n",
  "name\n1\n0 0\n181 0\n1 1\nEND\nEND\n",
  "name\n1\n0 0\n1 -91\n1 1\nEND\nEND\n",
  "name\n1\n0 0\nnan 0\n1 1\nEND\nEND\n",
  "name\n1\n0 0\n1 0\n0 0\nEND\nEND\n",
  "name\nEND\n",
  "",
  "{",
  json.dumps({"type": "Point", "coordinates": [0, 0]}),
  json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [True, 1]]]}),
  json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [200, 0], [1, 1]]]}),
  json.dumps({"type": "Polygon", "coordinates": []}),
  json.dumps({"type": "FeatureCollection", "features": {}}),
])
def test_parse_errors(text):
  """Invalid polygons raise ValueError."""
  with pytest.raises(ValueError):
    vs.parse_polygon(text)

def test_matches_tile_selection():
  """A polygon of a map tile's bounds selects the same cells as the z/x/y endpoint."""
  rng = random.Random(1)
  tiles = [(0, 0, 0), (1, 1, 0), (1, 0, 1), (3, 4, 2), (7, 76, 52), (10, 612, 415)]
  for _ in range(300):
    z = rng.randint(3, 12)
    tiles.append((z, rng.randrange(2 ** z), rng.randrange(2 ** z)))
  for z, x, y in tiles:
    b = vs.mercantile.bounds(x, y, z)
    polygons = [[[(b.west, b.south), (b.east, b.south), (b.east, b.north), (b.west, b.north)]]]
    assert vs.get_valhalla_tile_ids_polygon(polygons) == vs.get_valhalla_tile_ids(z, x, y), (z, x, y)

@pytest.mark.parametrize("seed", range(40))
def test_matches_oracle(seed):
  """Random polygons, with holes and with vertices on grid lines, select exactly the cells they overlap by a positive area."""
  rng = random.Random(seed)
  center = (rng.uniform(-170, 170), rng.uniform(-80, 80))
  radius = rng.choice([0.2, 1.0, 3.0, 8.0])
  step = rng.choice([None, 0.25, 1.0]) if seed % 2 else None
  shape = random_star(rng, center, radius, rng.randint(3, 25), step)
  if not shape.is_valid or shape.area == 0:
    pytest.skip("invalid random shape")
  if seed % 3 == 0:
    hole = random_star(rng, center, radius * 0.25, rng.randint(3, 10))
    if shape.contains(hole):
      shape = Polygon(shape.exterior.coords, [hole.exterior.coords])
  assert vs.get_valhalla_tile_ids_polygon(to_polygons(shape)) == oracle_tile_ids(shape)

@pytest.mark.parametrize("coords", [
  [(178, -20), (180, -20), (180, -15), (178, -15)],
  [(-180, -20), (-178, -20), (-178.5, -15), (-180, -15)],
  [(170, 80), (180, 80), (180, 90), (170, 90)],
  [(-180, -90), (-170.3, -90), (-175, -80.1)],
  [(0, 0), (0.25, 0), (0.25, 0.25), (0, 0.25)],
  [(0, 0), (1, 1), (0, 2), (-1, 1)],
])
def test_world_edges_and_grid_lines(coords):
  """Polygons along lon/lat limits and grid lines select only the cells they overlap."""
  shape = Polygon(coords)
  assert vs.get_valhalla_tile_ids_polygon(to_polygons(shape)) == oracle_tile_ids(shape)

def test_whole_world():
  """The whole world selects every cell at every level, and nothing beyond."""
  polygons = [[[(-180, -90), (180, -90), (180, 90), (-180, 90)]]]
  tile_ids = vs.get_valhalla_tile_ids_polygon(polygons)
  counts = [sum(1 for t in tile_ids if t & 7 == level) for level, _ in LEVELS]
  assert counts == [90 * 45, 360 * 180, 1440 * 720]

def test_get_and_post_return_same_archive(service):
  """The same area as GeoJSON in GET and as .poly in POST returns the same tiles and index."""
  client = service[0]
  contents = service[1]
  get_response = client.get("/valhalla.tgz", params={"polygon": triangle_geojson()})
  post_response = client.post("/valhalla.tgz", content=triangle_poly())
  assert get_response.status_code == 200
  assert post_response.status_code == 200
  assert get_response.headers["content-disposition"] == "attachment; filename=valhalla.tgz"
  assert int(get_response.headers["content-length"]) == len(get_response.content)
  get_members = read_tgz(get_response.content)
  post_members = read_tgz(post_response.content)
  assert get_members == post_members
  expected = [t for t in oracle_tile_ids(Polygon(TRIANGLE)) if t in contents]
  assert expected
  assert sorted(get_members) == sorted(["index.bin"] + [vs.get_tile_path(t) for t in expected])
  for tile_id in expected:
    assert get_members[vs.get_tile_path(tile_id)] == contents[tile_id]

def test_tile_endpoint(service):
  """The z/x/y endpoint returns the tiles of get_valhalla_tile_ids under its own file name."""
  client = service[0]
  contents = service[1]
  response = client.get("/valhalla+7-76-52.tgz")
  assert response.status_code == 200
  assert response.headers["content-disposition"] == "attachment; filename=valhalla+7-76-52.tgz"
  members = read_tgz(response.content)
  expected = [vs.get_tile_path(t) for t in vs.get_valhalla_tile_ids(7, 76, 52) if t in contents]
  assert expected
  assert sorted(members) == sorted(["index.bin"] + expected)

@pytest.mark.parametrize("method,name,params,body", [
  ("GET", "valhalla+7-76-52", None, None),
  ("GET", "valhalla", {"polygon": triangle_geojson()}, None),
  ("POST", "valhalla", None, triangle_poly()),
])
def test_tar_endpoints(service, method, name, params, body):
  """Each .tar endpoint returns the tiles and index of its .tgz endpoint uncompressed, with a Content-Length that matches its body."""
  client = service[0]
  tar_response = client.request(method, f"/{name}.tar", params=params, content=body)
  tgz_response = client.request(method, f"/{name}.tgz", params=params, content=body)
  assert tar_response.status_code == 200
  assert tar_response.headers["content-type"] == "application/x-tar"
  assert tar_response.headers["content-disposition"] == f"attachment; filename={name}.tar"
  assert int(tar_response.headers["content-length"]) == len(tar_response.content)
  assert read_tar(tar_response.content) == read_tgz(tgz_response.content)

@pytest.mark.parametrize("method,url,body,status", [
  ("GET", "/valhalla.tgz", None, 400),
  ("GET", "/valhalla.tgz?polygon=", None, 400),
  ("GET", "/valhalla.tgz?polygon=%7B", None, 400),
  ("GET", "/valhalla.tgz?polygon=a&polygon=b", None, 400),
  ("POST", "/valhalla.tgz", b"", 400),
  ("POST", "/valhalla.tgz", b" \n", 400),
  ("POST", "/valhalla.tgz", b"\xff\xfe", 400),
  ("POST", "/valhalla.tgz?polygon=x", square_poly().encode(), 400),
  ("POST", "/valhalla.tgz", b"name\n1\n0 0\n", 400),
  ("GET", "/valhalla.tar", None, 400),
  ("POST", "/valhalla.tar", b"", 400),
])
def test_http_errors(service, method, url, body, status):
  """Missing, repeated, misplaced and invalid polygons are rejected."""
  response = service[0].request(method, url, content=body)
  assert response.status_code == status

def test_post_too_large(service, monkeypatch):
  """A body over MAX_POLYGON_BYTES is rejected with 413."""
  monkeypatch.setattr(vs, "MAX_POLYGON_BYTES", 10)
  response = service[0].post("/valhalla.tgz", content=square_poly())
  assert response.status_code == 413

def read_tgz_infos(data):
  """Returns the TarInfo of each member of a .tgz response by name."""
  with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
    return {m.name: m for m in tar.getmembers()}

def list_date(mtime):
  """Formats a modification time as the /list endpoint does."""
  return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))

def test_archive_header_times(service):
  """Tiles keep the mtime of their original headers and the generated index.bin gets the current time."""
  start = int(time.time())
  response = service[0].get("/valhalla+7-76-52.tgz")
  infos = read_tgz_infos(response.content)
  assert start <= infos["index.bin"].mtime <= int(time.time())
  assert infos["index.bin"].size == 16 * (len(infos) - 1)
  assert all(info.mtime == TILE_MTIME for name, info in infos.items() if name != "index.bin")

def test_list(service):
  """/list lists index.bin and then every tile in archive order, like tar -tvf."""
  contents = service[1]
  response = service[0].get("/list")
  assert response.status_code == 200
  lines = response.text.splitlines()
  assert lines[0] == f"-rw-r--r-- {'0/0':<13} {16 * len(contents):>8} {list_date(TILE_MTIME)} index.bin"
  expected = [f"-rw-r--r-- {'ubuntu/ubuntu':<13} {len(contents[t]):>8} {list_date(TILE_MTIME)} {vs.get_tile_path(t)}" for t in archive_tile_ids()]
  assert lines[1:] == expected

def test_list_unreadable_mtime(tmp_path, monkeypatch):
  """A tile header whose mtime is not octal text is listed with the archive's mtime."""
  tile_ids = archive_tile_ids()[:3]
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, tile_ids, large_mtime_tile=tile_ids[1])
  monkeypatch.setattr(vs, "state_manager", vs.StateManager(path, verbose=False))
  lines = TestClient(vs.app).get("/list").text.splitlines()
  assert lines[2].split()[3:5] == list_date(os.stat(path).st_mtime).split()
  assert lines[1].split()[3:5] == list_date(TILE_MTIME).split()

def test_archive_without_index(tmp_path, monkeypatch, capsys):
  """An archive without index.bin loads with an empty index, lists nothing and returns empty archives."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids()[:3], with_index=False)
  monkeypatch.setattr(vs, "state_manager", vs.StateManager(path, verbose=True))
  assert "index.bin not found" in capsys.readouterr().out
  client = TestClient(vs.app)
  assert client.get("/list").text == ""
  response = client.get("/valhalla+7-76-52.tgz")
  assert response.status_code == 200
  assert gzip.decompress(response.content) == b"\0" * 1024
  response = client.get("/valhalla+7-76-52.tar")
  assert response.content == b"\0" * 1024
  assert response.headers["content-length"] == "1024"

def test_truncated_tile_data(tmp_path, monkeypatch):
  """A tile whose data was cut off after loading ends its data early instead of hanging."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids())
  manager = vs.StateManager(path, verbose=False)
  monkeypatch.setattr(vs, "state_manager", manager)
  area = "\n".join(["area", "1", f"{ARCHIVE_AREA[0]} {ARCHIVE_AREA[1]}", f"{ARCHIVE_AREA[2]} {ARCHIVE_AREA[1]}", f"{ARCHIVE_AREA[2]} {ARCHIVE_AREA[3]}", f"{ARCHIVE_AREA[0]} {ARCHIVE_AREA[3]}", "END", "END"])
  client = TestClient(vs.app)
  full = gzip.decompress(client.post("/valhalla.tgz", content=area).content)
  last = max(manager.current_state.index.values(), key=lambda info: info["offset"])
  os.truncate(path, last["offset"] + 1)
  response = client.post("/valhalla.tgz", content=area)
  assert response.status_code == 200
  assert len(gzip.decompress(response.content)) == len(full) - (last["length"] - 1)

def test_verbose_logging(tmp_path, monkeypatch, capsys):
  """Verbose mode logs the archive load, each request and each archive built."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids())
  monkeypatch.setattr(vs, "state_manager", vs.StateManager(path, verbose=True))
  out = capsys.readouterr().out
  assert f"{len(archive_tile_ids())} tiles loaded from {path}" in out
  assert "Located index.bin" in out
  client = TestClient(vs.app)
  client.get("/valhalla+7-76-52.tgz")
  client.post("/valhalla.tgz", content=triangle_poly())
  client.get("/valhalla+7-76-52.tar")
  out = capsys.readouterr().out
  assert "Streaming tgz request for z=7, x=76, y=52." in out
  assert "Streaming tgz request for a polygon." in out
  assert "Streaming tar request for z=7, x=76, y=52." in out
  assert "Successfully streamed" in out
  assert '"GET /valhalla+7-76-52.tgz HTTP/1.1" 200' in out
  assert '"POST /valhalla.tgz HTTP/1.1" 200' in out

def test_reload_swaps_archive(tmp_path, capsys):
  """A reload serves the new archive and closes the old one once its last request is done."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids())
  manager = vs.StateManager(path, verbose=True)
  old_state = manager.get_state()
  new_path = str(tmp_path / "new.tar")
  build_archive(new_path, archive_tile_ids()[:5])
  os.replace(new_path, path)
  manager._reload()
  assert f"Hot-reload complete. New inode: {os.stat(path).st_ino}" in capsys.readouterr().out
  new_state = manager.get_state()
  assert new_state is not old_state
  assert len(new_state.index) == 5
  assert manager.active_inode == os.stat(path).st_ino
  assert not old_state.fd.closed
  old_state.release()
  assert old_state.fd.closed
  new_state.release()

def test_retire_unused_state(tmp_path):
  """Retiring a state that no request holds closes it at once."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids()[:3])
  state = vs.ArchiveState(path, verbose=False)
  state.retire()
  assert state.fd.closed

def test_reload_failure_keeps_state(tmp_path, capsys):
  """A replacement that is not a readable archive leaves the current state in place."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids()[:3])
  manager = vs.StateManager(path, verbose=True)
  state = manager.current_state
  (tmp_path / "bad.tar").write_bytes(b"not a tar archive" * 100)
  os.replace(str(tmp_path / "bad.tar"), path)
  manager._reload()
  assert manager.current_state is state
  assert "Failed to reload archive state" in capsys.readouterr().err

def test_poll_inode(tmp_path, monkeypatch):
  """Polling reloads when the archive's inode changes and skips a missing archive."""
  path = str(tmp_path / "valhalla_tiles.tar")
  build_archive(path, archive_tile_ids()[:3])
  manager = vs.StateManager(path, verbose=False)
  new_path = str(tmp_path / "new.tar")
  build_archive(new_path, archive_tile_ids()[:4])
  sleeps = []
  real_sleep = time.sleep

  class StopPolling(Exception):
    """Ends the polling loop."""

  def fake_sleep(seconds):
    """Replaces the archive, then removes it, then stops the loop; other threads sleep normally."""
    if threading.current_thread() is not threading.main_thread():
      return real_sleep(seconds)
    sleeps.append(seconds)
    if len(sleeps) == 1:
      os.replace(new_path, path)
    elif len(sleeps) == 2:
      os.remove(path)
    else:
      raise StopPolling()
  monkeypatch.setattr(vs.time, "sleep", fake_sleep)
  with pytest.raises(StopPolling):
    manager._poll_inode()
  assert sleeps == [300, 300, 300]
  assert len(manager.current_state.index) == 4

@pytest.mark.parametrize("argv,port,verbose", [
  (["valhalla-slice"], 8000, True),
  (["valhalla-slice", "--port=9000", "-q"], 9000, False),
  (["valhalla-slice", "--quiet"], 8000, False),
])
def test_main(tmp_path, monkeypatch, argv, port, verbose):
  """The CLI loads the archive given by --tar (default valhalla_tiles.tar) and serves on --port."""
  build_archive(str(tmp_path / "valhalla_tiles.tar"), archive_tile_ids()[:3])
  monkeypatch.chdir(tmp_path)
  calls = []
  monkeypatch.setattr(vs.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
  monkeypatch.setattr(vs, "state_manager", None)
  monkeypatch.setattr(sys, "argv", argv)
  vs.main()
  assert calls == [(vs.app, {"host": "0.0.0.0", "port": port, "access_log": False})]
  assert vs.state_manager.verbose == verbose
  assert vs.state_manager.tar_path == "valhalla_tiles.tar"

def test_script_help():
  """Running the script with --help prints the usage."""
  path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valhalla-slice")
  result = subprocess.run([sys.executable, path, "--help"], capture_output=True, text=True)
  assert result.returncode == 0
  assert "valhalla-slice [--tar=<tar>] [--port=<port>] [--quiet | -q]" in result.stdout

def test_ensure_package_installs_missing(monkeypatch):
  """A package that fails to import is installed with pip and then imported."""
  attempts = []
  installs = []
  real_import = vs.importlib.import_module

  def fake_import(name):
    """Fails the first import of the package."""
    attempts.append(name)
    if len(attempts) == 1:
      raise ImportError(name)
    return real_import(name)
  monkeypatch.setattr(vs.importlib, "import_module", fake_import)
  monkeypatch.setattr(vs.subprocess, "check_call", installs.append)
  assert vs.ensure_package("json") is json
  assert installs == [[sys.executable, "-m", "pip", "install", "json"]]
