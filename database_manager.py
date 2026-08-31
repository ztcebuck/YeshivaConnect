import os
import sqlite3


class DatabaseManager:
    def __init__(self, db_path="local_chat.db"):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        """יוצר את בסיס הנתונים והטבלאות במידה ואינם קיימים"""
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS contacts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    last_ip TEXT NOT NULL,
                    port INTEGER DEFAULT 5050
                )
            ''')

            cursor.execute("PRAGMA table_info(contacts)")
            columns = {row[1] for row in cursor.fetchall()}
            if 'port' not in columns:
                cursor.execute("ALTER TABLE contacts ADD COLUMN port INTEGER DEFAULT 5050")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id TEXT NOT NULL,
                    is_outgoing BOOLEAN NOT NULL,
                    msg_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS received_files (
                    message_id INTEGER PRIMARY KEY,
                    temp_path TEXT NOT NULL,
                    saved INTEGER DEFAULT 0,
                    final_path TEXT,
                    FOREIGN KEY(message_id) REFERENCES messages(id)
                )
            ''')
        conn.close()

    def add_or_update_contact(self, contact_id: str, name: str, ip_address: str, port: int | None = None):
        """מוסיף מחשב חדש או מעדכן את ה-IP, הפורט והשם של מחשב קיים"""
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()
            port_value = port if port is not None else 5050

            cursor.execute('SELECT id FROM contacts WHERE last_ip = ? LIMIT 1', (ip_address,))
            existing_by_ip = cursor.fetchone()

            if existing_by_ip and existing_by_ip[0] != contact_id:
                old_id = existing_by_ip[0]
                cursor.execute('SELECT id FROM contacts WHERE id = ? LIMIT 1', (contact_id,))
                existing_target = cursor.fetchone()
                if existing_target and existing_target[0] != old_id:
                    cursor.execute('UPDATE messages SET contact_id = ? WHERE contact_id = ?', (contact_id, old_id))
                    cursor.execute('DELETE FROM contacts WHERE id = ?', (old_id,))
                else:
                    cursor.execute('UPDATE messages SET contact_id = ? WHERE contact_id = ?', (contact_id, old_id))
                    cursor.execute('UPDATE contacts SET id = ?, name = ?, last_ip = ?, port = ? WHERE id = ?', (contact_id, name, ip_address, port_value, old_id))
                    conn.commit()
                    conn.close()
                    return

            cursor.execute('''
                INSERT INTO contacts (id, name, last_ip, port)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    last_ip=excluded.last_ip,
                    port=excluded.port
            ''', (contact_id, name, ip_address, port_value))
        conn.close()

    def update_contact_name(self, contact_id: str, new_name: str):
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE contacts SET name = ? WHERE id = ?', (new_name, contact_id))
        conn.close()

    def get_contacts(self) -> list:
        """מחזיר את כל אנשי הקשר שנשמרו בתוכנה"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, last_ip, port FROM contacts')
        results = [{"id": row[0], "name": row[1], "ip": row[2], "port": row[3]} for row in cursor.fetchall()]
        conn.close()
        return results

    def save_message(self, contact_id: str, is_outgoing: bool, msg_type: str, content: str):
        """שומר הודעה, קישור או קובץ שהועברו בהיסטוריה. מחזיר את מזהה ההודעה."""
        conn = sqlite3.connect(self.db_path)
        message_id = None
        with conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO messages (contact_id, is_outgoing, msg_type, content, timestamp)
                VALUES (?, ?, ?, ?, datetime('now','localtime'))
            ''', (contact_id, is_outgoing, msg_type, content))
            message_id = cursor.lastrowid
        conn.close()
        return message_id

    def track_received_file(self, message_id: int, temp_path: str):
        """רושם קובץ/תיקייה שהתקבלו כ'לא נשמרו' (staging) עד שהמשתמש יבחר לשמור."""
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO received_files (message_id, temp_path, saved, final_path)
                VALUES (?, ?, 0, NULL)
                ON CONFLICT(message_id) DO UPDATE SET temp_path=excluded.temp_path
            ''', (message_id, temp_path))
        conn.close()

    def get_received_file(self, message_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, temp_path, saved, final_path FROM received_files WHERE message_id = ?', (message_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {"message_id": row[0], "temp_path": row[1], "saved": bool(row[2]), "final_path": row[3]}

    def mark_received_file_saved(self, message_id: int, final_path: str):
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE received_files SET saved = 1, final_path = ? WHERE message_id = ?', (final_path, message_id))
            cursor.execute('UPDATE messages SET content = ? WHERE id = ?', (final_path, message_id))
        conn.close()

    def get_unsaved_files(self):
        """מחזיר את כל הקבצים שהתקבלו ולא נשמרו — למחיקה בסגירת התוכנה."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, temp_path FROM received_files WHERE saved = 0')
        rows = [{"message_id": r[0], "temp_path": r[1]} for r in cursor.fetchall()]
        conn.close()
        return rows

    def delete_received_file_record(self, message_id: int):
        conn = sqlite3.connect(self.db_path)
        with conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM received_files WHERE message_id = ?', (message_id,))
        conn.close()

    def get_chat_history(self, contact_id: str) -> list:
        """שולף את כל היסטוריית השיחה עם מחשב ספציפי מסודר לפי תאריך"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.id, m.is_outgoing, m.msg_type, m.content, m.timestamp, rf.saved
            FROM messages m
            LEFT JOIN received_files rf ON rf.message_id = m.id
            WHERE m.contact_id = ?
            ORDER BY m.timestamp ASC
        ''', (contact_id,))

        history = []
        for row in cursor.fetchall():
            is_outgoing = row[1]
            msg_type = row[2]
            # קובץ/תיקייה נכנסים ניתנים לשמירה; saved=1 אם כבר נשמרו, None אם לא רלוונטי
            is_saveable = (not is_outgoing) and msg_type in ('file', 'folder')
            history.append({
                "id": row[0],
                "is_outgoing": is_outgoing,
                "type": msg_type,
                "content": row[3],
                "time": row[4],
                "saveable": is_saveable,
                "saved": bool(row[5]) if row[5] is not None else False,
            })
        conn.close()
        return history

# קוד לבדיקת המודול באופן עצמאי
if __name__ == "__main__":
    print("[Database] Initializing test...")
    test_db_file = "test_chat.db"
    
    # ניקוי קובץ קודם אם קיים במקרה
    if os.path.exists(test_db_file):
        try:
            os.remove(test_db_file)
        except:
            pass
        
    db = DatabaseManager(test_db_file)
    
    # בדיקה 1: הוספת מחשב חדש לרשימה
    print("\n[Database] Adding contact...")
    db.add_or_update_contact("PC-MAC-8899", "מחשב נייד - שלום", "192.168.137.5")
    contacts = db.get_contacts()
    print(f"Contacts list: {contacts}")
    
    # בדיקה 2: שמירת העברות
    print("\n[Database] Simulating file and link transfers...")
    db.save_message("PC-MAC-8899", is_outgoing=True, msg_type="link", content="https://github.com")
    db.save_message("PC-MAC-8899", is_outgoing=False, msg_type="file", content="C:/Downloads/invoice.pdf")
    
    # בדיקה 3: קריאת ההיסטוריה
    print("\n[Database] Chat History with PC-MAC-8899:")
    history = db.get_chat_history("PC-MAC-8899")
    for msg in history:
        direction = "Sent" if msg['is_outgoing'] else "Received"
        print(f"  [{msg['time']}] {direction} [{msg['type'].upper()}]: {msg['content']}")
        
    # מחיקת קובץ הבדיקה בסיום (כעת יעבוד ללא שגיאת WinError 32)
    if os.path.exists(test_db_file):
        os.remove(test_db_file)
        print("\n[Database] Test complete. Test database removed successfully.")