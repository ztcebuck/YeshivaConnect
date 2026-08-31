import html
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, parse_qs

MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5GB לגוף העלאה בודד מהדפדפן

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_upload_name(raw_name):
    """מנקה שם קובץ שמגיע מהדפדפן מתווים אסורים ומ-path traversal."""
    name = os.path.basename((raw_name or "").replace('\\', '/'))
    name = _INVALID_NAME_CHARS.sub('_', name).strip().strip('.')
    if not name or name in ('.', '..'):
        name = f"upload_{uuid.uuid4().hex[:8]}"
    return name[:200]


def _guess_content_type(path):
    """זיהוי בסיסי של סוג התוכן לפי סיומת, ללא תלות ב-mimetypes של המערכת."""
    ext = os.path.splitext(path)[1].lower()
    return {
        '.txt': 'text/plain; charset=utf-8',
        '.pdf': 'application/pdf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.mp4': 'video/mp4',
        '.mp3': 'audio/mpeg',
        '.zip': 'application/zip',
        '.doc': 'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    }.get(ext, 'application/octet-stream')


def _human_size(num_bytes):
    size = float(num_bytes or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class _MultipartReader:
    """
    מפענח multipart/form-data בזרימה (streaming) מ-rfile של הבקשה, ללא cgi.
    כותב כל חלק-קובץ ישירות לדיסק, כך שגם קבצים גדולים אינם נטענים לזיכרון.
    מטפל בגבול (boundary) שמפוצל בין קטעי קריאה באמצעות buffer מתגלגל.
    """

    CHUNK = 65536

    def __init__(self, rfile, boundary, content_length, save_dir, name_sanitizer, progress_cb=None):
        self._rfile = rfile
        self._delimiter = b'--' + boundary
        self._remaining = content_length
        self._content_length = content_length
        self._save_dir = save_dir
        self._sanitize = name_sanitizer
        self._buf = b''
        self._progress_cb = progress_cb
        self._last_percent = 0

    def _read_more(self):
        if self._remaining <= 0:
            return b''
        want = min(self.CHUNK, self._remaining)
        chunk = self._rfile.read(want)
        self._remaining -= len(chunk)
        # דיווח התקדמות לפי בתים שנקראו מהרשת (עבור פס ההתקדמות בתוכנה המארחת)
        if self._progress_cb and self._content_length > 0:
            read_so_far = self._content_length - self._remaining
            percent = int((read_so_far / self._content_length) * 100)
            if percent >= self._last_percent + 1:
                self._last_percent = percent
                try:
                    self._progress_cb(percent)
                except Exception:
                    pass
        return chunk

    def _fill(self, min_len):
        """דואג שב-buffer יש לפחות min_len בתים (או שהזרם נגמר)."""
        while len(self._buf) < min_len:
            chunk = self._read_more()
            if not chunk:
                break
            self._buf += chunk

    def _read_line(self):
        """קורא שורה שמסתיימת ב-CRLF מתוך ה-buffer."""
        while b'\r\n' not in self._buf:
            chunk = self._read_more()
            if not chunk:
                line, _, self._buf = self._buf.partition(b'\r\n')
                return line
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b'\r\n')
        return line

    def _read_headers(self):
        headers = {}
        while True:
            line = self._read_line()
            if line == b'':
                break
            if b':' in line:
                key, _, val = line.partition(b':')
                headers[key.strip().lower().decode('latin-1', 'replace')] = val.strip().decode('latin-1', 'replace')
        return headers

    def parse(self):
        """מריץ את הפענוח. מחזיר רשימת (filename, temp_path) של קבצים שנשמרו."""
        saved = []
        # דילוג עד הגבול הפותח הראשון
        self._fill(len(self._delimiter) + 2)
        idx = self._buf.find(self._delimiter)
        if idx == -1:
            return saved
        self._buf = self._buf[idx + len(self._delimiter):]

        while True:
            # אחרי גבול: '--' מציין סוף המסמך, אחרת CRLF לפני החלק הבא
            self._fill(2)
            if self._buf[:2] == b'--':
                break
            if self._buf[:2] == b'\r\n':
                self._buf = self._buf[2:]

            headers = self._read_headers()
            filename = self._extract_filename(headers.get('content-disposition', ''))

            if filename:
                safe_name = self._sanitize(filename)
                fd, temp_path = tempfile.mkstemp(prefix='upload_', dir=self._save_dir)
                with os.fdopen(fd, 'wb') as out:
                    wrote_any = self._stream_until_boundary(out.write)
                if wrote_any:
                    saved.append((safe_name, temp_path))
                else:
                    self._safe_remove(temp_path)
            else:
                # חלק שאינו קובץ (שדה טקסט) — נצרוך ונזרוק
                self._stream_until_boundary(None)

            if self._stream_ended:
                break
        return saved

    def _extract_filename(self, disposition):
        # עדיפות ל-filename* (RFC 5987) — מקודד percent ב-UTF-8
        star = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]*)\"?", disposition, re.IGNORECASE)
        if star and star.group(1).strip():
            raw = star.group(1).strip()
            try:
                return unquote(raw)
            except Exception:
                return raw
        # filename רגיל — דפדפנים שולחים את השם כבתי UTF-8 גולמיים,
        # שנקראו כאן ככותרת latin-1; משחזרים אותם חזרה ל-UTF-8.
        plain = re.search(r'filename="?([^";]*)"?', disposition, re.IGNORECASE)
        if not plain:
            return None
        raw = plain.group(1).strip()
        if not raw:
            return None
        try:
            raw = raw.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return raw

    def _stream_until_boundary(self, write_fn):
        """
        זורם את גוף החלק הנוכחי עד הגבול הבא (וצורך אותו).
        write_fn=None => דילוג. מחזיר True אם נכתבו בתים כלשהם.
        """
        marker = b'\r\n' + self._delimiter
        keep = len(marker) - 1
        wrote_any = False
        self._stream_ended = False
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                if idx > 0:
                    if write_fn:
                        write_fn(self._buf[:idx])
                    wrote_any = True
                self._buf = self._buf[idx + len(marker):]
                return wrote_any
            # הגבול אינו נמצא במלואו — שוטפים את הקידומת הבטוחה,
            # שומרים זנב שעלול להכיל תחילת גבול, ואז קוראים עוד.
            if len(self._buf) > keep:
                flush_to = len(self._buf) - keep
                if write_fn:
                    write_fn(self._buf[:flush_to])
                wrote_any = True
                self._buf = self._buf[flush_to:]
            chunk = self._read_more()
            if not chunk:
                # הזרם נגמר ללא גבול סוגר — נכתוב את השארית ונסמן סיום
                if self._buf:
                    if write_fn:
                        write_fn(self._buf)
                    wrote_any = True
                    self._buf = b''
                self._stream_ended = True
                return wrote_any
            self._buf += chunk

    @property
    def _stream_ended(self):
        return getattr(self, '_ended_flag', False)

    @_stream_ended.setter
    def _stream_ended(self, value):
        self._ended_flag = value

    @staticmethod
    def _safe_remove(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


class WebShareManager:
    """
    שרת HTTP זמני להורדת קבצים בדפדפן — עבור חברים ללא התוכנה.
    החבר מתחבר לנקודה החמה, נכנס לכתובת שהתוכנה מציגה, ומוריד את הקבצים
    שהמשתמש בחר לשתף. עובד לחלוטין אופליין (ספריית התקן בלבד).
    רק קבצים שנוספו במפורש נגישים, כל אחד דרך מזהה אקראי — אין גישה לדיסק.
    """

    def __init__(self, host_ip_provider=None, port=8080, upload_dir="Browser_Uploads"):
        self._items = {}  # id -> {"id","name","path","size","is_temp"}
        self._uploads = {}  # id -> {"id","name","path","size","time_str"}
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._port = port
        self._actual_port = None
        self._host_ip_provider = host_ip_provider  # callable שמחזיר IP להצגה למשתמש
        self._upload_dir = os.path.abspath(upload_dir)
        self.on_upload_callback = None  # נקרא עם רשומת ההעלאה כשמתקבל קובץ מהדפדפן
        self.on_progress_callback = None  # callable(direction, percent) — לפס התקדמות בתוכנה המארחת

    # ------------------------- ניהול פריטים -------------------------
    def add_file(self, file_path):
        """מוסיף קובץ בודד לשיתוף. מחזיר את רשומת הפריט או None."""
        if not file_path or not os.path.isfile(file_path):
            return None
        return self._register(os.path.basename(file_path), file_path, is_temp=False)

    def add_folder(self, folder_path):
        """דוחס תיקייה ל-zip זמני ומוסיף אותו לשיתוף."""
        if not folder_path or not os.path.isdir(folder_path):
            return None
        folder_name = os.path.basename(os.path.normpath(folder_path))
        archive_path = self._zip_folder(folder_path, folder_name)
        return self._register(f"{folder_name}.zip", archive_path, is_temp=True)

    def _zip_folder(self, folder_path, folder_name):
        temp_fd, archive_path = tempfile.mkstemp(suffix='.zip', prefix='webshare_')
        os.close(temp_fd)
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(folder_path):
                rel_root = os.path.relpath(root, folder_path)
                rel_root = '' if rel_root == '.' else rel_root
                for filename in files:
                    full_path = os.path.join(root, filename)
                    arcname = os.path.join(folder_name, rel_root, filename) if rel_root else os.path.join(folder_name, filename)
                    zf.write(full_path, arcname)
        return archive_path

    def _register(self, name, path, is_temp):
        item_id = uuid.uuid4().hex
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        record = {"id": item_id, "name": name, "path": path, "size": size, "is_temp": is_temp}
        with self._lock:
            self._items[item_id] = record
        return {"id": item_id, "name": name, "size": size, "size_h": _human_size(size)}

    def remove_item(self, item_id):
        with self._lock:
            record = self._items.pop(item_id, None)
        if record and record.get("is_temp"):
            self._remove_path(record["path"])
        return bool(record)

    def list_items(self):
        with self._lock:
            return [
                {"id": r["id"], "name": r["name"], "size": r["size"], "size_h": _human_size(r["size"])}
                for r in self._items.values()
            ]

    def _get_item(self, item_id):
        with self._lock:
            return self._items.get(item_id)

    @staticmethod
    def _remove_path(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ------------------------- קבצים שהתקבלו מהדפדפן -------------------------
    def _unique_upload_path(self, safe_name):
        """מחזיר נתיב פנוי בתוך תיקיית ההעלאות (מוסיף (1),(2)... אם קיים)."""
        base, ext = os.path.splitext(safe_name)
        candidate = os.path.join(self._upload_dir, safe_name)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(self._upload_dir, f"{base} ({counter}){ext}")
            counter += 1
        return candidate

    def _record_upload(self, safe_name, final_path, time_str):
        upload_id = uuid.uuid4().hex
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0
        record = {"id": upload_id, "name": safe_name, "path": final_path,
                  "size": size, "time_str": time_str}
        with self._lock:
            self._uploads[upload_id] = record
        return record

    def list_uploads(self):
        with self._lock:
            return [
                {"id": r["id"], "name": r["name"], "path": r["path"],
                 "size": r["size"], "size_h": _human_size(r["size"]), "time_str": r["time_str"]}
                for r in self._uploads.values()
            ]

    def get_upload(self, upload_id):
        with self._lock:
            return self._uploads.get(upload_id)

    def remove_upload(self, upload_id, delete_file=False):
        """מסיר העלאה מהרשימה. delete_file=True מוחק גם את הקובץ מהדיסק."""
        with self._lock:
            record = self._uploads.pop(upload_id, None)
        if record and delete_file:
            self._remove_path(record["path"])
        return bool(record)

    # ------------------------- מחזור חיי השרת -------------------------
    def is_running(self):
        return self._server is not None

    def start(self):
        """מפעיל את שרת ה-HTTP. מחזיר את הפורט בפועל, או None אם נכשל."""
        if self._server is not None:
            return self._actual_port
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # אין הדפסת לוג לכל בקשה

            def _send_html(self, body_bytes, status=200):
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body_bytes)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path in ('/', '/index.html'):
                    body = manager._render_index(parse_qs(parsed.query)).encode('utf-8')
                    self._send_html(body)
                    return
                if parsed.path == '/download':
                    params = parse_qs(parsed.query)
                    item_id = (params.get('id') or [''])[0]
                    manager._serve_download(self, item_id)
                    return
                self._send_html('<h1>404</h1>'.encode('utf-8'), status=404)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path == '/upload':
                    manager._handle_upload(self)
                    return
                self._send_html('<h1>404</h1>'.encode('utf-8'), status=404)

        # מציאת פורט פנוי החל מ-self._port
        last_error = None
        for candidate in range(self._port, self._port + 20):
            try:
                server = ThreadingHTTPServer(('0.0.0.0', candidate), Handler)
                self._server = server
                self._actual_port = candidate
                self._thread = threading.Thread(target=server.serve_forever, daemon=True)
                self._thread.start()
                return candidate
            except OSError as exc:
                last_error = exc
                continue
        print(f"[WebShare] failed to bind port: {last_error}")
        return None

    def stop(self):
        """עוצר את השרת ומנקה קבצי zip זמניים."""
        server = self._server
        self._server = None
        self._actual_port = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        with self._lock:
            records = list(self._items.values())
            self._items.clear()
        for record in records:
            if record.get("is_temp"):
                self._remove_path(record["path"])

    def get_url(self):
        if self._actual_port is None:
            return None
        ip = None
        if self._host_ip_provider:
            try:
                ip = self._host_ip_provider()
            except Exception:
                ip = None
        ip = ip or '192.168.137.1'
        return f"http://{ip}:{self._actual_port}/"

    def get_status(self):
        return {
            "running": self.is_running(),
            "url": self.get_url(),
            "port": self._actual_port,
            "items": self.list_items(),
            "uploads": self.list_uploads(),
        }

    # ------------------------- קליטת העלאות מהדפדפן -------------------------
    def _emit_progress(self, direction, percent):
        """מדווח התקדמות העברה לתוכנה המארחת (אותו פס של העברות ה-socket)."""
        cb = self.on_progress_callback
        if cb:
            try:
                cb(direction, percent)
            except Exception:
                pass

    def _handle_upload(self, handler):
        """קולט קבצים שהדפדפן מעלה, שומר לתיקיית ההעלאות, ומחזיר תשובה."""
        ajax = handler.headers.get('X-Upload-Ajax') == '1'
        content_type = handler.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type.lower():
            self._upload_respond(handler, error=1, ajax=ajax)
            return

        boundary = self._parse_boundary(content_type)
        if not boundary:
            self._upload_respond(handler, error=1, ajax=ajax)
            return

        try:
            content_length = int(handler.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            content_length = 0
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self._upload_respond(handler, error=1, ajax=ajax)
            return

        os.makedirs(self._upload_dir, exist_ok=True)
        saved_count = 0
        # פס התקדמות מיידי (0%) בתוכנה המארחת ברגע שמתחילים לקבל מהדפדפן
        self._emit_progress('receive', 0)
        try:
            reader = _MultipartReader(handler.rfile, boundary, content_length,
                                     self._upload_dir, _sanitize_upload_name,
                                     progress_cb=lambda pct: self._emit_progress('receive', pct))
            time_str = time.strftime('%H:%M')
            for safe_name, temp_path in reader.parse():
                final_path = self._unique_upload_path(safe_name)
                try:
                    os.replace(temp_path, final_path)
                except OSError:
                    self._remove_path(temp_path)
                    continue
                record = self._record_upload(safe_name, final_path, time_str)
                saved_count += 1
                if self.on_upload_callback:
                    try:
                        self.on_upload_callback(record)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[WebShare] upload error: {exc}")
            self._emit_progress('receive', 100)  # מנקה את פס ההתקדמות בתוכנה המארחת
            self._upload_respond(handler, error=1, ajax=ajax)
            return

        self._emit_progress('receive', 100)  # סיום — מסתיר את הפס בתוכנה המארחת
        self._upload_respond(handler, uploaded=saved_count, ajax=ajax)

    @staticmethod
    def _parse_boundary(content_type):
        match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.IGNORECASE)
        if not match:
            return None
        boundary = (match.group(1) or match.group(2) or '').strip()
        return boundary.encode('latin-1', 'replace') if boundary else None

    def _upload_respond(self, handler, uploaded=None, error=None, ajax=False):
        """מגיב להעלאה: JSON כשהיא מ-XHR (עם פס התקדמות), אחרת redirect רגיל."""
        if ajax:
            if error is not None:
                payload = {"ok": False, "uploaded": 0}
            else:
                payload = {"ok": True, "uploaded": int(uploaded or 0)}
            body = json.dumps(payload).encode('utf-8')
            try:
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json; charset=utf-8')
                handler.send_header('Content-Length', str(len(body)))
                handler.send_header('Cache-Control', 'no-store')
                handler.end_headers()
                handler.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        query = f"?uploaded={uploaded}" if uploaded is not None else "?upload_error=1"
        try:
            handler.send_response(303)
            handler.send_header('Location', '/' + query)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------- הגשת תוכן -------------------------
    def _serve_download(self, handler, item_id):
        record = self._get_item(item_id)
        if not record or not os.path.isfile(record["path"]):
            handler.send_response(404)
            handler.send_header('Content-Type', 'text/plain; charset=utf-8')
            handler.end_headers()
            handler.wfile.write('File not found'.encode('utf-8'))
            return
        path = record["path"]
        size = os.path.getsize(path)
        handler.send_response(200)
        handler.send_header('Content-Type', _guess_content_type(path))
        # שם קובץ בכותרת, כולל תמיכה בשמות לא-לטיניים (RFC 5987)
        ascii_name = record["name"].encode('ascii', 'ignore').decode('ascii') or 'download'
        handler.send_header(
            'Content-Disposition',
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(record['name'])}"
        )
        handler.send_header('Content-Length', str(size))
        handler.end_headers()
        # פס התקדמות בתוכנה המארחת בזמן שהחבר מוריד קובץ ששותף
        self._emit_progress('send', 0)
        sent_bytes = 0
        last_percent = 0
        try:
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    sent_bytes += len(chunk)
                    if size > 0:
                        percent = int((sent_bytes / size) * 100)
                        if percent >= last_percent + 1:
                            last_percent = percent
                            self._emit_progress('send', percent)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self._emit_progress('send', 100)  # סיום/ניתוק — מסתיר את הפס בתוכנה המארחת

    def _render_index(self, query=None):
        query = query or {}
        items = self.list_items()
        rows = []
        if not items:
            rows.append(
                '<div class="empty">אין קבצים זמינים כרגע.<br>המתן שהמשתמש ישתף קבצים.</div>'
            )
        else:
            for item in items:
                name = html.escape(item["name"])
                href = '/download?id=' + quote(item["id"])
                rows.append(
                    f'<a class="file" href="{href}">'
                    f'<span class="file-info"><span class="file-name">{name}</span>'
                    f'<span class="file-size">{item["size_h"]}</span></span>'
                    f'<span class="dl">⬇ הורדה</span></a>'
                )
        rows_html = '\n'.join(rows)

        # באנר משוב אחרי העלאה (הפניה מ-POST /upload)
        banner_html = ''
        uploaded = (query.get('uploaded') or [None])[0]
        if uploaded is not None:
            try:
                count = int(uploaded)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                banner_html = (
                    f'<div class="banner ok">✅ הועלו {count} קבצים בהצלחה! '
                    f'הם מופיעים כעת בתוכנה של החבר.</div>'
                )
            else:
                banner_html = '<div class="banner warn">לא נבחרו קבצים להעלאה.</div>'
        elif (query.get('upload_error') or [None])[0] is not None:
            banner_html = '<div class="banner err">⚠️ ההעלאה נכשלה. נסה שוב.</div>'

        # דף עצמאי לחלוטין — כל ה-CSS מוטמע, ללא תלות באינטרנט
        return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>שיתוף קבצים - YeshivaConnect</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; font-family: -apple-system, "Segoe UI", Arial, sans-serif;
    background: radial-gradient(circle at top right, #1e293b, #0b1120);
    color: #f1f5f9; padding: 24px; display: flex; flex-direction: column; align-items: center;
  }}
  .card {{
    width: 100%; max-width: 640px; background: rgba(30,41,59,0.72);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 26px;
    box-shadow: 0 24px 60px rgba(0,0,0,0.45); margin-top: 24px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 6px; color: #38bdf8; display: flex; align-items: center; gap: 10px; }}
  h2 {{ font-size: 18px; margin: 26px 0 12px; color: #a5b4fc; display: flex; align-items: center; gap: 8px; }}
  .subtitle {{ color: #94a3b8; font-size: 14px; margin-bottom: 22px; }}
  .file {{
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 14px; padding: 14px 18px; margin-bottom: 12px; text-decoration: none;
    color: #f1f5f9; transition: background 0.2s, transform 0.2s;
  }}
  .file:hover {{ background: rgba(255,255,255,0.09); transform: translateY(-1px); }}
  .file-info {{ display: flex; flex-direction: column; gap: 3px; overflow: hidden; }}
  .file-name {{ font-weight: 700; word-break: break-all; }}
  .file-size {{ font-size: 13px; color: #94a3b8; }}
  .dl {{
    background: linear-gradient(135deg, #38bdf8, #818cf8); color: #fff; font-weight: 700;
    padding: 8px 16px; border-radius: 10px; white-space: nowrap; flex: none;
  }}
  .empty {{ text-align: center; color: #94a3b8; padding: 30px 10px; line-height: 1.7; }}
  .footer {{ color: #64748b; font-size: 12px; margin-top: 18px; text-align: center; }}
  .banner {{ border-radius: 12px; padding: 13px 16px; margin-bottom: 18px; font-weight: 600; font-size: 14px; }}
  .banner.ok {{ background: rgba(34,197,94,0.16); border: 1px solid rgba(34,197,94,0.5); color: #86efac; }}
  .banner.warn {{ background: rgba(234,179,8,0.16); border: 1px solid rgba(234,179,8,0.5); color: #fde68a; }}
  .banner.err {{ background: rgba(239,68,68,0.16); border: 1px solid rgba(239,68,68,0.5); color: #fca5a5; }}
  .upload {{
    border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 18px;
    background: rgba(129,140,248,0.06); display: flex; flex-direction: column; gap: 12px;
  }}
  .dropzone {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 6px; text-align: center; cursor: pointer;
    border: 2px dashed rgba(129,140,248,0.55); border-radius: 12px;
    padding: 26px 16px; background: rgba(255,255,255,0.03);
    transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
  }}
  .dropzone:hover {{ background: rgba(129,140,248,0.12); }}
  .dropzone.dragover {{
    background: rgba(56,189,248,0.18); border-color: #38bdf8; transform: scale(1.01);
  }}
  .dz-icon {{ font-size: 30px; line-height: 1; }}
  .dz-title {{ font-weight: 700; font-size: 15px; color: #e2e8f0; }}
  .dz-sub {{ font-size: 13px; color: #94a3b8; }}
  .selected {{ font-size: 13px; color: #a5b4fc; word-break: break-all; min-height: 1em; }}
  .upload button {{
    background: linear-gradient(135deg, #818cf8, #6366f1); color: #fff; font-weight: 700;
    border: none; padding: 12px 18px; border-radius: 10px; font-size: 15px; cursor: pointer;
  }}
  .upload button:hover {{ filter: brightness(1.08); }}
  .upload .hint {{ color: #94a3b8; font-size: 13px; }}
  #uploading {{ display: none; color: #a5b4fc; font-size: 14px; margin-top: 6px; }}
  .progress-wrap {{ display: none; margin-top: 12px; }}
  .progress-track {{
    width: 100%; height: 14px; border-radius: 999px; overflow: hidden;
    background: rgba(255,255,255,0.09); border: 1px solid rgba(255,255,255,0.12);
  }}
  .progress-bar {{
    height: 100%; width: 0%; border-radius: 999px;
    background: linear-gradient(135deg, #38bdf8, #818cf8); transition: width 0.15s ease;
  }}
  .progress-text {{ color: #a5b4fc; font-size: 13px; margin-top: 7px; text-align: center; }}
</style>
</head>
<body>
  <div class="card">
    {banner_html}
    <h1>📥 הורדת קבצים</h1>
    <div class="subtitle">קבצים ששותפו איתך דרך YeshivaConnect — לחץ להורדה.</div>
    {rows_html}

    <h2>📤 העלאת קבצים למחשב</h2>
    <div class="upload">
      <div class="hint">בחר קבצים כדי לשלוח אותם אל מי שמשתף איתך — הם יופיעו אצלו בתוכנה.</div>
      <form id="upform" method="post" action="/upload" enctype="multipart/form-data">
        <label class="dropzone" id="dropzone" for="filepick">
          <div class="dz-icon">📁⬆</div>
          <div class="dz-title">גרור לכאן קבצים</div>
          <div class="dz-sub">או לחץ לבחירה מהמכשיר</div>
        </label>
        <input type="file" name="files" id="filepick" multiple hidden>
        <div class="selected" id="selected"></div>
        <button type="submit" id="upbtn">⬆ העלה קבצים</button>
      </form>
      <div id="uploading">⏳ מעלה קבצים... נא להמתין ולא לסגור את הדף.</div>
      <div class="progress-wrap" id="progress-wrap">
        <div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div>
        <div class="progress-text" id="progress-text">0%</div>
      </div>
    </div>

    <div class="footer">מרוענן אוטומטית · YeshivaConnect</div>
  </div>
  <script>
    var form = document.getElementById('upform');
    var fileInput = document.getElementById('filepick');
    var dropzone = document.getElementById('dropzone');
    var selectedBox = document.getElementById('selected');
    var upBtn = document.getElementById('upbtn');
    var uploadingBox = document.getElementById('uploading');
    var progressWrap = document.getElementById('progress-wrap');
    var progressBar = document.getElementById('progress-bar');
    var progressText = document.getElementById('progress-text');
    var uploadingActive = false;

    function humanSize(n) {{
      var units = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
      n = n || 0;
      while (n >= 1024 && i < units.length - 1) {{ n /= 1024; i++; }}
      return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
    }}

    // מציג את שמות הקבצים שנבחרו (עד 4, והשאר בספירה)
    function updateSelected() {{
      var f = fileInput.files;
      if (!f || !f.length) {{ selectedBox.textContent = ''; return; }}
      var names = [];
      for (var i = 0; i < f.length && i < 4; i++) {{ names.push(f[i].name); }}
      var txt = names.join(', ');
      if (f.length > 4) {{ txt += ' + עוד ' + (f.length - 4); }}
      selectedBox.textContent = '📎 ' + f.length + ' נבחרו: ' + txt;
    }}

    function startUpload() {{
      if (uploadingActive) return;
      if (!fileInput.files || !fileInput.files.length) return;

      uploadingActive = true;
      upBtn.disabled = true;
      uploadingBox.style.display = 'block';
      progressWrap.style.display = 'block';
      progressBar.style.width = '0%';
      progressText.textContent = '0%';

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload', true);
      xhr.setRequestHeader('X-Upload-Ajax', '1');

      // פס התקדמות אמיתי לפי בתים שנשלחו
      xhr.upload.addEventListener('progress', function(ev) {{
        if (ev.lengthComputable) {{
          var pct = Math.round((ev.loaded / ev.total) * 100);
          progressBar.style.width = pct + '%';
          progressText.textContent = pct + '%  ·  ' + humanSize(ev.loaded) + ' / ' + humanSize(ev.total);
        }}
      }});
      // סיום השליחה — השרת עדיין כותב לדיסק
      xhr.upload.addEventListener('load', function() {{
        progressBar.style.width = '100%';
        progressText.textContent = 'מסיים ושומר...';
      }});
      xhr.addEventListener('load', function() {{
        var count = null;
        try {{ count = JSON.parse(xhr.responseText).uploaded; }} catch (err) {{ count = null; }}
        if (count === null) {{ location.reload(); }}
        else {{ location.href = '/?uploaded=' + count; }}
      }});
      xhr.addEventListener('error', function() {{ location.href = '/?upload_error=1'; }});
      xhr.addEventListener('abort', function() {{ location.href = '/?upload_error=1'; }});

      xhr.send(new FormData(form));
    }}

    // בחירה דרך התווית/כפתור — רק מציג את הקבצים, ההעלאה בלחיצה על "העלה"
    fileInput.addEventListener('change', updateSelected);

    form.addEventListener('submit', function(e) {{
      e.preventDefault();
      startUpload();
    }});

    // גרירה ושחרור (drag & drop) — משייך את הקבצים ומתחיל העלאה מיד
    function assignFiles(fileList) {{
      try {{
        var dt = new DataTransfer();
        for (var i = 0; i < fileList.length; i++) {{ dt.items.add(fileList[i]); }}
        fileInput.files = dt.files;
        return true;
      }} catch (err) {{ return false; }}
    }}

    ['dragenter', 'dragover'].forEach(function(evt) {{
      dropzone.addEventListener(evt, function(e) {{
        e.preventDefault(); e.stopPropagation();
        dropzone.classList.add('dragover');
      }});
    }});
    ['dragleave', 'dragend'].forEach(function(evt) {{
      dropzone.addEventListener(evt, function(e) {{
        e.preventDefault(); e.stopPropagation();
        dropzone.classList.remove('dragover');
      }});
    }});
    dropzone.addEventListener('drop', function(e) {{
      e.preventDefault(); e.stopPropagation();
      dropzone.classList.remove('dragover');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      if (assignFiles(files)) {{
        updateSelected();
        startUpload();
      }}
    }});

    // מונע מהדפדפן לפתוח קובץ ששוחרר מחוץ לאזור הגרירה
    ['dragover', 'drop'].forEach(function(evt) {{
      document.addEventListener(evt, function(e) {{ e.preventDefault(); }});
    }});

    // רענון עדין כדי לראות קבצים חדשים שהמשתמש הוסיף — נעצר בזמן העלאה
    setTimeout(function() {{ if (!uploadingActive) location.reload(); }}, 8000);
  </script>
</body>
</html>"""
