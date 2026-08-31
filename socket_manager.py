import os
import re
import shutil
import socket
import struct
import tempfile
import threading
import time
import zipfile
import json

# תקרות הגנה מפני קלט זדוני
MAX_HEADER_BYTES = 1 * 1024 * 1024          # 1MB ל-header JSON
MAX_TRANSFER_BYTES = 5 * 1024 * 1024 * 1024  # 5GB לקובץ/תיקייה בודדים

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(raw_name, fallback):
    """מנקה שם קובץ מתווים אסורים ומנתיבי traversal. מחזיר שם בסיס בטוח בלבד."""
    name = os.path.basename(raw_name or "")
    name = _INVALID_NAME_CHARS.sub('_', name).strip().strip('.')
    if not name or name in ('.', '..'):
        return fallback
    return name[:200]


class SocketManager:
    def __init__(self, port=5050, save_dir="Received_Files"):
        self.port = port
        self.save_dir = save_dir
        self.server_socket = None
        self.is_running = False
        self.on_receive_callback = None
        self.on_progress_callback = None

        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

    def set_callback(self, callback_function):
        self.on_receive_callback = callback_function

    def set_progress_callback(self, callback_function):
        self.on_progress_callback = callback_function

    def start_server(self):
        self.stop_server()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind(('0.0.0.0', self.port))
        except OSError:
            self.server_socket.close()
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('0.0.0.0', 0))
        self.port = self.server_socket.getsockname()[1]
        self.server_socket.listen(5)
        self.is_running = True

        thread = threading.Thread(target=self._accept_connections, daemon=True)
        thread.start()

    def stop_server(self):
        self.is_running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None

    def _accept_connections(self):
        while self.is_running:
            try:
                conn, addr = self.server_socket.accept()
                client_thread = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
                client_thread.start()
            except OSError:
                break

    def _recv_exact(self, conn, size):
        chunks = bytearray()
        while len(chunks) < size:
            chunk = conn.recv(size - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def _handle_client(self, conn, addr):
        try:
            raw_header_len = self._recv_exact(conn, 4)
            if len(raw_header_len) < 4:
                return
            header_len = struct.unpack('>I', raw_header_len)[0]
            if header_len <= 0 or header_len > MAX_HEADER_BYTES:
                print(f"[Socket] Rejected header_len={header_len} from {addr[0]}")
                return

            header_bytes = self._recv_exact(conn, header_len)
            header = json.loads(header_bytes.decode('utf-8'))

            msg_type = header.get('type')
            content_size = header.get('size')

            sender_id = header.get('sender_id', addr[0])
            sender_name = header.get('sender_name', f"מחשב לא מזוהה ({addr[0]})")
            sender_ip = addr[0]
            sender_port = header.get('port', self.port)

            if msg_type in ('link', 'text'):
                if not isinstance(content_size, int) or content_size < 0 or content_size > MAX_HEADER_BYTES:
                    print(f"[Socket] Rejected text size={content_size} from {addr[0]}")
                    return
                payload_data = self._recv_exact(conn, content_size).decode('utf-8', errors='replace')
                if self.on_receive_callback:
                    self.on_receive_callback(sender_ip, sender_id, sender_name, msg_type, payload_data, sender_port)

            elif msg_type in ('file', 'folder'):
                if not isinstance(content_size, int) or content_size < 0 or content_size > MAX_TRANSFER_BYTES:
                    print(f"[Socket] Rejected {msg_type} size={content_size} from {addr[0]}")
                    return
                raw_filename = header.get('filename')
                safe_filename = sanitize_filename(raw_filename, f"{msg_type}_{int(time.time())}")
                filepath = os.path.abspath(os.path.join(self.save_dir, safe_filename))

                received_bytes = 0
                last_update_percent = 0

                # פס התקדמות מיידי (0%) ברגע שמתחילים לקבל — במקביל לצד השולח
                if self.on_progress_callback:
                    self.on_progress_callback('receive', 0)

                with open(filepath, 'wb') as f:
                    while received_bytes < content_size:
                        chunk_size = min(65536, content_size - received_bytes)
                        chunk = self._recv_exact(conn, chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        received_bytes += len(chunk)

                        if content_size > 0:
                            percent = int((received_bytes / content_size) * 100)
                            if percent >= last_update_percent + 1:
                                if self.on_progress_callback:
                                    self.on_progress_callback('receive', percent)
                                last_update_percent = percent

                content_for_callback = filepath
                if msg_type == 'folder':
                    raw_folder = header.get('folder_name') or os.path.splitext(os.path.basename(filepath))[0]
                    folder_name = sanitize_filename(raw_folder, f"folder_{int(time.time())}")
                    content_for_callback = self._extract_zip_archive(filepath, folder_name)

                if self.on_receive_callback:
                    self.on_receive_callback(sender_ip, sender_id, sender_name, msg_type, content_for_callback, sender_port)

        except Exception as e:
            print(f"[Socket] Error: {e}")
        finally:
            conn.close()

    def _create_zip_archive(self, folder_path):
        temp_fd, archive_path = tempfile.mkstemp(suffix='.zip', prefix='folder_')
        os.close(temp_fd)
        folder_name = os.path.basename(os.path.normpath(folder_path))
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(folder_path):
                rel_root = os.path.relpath(root, folder_path)
                if rel_root == '.':
                    rel_root = ''
                for filename in files:
                    full_path = os.path.join(root, filename)
                    arcname = os.path.join(folder_name, rel_root, filename) if rel_root else os.path.join(folder_name, filename)
                    zf.write(full_path, arcname)
        return archive_path

    def _extract_zip_archive(self, zip_path, folder_name):
        target_dir = os.path.abspath(os.path.join(self.save_dir, folder_name))
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        os.makedirs(target_dir, exist_ok=True)

        safe_root = os.path.realpath(target_dir)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                parts = member.filename.split('/')
                if len(parts) > 1 and parts[0] == folder_name:
                    rel_path = '/'.join(parts[1:])
                else:
                    rel_path = member.filename
                # ניקוי כל רכיב בנתיב מפני traversal (../, נתיבים מוחלטים, שמות אסורים)
                clean_parts = [p for p in rel_path.replace('\\', '/').split('/')
                               if p and p not in ('.', '..')]
                if not clean_parts:
                    continue
                target_path = os.path.join(target_dir, *clean_parts)
                # הגנת Zip Slip: לוודא שהיעד הסופי נשאר בתוך target_dir
                real_target = os.path.realpath(target_path)
                if real_target != safe_root and not real_target.startswith(safe_root + os.sep):
                    print(f"[Socket] Zip Slip blocked: {member.filename}")
                    continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zf.open(member) as src, open(target_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)

        return target_dir

    def send_data(self, target_ip, msg_type, payload_data, sender_id, sender_name, metadata=None, port=None):
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(2)
        cleanup_archive = False
        try:
            target_port = port or self.port
            client_socket.connect((target_ip, target_port))
            # ה-timeout של 2ש' נועד רק לחיבור; העברת קבצים גדולים יכולה להימשך זמן רב
            client_socket.settimeout(300)

            if msg_type in ('link', 'text'):
                payload_bytes = payload_data.encode('utf-8')
                size = len(payload_bytes)
                filename = None
                folder_name = None
            elif msg_type == 'file':
                if not os.path.exists(payload_data):
                    return False
                size = os.path.getsize(payload_data)
                filename = os.path.basename(payload_data)
                folder_name = None
            elif msg_type == 'folder':
                cleanup_archive = False
                if os.path.isdir(payload_data):
                    archive_path = self._create_zip_archive(payload_data)
                    cleanup_archive = True
                    size = os.path.getsize(archive_path)
                    filename = os.path.basename(archive_path)
                    folder_name = metadata.get('folder_name') if metadata else os.path.basename(os.path.normpath(payload_data))
                elif os.path.isfile(payload_data) and payload_data.lower().endswith('.zip'):
                    archive_path = payload_data
                    size = os.path.getsize(archive_path)
                    filename = os.path.basename(archive_path)
                    folder_name = metadata.get('folder_name') if metadata else os.path.splitext(os.path.basename(payload_data))[0]
                else:
                    return False

            header = {
                "type": msg_type,
                "size": size,
                "filename": filename,
                "folder_name": folder_name,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "port": self.port
            }
            header_bytes = json.dumps(header).encode('utf-8')

            client_socket.sendall(struct.pack('>I', len(header_bytes)))
            client_socket.sendall(header_bytes)

            if msg_type in ('link', 'text'):
                client_socket.sendall(payload_bytes)
            elif msg_type in ('file', 'folder'):
                source_path = payload_data if msg_type == 'file' else archive_path
                sent_bytes = 0
                last_update_percent = 0

                with open(source_path, 'rb') as f:
                    while chunk := f.read(65536):
                        client_socket.sendall(chunk)
                        sent_bytes += len(chunk)

                        if size > 0:
                            percent = int((sent_bytes / size) * 100)
                            if percent >= last_update_percent + 1:
                                if self.on_progress_callback:
                                    self.on_progress_callback('send', percent)
                                last_update_percent = percent

                if msg_type == 'folder' and cleanup_archive and os.path.exists(archive_path):
                    try:
                        os.remove(archive_path)
                    except OSError:
                        pass
            return True

        except Exception as exc:
            print(f"[SocketManager] send_data error target={target_ip}:{port} type={msg_type} payload={payload_data} error={exc}")
            return False
        finally:
            if msg_type == 'folder' and cleanup_archive and os.path.exists(archive_path):
                try:
                    os.remove(archive_path)
                except OSError:
                    pass
            client_socket.close()


class DiscoveryManager:
    def __init__(self, my_id, my_name, on_peer_found_callback, port=5051, chat_port=None):
        self.my_id = my_id
        self.my_name = my_name
        self.on_peer_found_callback = on_peer_found_callback
        self.port = port
        self.chat_port = chat_port
        self.is_running = False
        self._broadcast_sock = None
        self._listen_sock = None

    def start(self):
        self.is_running = True
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self.is_running = False
        for sock in (self._broadcast_sock, self._listen_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._broadcast_sock = None
        self._listen_sock = None

    def set_name(self, new_name):
        self.my_name = new_name

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while self.is_running:
            try:
                payload = json.dumps({"id": self.my_id, "name": self.my_name, "port": self.chat_port}).encode('utf-8')
                sock.sendto(payload, ('255.255.255.255', self.port))
            except Exception:
                pass
            time.sleep(3)
        sock.close()
        if self._broadcast_sock is sock:
            self._broadcast_sock = None

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._listen_sock = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except AttributeError:
            pass

        try:
            sock.bind(('', self.port))
        except OSError as e:
            print(f"[Discovery] Port {self.port} might be in use: {e}")
            return

        while self.is_running:
            try:
                sock.settimeout(1.0)
                data, addr = sock.recvfrom(1024)
                peer_ip = addr[0]
                peer_info = json.loads(data.decode('utf-8'))

                peer_id = peer_info.get('id')
                peer_name = peer_info.get('name')
                peer_port = peer_info.get('port')

                if peer_id and peer_name and self.on_peer_found_callback:
                    self.on_peer_found_callback(peer_id, peer_name, peer_ip, peer_port)
            except socket.timeout:
                continue
            except Exception:
                pass
        sock.close()
        if self._listen_sock is sock:
            self._listen_sock = None