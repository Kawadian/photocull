from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from httpx2 import Client, ConnectError

from photocull.api import create_app
from photocull.config import DerivativeSettings, Library, Settings
from photocull.db import connect, initialize, write
from photocull.media.derivatives import VARIANTS, derivative_path, recipe
from tests.worker_harness import wait_for

NGINX = shutil.which("nginx")


@pytest.mark.skipif(NGINX is None, reason="nginx is required for proxy integration")
def test_nginx_authenticated_offload_ranges_304_and_gzip(tmp_path):
    state, large = tmp_path / "state", tmp_path / "large"
    hot = state / "derivatives" / "hot"
    photo = tmp_path / "photos"
    token_path = tmp_path / "token"
    token_path.write_text("fixture-proxy-token")
    settings = Settings(
        state_dir=state,
        libraries={"photo": Library(root=photo)},
        token_file=token_path,
        derivatives=DerivativeSettings(large_dir=large, accel_redirect=True),
    )
    variant = VARIANTS["fit-1280.avif"]
    version = "b" * 64 + "-" + recipe(settings.derivatives)
    path = settings.library_state("photo") / "index.db"
    initialize(path)
    with connect(path) as db, write(db):
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        db.executemany(
            """INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,day)
               VALUES (?,?,'','image',0,0,?,1,'2025-01-01')""",
            ((i, f"synthetic-{i:03}.jpg", "b" * 64) for i in range(1, 30)),
        )
        db.execute(
            """INSERT INTO derivative_files
               (asset_id,variant,version,tier,status,priority,requested_at,last_access,byte_size)
               VALUES (1,?,?,'large','ready',1,0,0,64)""",
            (variant.name, version),
        )

    target = derivative_path(settings, "photo", 1, variant, version)
    target.parent.mkdir(parents=True)
    media = bytes(range(64))
    target.write_bytes(media)

    snippets = tmp_path / "snippets"
    snippets.mkdir()
    (snippets / "token.conf").write_text('set $photocull_token "fixture-proxy-token";\n')

    with socket.socket() as api_socket, socket.socket() as proxy_socket:
        api_socket.bind(("127.0.0.1", 0))
        proxy_socket.bind(("127.0.0.1", 0))
        api_port, proxy_port = api_socket.getsockname()[1], proxy_socket.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [api_socket]}, daemon=True)
        thread.start()
        wait_for(lambda: server.started)

        example = Path("deploy/systemd/nginx-photocull.conf").read_text()
        example = (
            example.replace("listen 80 default_server;", f"listen 127.0.0.1:{proxy_port};")
            .replace("    listen [::]:80 default_server;\n", "")
            .replace("127.0.0.1:8000", f"127.0.0.1:{api_port}")
            .replace("/etc/nginx/photocull.d/", f"{snippets}/")
            .replace("/var/lib/photocull/derivatives/hot/", f"{hot}/")
            .replace("/srv/photocull-cache-hdd/", f"{large}/")
        )
        nginx = tmp_path / "nginx.conf"
        nginx.write_text(
            f"""daemon off;
            master_process off;
            pid {tmp_path}/nginx.pid;
            error_log stderr warn;
            events {{}}
            http {{
                access_log off;
                client_body_temp_path {tmp_path}/body;
                proxy_temp_path {tmp_path}/proxy;
                fastcgi_temp_path {tmp_path}/fastcgi;
                uwsgi_temp_path {tmp_path}/uwsgi;
                scgi_temp_path {tmp_path}/scgi;
                types {{ image/avif avif; }}
                {example}
            }}
            """
        )
        proxy_socket.close()
        process = subprocess.Popen([NGINX, "-p", str(tmp_path), "-c", str(nginx)])
        try:
            with Client(base_url=f"http://127.0.0.1:{proxy_port}", timeout=10) as proxy:
                media_url = f"/api/libraries/photo/assets/1/media/{variant.name}/{version}"

                def get_media():
                    try:
                        return proxy.get(media_url)
                    except ConnectError:
                        return None

                response = wait_for(get_media)
                assert response.status_code == 200
                assert response.content == media
                assert response.headers["content-type"] == "image/avif"
                assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
                assert "x-accel-redirect" not in response.headers
                for key in ("ETag", "Last-Modified"):
                    assert key in response.headers
                assert (
                    proxy.get(
                        media_url,
                        headers={
                            "If-None-Match": response.headers["ETag"],
                        },
                    ).status_code
                    == 304
                )
                assert (
                    proxy.get(
                        media_url,
                        headers={
                            "If-Modified-Since": response.headers["Last-Modified"],
                        },
                    ).status_code
                    == 304
                )
                partial = proxy.get(media_url, headers={"Range": "bytes=4-15"})
                assert partial.status_code == 206 and partial.content == media[4:16]
                internal = "/_photocull/large/" + target.relative_to(large).as_posix()
                assert proxy.get(internal).status_code == 404
                timeline = proxy.get("/api/browse/timeline", headers={"Accept-Encoding": "gzip"})
                assert timeline.headers["Content-Encoding"] == "gzip"
                assert len(timeline.json()["items"]) == 29
            with Client(base_url=f"http://127.0.0.1:{api_port}", timeout=5) as direct:
                assert direct.get(media_url).status_code == 401
        finally:
            process.terminate()
            process.wait(timeout=5)
            server.should_exit = True
            thread.join(timeout=5)
