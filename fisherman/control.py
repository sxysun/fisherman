import asyncio
import base64
import json
import os
from urllib.parse import unquote

import structlog

log = structlog.get_logger()

VIEWER_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Fisherman Viewer</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #eee; padding: 20px; }
  h1 { font-size: 18px; font-weight: 500; margin-bottom: 16px; color: #aaa; }
  .controls { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
  .controls button { background: #333; color: #eee; border: 1px solid #555; border-radius: 6px;
    padding: 6px 14px; cursor: pointer; font-size: 13px; }
  .controls button:hover { background: #444; }
  .controls button.active { background: #2563eb; border-color: #2563eb; }
  .controls span { color: #888; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; overflow: hidden; }
  .card img { width: 100%; display: block; cursor: pointer; }
  .card img.expanded { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
    object-fit: contain; z-index: 1000; background: rgba(0,0,0,0.9); border-radius: 0; }
  .no-media { padding: 24px; text-align: center; color: #555; font-size: 12px; }
  .meta { padding: 10px 12px; font-size: 12px; line-height: 1.6; }
  .meta .app { color: #60a5fa; font-weight: 600; }
  .meta .window { color: #888; }
  .meta .time { color: #666; float: right; }
  .meta .tier { display: inline-block; background: #333; border-radius: 4px; padding: 1px 6px;
    font-size: 11px; margin-left: 6px; }
  .meta .tier.t1 { background: #1e3a2f; color: #4ade80; }
  .meta .tier.t2 { background: #3b2f1e; color: #facc15; }
  .ocr { padding: 0 12px 10px; font-size: 11px; color: #999; max-height: 80px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word; font-family: monospace; }
  .urls { padding: 0 12px 10px; }
  .urls a { font-size: 11px; color: #60a5fa; text-decoration: none; display: block;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .scene { padding: 6px 12px 10px; font-size: 12px; color: #c4b5fd; font-style: italic;
    border-left: 3px solid #7c3aed; margin: 0 12px 10px; }
</style>
</head>
<body>
<h1>Fisherman Viewer</h1>
<div class="controls">
  <button id="refreshBtn" onclick="loadFrames()">Refresh</button>
  <button id="autoBtn" onclick="toggleAuto()">Auto-refresh: OFF</button>
  <span id="countLabel"></span>
</div>
<div class="grid" id="grid"></div>
<script>
let autoRefresh = false;
let autoTimer = null;

async function loadFrames() {
  try {
    const resp = await fetch('/frames?count=100');
    const frames = await resp.json();
    document.getElementById('countLabel').textContent = frames.length + ' frames';
    const grid = document.getElementById('grid');
    grid.innerHTML = '';
    frames.forEach(f => {
      const card = document.createElement('div');
      card.className = 'card';
      const ts = new Date(f.ts * 1000);
      const timeStr = ts.toLocaleTimeString();
      const tier = f.tier_hint || '?';
      const tierClass = tier === 1 ? 't1' : 't2';
      let html;
      if (f.has_image) {
        html = '<img src="/frames/' + f.ts_ms + '/image" loading="lazy" onclick="toggleExpand(this)">';
      } else {
        html = '<div class="no-media">No image</div>';
      }
      html += '<div class="meta">';
      html += '<span class="app">' + esc(f.app || 'Unknown') + '</span>';
      html += '<span class="tier ' + tierClass + '">T' + tier + '</span>';
      html += '<span class="time">' + timeStr + '</span>';
      html += '<br><span class="window">' + esc(f.window || '') + '</span>';
      html += '</div>';
      if (f.ocr_text) {
        html += '<div class="ocr">' + esc(f.ocr_text.slice(0, 500)) + '</div>';
      }
      if (f.scene_description) {
        html += '<div class="scene">' + esc(f.scene_description) + '</div>';
      }
      if (f.urls && f.urls.length > 0) {
        html += '<div class="urls">';
        f.urls.slice(0, 3).forEach(u => {
          html += '<a href="' + esc(u) + '" target="_blank">' + esc(u) + '</a>';
        });
        html += '</div>';
      }
      card.innerHTML = html;
      grid.appendChild(card);
    });
  } catch (e) {
    console.error('Failed to load frames:', e);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function toggleExpand(img) {
  img.classList.toggle('expanded');
}

function toggleAuto() {
  autoRefresh = !autoRefresh;
  const btn = document.getElementById('autoBtn');
  if (autoRefresh) {
    btn.textContent = 'Auto-refresh: ON';
    btn.classList.add('active');
    autoTimer = setInterval(loadFrames, 3000);
  } else {
    btn.textContent = 'Auto-refresh: OFF';
    btn.classList.remove('active');
    clearInterval(autoTimer);
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('img.expanded').forEach(i => i.classList.remove('expanded'));
  }
});

loadFrames();
</script>
</body>
</html>
"""


class ControlServer:
    """Tiny HTTP server for local pause/resume/status control and frame viewer."""

    def __init__(self, port: int, get_status_fn, pause_fn, resume_fn, frame_store=None, frame_queue: asyncio.Queue | None = None, screenpipe_data_dir: str | None = None):
        self._port = port
        self._get_status = get_status_fn
        self._pause = pause_fn
        self._resume = resume_fn
        self._frame_store = frame_store
        self._frame_queue = frame_queue
        self._screenpipe_data_dir = screenpipe_data_dir
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port
        )
        log.info("control_server_started", port=self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode().strip().split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            # Drain headers, track content-length and range
            content_length = 0
            range_header = None
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                lower = line.lower()
                if lower.startswith(b"content-length:"):
                    content_length = int(line.split(b":")[1].strip())
                elif lower.startswith(b"range:"):
                    range_header = line.split(b":", 1)[1].strip().decode()

            # Read body for POST requests
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            # Route request
            if method == "GET" and path == "/status":
                body = json.dumps(self._get_status())
                self._send_json(writer, body)
            elif method == "POST" and path == "/pause":
                self._pause()
                self._send_json(writer, json.dumps({"paused": True}))
            elif method == "POST" and path == "/resume":
                self._resume()
                self._send_json(writer, json.dumps({"paused": False}))
            elif method == "POST" and path == "/frame":
                await self._handle_frame_post(body, writer)
            elif method == "GET" and path == "/viewer":
                self._send_html(writer, VIEWER_HTML)
            elif method == "GET" and path.startswith("/video/"):
                await self._handle_video(path, range_header, writer)
            elif method == "GET" and path.startswith("/frames"):
                await self._handle_frames(path, writer)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")

            await writer.drain()
        except Exception:
            log.warning("control_request_failed", exc_info=True)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_frames(self, path: str, writer: asyncio.StreamWriter) -> None:
        if not self._frame_store:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            return

        # GET /frames?count=N — list recent frame metadata
        if path == "/frames" or path.startswith("/frames?"):
            count = 50
            if "count=" in path:
                try:
                    count = int(path.split("count=")[1].split("&")[0])
                except ValueError:
                    pass
            frames = self._frame_store.list_recent(count)
            self._send_json(writer, json.dumps(frames))
            return

        # GET /frames/{ts_ms}/image — serve JPEG
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "image":
            try:
                ts_ms = int(parts[1])
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            img_path = self._frame_store.get_image_path(ts_ms)
            if img_path:
                with open(img_path, "rb") as f:
                    data = f.read()
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n"
                    "Cache-Control: public, max-age=3600\r\n"
                    "\r\n"
                )
                writer.write(header.encode() + data)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return

        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")

    async def _handle_video(self, path: str, range_header: str | None, writer: asyncio.StreamWriter) -> None:
        """Serve MP4 video chunks from screenpipe data dir with Range support."""
        parts = path.split("/video/", 1)
        if len(parts) < 2 or not parts[1] or not self._screenpipe_data_dir:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return

        # Strip fragment (#t=...) and decode
        filename = unquote(parts[1].split("#")[0])
        filename = os.path.basename(filename)  # prevent path traversal

        if not filename.endswith(".mp4"):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return

        video_path = os.path.join(self._screenpipe_data_dir, filename)
        if not os.path.isfile(video_path):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return

        file_size = os.path.getsize(video_path)

        if range_header and range_header.startswith("bytes="):
            range_spec = range_header[6:]
            range_parts = range_spec.split("-", 1)
            start = int(range_parts[0]) if range_parts[0] else 0
            end = int(range_parts[1]) if len(range_parts) > 1 and range_parts[1] else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            with open(video_path, "rb") as f:
                f.seek(start)
                data = f.read(length)

            header = (
                "HTTP/1.1 206 Partial Content\r\n"
                "Content-Type: video/mp4\r\n"
                f"Content-Range: bytes {start}-{end}/{file_size}\r\n"
                f"Content-Length: {length}\r\n"
                "Accept-Ranges: bytes\r\n"
                "Cache-Control: public, max-age=3600\r\n"
                "\r\n"
            )
            writer.write(header.encode() + data)
        else:
            with open(video_path, "rb") as f:
                data = f.read()
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: video/mp4\r\n"
                f"Content-Length: {file_size}\r\n"
                "Accept-Ranges: bytes\r\n"
                "Cache-Control: public, max-age=3600\r\n"
                "\r\n"
            )
            writer.write(header.encode() + data)

    async def _handle_frame_post(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        if not self._frame_queue:
            self._send_json(writer, '{"error":"no frame queue"}')
            return
        try:
            msg = json.loads(body)
            jpeg_data = base64.b64decode(msg["jpeg_b64"])
            from fisherman.capture import ScreenFrame

            frame = ScreenFrame(
                jpeg_data=jpeg_data,
                width=msg["width"],
                height=msg["height"],
                app_name=msg.get("app_name") or None,
                bundle_id=msg.get("bundle_id") or None,
                window_title=msg.get("window_title") or None,
                timestamp=msg["timestamp"],
            )
            dhash_distance = msg.get("dhash_distance", 64)
            # Swift CaptureEngine may provide OCR text + urls
            ocr_text = msg.get("ocr_text") or None
            ocr_urls = msg.get("urls") or None
            text_source = msg.get("text_source") or None
            try:
                self._frame_queue.put_nowait((frame, dhash_distance, ocr_text, ocr_urls, text_source))
            except asyncio.QueueFull:
                log.warning("frame_queue_full")
            self._send_json(writer, '{"ok":true}')
        except Exception as e:
            log.warning("frame_post_failed", error=str(e))
            self._send_json(writer, json.dumps({"error": str(e)}))

    def _send_json(self, writer: asyncio.StreamWriter, body: str) -> None:
        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
            f"{body}"
        )
        writer.write(resp.encode())

    def _send_html(self, writer: asyncio.StreamWriter, body: str) -> None:
        encoded = body.encode()
        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "\r\n"
        )
        writer.write(resp.encode() + encoded)
