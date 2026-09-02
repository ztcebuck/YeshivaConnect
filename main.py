import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import eel

from database_manager import DatabaseManager
from network_manager import NetworkManager
from socket_manager import DiscoveryManager, SocketManager
from web_share import WebShareManager

QR_PREFIX = "YCONNECT:"


def is_admin():
    """בודק אם התהליך רץ עם הרשאות מנהל (Windows)."""
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_admin():
    """
    מפעיל מחדש את התוכנה עם הרשאות מנהל (בקשת UAC חד-פעמית).
    נדרש כדי להפעיל נקודה חמה (netsh hostednetwork) גם ללא אינטרנט.
    מחזיר True אם צריך לצאת (הופעל מופע מוגבה), False אם להמשיך כרגיל.
    """
    if sys.platform != 'win32' or is_admin():
        return False
    try:
        import ctypes
        if getattr(sys, 'frozen', False):
            executable = sys.executable
            params = ''
        else:
            executable = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        # ShellExecuteW עם "runas" מציג את חלון ה-UAC פעם אחת
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        # >32 = הצלחה: המופע המוגבה עלה, נסגור את הנוכחי
        return ret > 32
    except Exception as exc:
        print(f"[Admin] elevation failed: {exc}")
        return False


class ChatApp:
    def __init__(self):
        # ב-EXE של קובץ יחיד (frozen) __file__ מצביע לתיקייה זמנית שנמחקת ביציאה;
        # לכן נתוני המשתמש (DB/זהות/הגדרות/קבצים) נשמרים ליד קובץ ההרצה עצמו.
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.identity_file = os.path.join(self.base_dir, "my_identity.json")
        self.settings_file = os.path.join(self.base_dir, "settings.json")

        self.load_or_create_identity()
        self.settings = self._load_settings()

        self.db = DatabaseManager(os.path.join(self.base_dir, "local_chat.db"))
        self.port = self._load_port()
        # תיקיית staging לקבצים נכנסים — נשארים כאן עד שהמשתמש בוחר לשמור
        self.staging_dir = os.path.join(self.base_dir, "Received_Files")
        self.sock = SocketManager(port=self.port, save_dir=self.staging_dir)
        self.net = NetworkManager()
        self.net.set_credentials(self.settings.get("hotspot_ssid"), self.settings.get("hotspot_key"))

        # שרת שיתוף בדפדפן — עבור חברים ללא התוכנה (מגיש דף הורדה על הנקודה החמה)
        self.web_share_upload_dir = os.path.join(self.base_dir, "Browser_Uploads")
        self.web_share = WebShareManager(
            host_ip_provider=self.net.get_hotspot_ip,
            upload_dir=self.web_share_upload_dir,
        )
        self.web_share.on_upload_callback = self._on_browser_upload
        # פס התקדמות בתוכנה המארחת בעת העלאה/הורדה דרך הדפדפן (אותו פס של העברות ה-socket)
        self.web_share.on_progress_callback = self.update_progress_bar

        self.current_chat_id = None
        self.current_chat_ip = None
        self.current_chat_port = None
        self.contacts = []

        self.sock.set_callback(self.handle_incoming_message)
        self.sock.set_progress_callback(self.update_progress_bar)
        self.sock.start_server()
        self.port = self.sock.port
        self.save_identity()

        self.discovery = DiscoveryManager(self.my_id, self.my_name, self.on_peer_discovered, chat_port=self.port)
        self.discovery.start()

    # ------------------------- הגדרות -------------------------
    def _default_download_dir(self):
        candidate = os.path.join(os.path.expanduser("~"), "Downloads")
        if os.path.isdir(candidate):
            return candidate
        return os.path.join(self.base_dir, "Saved_Files")

    def _default_settings(self):
        return {
            "download_dir": self._default_download_dir(),
            "theme": "dark",
            "hotspot_ssid": "",
            "hotspot_key": "",
            "delete_unsaved_on_close": True,
        }

    def _load_settings(self):
        defaults = self._default_settings()
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                defaults.update({k: v for k, v in data.items() if k in defaults})
        except Exception:
            pass
        if not defaults.get("download_dir"):
            defaults["download_dir"] = self._default_download_dir()
        return defaults

    def _save_settings(self):
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[Settings] save error: {exc}")

    def get_settings(self):
        return self.settings

    def set_settings(self, new_settings):
        if not isinstance(new_settings, dict):
            return self.settings
        allowed = set(self._default_settings().keys())
        for key, value in new_settings.items():
            if key in allowed:
                self.settings[key] = value
        # ודא שיעד ההורדות קיים או חזור לברירת מחדל
        download_dir = self.settings.get("download_dir")
        if download_dir:
            try:
                os.makedirs(download_dir, exist_ok=True)
            except Exception:
                self.settings["download_dir"] = self._default_download_dir()
        self.net.set_credentials(self.settings.get("hotspot_ssid"), self.settings.get("hotspot_key"))
        self._save_settings()
        return self.settings

    def choose_download_dir(self):
        root = tk.Tk()
        root.withdraw()
        try:
            chosen = filedialog.askdirectory(title="בחר תיקיית שמירה לקבצים")
        finally:
            root.destroy()
        if chosen:
            self.settings["download_dir"] = chosen
            self._save_settings()
            eel.show_notification("עודכן", f"קבצים שמורים יישמרו אל: {chosen}")
        return self.settings.get("download_dir")

    def load_or_create_identity(self):
        config_file = self.identity_file
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.my_id = data.get("id") or uuid.uuid4().hex
                    self.my_name = data.get("name") or data.get("computer_name") or self._default_name()
            except Exception:
                self._create_new_identity(config_file)
        else:
            self._create_new_identity(config_file)

    def _create_new_identity(self, config_file):
        self.my_id = uuid.uuid4().hex
        self.my_name = self._default_name()
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump({"id": self.my_id, "name": self.my_name}, f)

    def _default_name(self):
        try:
            return socket.gethostname()
        except Exception:
            return "המחשב שלי"

    def save_identity(self):
        os.makedirs(os.path.dirname(self.identity_file), exist_ok=True)
        with open(self.identity_file, "w", encoding="utf-8") as f:
            json.dump({"id": self.my_id, "name": self.my_name, "port": self.port}, f)

    @staticmethod
    def _find_free_port(start_port=5050, max_attempts=20):
        candidate = start_port
        host = '127.0.0.1'
        for _ in range(max_attempts):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', 0), 1)
                    except OSError:
                        pass
                    sock.bind((host, candidate))
                    sock.listen(1)
                    return candidate
            except OSError:
                candidate += 1
        return 0

    def _load_port(self):
        try:
            with open(self.identity_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                port = data.get("port")
                if isinstance(port, int) and port > 0:
                    return self._find_free_port(start_port=port, max_attempts=10) or port
        except Exception:
            pass
        return self._find_free_port(start_port=5050, max_attempts=30) or 5050

    def _push_contacts(self):
        self.contacts = self.db.get_contacts()
        eel.update_contacts_from_backend(self.contacts)

    def _push_status(self, status, kind):
        eel.update_network_status(status, kind)

    def _push_my_name(self):
        eel.update_my_name(self.my_name)

    def _push_chat(self):
        if not self.current_chat_id:
            eel.refresh_chat_from_backend([])
            return
        history = self.db.get_chat_history(self.current_chat_id)
        eel.refresh_chat_from_backend(history)

    def update_progress_bar(self, direction, percent):
        eel.update_transfer_status("⬆️ שולח..." if direction == "send" else "⬇️ מקבל...", percent)

    def handle_incoming_message(self, sender_ip, sender_id, sender_name, msg_type, content, sender_port=None):
        contacts = self.db.get_contacts()
        known = next((c for c in contacts if c['id'] == sender_id or c['ip'] == sender_ip), None)
        resolved_name = known['name'] if known else sender_name
        target_contact_id = known['id'] if known else sender_id

        contact_port = sender_port or self.port
        if msg_type == 'link' and content == '__CHAT_PING_REQ__':
            self.db.add_or_update_contact(target_contact_id, resolved_name, sender_ip, port=contact_port)
            self._push_contacts()
            eel.show_notification("🔔 פינג נכנס!", f"המחשב '{resolved_name}' מנסה להשיג אותך בצ'אט")
            threading.Thread(target=self._show_ping_popup, args=(resolved_name,), daemon=True).start()
            return

        self.db.add_or_update_contact(target_contact_id, resolved_name, sender_ip, port=contact_port)
        message_id = self.db.save_message(target_contact_id, False, msg_type, content)
        # קבצים/תיקיות נכנסים נכנסים ל-staging ומסומנים כלא-שמורים עד שהמשתמש יבחר לשמור
        if msg_type in ('file', 'folder') and message_id:
            self.db.track_received_file(message_id, content)
        self._push_contacts()
        eel.update_transfer_status("", 100)

        if self.current_chat_id == target_contact_id:
            self.current_chat_ip = sender_ip
            self.current_chat_port = contact_port
            self._push_chat()
        else:
            eel.show_notification("הודעה חדשה", f"התקבלה הודעה חדשה מ-{resolved_name}")

    def _show_ping_popup(self, resolved_name):
        root = None
        try:
            if os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, f"התקבלה בקשת פינג מ-{resolved_name}", "פינג", 0x40)
                return
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("פינג", f"התקבלה בקשת פינג מ-{resolved_name}")
        except Exception:
            pass
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    def on_peer_discovered(self, peer_id, peer_name, peer_ip, peer_port=None):
        if peer_id == self.my_id:
            return
        contacts = self.db.get_contacts()
        known = next((c for c in contacts if c['id'] == peer_id or c['ip'] == peer_ip), None)
        resolved_name = known['name'] if known else peer_name
        target_contact_id = known['id'] if known else peer_id
        should_refresh = False
        if known:
            should_refresh = known['ip'] != peer_ip or known['name'] != resolved_name or (isinstance(peer_port, int) and peer_port > 0 and known.get('port') != peer_port)
        else:
            should_refresh = True

        if should_refresh:
            port_to_store = peer_port or self._resolve_contact_port(target_contact_id, peer_ip, self.port)
            self.db.add_or_update_contact(target_contact_id, resolved_name, peer_ip, port=port_to_store)
            if self.current_chat_id == target_contact_id:
                self.current_chat_ip = peer_ip
                self.current_chat_port = port_to_store
            self._push_contacts()

    def on_page_ready(self):
        self._push_contacts()
        self._push_status("מצב רשת: לא מחובר", "idle")
        self._push_my_name()
        self._push_chat()
        return True

    def get_contacts(self):
        return self.db.get_contacts()

    def get_my_name(self):
        return self.my_name

    def _find_contact(self, contact_id=None, contact_ip=None):
        contacts = self.db.get_contacts()
        if contact_id:
            for contact in contacts:
                if contact['id'] == contact_id:
                    return contact
        if contact_ip:
            for contact in contacts:
                if contact['ip'] == contact_ip:
                    return contact
        return None

    def _resolve_contact_port(self, contact_id=None, contact_ip=None, fallback_port=None):
        contact = self._find_contact(contact_id, contact_ip)
        if contact and isinstance(contact.get('port'), int) and contact['port'] > 0:
            return contact['port']
        if isinstance(fallback_port, int) and fallback_port > 0 and fallback_port != self.port:
            return fallback_port
        return 5050

    def _resolve_contact_ip(self, contact_id=None, contact_ip=None):
        if contact_ip:
            return contact_ip
        contact = self._find_contact(contact_id=contact_id)
        if contact:
            return contact.get('ip')
        return None

    def select_contact(self, contact_id, contact_ip, contact_port=None):
        self.current_chat_id = contact_id
        self.current_chat_ip = self._resolve_contact_ip(contact_id, contact_ip)
        self.current_chat_port = self._resolve_contact_port(contact_id, contact_ip, contact_port or self.current_chat_port or self.port)
        self._push_chat()
        return self.db.get_chat_history(contact_id)

    def add_contact(self, name, ip):
        if not name or not ip:
            return self.db.get_contacts()
        tmp_id = f"manual_{ip}"
        self.db.add_or_update_contact(tmp_id, name, ip, port=None)
        self._push_contacts()
        return self.db.get_contacts()

    def rename_contact(self, contact_id, new_name):
        if not contact_id or not new_name:
            return self.db.get_contacts()
        self.db.update_contact_name(contact_id, new_name.strip())
        self._push_contacts()
        return self.db.get_contacts()

    def send_text(self, text):
        if not self.current_chat_id:
            eel.show_notification("שגיאה", "בחר מחשב קודם")
            return False
        if not text:
            return False
        target_ip = self._resolve_contact_ip(self.current_chat_id, self.current_chat_ip)
        if not target_ip:
            eel.show_notification("שגיאה", "לא נמצאה כתובת IP תקינה לאיש הקשר")
            return False
        target_port = self._resolve_contact_port(self.current_chat_id, target_ip, self.current_chat_port)
        self.current_chat_ip = target_ip
        self.current_chat_port = target_port
        msg_type = 'link' if text.startswith(('http://', 'https://', 'www.')) else 'text'
        threading.Thread(target=self._send_and_save, args=(self.current_chat_id, target_ip, msg_type, text), daemon=True).start()
        return True

    def trigger_file_dialog(self, file_path=None):
        if not self.current_chat_id:
            eel.show_notification("שגיאה", "בחר מחשב קודם")
            return None

        target_ip = self._resolve_contact_ip(self.current_chat_id, self.current_chat_ip)
        if not target_ip:
            eel.show_notification("שגיאה", "לא נמצאה כתובת IP תקינה לאיש הקשר")
            return None

        target_port = self._resolve_contact_port(self.current_chat_id, target_ip, self.current_chat_port)
        self.current_chat_ip = target_ip
        self.current_chat_port = target_port

        if not file_path:
            root = tk.Tk()
            root.withdraw()
            try:
                file_path = filedialog.askopenfilename(title="בחר קובץ לשליחה")
            finally:
                root.destroy()
        if file_path:
            threading.Thread(target=self._send_and_save, args=(self.current_chat_id, target_ip, 'file', file_path), daemon=True).start()
        return file_path

    def trigger_folder_dialog(self, folder_path=None):
        if not self.current_chat_id:
            eel.show_notification("שגיאה", "בחר מחשב קודם")
            return None

        target_ip = self._resolve_contact_ip(self.current_chat_id, self.current_chat_ip)
        if not target_ip:
            eel.show_notification("שגיאה", "לא נמצאה כתובת IP תקינה לאיש הקשר")
            return None

        target_port = self._resolve_contact_port(self.current_chat_id, target_ip, self.current_chat_port)
        self.current_chat_ip = target_ip
        self.current_chat_port = target_port

        if not folder_path:
            root = tk.Tk()
            root.withdraw()
            try:
                folder_path = filedialog.askdirectory(title="בחר תיקייה לשליחה")
            finally:
                root.destroy()
        if folder_path:
            threading.Thread(target=self._send_and_save, args=(self.current_chat_id, target_ip, 'folder', folder_path), daemon=True).start()
        return folder_path

    def receive_dropped_file(self, contact_id, filename, base64_data, msg_type='file'):
        if not contact_id or not filename or not base64_data:
            return False

        contact = self._find_contact(contact_id=contact_id)
        target_ip = contact['ip'] if contact else self._resolve_contact_ip(contact_id, self.current_chat_ip)
        target_port = self._resolve_contact_port(contact_id, target_ip, self.current_chat_port or self.port)

        if not target_ip:
            eel.show_notification("שגיאה", "אין כתובת IP עבור איש הקשר שנבחר")
            return False

        try:
            data = base64.b64decode(base64_data)
        except Exception:
            eel.show_notification("שגיאה", "קובץ גרור לא תקין")
            return False

        temp_dir = os.path.join(self.base_dir, 'Dropped_Files')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = os.path.basename(filename)
        temp_path = os.path.join(temp_dir, f"drop_{uuid.uuid4().hex}_{safe_name}")

        try:
            with open(temp_path, 'wb') as f:
                f.write(data)
        except Exception as exc:
            eel.show_notification("שגיאה", f"שגיאת כתיבה לקובץ: {exc}")
            return False

        self.current_chat_ip = target_ip
        self.current_chat_port = target_port
        threading.Thread(target=self._send_and_save, args=(contact_id, target_ip, msg_type, temp_path), kwargs={'cleanup_source': True}, daemon=True).start()
        return True

    def send_ping(self):
        if not self.current_chat_id:
            eel.show_notification("שגיאה", "בחר מחשב קודם")
            return False

        target_ip = self._resolve_contact_ip(self.current_chat_id, self.current_chat_ip)
        if not target_ip:
            eel.show_notification("שגיאה", "לא נמצאה כתובת IP תקינה לאיש הקשר")
            return False

        target_port = self._resolve_contact_port(self.current_chat_id, target_ip, self.current_chat_port)
        self.current_chat_ip = target_ip
        self.current_chat_port = target_port
        threading.Thread(target=self.sock.send_data, args=(target_ip, 'link', '__CHAT_PING_REQ__', self.my_id, self.my_name), kwargs={'port': target_port}, daemon=True).start()
        return True

    def start_network(self):
        threading.Thread(target=self._run_network_async, daemon=True).start()
        return True

    def change_my_name(self):
        root = tk.Tk()
        root.withdraw()
        try:
            new_name = simpledialog.askstring("שם המחשב", "הכנס שם חדש למחשב זה:", initialvalue=self.my_name)
        finally:
            root.destroy()
        if new_name:
            self.my_name = new_name.strip() or self._default_name()
            self.save_identity()
            self.discovery.set_name(self.my_name)
            self._push_my_name()
            eel.show_notification("עודכן", "השם נשמר לרשת")
        return self.my_name

    def set_my_name(self, new_name):
        """הגדרת שם המחשב מתוך פאנל ההגדרות (כך ייראה ממחשבים אחרים)."""
        if new_name and new_name.strip():
            self.my_name = new_name.strip()
            self.save_identity()
            self.discovery.set_name(self.my_name)
            self._push_my_name()
            eel.show_notification("עודכן", "שם המחשב נשמר ושודר לרשת")
        return self.my_name

    def get_hotspot_info(self):
        """מחזיר את פרטי הנקודה החמה + זהות המחשב, ומחרוזת QR מוכנה לסריקה."""
        info = self.net.get_hotspot_info()
        payload = {
            "id": self.my_id,
            "name": self.my_name,
            "ip": info.get("ip"),
            "port": self.port,
            "ssid": info.get("ssid"),
            "key": info.get("key"),
        }
        return {
            "ssid": payload["ssid"],
            "key": payload["key"],
            "ip": payload["ip"],
            "port": self.port,
            "active": info.get("active", False),
            "qr": QR_PREFIX + json.dumps(payload, ensure_ascii=False),
        }

    def connect_from_qr(self, payload):
        """מפענח QR שנסרק ממחשב אחר: מתחבר לנקודה החמה ומוסיף אותו כאיש קשר."""
        if not payload:
            return False
        text = payload.strip()
        if text.startswith(QR_PREFIX):
            text = text[len(QR_PREFIX):]
        try:
            data = json.loads(text)
        except Exception:
            eel.show_notification("שגיאה", "ברקוד לא תקין")
            return False

        peer_id = data.get("id")
        peer_name = data.get("name") or "מחשב"
        peer_ip = data.get("ip")
        peer_port = data.get("port")
        ssid = data.get("ssid")
        key = data.get("key")

        # שלב 1: התחברות לנקודה החמה (אם צוין SSID)
        if ssid:
            threading.Thread(target=self._connect_wifi_thread, args=(ssid, key), daemon=True).start()

        # שלב 2: הוספת המחשב כאיש קשר
        if peer_id and peer_ip:
            self.db.add_or_update_contact(peer_id, peer_name, peer_ip, port=peer_port)
            self._push_contacts()
            eel.show_notification("נוסף איש קשר", f"'{peer_name}' נוסף מהברקוד")
            return True
        eel.show_notification("שגיאה", "בברקוד חסרים פרטי חיבור")
        return False

    def _connect_wifi_thread(self, ssid, key):
        success, message = self.net.connect_to_wifi(ssid, key)
        eel.show_notification("נקודה חמה" if success else "שגיאת התחברות", message)

    # ------------------------- שיתוף בדפדפן (לחברים ללא התוכנה) -------------------------
    def web_share_status(self):
        """מחזיר את מצב שרת השיתוף: פעיל/כתובת/קבצים משותפים."""
        return self.web_share.get_status()

    def web_share_start(self):
        """מפעיל את שרת ההורדות ומחזיר את המצב (כולל הכתובת להצגה בדפדפן)."""
        port = self.web_share.start()
        if not port:
            eel.show_notification("שגיאה", "לא ניתן להפעיל את שרת השיתוף")
        else:
            eel.show_notification("שיתוף פעיל", "חברים יכולים להיכנס לכתובת שמוצגת")
        return self.web_share.get_status()

    def web_share_stop(self):
        """עוצר את שרת ההורדות ומנקה קבצים זמניים."""
        self.web_share.stop()
        eel.show_notification("שיתוף הופסק", "שרת ההורדות נסגר")
        return self.web_share.get_status()

    def web_share_add_file(self):
        """פותח דיאלוג בחירת קובץ ומוסיף אותו לשיתוף."""
        root = tk.Tk()
        root.withdraw()
        try:
            file_paths = filedialog.askopenfilenames(title="בחר קבצים לשיתוף בדפדפן")
        finally:
            root.destroy()
        added = 0
        for path in file_paths or []:
            if self.web_share.add_file(path):
                added += 1
        if added:
            eel.show_notification("נוסף לשיתוף", f"נוספו {added} קבצים לשיתוף בדפדפן")
        return self.web_share.get_status()

    def web_share_add_folder(self):
        """פותח דיאלוג בחירת תיקייה, דוחס אותה ל-zip ומוסיף לשיתוף."""
        root = tk.Tk()
        root.withdraw()
        try:
            folder_path = filedialog.askdirectory(title="בחר תיקייה לשיתוף בדפדפן")
        finally:
            root.destroy()
        if folder_path:
            if self.web_share.add_folder(folder_path):
                eel.show_notification("נוסף לשיתוף", "התיקייה נדחסה ונוספה לשיתוף")
            else:
                eel.show_notification("שגיאה", "לא ניתן להוסיף את התיקייה")
        return self.web_share.get_status()

    def web_share_remove(self, item_id):
        """מסיר פריט מהשיתוף."""
        self.web_share.remove_item(item_id)
        return self.web_share.get_status()

    def _on_browser_upload(self, record):
        """נקרא כשחבר מעלה קובץ דרך הדפדפן — מעדכן את הממשק ומודיע."""
        try:
            name = record.get("name", "קובץ")
            eel.show_notification("התקבל קובץ מהדפדפן", f"'{name}' נשמר ומופיע בשיתוף")
            eel.web_share_refresh()  # מרענן את המודל אם פתוח
        except Exception:
            pass

    def web_share_open_upload(self, upload_id):
        """פותח קובץ שהתקבל מהדפדפן."""
        record = self.web_share.get_upload(upload_id)
        if record:
            self.open_file(record["path"])
        else:
            eel.show_notification("שגיאה", "הקובץ לא נמצא")
        return self.web_share.get_status()

    def web_share_locate_upload(self, upload_id):
        """פותח את מיקום הקובץ שהתקבל מהדפדפן בסייר הקבצים."""
        record = self.web_share.get_upload(upload_id)
        if record:
            self.open_folder(record["path"])
        else:
            eel.show_notification("שגיאה", "הקובץ לא נמצא")
        return self.web_share.get_status()

    def web_share_remove_upload(self, upload_id, delete_file=False):
        """מסיר קובץ שהתקבל מהדפדפן מהרשימה (ואופציונלית מוחק אותו מהדיסק)."""
        self.web_share.remove_upload(upload_id, delete_file=delete_file)
        return self.web_share.get_status()

    def save_received_file(self, message_id):
        """מעביר קובץ/תיקייה שהתקבלו מ-staging אל תיקיית השמירה שהוגדרה."""
        record = self.db.get_received_file(message_id)
        if not record:
            eel.show_notification("שגיאה", "לא נמצא קובץ לשמירה")
            return False
        if record["saved"]:
            eel.show_notification("כבר נשמר", "הקובץ כבר נשמר")
            return True

        temp_path = record["temp_path"]
        if not temp_path or not os.path.exists(temp_path):
            eel.show_notification("שגיאה", "הקובץ המקורי לא נמצא")
            return False

        download_dir = self.settings.get("download_dir") or self._default_download_dir()
        try:
            os.makedirs(download_dir, exist_ok=True)
        except Exception as exc:
            eel.show_notification("שגיאה", f"לא ניתן ליצור את תיקיית השמירה: {exc}")
            return False

        dest = self._unique_dest(download_dir, os.path.basename(os.path.normpath(temp_path)))
        try:
            shutil.move(temp_path, dest)
        except Exception as exc:
            eel.show_notification("שגיאה", f"שמירה נכשלה: {exc}")
            return False

        self.db.mark_received_file_saved(message_id, dest)
        self._push_chat()
        eel.show_notification("נשמר", f"הקובץ נשמר אל: {dest}")
        return True

    @staticmethod
    def _unique_dest(directory, name):
        dest = os.path.join(directory, name)
        if not os.path.exists(dest):
            return dest
        base, ext = os.path.splitext(name)
        counter = 1
        while True:
            candidate = os.path.join(directory, f"{base} ({counter}){ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def list_connected_devices(self):
        """מרענן את רשימת המכשירים המחוברים לנקודה החמה (ARP + discovery ping מקבילי)."""
        threading.Thread(target=self._list_connected_devices_thread, daemon=True).start()
        return True

    def scan_hotspot_devices(self):
        threading.Thread(target=self._scan_hotspot_devices_thread, daemon=True).start()
        return True

    def open_link(self, content):
        if not content.startswith(('http://', 'https://')):
            content = 'http://' + content
        webbrowser.open(content)

    def open_file(self, content):
        if os.path.exists(content):
            try:
                if hasattr(os, 'startfile'):
                    os.startfile(content)
                else:
                    subprocess.run(['xdg-open', content],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
            except Exception as exc:
                eel.show_notification("שגיאה", f"לא ניתן לפתוח את הקובץ: {exc}")
        else:
            eel.show_notification("שגיאה", f"הקובץ לא נמצא בנתיב: {content}")

    def open_folder(self, content):
        if os.path.exists(content):
            try:
                subprocess.run(['explorer', '/select,', os.path.normpath(content)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
            except Exception as exc:
                eel.show_notification("שגיאה", f"לא ניתן לפתוח את התיקייה: {exc}")
        else:
            eel.show_notification("שגיאה", f"התיקייה לא נמצאת בנתיב: {content}")

    def _run_network_async(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        mode, msg = loop.run_until_complete(self.net.initialize_network())
        self._push_status(f"[{mode}] {msg}", "success" if mode != "NONE" else "error")

    def _ping_ip(self, target_ip):
        """שולח discovery-ping יחיד לכתובת. מחזיר True אם החיבור הצליח."""
        try:
            return bool(self.sock.send_data(target_ip, 'link', '__CHAT_PING_REQ__', self.my_id, self.my_name))
        except Exception:
            return False

    def _parallel_ping(self, targets, max_workers=60):
        """שולח ping מקבילי לרשימת כתובות ומחזיר את אלו שהגיבו."""
        reached = []
        targets = list(dict.fromkeys(targets))  # הסרת כפילויות תוך שמירת סדר
        if not targets:
            return reached
        with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as executor:
            results = executor.map(lambda ip: (ip, self._ping_ip(ip)), targets)
            for ip, ok in results:
                if ok:
                    reached.append(ip)
        return reached

    def _local_ipv4s(self):
        ips = []
        try:
            host = socket.gethostname()
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    ips.append(ip)
        except Exception:
            pass
        return ips

    def _scan_hotspot_devices_thread(self):
        targets = []
        my_ips = set(self._local_ipv4s())
        for host_ip in (my_ips or {None}):
            if not host_ip:
                continue
            base = '.'.join(host_ip.split('.')[:3])
            for last in range(1, 255):
                target_ip = f"{base}.{last}"
                if target_ip not in my_ips:
                    targets.append(target_ip)

        reached = self._parallel_ping(targets)
        if reached:
            eel.show_notification("סריקה הושלמה", f"נמצאו {len(reached)} מכשירים פעילים ברשת")
        else:
            eel.show_notification("סריקה הושלמה", "לא נמצאו מכשירים פעילים בשכבת הרשת")

    def _arp_hotspot_ips(self):
        """קורא את טבלת ה-ARP ומחזיר כתובות בתת-רשת של הנקודה החמה (192.168.137.*) + כל תת-רשת מקומית."""
        ips = set()
        local_prefixes = {'.'.join(ip.split('.')[:3]) for ip in self._local_ipv4s()}
        local_prefixes.add('192.168.137')  # תת-הרשת הטיפוסית של ICS ב-Windows
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            result = subprocess.run(["arp", "-a"],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    creationflags=creationflags, check=False, timeout=10)
            for match in re.finditer(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', result.stdout or ""):
                ip = match.group(1)
                prefix = '.'.join(ip.split('.')[:3])
                last_octet = ip.split('.')[-1]
                if prefix in local_prefixes and last_octet not in ('0', '255'):
                    ips.add(ip)
        except Exception as exc:
            print(f"[Scan] arp error: {exc}")
        return ips

    def _list_connected_devices_thread(self):
        targets = set(self._arp_hotspot_ips())
        # תמיד כולל את תת-הרשת של הנקודה החמה (192.168.137.*), גם אם עדיין לא ב-ARP
        targets.update(f"192.168.137.{last}" for last in range(1, 255))
        my_ips = set(self._local_ipv4s())
        targets = [ip for ip in targets if ip not in my_ips]

        reached = self._parallel_ping(targets)
        if reached:
            eel.show_notification("רענון הושלם", f"{len(reached)} מכשירים מחוברים לנקודה החמה")
        else:
            eel.show_notification("רענון הושלם", "לא נמצאו מכשירים מחוברים כרגע")

    def _send_and_save(self, target_id, target_ip, msg_type, content, cleanup_source=False):
        metadata = None
        if msg_type == 'folder':
            metadata = {'folder_name': os.path.basename(os.path.normpath(content))}

        if msg_type in ('file', 'folder'):
            eel.update_transfer_status("⬆️ שולח...", 0)

        target_port = self._resolve_contact_port(target_id, target_ip, self.current_chat_port)
        success = self.sock.send_data(target_ip, msg_type, content, self.my_id, self.my_name, metadata=metadata, port=target_port)
        if success:
            # לקבצים שנגררו — לא שומרים עותק בצד השולח, לכן נשמר רק השם בהיסטוריה
            stored_content = os.path.basename(os.path.normpath(content)) if cleanup_source else content
            self.db.save_message(target_id, True, msg_type, stored_content)
            self._push_chat()
        else:
            eel.update_transfer_status("", 100)
            eel.show_notification("שגיאת תקשורת", f"לא ניתן להתחבר למחשב בכתובת {target_ip}")

        # מחיקת העותק הזמני מהמחשב של השולח כדי לא לתפוס מקום
        if cleanup_source:
            self._remove_temp(content)

    @staticmethod
    def _remove_temp(path):
        try:
            if not path or not os.path.exists(path):
                return
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass

    def _cleanup_unsaved_files(self):
        """מוחק קבצים/תיקיות שהתקבלו ולא נשמרו — נקרא בסגירת התוכנה."""
        if not self.settings.get("delete_unsaved_on_close", True):
            return
        try:
            for record in self.db.get_unsaved_files():
                temp_path = record.get("temp_path")
                if not temp_path or not os.path.exists(temp_path):
                    self.db.delete_received_file_record(record["message_id"])
                    continue
                try:
                    if os.path.isdir(temp_path):
                        shutil.rmtree(temp_path, ignore_errors=True)
                    else:
                        os.remove(temp_path)
                except OSError:
                    pass
                self.db.delete_received_file_record(record["message_id"])
        except Exception as exc:
            print(f"[Cleanup] error: {exc}")

    def on_close(self):
        self._cleanup_unsaved_files()
        self.web_share.stop()
        self.discovery.stop()
        self.sock.stop_server()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.net.stop_hotspot())
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    # בקשת הרשאות מנהל חד-פעמית (UAC) — נדרש להפעלת נקודה חמה.
    # אם המשתמש מאשר, מופע מוגבה עולה והנוכחי נסגר; אם דוחה, ממשיכים ללא הרשאות.
    if ensure_admin():
        sys.exit(0)

    app = ChatApp()
    eel_port = ChatApp._find_free_port(start_port=8000, max_attempts=20) or 8000

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, 'web'))
        candidates.append(os.path.join(os.path.dirname(sys.executable), 'web'))
        candidates.append(os.path.dirname(sys.executable))
    candidates.append(os.path.join(base_dir, 'web'))
    candidates.append(os.path.join(base_dir, 'dist', 'web'))
    candidates.append(base_dir)

    web_dir = next((candidate for candidate in candidates if os.path.isdir(candidate)), base_dir)
    if os.path.isfile(os.path.join(web_dir, 'index.html')):
        eel.init(web_dir)
    else:
        eel.init(base_dir)
    eel.expose(app.on_page_ready)
    eel.expose(app.get_contacts)
    eel.expose(app.get_my_name)
    eel.expose(app.select_contact)
    eel.expose(app.add_contact)
    eel.expose(app.rename_contact)
    eel.expose(app.send_text)
    eel.expose(app.trigger_file_dialog)
    eel.expose(app.trigger_folder_dialog)
    eel.expose(app.send_ping)
    eel.expose(app.start_network)
    eel.expose(app.change_my_name)
    eel.expose(app.scan_hotspot_devices)
    eel.expose(app.open_link)
    eel.expose(app.open_file)
    eel.expose(app.open_folder)
    eel.expose(app.receive_dropped_file)
    eel.expose(app.get_settings)
    eel.expose(app.set_settings)
    eel.expose(app.choose_download_dir)
    eel.expose(app.set_my_name)
    eel.expose(app.get_hotspot_info)
    eel.expose(app.connect_from_qr)
    eel.expose(app.save_received_file)
    eel.expose(app.list_connected_devices)
    eel.expose(app.web_share_status)
    eel.expose(app.web_share_start)
    eel.expose(app.web_share_stop)
    eel.expose(app.web_share_add_file)
    eel.expose(app.web_share_add_folder)
    eel.expose(app.web_share_remove)
    eel.expose(app.web_share_open_upload)
    eel.expose(app.web_share_locate_upload)
    def _launch_eel():
        modes = []
        try:
            import eel.chrome as chm
            if chm.find_path():
                modes.append('chrome')
        except Exception:
            pass
        try:
            import eel.edge as edge
            if edge.find_path():
                modes.append('edge')
        except Exception:
            pass
        modes.extend(['default', False])

        for mode in modes:
            try:
                if mode is False:
                    webbrowser.open(f"http://127.0.0.1:{eel_port}/index.html")
                    eel.start('index.html', mode=False, port=eel_port, host="127.0.0.1")
                else:
                    eel.start('index.html', size=(1280, 820), port=eel_port, host="127.0.0.1", mode=mode)
                return
            except (EnvironmentError, OSError) as exc:
                print(f"[Eel] Browser mode '{mode}' unavailable: {exc}, trying next option...")
                continue
            except Exception as exc:
                print(f"[Eel] Unexpected error with mode '{mode}': {exc}")
                break

    _launch_eel()
    app.on_close()
