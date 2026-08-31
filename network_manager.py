import asyncio
import socket
import subprocess
import sys
import tempfile
import os


class NetworkMode:
    WIFI_HOTSPOT = "WIFI_HOTSPOT"
    BLUETOOTH = "BLUETOOTH"
    NONE = "NONE"


class NetworkManager:
    def __init__(self, default_ssid="YeshivaConnect", default_key="Yeshiva123"):
        self.current_mode = NetworkMode.NONE
        self.hotspot_manager = None
        self.default_ssid = default_ssid
        self.default_key = default_key
        self.active_ssid = default_ssid
        self.active_key = default_key
        self.last_error = None

    def set_credentials(self, ssid=None, key=None):
        """מעדכן SSID/סיסמה לנקודה החמה (מגיע מההגדרות). ריק => שימוש בברירת מחדל."""
        if ssid:
            self.default_ssid = ssid.strip()
        if key:
            self.default_key = key.strip()

    def _build_hosted_network_commands(self, ssid=None, key=None):
        ssid_value = (ssid or self.default_ssid).strip()
        key_value = (key or self.default_key).strip()
        return [
            ["netsh", "wlan", "set", "hostednetwork", f"mode=allow", f"ssid={ssid_value}", f"key={key_value}"],
            ["netsh", "wlan", "start", "hostednetwork"],
        ]

    def _run_netsh(self, args):
        # ב-EXE עם --noconsole אין stdin/stdout/stderr תקינים; חובה לנתב את שלושתם
        # במפורש אחרת netsh נכשל. CREATE_NO_WINDOW מונע הבזק חלון cmd.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
            check=False,
        )

    def _try_netsh_hotspot(self):
        try:
            status_result = self._run_netsh(["netsh", "wlan", "show", "hostednetwork"])
            if status_result.returncode == 0 and "Status: Started" in status_result.stdout:
                self.current_mode = NetworkMode.WIFI_HOTSPOT
                return self.current_mode, "Success: Wi-Fi Hotspot already active."
        except OSError as exc:
            self.last_error = str(exc)

        commands = self._build_hosted_network_commands()
        for command in commands:
            result = self._run_netsh(command)
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.returncode != 0:
                self.last_error = output or "Unknown netsh error"
                return NetworkMode.NONE, self._friendly_netsh_error(output)

        self.current_mode = NetworkMode.WIFI_HOTSPOT
        self.active_ssid = self.default_ssid
        self.active_key = self.default_key
        return self.current_mode, f"Success: Wi-Fi Hotspot active. SSID: {self.default_ssid}"

    def _friendly_netsh_error(self, output):
        """מתרגם שגיאות netsh נפוצות להודעה ברורה בעברית."""
        low = (output or "").lower()
        if any(k in low for k in ("access is denied", "requires elevation", "גישה נדחתה", "הרשאות")):
            return "נדרשות הרשאות מנהל. הפעל את התוכנה מחדש ואשר את בקשת ההרשאה (UAC)."
        if any(k in low for k in ("not supported", "group or resource", "correct state", "wireless autoconfig")):
            return ("הרשת המתארחת אינה נתמכת/מושבתת. ודא ששירות 'WLAN AutoConfig' פועל, "
                    "או הפעל נקודה חמה ידנית: הגדרות Windows > רשת > נקודה חמה ניידת.")
        return f"לא ניתן להפעיל נקודה חמה: {output or 'שגיאה לא ידועה'}"

    async def initialize_network(self) -> tuple[str, str]:
        """מנסה להפעיל נקודת חום דרך Windows API, ובמקרה של כשל עוברת ל-netsh."""
        print("[Network] Attempting to initialize Wi-Fi Hotspot...")

        if sys.platform != "win32":
            self.current_mode = NetworkMode.NONE
            return self.current_mode, "Error: Windows OS is required."

        try:
            import winsdk.windows.networking.connectivity as connectivity
            import winsdk.windows.networking.networkoperators as operators

            connection_profile = connectivity.NetworkInformation.get_internet_connection_profile()

            if not connection_profile:
                profiles = connectivity.NetworkInformation.get_connection_profiles()
                if profiles and len(profiles) > 0:
                    connection_profile = profiles[0]
                else:
                    raise Exception("No network adapters found.")

            self.hotspot_manager = operators.NetworkOperatorTetheringManager.create_from_connection_profile(connection_profile)

            if self.hotspot_manager.tethering_operational_state == operators.TetheringOperationalState.OFF:
                print("[Network] Starting Wi-Fi Hotspot...")
                result = await self.hotspot_manager.start_tethering_async()

                if result.status != operators.TetheringOperationStatus.SUCCESS:
                    raise Exception(f"Tethering operation failed with status: {result.status}")

            self.current_mode = NetworkMode.WIFI_HOTSPOT

            try:
                credential = self.hotspot_manager.get_current_access_point_configuration()
                ssid_name = credential.ssid
                self.active_ssid = ssid_name or self.default_ssid
                try:
                    self.active_key = credential.passphrase or self.default_key
                except Exception:
                    self.active_key = self.default_key
            except Exception as exc:
                print(f"[Network] Could not retrieve SSID name: {exc}")
                ssid_name = self.default_ssid
                self.active_ssid = self.default_ssid
                self.active_key = self.default_key

            return self.current_mode, f"Success: Wi-Fi Hotspot active. SSID: {ssid_name}"

        except Exception as exc:
            print(f"[Network] Windows SDK path unavailable or failed: {exc}")

        return self._try_netsh_hotspot()

    def get_hotspot_ip(self):
        """מחזיר את כתובת ה-IP של המחשב על מנשק הנקודה החמה (עדיפות ל-192.168.137.* של ICS)."""
        candidates = []
        try:
            host = socket.gethostname()
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    candidates.append(ip)
        except Exception:
            pass
        # עדיפות לתת-הרשת הטיפוסית של ICS/hostednetwork ב-Windows
        for ip in candidates:
            if ip.startswith('192.168.137.'):
                return ip
        if candidates:
            return candidates[0]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(('8.8.8.8', 80))
                return s.getsockname()[0]
        except Exception:
            return '192.168.137.1'

    def get_hotspot_info(self):
        """פרטי הנקודה החמה הפעילה עבור יצירת QR/הצגה."""
        return {
            "ssid": self.active_ssid or self.default_ssid,
            "key": self.active_key or self.default_key,
            "ip": self.get_hotspot_ip(),
            "active": self.current_mode == NetworkMode.WIFI_HOTSPOT,
        }

    def connect_to_wifi(self, ssid, key):
        """מתחבר לרשת Wi-Fi (WPA2PSK) דרך netsh — משמש בסריקת QR של מחשב אחר."""
        if sys.platform != "win32":
            return False, "Windows required"
        if not ssid:
            return False, "SSID חסר"

        ssid_xml = self._xml_escape(ssid)
        key_xml = self._xml_escape(key or "")
        auth = "WPA2PSK" if key else "open"
        encryption = "AES" if key else "none"
        shared_key_block = (
            f"<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
            f"<keyMaterial>{key_xml}</keyMaterial></sharedKey>"
        ) if key else ""
        profile_xml = (
            '<?xml version="1.0"?>'
            '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
            f'<name>{ssid_xml}</name>'
            f'<SSIDConfig><SSID><name>{ssid_xml}</name></SSID></SSIDConfig>'
            '<connectionType>ESS</connectionType><connectionMode>auto</connectionMode>'
            '<MSM><security><authEncryption>'
            f'<authentication>{auth}</authentication><encryption>{encryption}</encryption>'
            '<useOneX>false</useOneX></authEncryption>'
            f'{shared_key_block}'
            '</security></MSM></WLANProfile>'
        )

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix='.xml', prefix='wifi_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(profile_xml)

            add_res = self._run_netsh(["netsh", "wlan", "add", "profile", f"filename={tmp_path}", "user=all"])
            if add_res.returncode != 0:
                detail = (add_res.stdout or "") + (add_res.stderr or "")
                self.last_error = detail.strip()
                return False, f"הוספת פרופיל נכשלה: {self.last_error or 'שגיאה'}"

            conn_res = self._run_netsh(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"])
            if conn_res.returncode != 0:
                detail = (conn_res.stdout or "") + (conn_res.stderr or "")
                self.last_error = detail.strip()
                return False, f"התחברות נכשלה: {self.last_error or 'שגיאה'}"
            return True, f"מתחבר לרשת {ssid}..."
        except OSError as exc:
            self.last_error = str(exc)
            return False, f"שגיאת מערכת: {exc}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _xml_escape(value):
        return (str(value).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&apos;'))

    async def stop_hotspot(self):
        """סגירה מסודרת של הנקודה החמה בעת סגירת התוכנה."""
        if self.hotspot_manager and self.current_mode == NetworkMode.WIFI_HOTSPOT:
            try:
                import winsdk.windows.networking.networkoperators as operators
                state = self.hotspot_manager.tethering_operational_state
                if state == operators.TetheringOperationalState.ON:
                    print("[Network] Stopping Wi-Fi Hotspot...")
                    await self.hotspot_manager.stop_tethering_async()
            except Exception:
                pass

        try:
            result = self._run_netsh(["netsh", "wlan", "stop", "hostednetwork"])
            if result.returncode == 0:
                self.current_mode = NetworkMode.NONE
                return True
        except OSError:
            pass

        self.current_mode = NetworkMode.NONE
        return False


if __name__ == "__main__":
    async def main():
        manager = NetworkManager()
        mode, message = await manager.initialize_network()
        print(f"\nFinal Status -> Mode: {mode} | Message: {message}")

        if mode == NetworkMode.WIFI_HOTSPOT:
            input("\nPress Enter to stop hotspot and exit...")
            await manager.stop_hotspot()

    asyncio.run(main())