import os

import secrets

from datetime import datetime, timedelta

from flask import Flask, render_template_string, request, redirect, url_for, session, send_file, send_from_directory

from werkzeug.utils import secure_filename

from werkzeug.security import generate_password_hash, check_password_hash

import psycopg2

import psycopg2.extras

from psycopg2 import IntegrityError

import io

import zipfile

import uuid

import socket

import smtplib

import json

import urllib.request

import urllib.error

from collections import defaultdict

from email.message import EmailMessage

from supabase import create_client, Client

from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)

# مفتاح الجلسة يُقرأ من متغير بيئة SECRET_KEY (يجب ضبطه في إعدادات الاستضافة/Render).

# إذا لم يكن مضبوطًا، يتم توليد مفتاح عشوائي مؤقت في كل إقلاع - وهذا يعني إبطال كل الجلسات

# المفتوحة عند إعادة تشغيل السيرفر، لذا يُفضّل بشدة ضبط SECRET_KEY كمتغير بيئة دائم.

app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

# --- تفعيل التواصل اللحظي (WebSocket) للشات الداخلي بين الإدارات ---
# async_mode='threading' يعمل مع أي نسخة بايثون وأي إعداد استضافة عادي (لا يحتاج eventlet/gevent).
# يحتاج مكتبة simple-websocket (راجع ملاحظات التشغيل في نهاية الملف) لدعم WebSocket الحقيقي،
# وإلا يعمل تلقائياً عبر Long-Polling كبديل (أبطأ شوي لكنه يعمل بدون أي إعداد إضافي).
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- إعدادات الاتصال بقاعدة بيانات Supabase PostgreSQL ---

# يجب ضبط هذه القيم كمتغيرات بيئة في إعدادات الاستضافة (Render) - لا توجد قيم افتراضية

# مكتوبة بالكود لتفادي تسريب بيانات الاتصال الحساسة عند مشاركة الكود المصدري.

NEON_DATABASE_URL = os.environ.get('DATABASE_URL')

if not NEON_DATABASE_URL:

    raise RuntimeError("متغير البيئة DATABASE_URL غير مضبوط. الرجاء ضبطه في إعدادات الاستضافة قبل تشغيل التطبيق.")

# --- إعدادات تخزين الملفات الحقيقية على Supabase Storage ---

SUPABASE_URL = os.environ.get('SUPABASE_URL')

if not SUPABASE_URL:

    raise RuntimeError("متغير البيئة SUPABASE_URL غير مضبوط. الرجاء ضبطه في إعدادات الاستضافة قبل تشغيل التطبيق.")

SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')  # مفتاح service_role السري (وليس anon key)

if not SUPABASE_SERVICE_KEY:

    raise RuntimeError("متغير البيئة SUPABASE_SERVICE_KEY غير مضبوط. الرجاء ضبطه في إعدادات الاستضافة قبل تشغيل التطبيق.")

SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', 'archive-files')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

import re

def sanitize_folder_name(name):

    """

    يحوّل اسم/معرّف الإدارة إلى صيغة آمنة كاسم مجلد لـ Supabase Storage.

    Supabase Storage لا يقبل إلا حروف إنجليزية وأرقام ورموز محدودة في مسار الملف (key)،

    لذلك لا يمكن استخدام حروف عربية مباشرة هنا.

    """

    if not name:

        return "unknown"

    name = name.strip()

    name = re.sub(r'\s+', '_', name)

    name = re.sub(r'[^A-Za-z0-9_-]', '', name)

    return name or "unknown"

def upload_file_to_supabase(file_storage, subfolder='', dept_folder=None):

    """

    يرفع الملف فعلياً إلى Supabase Storage ويرجع:

    (الاسم الأصلي المعروض, المسار المخزن داخل الـ bucket, نوع الملف)

    التنظيم داخل الـ bucket: <معرّف_الإدارة_الآمن>/<نوع الملف>/اسم_الملف

    dept_folder يجب أن يكون قيمة إنجليزية آمنة (مثل username الإدارة)، وليس اسمها العربي.

    """

    original_name = secure_filename(file_storage.filename)

    unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{original_name}"

    path_parts = []

    if dept_folder:

        path_parts.append(sanitize_folder_name(dept_folder))

    if subfolder:

        path_parts.append(subfolder)

    path_parts.append(unique_name)

    storage_path = "/".join(path_parts)

    file_bytes = file_storage.read()

    mimetype = file_storage.content_type or 'application/octet-stream'

    supabase.storage.from_(SUPABASE_BUCKET).upload(

        path=storage_path,

        file=file_bytes,

        file_options={"content-type": mimetype}

    )

    return original_name, storage_path, mimetype

def send_supabase_file(file_name, storage_path, mimetype, as_attachment):

    """يجلب ملفاً حقيقياً من Supabase Storage عبر مساره ويرسله للمستخدم."""

    if not storage_path:

        return "الملف غير موجود", 404

    try:

        file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(storage_path)

    except Exception:

        return "الملف غير موجود على التخزين", 404

    return send_file(

        io.BytesIO(file_bytes),

        mimetype=mimetype or 'application/octet-stream',

        as_attachment=as_attachment,

        download_name=file_name or 'file'

    )

def delete_file_from_supabase(storage_path):

    """يحذف الملف الفعلي من Supabase Storage عند حذف السجل من القاعدة."""

    if not storage_path:

        return

    try:

        supabase.storage.from_(SUPABASE_BUCKET).remove([storage_path])

    except Exception:

        pass

ADMIN_ROLES = [

    'الرئيس التنفيذي', 'رئيس تنفيذي', 'CEO',

    'مدير تقنية المعلومات', 'مدير تقنية معلومات', 'تقنية المعلومات', 'IT Manager', 'IT'

]

def is_admin_user(dept_name):

    if not dept_name:

        return False

    dept_clean = dept_name.strip()

    return any(role.lower() == dept_clean.lower() for role in ADMIN_ROLES) or 'تقنية' in dept_clean or 'تنفيذي' in dept_clean

# --- استعادة كلمة المرور عبر البريد الإلكتروني (رمز تحقق OTP) ---

class _IPv4SMTP(smtplib.SMTP):

    """نفس smtplib.SMTP لكن تجبر الاتصال يمر عبر IPv4 فقط. بيئات استضافة سحابية كثيرة (منها Render)

    ما عندها مسار خروج IPv6 شغّال، بينما smtp.gmail.com له عنوان IPv6 (AAAA) أيضاً - فلو حاول

    بايثون يتصل عبره أول شي يطلع خطأ "Network is unreachable" حتى لو الإنترنت شغّال تمام.

    إجبار الاتصال على IPv4 يحل هذي المشكلة نهائياً."""

    def _get_socket(self, host, port, timeout):

        addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)

        ip = addr_info[0][4][0]

        return socket.create_connection((ip, port), timeout, self.source_address)

def _send_email_via_brevo(to_email, subject, body):

    """يرسل البريد عبر واجهة Brevo البرمجية (HTTPS - منفذ 443) بدل SMTP.

    السبب: Render يحظر منافذ SMTP الصادرة (25 / 465 / 587) على الخطط المجانية، فتفشل

    أي محاولة SMTP بخطأ "timed out" أو "Network is unreachable" مهما ضبطنا الكود.

    منفذ HTTPS (443) غير محظور أبداً، فهذا الحل يعمل بشكل مضمون بغض النظر عن خطة Render.

    يتطلب ضبط متغيرات البيئة التالية على Render:

      - BREVO_API_KEY     : المفتاح السري من حساب Brevo

      - BREVO_SENDER_EMAIL: البريد المُرسِل (يجب توثيقه/التحقق منه في حساب Brevo)

      - BREVO_SENDER_NAME : الاسم الذي يظهر كمرسل (اختياري)

    ترجع True لو نجح الإرسال، أو None لو لم تُضبط متغيرات Brevo أصلاً (لتجربة SMTP بعدها كبديل)."""

    api_key = os.environ.get('BREVO_API_KEY')

    if not api_key:

        return None

    sender_email = os.environ.get('BREVO_SENDER_EMAIL')

    sender_name = os.environ.get('BREVO_SENDER_NAME', 'نادي فيفا الرياضي')

    if not sender_email:

        print("[EMAIL] BREVO_API_KEY مضبوط لكن BREVO_SENDER_EMAIL غير مضبوط - لا يمكن الإرسال عبر Brevo")

        return None

    try:

        payload = json.dumps({

            'sender': {'email': sender_email, 'name': sender_name},

            'to': [{'email': to_email}],

            'subject': subject,

            'textContent': body,

        }).encode('utf-8')

        req = urllib.request.Request(

            'https://api.brevo.com/v3/smtp/email',

            data=payload,

            method='POST',

            headers={

                'api-key': api_key,

                'content-type': 'application/json',

                'accept': 'application/json',

            },

        )

        with urllib.request.urlopen(req, timeout=15) as resp:

            if resp.status in (200, 201):

                return True

            print(f"[EMAIL] فشل الإرسال عبر Brevo إلى {to_email} - رمز الحالة: {resp.status}")

            return False

    except urllib.error.HTTPError as e:

        details = e.read().decode('utf-8', errors='ignore')

        print(f"[EMAIL] فشل الإرسال عبر Brevo إلى {to_email} - رمز الحالة: {e.code} - التفاصيل: {details}")

        return False

    except Exception as e:

        print(f"[EMAIL] خطأ أثناء الإرسال عبر Brevo إلى {to_email}: {e}")

        return False

def _send_email_via_smtp(to_email, subject, body):

    """يرسل البريد عبر خادم SMTP مضبوط بمتغيرات البيئة (جيميل أو غيره). يُستخدم فقط كخطة بديلة

    إن لم تُضبط متغيرات Brevo، أو على خطط Render المدفوعة التي لا تحظر منافذ SMTP."""

    smtp_host = os.environ.get('SMTP_HOST')

    smtp_port = int(os.environ.get('SMTP_PORT', '587'))

    smtp_username = os.environ.get('SMTP_USERNAME')

    smtp_password = os.environ.get('SMTP_PASSWORD')

    from_email = os.environ.get('SMTP_FROM_EMAIL', smtp_username)

    if not smtp_host or not smtp_username or not smtp_password:

        print(f"[EMAIL] لم يُرسل البريد إلى {to_email} - لا توجد إعدادات Brevo ولا إعدادات SMTP في متغيرات البيئة. الموضوع: {subject} - المحتوى: {body}")

        return False

    try:

        msg = EmailMessage()

        msg['Subject'] = subject

        msg['From'] = from_email

        msg['To'] = to_email

        msg.set_content(body)

        with _IPv4SMTP(smtp_host, smtp_port, timeout=10) as server:

            server.starttls()

            server.login(smtp_username, smtp_password)

            server.send_message(msg)

        return True

    except Exception as e:

        print(f"[EMAIL] خطأ أثناء إرسال البريد عبر SMTP إلى {to_email}: {e}")

        return False

def send_email(to_email, subject, body):

    """نقطة الدخول الموحّدة لإرسال البريد: يحاول أولاً عبر Brevo (HTTPS - يعمل دائماً على Render

    حتى في الخطة المجانية)، فإن لم تكن إعدادات Brevo مضبوطة يرجع تلقائياً لطريقة SMTP القديمة."""

    result = _send_email_via_brevo(to_email, subject, body)

    if result is not None:

        return result

    return _send_email_via_smtp(to_email, subject, body)

def is_hashed_password(stored_password):

    return bool(stored_password) and (stored_password.startswith('pbkdf2:') or stored_password.startswith('scrypt:'))

def verify_password(stored_password, provided_password):

    """يتحقق من كلمة المرور - يدعم القيم المشفّرة الحديثة (hash) وأيضاً الحسابات القديمة

    التي ما زالت كلمة مرورها مخزّنة نص صريح من قبل تفعيل التشفير، لضمان عدم انقطاع دخولها."""

    if not stored_password:

        return False

    if is_hashed_password(stored_password):

        try:

            return check_password_hash(stored_password, provided_password)

        except Exception:

            return False

    return stored_password == provided_password

def get_allowed_receiver_depts(cursor, dept_id, can_send_all):

    """يرجع قائمة الإدارات المسموح لهذه الإدارة إرسال خطابات إليها.

    إذا can_send_all == 1 يرجع كل الإدارات، وإلا يرجع فقط الإدارات المحددة لها في send_permissions."""

    if can_send_all == 1:

        cursor.execute('SELECT id, name FROM departments WHERE id != %s ORDER BY name', (dept_id,))

        return cursor.fetchall()

    cursor.execute('''

        SELECT d.id, d.name FROM departments d

        JOIN send_permissions sp ON sp.allowed_dept_id = d.id

        WHERE sp.dept_id = %s

        ORDER BY d.name

    ''', (dept_id,))

    return cursor.fetchall()

def is_receiver_allowed(cursor, sender_id, receiver_id):

    """تحقّق من صلاحية الإدارة المرسلة بإرسال خطاب إلى الإدارة المستلمة المحددة (حماية من التلاعب بالطلب مباشرة)."""

    if not receiver_id:

        return False

    cursor.execute('SELECT can_send_all FROM departments WHERE id = %s', (sender_id,))

    row = cursor.fetchone()

    if not row or row['can_send_all'] == 1:

        return True

    cursor.execute('SELECT 1 FROM send_permissions WHERE dept_id = %s AND allowed_dept_id = %s', (sender_id, receiver_id))

    return cursor.fetchone() is not None

def get_db_connection():

    conn = psycopg2.connect(NEON_DATABASE_URL, sslmode='require')

    conn.cursor_factory = psycopg2.extras.RealDictCursor

    return conn

def peek_next_letter_number(cursor):

    cursor.execute('SELECT next_letter_number FROM system_settings ORDER BY id LIMIT 1')

    row = cursor.fetchone()

    return row['next_letter_number'] if row else 1

def consume_next_letter_number(cursor):

    number = peek_next_letter_number(cursor)

    cursor.execute('''

        UPDATE system_settings 

        SET next_letter_number = next_letter_number + 1 

        WHERE id = (SELECT id FROM system_settings ORDER BY id LIMIT 1)

    ''')

    return number

def init_db():

    conn = get_db_connection()

    cursor = conn.cursor()

    

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS departments (

            id SERIAL PRIMARY KEY,

            name TEXT UNIQUE NOT NULL,

            username TEXT UNIQUE NOT NULL,

            password TEXT NOT NULL,

            can_access_archive INTEGER DEFAULT 1,

            can_delete INTEGER DEFAULT 0,

            can_view_all_archive INTEGER DEFAULT 1,

            can_view_all_achievements INTEGER DEFAULT 0,

            can_add_user INTEGER DEFAULT 1,

            can_page_inbox INTEGER DEFAULT 1,

            can_page_outbox INTEGER DEFAULT 1,

            can_page_achievements INTEGER DEFAULT 1,

            can_page_archive INTEGER DEFAULT 1,

            can_page_quick_upload INTEGER DEFAULT 1,

            can_page_suggestions INTEGER DEFAULT 1,
            can_page_chat INTEGER DEFAULT 1


        )

    ''')

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS letters (

            id SERIAL PRIMARY KEY,

            title TEXT NOT NULL,

            content TEXT,

            priority TEXT DEFAULT 'عادي',

            sender_id INTEGER,

            receiver_id INTEGER,

            file_name TEXT,

            file_path TEXT,

            file_data BYTEA,

            file_mimetype TEXT,

            created_at TEXT,

            archive_dept_id INTEGER,

            letter_number TEXT

        )

    ''')

    

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS monthly_achievements (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER,

            title TEXT,

            file_name TEXT,

            file_path TEXT,

            file_data BYTEA,

            file_mimetype TEXT,

            month_year TEXT,

            uploaded_at TEXT

        )

    ''')

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS course_certificates (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER,

            title TEXT,

            file_name TEXT,

            file_data BYTEA,

            file_mimetype TEXT,

            uploaded_at TEXT

        )

        

    ''')

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS shawahid (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER,

            title TEXT,

            file_name TEXT,

            file_path TEXT,

            file_mimetype TEXT,

            uploaded_at TEXT

        )

    ''')

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS suggestions (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER,

            dept_name TEXT,

            message TEXT NOT NULL,

            created_at TEXT,

            is_read INTEGER DEFAULT 0

        )

    ''')

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS system_settings (

            id SERIAL PRIMARY KEY,

            next_letter_number INTEGER NOT NULL DEFAULT 1

        )

    ''')

    cursor.execute('SELECT COUNT(*) as count FROM system_settings')

    if cursor.fetchone()['count'] == 0:

        cursor.execute('INSERT INTO system_settings (next_letter_number) VALUES (1)')

    

    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='departments'")

    dept_columns = [col['column_name'] for col in cursor.fetchall()]

    if 'can_view_all_archive' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_view_all_archive INTEGER DEFAULT 1')

    if 'can_view_all_achievements' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_view_all_achievements INTEGER DEFAULT 0')

    if 'can_add_user' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_add_user INTEGER DEFAULT 1')

        

    if 'can_page_inbox' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_inbox INTEGER DEFAULT 1')

    if 'can_page_outbox' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_outbox INTEGER DEFAULT 1')

    if 'can_page_achievements' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_achievements INTEGER DEFAULT 1')

    if 'can_page_archive' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_archive INTEGER DEFAULT 1')

    if 'can_page_quick_upload' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_quick_upload INTEGER DEFAULT 1')

    if 'can_page_suggestions' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_suggestions INTEGER DEFAULT 1')

    if 'can_page_chat' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_chat INTEGER DEFAULT 1')

    if 'failed_login_attempts' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN failed_login_attempts INTEGER DEFAULT 0')

    if 'is_locked' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN is_locked INTEGER DEFAULT 0')

    if 'can_send_all' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN can_send_all INTEGER DEFAULT 1')

    if 'can_page_admin_tools' not in dept_columns:

        # صلاحية تُمنح صراحة من صفحة إدارة الصلاحيات: تتيح لأي إدارة (غير المسؤولين الافتراضيين)

        # رؤية "لوحة التحكم الشاملة" و"إدارة الصلاحيات" - افتراضياً 0 حتى لا يظهرا لأحد تلقائياً

        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_admin_tools INTEGER DEFAULT 0')

    if 'signature_data' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN signature_data TEXT')

    if 'email' not in dept_columns:

        # البريد الإلكتروني للإدارة - يضيفه المسؤول فقط من صفحة إدارة الصلاحيات، يُستخدم لاستعادة كلمة المرور

        cursor.execute('ALTER TABLE departments ADD COLUMN email TEXT')

    if 'reset_otp_code' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN reset_otp_code TEXT')

    if 'reset_otp_expires_at' not in dept_columns:

        cursor.execute('ALTER TABLE departments ADD COLUMN reset_otp_expires_at TIMESTAMP')

    # --- جدول صلاحيات الإرسال المقيّد: أي إدارة مسموح لها الإرسال لأي إدارات أخرى عند can_send_all = 0 ---

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS send_permissions (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER NOT NULL,

            allowed_dept_id INTEGER NOT NULL

        )

    ''')

    # --- جدول التواقيع الإلكترونية المحفوظة (حتى 3 توقيعات لكل إدارة، بدل توقيع واحد فقط) ---

    cursor.execute('''

        CREATE TABLE IF NOT EXISTS saved_signatures (

            id SERIAL PRIMARY KEY,

            dept_id INTEGER NOT NULL,

            signature_data TEXT NOT NULL,

            created_at TEXT

        )

    ''')

    # ترحيل تلقائي مرة واحدة: أي إدارة عندها توقيع محفوظ بالنظام القديم (عمود departments.signature_data)

    # ولسه ما نُقل لجدول التواقيع الجديد، ننقله كأول توقيع محفوظ لها حتى لا تضيع تواقيع محفوظة سابقاً.

    cursor.execute('''

        SELECT id, signature_data FROM departments

        WHERE signature_data IS NOT NULL

          AND id NOT IN (SELECT DISTINCT dept_id FROM saved_signatures)

    ''')

    legacy_signature_depts = cursor.fetchall()

    for legacy_dept in legacy_signature_depts:

        cursor.execute(

            'INSERT INTO saved_signatures (dept_id, signature_data, created_at) VALUES (%s, %s, %s)',

            (legacy_dept['id'], legacy_dept['signature_data'], datetime.now().strftime('%Y-%m-%d %H:%M'))

        )

    # --- ترحيل جداول الملفات لدعم مسار Supabase Storage (file_path) ---

    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='course_certificates'")

    cert_columns = [col['column_name'] for col in cursor.fetchall()]

    if 'file_path' not in cert_columns:

        cursor.execute('ALTER TABLE course_certificates ADD COLUMN file_path TEXT')

    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='letters'")

    letters_columns = [col['column_name'] for col in cursor.fetchall()]

    if 'is_read' not in letters_columns:

        cursor.execute('ALTER TABLE letters ADD COLUMN is_read INTEGER DEFAULT 0')

        cursor.execute('UPDATE letters SET is_read = 1')

    # --- جداول الشات الداخلي السريع بين الإدارات (محادثات فردية وجماعية) ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'direct',
            name TEXT,
            created_by INTEGER,
            created_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_participants (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            dept_id INTEGER NOT NULL,
            last_read_message_id INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            joined_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL,
            content TEXT,
            file_name TEXT,
            file_path TEXT,
            file_mimetype TEXT,
            created_at TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_participants_dept ON chat_participants(dept_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_participants_conv ON chat_participants(conversation_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages(conversation_id, id)')
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='chat_participants'")
    chat_participants_columns = [col['column_name'] for col in cursor.fetchall()]
    if 'is_deleted' not in chat_participants_columns:
        cursor.execute('ALTER TABLE chat_participants ADD COLUMN is_deleted INTEGER DEFAULT 0')


    conn.commit()

    cursor.close()

    conn.close()

init_db()

# --- مسارات التحميل والمعاينة لكل ملفات النظام (تُقرأ الآن من Supabase Storage) ---

@app.route('/download_letter_file/<int:letter_id>')

def download_letter_file(letter_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM letters WHERE id = %s', (letter_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], True)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=True,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

@app.route('/download_archive_zip')

def download_archive_zip():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    dept_id = session['dept_id']

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (dept_id,))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    scope = request.args.get('scope', 'own')  # own = أرشيفي فقط | all = كل الأرشيف

    if scope == 'all' and current_dept['can_view_all_archive'] == 1:

        cursor.execute('''

            SELECT * FROM letters 

            WHERE (sender_id = receiver_id AND sender_id IS NOT NULL) OR (sender_id IS NULL AND receiver_id IS NULL)

        ''')

    else:

        cursor.execute('''

            SELECT * FROM letters 

            WHERE (sender_id = receiver_id AND sender_id = %s) OR (sender_id IS NULL AND receiver_id IS NULL AND archive_dept_id = %s)

        ''', (dept_id, dept_id))

    rows = cursor.fetchall()

    cursor.close()

    conn.close()

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

        used_names = set()

        for row in rows:

            file_bytes = None

            if row.get('file_path'):

                try:

                    file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(row['file_path'])

                except Exception:

                    file_bytes = None

            elif row.get('file_data'):

                file_bytes = bytes(row['file_data'])

            if file_bytes:

                base_name = row.get('file_name') or f"file_{row['id']}"

                name = base_name

                counter = 1

                while name in used_names:

                    name = f"{counter}_{base_name}"

                    counter += 1

                used_names.add(name)

                zip_file.writestr(name, file_bytes)

    if len(used_names) == 0:

        return '''<script>alert("لا توجد ملفات لتحميلها."); window.history.back();</script>'''

    zip_buffer.seek(0)

    return send_file(

        zip_buffer,

        mimetype='application/zip',

        as_attachment=True,

        download_name=f"archive_{scope}_{dept_id}.zip"

    )

    

@app.route('/view_letter_file/<int:letter_id>')

def view_letter_file(letter_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM letters WHERE id = %s', (letter_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], False)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=False,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

@app.route('/download_ach_file/<int:ach_id>')

def download_ach_file(ach_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM monthly_achievements WHERE id = %s', (ach_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], True)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=True,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

@app.route('/download_all_achievements/<int:dept_id>')

def download_all_achievements(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    rows = cursor.fetchall()

    cursor.close()

    conn.close()

    zip_buffer = io.BytesIO()

    used_names = set()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

        for row in rows:

            file_bytes = None

            if row.get('file_path'):

                try:

                    file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(row['file_path'])

                except Exception:

                    file_bytes = None

            elif row.get('file_data'):

                file_bytes = bytes(row['file_data'])

            if file_bytes:

                base_name = row.get('file_name') or 'file'

                name = base_name

                counter = 1

                while name in used_names:

                    name = f"{counter}_{base_name}"

                    counter += 1

                used_names.add(name)

                zip_file.writestr(name, file_bytes)

    if len(used_names) == 0:

        return '''<script>alert("لا توجد ملفات إنجازات لتحميلها."); window.history.back();</script>'''

    zip_buffer.seek(0)

    return send_file(

        zip_buffer,

        mimetype='application/zip',

        as_attachment=True,

        download_name=f"achievements_dept_{dept_id}.zip"

    )   

@app.route('/view_ach_file/<int:ach_id>')

def view_ach_file(ach_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM monthly_achievements WHERE id = %s', (ach_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], False)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=False,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

@app.route('/download_cert_file/<int:cert_id>')

def download_cert_file(cert_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM course_certificates WHERE id = %s', (cert_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], True)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=True,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

@app.route('/download_all_certificates/<int:dept_id>')

def download_all_certificates(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data FROM course_certificates WHERE dept_id = %s', (dept_id,))

    rows = cursor.fetchall()

    cursor.close()

    conn.close()

    zip_buffer = io.BytesIO()

    used_names = set()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

        for row in rows:

            file_bytes = None

            if row.get('file_path'):

                try:

                    file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(row['file_path'])

                except Exception:

                    file_bytes = None

            elif row.get('file_data'):

                file_bytes = bytes(row['file_data'])

            if file_bytes:

                base_name = row.get('file_name') or 'file'

                name = base_name

                counter = 1

                while name in used_names:

                    name = f"{counter}_{base_name}"

                    counter += 1

                used_names.add(name)

                zip_file.writestr(name, file_bytes)

    if len(used_names) == 0:

        return '''<script>alert("لا توجد شهادات دورات لتحميلها."); window.history.back();</script>'''

    zip_buffer.seek(0)

    return send_file(

        zip_buffer,

        mimetype='application/zip',

        as_attachment=True,

        download_name=f"certificates_dept_{dept_id}.zip"

    )

    

@app.route('/view_cert_file/<int:cert_id>')

def view_cert_file(cert_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_data, file_mimetype FROM course_certificates WHERE id = %s', (cert_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], False)

    if row and row.get('file_data'):

        return send_file(

            io.BytesIO(row['file_data']),

            mimetype=row['file_mimetype'] or 'application/octet-stream',

            as_attachment=False,

            download_name=row['file_name'] or 'file'

        )

    return "الملف غير موجود", 404

# --- مسارات قسم شواهد (مطابقة لقسم شهادات الدورات) ---

@app.route('/download_shahid_file/<int:shahid_id>')

def download_shahid_file(shahid_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_mimetype FROM shawahid WHERE id = %s', (shahid_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], True)

    return "الملف غير موجود", 404

@app.route('/view_shahid_file/<int:shahid_id>')

def view_shahid_file(shahid_id):

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path, file_mimetype FROM shawahid WHERE id = %s', (shahid_id,))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    if row and row.get('file_path'):

        return send_supabase_file(row['file_name'], row['file_path'], row['file_mimetype'], False)

    return "الملف غير موجود", 404

@app.route('/download_all_shawahid/<int:dept_id>')

def download_all_shawahid(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT file_name, file_path FROM shawahid WHERE dept_id = %s', (dept_id,))

    rows = cursor.fetchall()

    cursor.close()

    conn.close()

    zip_buffer = io.BytesIO()

    used_names = set()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

        for row in rows:

            file_bytes = None

            if row.get('file_path'):

                try:

                    file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(row['file_path'])

                except Exception:

                    file_bytes = None

            if file_bytes:

                base_name = row.get('file_name') or 'file'

                name = base_name

                counter = 1

                while name in used_names:

                    name = f"{counter}_{base_name}"

                    counter += 1

                used_names.add(name)

                zip_file.writestr(name, file_bytes)

    if len(used_names) == 0:

        return '''<script>alert("لا توجد شواهد لتحميلها."); window.history.back();</script>'''

    zip_buffer.seek(0)

    return send_file(

        zip_buffer,

        mimetype='application/zip',

        as_attachment=True,

        download_name=f"shawahid_dept_{dept_id}.zip"

    )

@app.route('/upload_shahid', methods=['POST'])

def upload_shahid():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    dept_id = request.form.get('dept_id')

    title = request.form.get('title')

    files = request.files.getlist('file')

    is_admin = is_admin_user(session.get('dept_name'))

    if str(session['dept_id']) != str(dept_id) and not is_admin:

        return '''<script>alert("غير مسموح لك برفع شواهد لهذه الإدارة."); window.location.href="/monthly_achievements";</script>'''

    valid_files = [f for f in files if f and f.filename != '']

    if valid_files:

        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute('SELECT username FROM departments WHERE id = %s', (dept_id,))

        dept_row = cursor.fetchone()

        dept_folder = dept_row['username'] if dept_row else f"dept_{dept_id}"

        for file in valid_files:

            original_name, storage_path, file_mimetype = upload_file_to_supabase(file, subfolder='shawahid', dept_folder=dept_folder)

            file_title = f"{title} - {original_name}" if len(valid_files) > 1 else title

            cursor.execute('''

                INSERT INTO shawahid (dept_id, title, file_name, file_path, file_mimetype, uploaded_at)

                VALUES (%s, %s, %s, %s, %s, %s)

            ''', (dept_id, file_title, original_name, storage_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M')))

        conn.commit()

        cursor.close()

        conn.close()

    return redirect(url_for('monthly_achievements'))

@app.route('/delete_shahid/<int:shahid_id>')

def delete_shahid(shahid_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM shawahid WHERE id = %s', (shahid_id,))

    file_row = cursor.fetchone()

    cursor.execute('DELETE FROM shawahid WHERE id = %s', (shahid_id,))

    conn.commit()

    cursor.close()

    conn.close()

    if file_row and file_row.get('file_path'):

        delete_file_from_supabase(file_row['file_path'])

    return '''<script>alert("تم حذف الشاهد بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_all_shawahid/<int:dept_id>')

def delete_all_shawahid(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM shawahid WHERE dept_id = %s', (dept_id,))

    file_rows = cursor.fetchall()

    cursor.execute('DELETE FROM shawahid WHERE dept_id = %s', (dept_id,))

    conn.commit()

    cursor.close()

    conn.close()

    for fr in file_rows:

        if fr.get('file_path'):

            delete_file_from_supabase(fr['file_path'])

    return '''<script>alert("تم حذف كل الشواهد لهذه الإدارة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_selected_shawahid', methods=['POST'])

def delete_selected_shawahid():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    item_ids_raw = request.form.getlist('item_ids')

    item_ids = [int(i) for i in item_ids_raw if i.isdigit()]

    if item_ids:

        cursor.execute('SELECT file_path FROM shawahid WHERE id = ANY(%s)', (item_ids,))

        file_rows = cursor.fetchall()

        cursor.execute('DELETE FROM shawahid WHERE id = ANY(%s)', (item_ids,))

        conn.commit()

        for fr in file_rows:

            if fr.get('file_path'):

                delete_file_from_supabase(fr['file_path'])

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف الشواهد المحددة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_letter/<int:letter_id>')

def delete_letter(letter_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.history.back();</script>'''

    cursor.execute('SELECT file_path FROM letters WHERE id = %s', (letter_id,))

    file_row = cursor.fetchone()

    cursor.execute('DELETE FROM letters WHERE id = %s', (letter_id,))

    conn.commit()

    cursor.close()

    conn.close()

    if file_row and file_row.get('file_path'):

        delete_file_from_supabase(file_row['file_path'])

    return '''<script>alert("تم الحذف بنجاح"); window.history.back();</script>'''

@app.route('/delete_selected_letters', methods=['POST'])

def delete_selected_letters():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.history.back();</script>'''

        

    letter_ids_raw = request.form.getlist('letter_ids')

    action_type = request.form.get('action_type')

    dept_id = session['dept_id']

    if action_type == 'all':

        if current_dept['can_view_all_archive'] == 1:

            cursor.execute('''

                DELETE FROM letters 

                WHERE (sender_id = receiver_id AND sender_id IS NOT NULL) OR (sender_id IS NULL AND receiver_id IS NULL)

            ''')

        else:

            cursor.execute('''

                DELETE FROM letters 

                WHERE (sender_id = receiver_id AND sender_id = %s) OR (sender_id IS NULL AND receiver_id IS NULL AND archive_dept_id = %s)

            ''', (dept_id, dept_id))

    elif letter_ids_raw:

        letter_ids = [int(lid) for lid in letter_ids_raw if lid.isdigit()]

        if letter_ids:

            cursor.execute('DELETE FROM letters WHERE id = ANY(%s)', (letter_ids,))

        

    conn.commit()

    cursor.close()

    conn.close()

    return '''<script>alert("تمت عملية الحذف بنجاح!"); window.location.href="/archive";</script>'''

@app.route('/upload_achievement', methods=['POST'])

def upload_achievement():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    dept_id = request.form.get('dept_id')

    title = request.form.get('title')

    files = request.files.getlist('file')

    

    is_admin = is_admin_user(session.get('dept_name'))

    if str(session['dept_id']) != str(dept_id) and not is_admin:

        return '''<script>alert("غير مسموح لك برفع إنجازات لهذه الإدارة."); window.location.href="/monthly_achievements";</script>'''

    

    valid_files = [f for f in files if f and f.filename != '']

    if valid_files:

        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute('SELECT username FROM departments WHERE id = %s', (dept_id,))

        dept_row = cursor.fetchone()

        dept_folder = dept_row['username'] if dept_row else f"dept_{dept_id}"

        for file in valid_files:

            original_name, storage_path, file_mimetype = upload_file_to_supabase(file, subfolder='achievements', dept_folder=dept_folder)

            file_title = f"{title} - {original_name}" if len(valid_files) > 1 else title

            cursor.execute('''

                INSERT INTO monthly_achievements (dept_id, title, file_name, file_path, file_mimetype, uploaded_at)

                VALUES (%s, %s, %s, %s, %s, %s)

            ''', (dept_id, file_title, original_name, storage_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M')))

        conn.commit()

        cursor.close()

        conn.close()

        

    return redirect(url_for('monthly_achievements'))

@app.route('/upload_certificate', methods=['POST'])

def upload_certificate():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    dept_id = request.form.get('dept_id')

    title = request.form.get('title')

    files = request.files.getlist('file')

    

    is_admin = is_admin_user(session.get('dept_name'))

    if str(session['dept_id']) != str(dept_id) and not is_admin:

        return '''<script>alert("غير مسموح لك برفع شهادات دورات لهذه الإدارة."); window.location.href="/monthly_achievements";</script>'''

    

    valid_files = [f for f in files if f and f.filename != '']

    if valid_files:

        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute('SELECT username FROM departments WHERE id = %s', (dept_id,))

        dept_row = cursor.fetchone()

        dept_folder = dept_row['username'] if dept_row else f"dept_{dept_id}"

        for file in valid_files:

            original_name, storage_path, file_mimetype = upload_file_to_supabase(file, subfolder='certificates', dept_folder=dept_folder)

            file_title = f"{title} - {original_name}" if len(valid_files) > 1 else title

            cursor.execute('''

                INSERT INTO course_certificates (dept_id, title, file_name, file_path, file_mimetype, uploaded_at)

                VALUES (%s, %s, %s, %s, %s, %s)

            ''', (dept_id, file_title, original_name, storage_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M')))

        conn.commit()

        cursor.close()

        conn.close()

        

    return redirect(url_for('monthly_achievements'))

@app.route('/admin/clear_monthly_files/<int:dept_id>')

def clear_monthly_files(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if str(session['dept_id']) != str(dept_id) and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("غير مسموح لك بتفريغ ملفات هذه الإدارة."); window.location.href="/monthly_achievements";</script>'''

    

    cursor.execute('SELECT * FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    achievements = cursor.fetchall()

    

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M')

    for ach in achievements:

        cursor.execute('''

            INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, archive_dept_id)

            VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s)

        ''', (

            f"أرشيف إنجازات شهرية: {ach['title']}",

            f"تمت الأرشفة التلقائية من إنجازات الشهر بتاريخ: {current_time}",

            "عادي",

            ach['file_name'],

            ach.get('file_path'),

            ach.get('file_mimetype'),

            current_time,

            dept_id

        ))

    cursor.execute('SELECT * FROM course_certificates WHERE dept_id = %s', (dept_id,))

    certs = cursor.fetchall()

    for cert in certs:

        cursor.execute('''

            INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, archive_dept_id)

            VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s)

        ''', (

            f"أرشيف شهادات دورات: {cert['title']}",

            f"تمت الأرشفة التلقائية من قسم شهادات الدورات بتاريخ: {current_time}",

            "عادي",

            cert['file_name'],

            cert.get('file_path'),

            cert.get('file_mimetype'),

            current_time,

            dept_id

        ))

    cursor.execute('SELECT * FROM shawahid WHERE dept_id = %s', (dept_id,))

    shawahid_rows = cursor.fetchall()

    for sh in shawahid_rows:

        cursor.execute('''

            INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, archive_dept_id)

            VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s)

        ''', (

            f"أرشيف شواهد: {sh['title']}",

            f"تمت الأرشفة التلقائية من قسم شواهد بتاريخ: {current_time}",

            "عادي",

            sh['file_name'],

            sh.get('file_path'),

            sh.get('file_mimetype'),

            current_time,

            dept_id

        ))

    

    cursor.execute('DELETE FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    cursor.execute('DELETE FROM course_certificates WHERE dept_id = %s', (dept_id,))

    cursor.execute('DELETE FROM shawahid WHERE dept_id = %s', (dept_id,))

    conn.commit()

    cursor.close()

    conn.close()

    

    return '''<script>alert("تم تفريغ وأرشفة الإنجازات وشهادات الدورات للإدارة بنجاح إلى أرشيفها الخاص!"); window.location.href="/monthly_achievements";</script>'''

@app.route('/forgot_password/request_code', methods=['POST'])

def forgot_password_request_code():

    """الخطوة 1 من استعادة كلمة المرور: يبحث عن الإدارة بالبريد الإلكتروني ويرسل لها رمز تحقق.

    يرجع دائماً رسالة عامة (سواء البريد مسجل أو لا) حتى لا يتمكن أحد من تجربة إيميلات عشوائية

    لمعرفة أي البريد مسجل فعلياً في النظام."""

    email_raw = request.form.get('email', '').strip().lower()

    generic_message = 'إذا كان البريد مسجلاً لدى إحدى الإدارات، وصلك رمز تحقق عبر البريد الإلكتروني خلال لحظات.'

    if not email_raw:

        return {'success': False, 'message': 'الرجاء إدخال البريد الإلكتروني.'}

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT id, username FROM departments WHERE LOWER(email) = %s', (email_raw,))

    dept = cursor.fetchone()

    if dept:

        code = ''.join(secrets.choice('0123456789') for _ in range(6))

        expires_at = datetime.now() + timedelta(minutes=10)

        cursor.execute(

            'UPDATE departments SET reset_otp_code = %s, reset_otp_expires_at = %s WHERE id = %s',

            (generate_password_hash(code), expires_at, dept['id'])

        )

        conn.commit()

        email_subject = "رمز التحقق لإعادة تعيين كلمة المرور - نادي فيفا الرياضي"

        email_body = f"رمز التحقق لإعادة تعيين كلمة المرور هو: {code}\nصالح لمدة 10 دقائق فقط.\n\nاسم المستخدم الخاص بك: {dept['username']}\n\nإذا لم تطلب هذا الرمز، تجاهل هذه الرسالة."

        send_email(email_raw, email_subject, email_body)

    cursor.close()

    conn.close()

    return {'success': True, 'message': generic_message}

@app.route('/forgot_password/reset', methods=['POST'])

def forgot_password_reset():

    """الخطوة 2: يتحقق من رمز التحقق ثم يستبدل كلمة المرور بالجديدة."""

    email_raw = request.form.get('email', '').strip().lower()

    code = request.form.get('otp_code', '').strip()

    new_password = request.form.get('new_password', '')

    confirm_password = request.form.get('confirm_password', '')

    if not email_raw or not code or not new_password or not confirm_password:

        return {'success': False, 'message': 'الرجاء تعبئة جميع الحقول.'}

    if new_password != confirm_password:

        return {'success': False, 'message': 'كلمتا المرور غير متطابقتين.'}

    if len(new_password) < 6:

        return {'success': False, 'message': 'كلمة المرور يجب ألا تقل عن 6 خانات.'}

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute(

        'SELECT id, reset_otp_code, reset_otp_expires_at FROM departments WHERE LOWER(email) = %s',

        (email_raw,)

    )

    dept = cursor.fetchone()

    if not dept or not dept.get('reset_otp_code') or not dept.get('reset_otp_expires_at'):

        cursor.close()

        conn.close()

        return {'success': False, 'message': 'الرجاء طلب رمز تحقق جديد أولاً.'}

    if datetime.now() > dept['reset_otp_expires_at']:

        cursor.close()

        conn.close()

        return {'success': False, 'message': 'انتهت صلاحية رمز التحقق، الرجاء طلب رمز جديد.'}

    if not check_password_hash(dept['reset_otp_code'], code):

        cursor.close()

        conn.close()

        return {'success': False, 'message': 'رمز التحقق غير صحيح.'}

    cursor.execute(

        '''UPDATE departments SET password = %s, reset_otp_code = NULL, reset_otp_expires_at = NULL,

           is_locked = 0, failed_login_attempts = 0 WHERE id = %s''',

        (generate_password_hash(new_password), dept['id'])

    )

    conn.commit()

    cursor.close()

    conn.close()

    return {'success': True, 'message': 'تم تغيير كلمة المرور بنجاح، يمكنك تسجيل الدخول الآن بكلمة المرور الجديدة.'}

@app.route('/', methods=['GET', 'POST'])

def login():

    if request.method == 'POST':

        username = request.form['username']

        password = request.form['password']

        conn = get_db_connection()

        cursor = conn.cursor()

        cursor.execute('SELECT * FROM departments WHERE username = %s', (username,))

        dept = cursor.fetchone()

        # الحساب مقفل مسبقاً بسبب محاولات دخول خاطئة

        if dept and dept.get('is_locked') == 1:

            cursor.close()

            conn.close()

            return '''<script>alert("تم قفل هذا الحساب بسبب تجاوز عدد محاولات الدخول الخاطئة. الرجاء التواصل مع مدير تقنية المعلومات لإعادة فتح الحساب."); window.location.href="/";</script>'''

        if dept and verify_password(dept['password'], password):

            # ترحيل تلقائي: إذا كانت كلمة المرور ما زالت مخزّنة نص صريح (حساب قديم)، شفّرها الآن فوراً

            if not is_hashed_password(dept['password']):

                cursor.execute('UPDATE departments SET password = %s WHERE id = %s', (generate_password_hash(password), dept['id']))

            cursor.execute('UPDATE departments SET failed_login_attempts = 0 WHERE id = %s', (dept['id'],))

            conn.commit()

            cursor.close()

            conn.close()

            session['dept_id'] = dept['id']

            session['dept_name'] = dept['name']

            session['dept_username'] = dept['username']

            is_admin = is_admin_user(dept['name'])

            if dept.get('can_page_inbox') == 1 or is_admin:

                return redirect(url_for('dashboard'))

            elif dept.get('can_page_outbox') == 1 or is_admin:

                return redirect(url_for('outbox'))

            elif dept.get('can_page_achievements') == 1 or is_admin:

                return redirect(url_for('monthly_achievements'))

            elif dept.get('can_page_archive') == 1 or is_admin:

                return redirect(url_for('archive'))

            elif dept.get('can_page_quick_upload') == 1 or is_admin:

                return redirect(url_for('quick_upload'))

            else:

                session.clear()

                return '''<script>alert("عذراً، لا تملك صلاحية الوصول لأي صفحة في النظام."); window.location.href="/";</script>'''

        else:

            if dept:

                attempts = (dept.get('failed_login_attempts') or 0) + 1

                if attempts >= 5:

                    cursor.execute('UPDATE departments SET failed_login_attempts = %s, is_locked = 1 WHERE id = %s', (attempts, dept['id']))

                    conn.commit()

                    cursor.close()

                    conn.close()

                    return '''<script>alert("تم قفل هذا الحساب بسبب تجاوز عدد محاولات الدخول المسموح بها (5 محاولات). الرجاء التواصل مع مدير تقنية المعلومات لإعادة فتح الحساب."); window.location.href="/";</script>'''

                else:

                    cursor.execute('UPDATE departments SET failed_login_attempts = %s WHERE id = %s', (attempts, dept['id']))

                    conn.commit()

                    remaining = 5 - attempts

                    cursor.close()

                    conn.close()

                    return f'''<script>alert("خطأ في اسم المستخدم أو كلمة المرور. المحاولات المتبقية قبل قفل الحساب: {remaining}"); window.location.href="/";</script>'''

            else:

                cursor.close()

                conn.close()

                return '''<script>alert("خطأ في اسم المستخدم أو كلمة المرور"); window.location.href="/";</script>'''

            

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>تسجيل الدخول - نظام أرشفة نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green: #123826; --fifa-green-hover: #1e563b; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; }

            body { font-family: 'Almarai', sans-serif; background: linear-gradient(135deg, #eaf3ec 0%, #d5e2d8 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; margin: 0; padding: 15px; }

            .login-card { background: rgba(255, 255, 255, 0.96); backdrop-filter: blur(10px); border-radius: 20px; border: 1px solid rgba(197, 160, 89, 0.2); box-shadow: 0 15px 35px rgba(18, 56, 38, 0.12); width: 100%; max-width: 440px; padding: 2rem 1.5rem; position: relative; overflow: hidden; }

            .login-card::before { content: ''; position: absolute; top: 0; right: 0; left: 0; height: 6px; background: linear-gradient(90deg, var(--fifa-green), var(--fifa-gold)); }

            .brand-logo-box { margin: 0 auto 1rem auto; text-align: center; }

            .brand-logo-box img { max-height: 85px; width: auto; object-fit: contain; }

            .custom-input-wrapper { position: relative; }

            .input-group-icon { position: absolute; top: 50%; right: 15px; transform: translateY(-50%); z-index: 10; color: var(--fifa-gold); font-size: 1.2rem; }

            .btn-fifa { background-color: var(--fifa-green); color: #ffffff; border-radius: 10px; padding: 0.8rem; font-weight: 700; border: none; width: 100%; transition: all 0.3s ease; }

            .btn-fifa:hover { background-color: var(--fifa-green-hover); color: #ffffff; }

            .login-footer { margin-top: 1.5rem; border-top: 1px solid #edf2f0; padding-top: 0.8rem; font-size: 0.8rem; color: #7c8a84; }

        </style>

    </head>

    <body>

        <button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn" style="position:fixed; top:15px; left:15px; z-index:2000;">

            <i class='bx bxs-moon' id="themeToggleIcon"></i>

        </button>

        <div class="login-card text-center">

            <div class="brand-logo-box">

                <img src="{{ url_for('static', filename='logo1.png') }}" alt="شعار نادي فيفا" onerror="this.style.display='none'; document.getElementById('alt-icon').style.display='inline-block';">

                <i id="alt-icon" class='bx bxs-shield-alt-2' style="display:none; font-size: 3.5rem; color: var(--fifa-green);"></i>

            </div>

            <h4 class="fw-bold mb-1" style="color: var(--fifa-green);">نادي فيفا الرياضي</h4>

            <p class="text-muted fs-7 mb-4">نظام الأرشفة والخطابات الإلكتروني</p>

            

            <form action="/" method="post" id="loginForm">

                <div class="mb-3 text-start">

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">اسم المستخدم</label>

                    <div class="custom-input-wrapper">

                        <i class='bx bxs-user input-group-icon'></i>

                        <input type="text" name="username" class="form-control" style="padding-right: 42px;" placeholder="أدخل اسم المستخدم" required>

                    </div>

                </div>

                <div class="mb-4 text-start">

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">كلمة المرور</label>

                    <div class="custom-input-wrapper">

                        <i class='bx bxs-lock-alt input-group-icon'></i>

                        <input type="password" name="password" class="form-control" style="padding-right: 42px;" placeholder="أدخل كلمة المرور" required>

                    </div>

                </div>

                <button type="submit" class="btn btn-fifa mb-2">

                    <i class='bx bx-log-in-circle ms-1 fs-5 align-middle'></i> تسجيل الدخول

                </button>

            </form>

            <a href="#" id="forgotPasswordLink" onclick="return showForgotPassword();" class="fs-8 text-muted d-block mt-2">نسيت كلمة المرور أو اسم المستخدم؟</a>

            <div id="forgotPasswordPanel" class="d-none text-start mt-3">

                <div id="fpStep1">

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">البريد الإلكتروني المسجل لدى إدارتك</label>

                    <div class="custom-input-wrapper">

                        <i class='bx bxs-envelope input-group-icon'></i>

                        <input type="email" id="fpEmail" class="form-control" style="padding-right: 42px;" placeholder="example@email.com">

                    </div>

                    <p class="fs-9 text-muted mt-1 mb-2">هذا هو البريد الإلكتروني الذي أضافه مدير النظام لإدارتك. إذا لم يكن مسجلاً لديك بريد، تواصل مع مدير تقنية المعلومات.</p>

                    <button type="button" id="fpSendBtn" class="btn btn-fifa mb-2" onclick="requestResetCode()">إرسال رمز التحقق</button>

                    <p id="fpStep1Msg" class="fs-8 mt-1 mb-0"></p>

                </div>

                <div id="fpStep2" class="d-none">

                    <p class="fs-8 text-muted mb-2">وصلك رمز تحقق مكون من 6 أرقام عبر البريد الإلكتروني (صالح لمدة 10 دقائق)، ورقّمناه لك اسم المستخدم أيضاً ضمن الرسالة.</p>

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">رمز التحقق</label>

                    <input type="text" id="fpCode" class="form-control mb-2" maxlength="6" placeholder="123456">

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">كلمة المرور الجديدة</label>

                    <input type="password" id="fpNewPassword" class="form-control mb-2" placeholder="كلمة مرور جديدة (6 خانات على الأقل)">

                    <label class="form-label fw-bold fs-7 mb-1" style="color: var(--fifa-green);">تأكيد كلمة المرور</label>

                    <input type="password" id="fpConfirmPassword" class="form-control mb-2" placeholder="أعد كتابة كلمة المرور">

                    <button type="button" id="fpResetBtn" class="btn btn-fifa mb-2" onclick="submitResetPassword()">تغيير كلمة المرور</button>

                    <p id="fpStep2Msg" class="fs-8 mt-1 mb-0"></p>

                </div>

                <a href="#" onclick="return backToLogin();" class="fs-8 text-muted d-block mt-1"><i class='bx bx-arrow-back'></i> رجوع لتسجيل الدخول</a>

            </div>

            <div class="login-footer">

                جميع الحقوق محفوظة &copy; نادي فيفا الرياضي 2026

            </div>

        </div>

        <script>

            function showForgotPassword() {

                document.getElementById('loginForm').classList.add('d-none');

                document.getElementById('forgotPasswordLink').classList.add('d-none');

                document.getElementById('forgotPasswordPanel').classList.remove('d-none');

                return false;

            }

            function backToLogin() {

                document.getElementById('forgotPasswordPanel').classList.add('d-none');

                document.getElementById('loginForm').classList.remove('d-none');

                document.getElementById('forgotPasswordLink').classList.remove('d-none');

                document.getElementById('fpStep1').classList.remove('d-none');

                document.getElementById('fpStep2').classList.add('d-none');

                document.getElementById('fpStep1Msg').textContent = '';

                document.getElementById('fpStep2Msg').textContent = '';

                return false;

            }

            function requestResetCode() {

                var email = document.getElementById('fpEmail').value.trim();

                var msgEl = document.getElementById('fpStep1Msg');

                if (!email) {

                    msgEl.textContent = 'الرجاء إدخال البريد الإلكتروني.';

                    msgEl.className = 'fs-8 mt-1 mb-0 text-danger';

                    return;

                }

                var btn = document.getElementById('fpSendBtn');

                btn.disabled = true;

                btn.textContent = 'جاري الإرسال...';

                fetch('/forgot_password/request_code', {

                    method: 'POST',

                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },

                    body: 'email=' + encodeURIComponent(email)

                }).then(function (r) { return r.json(); }).then(function (data) {

                    btn.disabled = false;

                    btn.textContent = 'إرسال رمز التحقق';

                    msgEl.textContent = data.message;

                    msgEl.className = 'fs-8 mt-1 mb-0 ' + (data.success ? 'text-success' : 'text-danger');

                    if (data.success) {

                        document.getElementById('fpStep1').classList.add('d-none');

                        document.getElementById('fpStep2').classList.remove('d-none');

                    }

                }).catch(function () {

                    btn.disabled = false;

                    btn.textContent = 'إرسال رمز التحقق';

                    msgEl.textContent = 'حدث خطأ، حاول مرة أخرى.';

                    msgEl.className = 'fs-8 mt-1 mb-0 text-danger';

                });

            }

            function submitResetPassword() {

                var email = document.getElementById('fpEmail').value.trim();

                var code = document.getElementById('fpCode').value.trim();

                var pass1 = document.getElementById('fpNewPassword').value;

                var pass2 = document.getElementById('fpConfirmPassword').value;

                var msgEl = document.getElementById('fpStep2Msg');

                if (!code || !pass1 || !pass2) {

                    msgEl.textContent = 'الرجاء تعبئة جميع الحقول.';

                    msgEl.className = 'fs-8 mt-1 mb-0 text-danger';

                    return;

                }

                var btn = document.getElementById('fpResetBtn');

                btn.disabled = true;

                btn.textContent = 'جاري التحقق...';

                fetch('/forgot_password/reset', {

                    method: 'POST',

                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },

                    body: 'email=' + encodeURIComponent(email) + '&otp_code=' + encodeURIComponent(code) +

                        '&new_password=' + encodeURIComponent(pass1) + '&confirm_password=' + encodeURIComponent(pass2)

                }).then(function (r) { return r.json(); }).then(function (data) {

                    btn.disabled = false;

                    btn.textContent = 'تغيير كلمة المرور';

                    msgEl.textContent = data.message;

                    msgEl.className = 'fs-8 mt-1 mb-0 ' + (data.success ? 'text-success' : 'text-danger');

                    if (data.success) {

                        setTimeout(function () { window.location.reload(); }, 1800);

                    }

                }).catch(function () {

                    btn.disabled = false;

                    btn.textContent = 'تغيير كلمة المرور';

                    msgEl.textContent = 'حدث خطأ، حاول مرة أخرى.';

                    msgEl.className = 'fs-8 mt-1 mb-0 text-danger';

                });

            }

            function updateFifaThemeIcon() {

                var icon = document.getElementById('themeToggleIcon');

                if (!icon) return;

                var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

                icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

            }

            function toggleFifaTheme() {

                var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

                var next = current === 'dark' ? 'light' : 'dark';

                document.documentElement.setAttribute('data-theme', next);

                try { localStorage.setItem('fifa_theme', next); } catch (e) {}

                updateFifaThemeIcon();

            }

            updateFifaThemeIcon();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code)

@app.route('/register', methods=['GET', 'POST'])

def register():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_add_user'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك الصلاحية لإضافة إدارة أو مستخدم جديد."); window.location.href="/dashboard";</script>'''

    if request.method == 'POST':

        dept_name = request.form['dept_name'].strip()

        username = request.form['username'].strip()

        password = request.form['password']

        

        try:

            cursor.execute('''

                INSERT INTO departments (name, username, password, can_access_archive, can_view_all_archive, can_view_all_achievements, can_add_user, can_page_inbox, can_page_outbox, can_page_achievements, can_page_archive, can_page_quick_upload, can_page_suggestions, can_page_chat) 

                VALUES (%s, %s, %s, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1)

            ''', (dept_name, username, generate_password_hash(password)))

            conn.commit()

            cursor.close()

            conn.close()

            return '''<script>alert("تم إنشاء حساب الإدارة بنجاح!"); window.location.href="/admin/permissions";</script>'''

        except IntegrityError as e:

            conn.rollback()

            cursor.execute('SELECT id FROM departments WHERE username = %s', (username,))

            user_exists = cursor.fetchone()

            cursor.execute('SELECT id FROM departments WHERE name = %s', (dept_name,))

            name_exists = cursor.fetchone()

            cursor.close()

            conn.close()

            

            if user_exists:

                return '''<script>alert("خطأ: اسم المستخدم (username) مستخدم مكرر بالفعل، يرجى اختيار اسم مستخدم آخر."); window.location.href="/register";</script>'''

            elif name_exists:

                return '''<script>alert("خطأ: اسم الإدارة أو القسم (name) مسجل مكرر بالفعل، يرجى تغييره."); window.location.href="/register";</script>'''

            else:

                return '''<script>alert("حدث خطأ في قاعدة البيانات أثناء التسجيل (قيمة مكررة)!"); window.location.href="/register";</script>'''

            

    cursor.close()

    conn.close()

    

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>إنشاء حساب إدارة - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

            .top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }

            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            .sidebar { width: 260px; background-color: var(--fifa-green); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; display: flex; align-items: center; justify-content: center; }

            .register-card { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-radius: 16px; border: 1px solid #d5e2d8; box-shadow: 0 10px 30px rgba(18, 56, 38, 0.08); width: 100%; max-width: 450px; padding: 1.5rem; position: relative; overflow: hidden; }

            .register-card::before { content: ''; position: absolute; top: 0; right: 0; left: 0; height: 5px; background: linear-gradient(90deg, var(--fifa-gold), var(--fifa-green)); }

            .form-control { border-radius: 8px; padding: 0.75rem 1rem; border-color: #dbe3df; }

            .btn-fifa-gold { background-color: var(--fifa-gold); color: #ffffff; border-radius: 8px; padding: 0.75rem; font-weight: 700; border: none; width: 100%; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

                <button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                        <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                        <span class="fw-bold fs-7" style="color: var(--fifa-green);">{{ dept_name }}</span>

                    </button>

                    <ul class="dropdown-menu dropdown-menu-start shadow">

                        <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                    </ul>

                </div>

            </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                {% if current_dept['can_page_inbox'] == 1 or is_admin %}

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                {% endif %}

                {% if current_dept['can_page_outbox'] == 1 or is_admin %}

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                {% endif %}

                {% if current_dept['can_page_achievements'] == 1 or is_admin %}

                <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                {% endif %}

                {% if current_dept['can_page_archive'] == 1 or is_admin %}

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                {% endif %}

                {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}

                <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                {% endif %}

                <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>
                {% if current_dept['can_page_chat'] == 1 or is_admin %}

                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                {% endif %}

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                {% if is_admin or current_dept['can_page_admin_tools'] == 1 %}

                <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                {% endif %}

                {% if current_dept['can_add_user'] == 1 %}

                <a href="/register" class="sidebar-link active"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                {% endif %}

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

                <div class="register-card text-center">

                    <h5 class="fw-bold mb-1" style="color: var(--fifa-green);">تسجيل إدارة/قسم جديد</h5>

                    <p class="text-muted fs-7 mb-4">إنشاء حساب إداري متصل بنظام أرشفة النادي</p>

                    <form action="/register" method="post">

                        <div class="mb-3 text-start">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green);">اسم الإدارة / القسم</label>

                            <input type="text" name="dept_name" class="form-control" placeholder="مثال: إدارة الألعاب الرياضية" required>

                        </div>

                        <div class="mb-3 text-start">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green);">اسم المستخدم (للدخول)</label>

                            <input type="text" name="username" class="form-control" placeholder="مثال: sports_dept" required>

                        </div>

                        <div class="mb-4 text-start">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green);">كلمة المرور</label>

                            <input type="password" name="password" class="form-control" placeholder="أدخل كلمة مرور قوية" required>

                        </div>

                        <button type="submit" class="btn btn-fifa-gold mb-3">تسجيل الحساب</button>

                    </form>

                    <div class="border-top pt-3 mt-2">

                        <a href="/dashboard" class="text-muted text-decoration-none fs-7"><i class='bx bx-right-arrow-alt ms-1'></i>العودة لوحة التحكم</a>

                    </div>

                </div>

            </main>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

            function updateNavbarHeightVar() {

                var nav = document.querySelector('.top-navbar');

                if (nav) { document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px'); }

            }

            updateNavbarHeightVar();

            window.addEventListener('load', updateNavbarHeightVar);

            window.addEventListener('resize', updateNavbarHeightVar);

            function toggleSidebar() {

                document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

                document.getElementById('mobileOverlay').classList.toggle('active');

            }

            (function() {

                var touchStartX = 0;

                var touchStartY = 0;

                var edgeThreshold = 25;

                var swipeThreshold = 60;

                document.addEventListener('touchstart', function(e) {

                    touchStartX = e.touches[0].clientX;

                    touchStartY = e.touches[0].clientY;

                }, { passive: true });

                document.addEventListener('touchend', function(e) {

                    if (window.innerWidth > 991.98) return;

                    var sidebarEl = document.getElementById('sidebarMenu');

                    if (!sidebarEl) return;

                    var touchEndX = e.changedTouches[0].clientX;

                    var touchEndY = e.changedTouches[0].clientY;

                    var deltaX = touchEndX - touchStartX;

                    var deltaY = touchEndY - touchStartY;

                    if (Math.abs(deltaY) > 60) return;

                    var isOpen = sidebarEl.classList.contains('show-sidebar');

                    if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

                        toggleSidebar();

                    }

                    else if (isOpen && deltaX > swipeThreshold) {

                        toggleSidebar();

                    }

                }, { passive: true });

            })();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, dept_name=session['dept_name'], current_dept=current_dept, is_admin=is_admin)

@app.route('/logout')

def logout():

    session.clear()

    return redirect(url_for('login'))

@app.route('/suggestions', methods=['GET', 'POST'])

def suggestions():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT can_page_suggestions FROM departments WHERE id = %s', (session['dept_id'],))

    perm_check = cursor.fetchone()

    if perm_check['can_page_suggestions'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة مشاكل واقتراحات."); window.location.href="/dashboard";</script>'''

    if request.method == 'POST':

        message = request.form.get('message', '').strip()

        if message:

            cursor.execute('''

                INSERT INTO suggestions (dept_id, dept_name, message, created_at)

                VALUES (%s, %s, %s, %s)

            ''', (session['dept_id'], session['dept_name'], message, datetime.now().strftime('%Y-%m-%d %H:%M')))

            conn.commit()

        cursor.close()

        conn.close()

        return '''<script>alert("تم إرسال رسالتك بنجاح إلى مدير تقنية المعلومات."); window.location.href="/suggestions";</script>'''

    all_suggestions = []

    if is_admin:

        cursor.execute('SELECT * FROM suggestions ORDER BY id DESC')

        all_suggestions = cursor.fetchall()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    cursor.close()

    conn.close()

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>مشاكل واقتراحات - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green-primary: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; --fifa-card-border: #d5e2d8; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

            .top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }

            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; width: 100%; min-width: 0; overflow-x: hidden; }

            .modern-card { background: rgba(255, 255, 255, 0.95); border-radius: 12px; border: 1px solid var(--fifa-card-border); padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 4px 15px rgba(0,0,0,0.03); }

            .btn-fifa-primary { background-color: var(--fifa-green-primary); color: #ffffff; border-radius: 8px; padding: 0.7rem 1.5rem; font-weight: 700; border: none; }

            .btn-fifa-primary:hover { color: #fff; background-color: #1e563b; }

            .suggestion-item { border-bottom: 1px solid #f0f4f2; padding: 1rem; }

            .suggestion-item:last-child { border-bottom: none; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

                <button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                        <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                        <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>

                    </button>

                    <ul class="dropdown-menu dropdown-menu-start shadow">

                        <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                    </ul>

                </div>

            </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                {% if current_dept['can_page_inbox'] == 1 or is_admin %}

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                {% endif %}

                {% if current_dept['can_page_outbox'] == 1 or is_admin %}

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                {% endif %}

                {% if current_dept['can_page_achievements'] == 1 or is_admin %}

                <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                {% endif %}

                {% if current_dept['can_page_archive'] == 1 or is_admin %}

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                {% endif %}

                {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}

                <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                {% endif %}

                {% if current_dept['can_page_suggestions'] == 1 or is_admin %}

                <a href="/suggestions" class="sidebar-link active"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>

                {% endif %}
                {% if current_dept['can_page_chat'] == 1 or is_admin %}

                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                {% endif %}

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                {% if is_admin or current_dept['can_page_admin_tools'] == 1 %}

                <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                {% endif %}

                {% if current_dept['can_add_user'] == 1 %}

                <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                {% endif %}

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

                <div class="container-fluid p-0">

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);">

                            <i class='bx bxs-message-square-detail ms-1' style="color: var(--fifa-gold);"></i>

                            إرسال مشكلة أو اقتراح

                        </h5>

                        <p class="text-muted fs-7">اكتب مشكلتك أو اقتراحك بالأسفل، وسيتم إرساله مباشرة إلى مدير تقنية المعلومات.</p>

                        <form action="/suggestions" method="post">

                            <textarea name="message" class="form-control mb-3" rows="5" placeholder="اكتب المشكلة أو الاقتراح هنا..." required></textarea>

                            <button type="submit" class="btn btn-fifa-primary">

                                <i class='bx bx-send ms-1'></i> إرسال

                            </button>

                        </form>

                    </div>

                    {% if is_admin %}

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3 d-flex justify-content-between align-items-center flex-wrap gap-2" style="color: var(--fifa-green-primary);">

                            <span>

                                <i class='bx bxs-inbox ms-1' style="color: var(--fifa-gold);"></i>

                                الرسائل الواردة

                                <span class="badge bg-success">{{ all_suggestions|length }}</span>

                            </span>

                            {% if all_suggestions %}

                            <a href="/suggestions/delete_all" class="btn btn-sm btn-outline-danger py-1 px-2 fs-8" onclick="return confirm('تحذير: سيتم حذف جميع الرسائل الواردة نهائياً. هل أنت متأكد؟');">

                                <i class='bx bx-trash ms-1'></i> حذف الكل

                            </a>

                            {% endif %}

                        </h5>

                        {% if all_suggestions %}

                            {% for s in all_suggestions %}

                            <div class="suggestion-item">

                                <div class="d-flex justify-content-between align-items-center mb-1 flex-wrap gap-1">

                                    <span class="fw-bold text-dark fs-7">{{ s.dept_name }}</span>

                                    <div class="d-flex align-items-center gap-2">

                                        <small class="text-muted fs-8">{{ s.created_at }}</small>

                                        <a href="/suggestions/delete/{{ s.id }}" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="return confirm('حذف هذه الرسالة؟');">

                                            <i class='bx bx-trash'></i>

                                        </a>

                                    </div>

                                </div>

                                <p class="mb-0 text-secondary fs-7">{{ s.message }}</p>

                            </div>

                            {% endfor %}

                        {% else %}

                            <p class="text-muted fs-7 text-center py-3">لا توجد رسائل حالياً.</p>

                        {% endif %}

                    </div>

                    {% endif %}

                </div>

            </main>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

            function updateNavbarHeightVar() {

                var nav = document.querySelector('.top-navbar');

                if (nav) { document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px'); }

            }

            updateNavbarHeightVar();

            window.addEventListener('load', updateNavbarHeightVar);

            window.addEventListener('resize', updateNavbarHeightVar);

            function toggleSidebar() {

                document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

                document.getElementById('mobileOverlay').classList.toggle('active');

            }

            (function() {

                var touchStartX = 0;

                var touchStartY = 0;

                var edgeThreshold = 25;

                var swipeThreshold = 60;

                document.addEventListener('touchstart', function(e) {

                    touchStartX = e.touches[0].clientX;

                    touchStartY = e.touches[0].clientY;

                }, { passive: true });

                document.addEventListener('touchend', function(e) {

                    if (window.innerWidth > 991.98) return;

                    var sidebarEl = document.getElementById('sidebarMenu');

                    if (!sidebarEl) return;

                    var touchEndX = e.changedTouches[0].clientX;

                    var touchEndY = e.changedTouches[0].clientY;

                    var deltaX = touchEndX - touchStartX;

                    var deltaY = touchEndY - touchStartY;

                    if (Math.abs(deltaY) > 60) return;

                    var isOpen = sidebarEl.classList.contains('show-sidebar');

                    if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

                        toggleSidebar();

                    }

                    else if (isOpen && deltaX > swipeThreshold) {

                        toggleSidebar();

                    }

                }, { passive: true });

            })();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, dept_name=session['dept_name'], current_dept=current_dept, is_admin=is_admin, all_suggestions=all_suggestions)

@app.route('/suggestions/delete/<int:suggestion_id>')

def delete_suggestion(suggestion_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    if not is_admin:

        return '''<script>alert("عذراً، هذه الصلاحية مخصصة لمدير تقنية المعلومات فقط."); window.location.href="/suggestions";</script>'''

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('DELETE FROM suggestions WHERE id = %s', (suggestion_id,))

    conn.commit()

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف الرسالة بنجاح"); window.location.href="/suggestions";</script>'''

@app.route('/suggestions/delete_all')

def delete_all_suggestions():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    if not is_admin:

        return '''<script>alert("عذراً، هذه الصلاحية مخصصة لمدير تقنية المعلومات فقط."); window.location.href="/suggestions";</script>'''

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('DELETE FROM suggestions')

    conn.commit()

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف جميع الرسائل بنجاح"); window.location.href="/suggestions";</script>'''

DASHBOARD_HTML = '''

{% macro render_letter_item(letter) %}

<div class="letter-item d-flex flex-column flex-sm-row align-items-start justify-content-between gap-2">

    <div class="d-flex align-items-start gap-2 w-100">

        {% if current_page == 'archive' and can_delete == 1 %}

            <input class="form-check-input letter-checkbox mt-2" type="checkbox" name="letter_ids" value="{{ letter.id }}" form="bulkDeleteForm">

        {% endif %}

        <i class='bx bxs-file-archive fs-3 text-success mt-1 d-none d-sm-block'></i>

        <div class="w-100">

            <div class="d-flex flex-wrap justify-content-between align-items-center mb-1 gap-1">

                <span class="fw-bold text-dark fs-6 text-break"><bdi>{{ letter.title }}</bdi></span>

                {% if current_page == 'inbox' and letter.is_read == 0 %}<span class="badge bg-danger">جديد</span>{% endif %}

                <small class="text-muted fs-8" dir="ltr">{{ letter.created_at.split(' ')[0] if letter.created_at else '' }}</small>

            </div>

            {% if letter.content %}<div class="text-secondary small mb-2 d-none" id="letter-text-{{ letter.id }}">{{ letter.content|safe }}</div>{% endif %}

            <div class="d-flex flex-wrap align-items-center gap-2 mt-2">

                <span class="fs-7 text-muted">

                    {% if current_page == 'outbox' %}إلى: <strong>{{ letter.receiver_name }}</strong>

                    {% elif current_page == 'inbox' %}من: <strong>{{ letter.sender_name }}</strong>

                    {% elif current_page == 'archive' %}

                        {% if letter.archive_dept_name %}

                            أرشيف إدارة: <span class="badge bg-success text-white px-2 py-1">{{ letter.archive_dept_name }}</span>

                        {% elif letter.sender_id and letter.receiver_id %}

                            من: <strong>{{ letter.sender_name }}</strong> إلى: <strong>{{ letter.receiver_name }}</strong>

                        {% else %}

                            <span class="badge bg-secondary">أرشيف عام</span>

                        {% endif %}

                    {% else %}<span class="badge bg-warning text-dark">رفع فوري خاص</span>{% endif %}

                </span>

            </div>

        </div>

    </div>

    <div class="d-flex align-items-center gap-2 w-100 justify-content-end mt-2 mt-sm-0 flex-wrap">

        {% if current_page == 'inbox' or (current_page == 'outbox' and letter.content) %}

            <button type="button" class="btn btn-sm btn-outline-primary py-1 px-2 fs-7"

                data-id="{{ letter.id }}" data-title="{{ letter.title|e }}"

                data-sender-id="{{ letter.sender_id or '' }}" data-receiver-id="{{ letter.receiver_id or '' }}"

                data-priority="{{ letter.priority }}" data-page="{{ current_page }}"

                data-letter-number="{{ letter.letter_number or '' }}"

                data-sender-name="{{ letter.sender_name|e if letter.sender_name else '' }}"

                data-date="{{ letter.created_at.split(' ')[0] if letter.created_at else '' }}"

                onclick="{% if current_page == 'inbox' %}{% if letter.content %}loadLetterToEditor(this){% else %}openQuickReply(this){% endif %}{% else %}loadLetterToEditor(this){% endif %}">

                {% if current_page == 'inbox' %}

                <i class='bx bx-reply ms-1'></i> رد

                {% else %}

                <i class='bx bx-edit ms-1'></i> تعديل / إرسال

                {% endif %}

            </button>

            {% if letter.content %}

            <button type="button" class="btn btn-sm btn-outline-dark py-1 px-2 fs-7"

                data-title="{{ letter.title|e }}" data-content-id="letter-text-{{ letter.id }}"

                data-date="{{ letter.created_at.split(' ')[0] if letter.created_at else '' }}"

                data-number="{{ letter.letter_number or letter.id }}"

                onclick="previewArchivedLetter(this)">

                <i class='bx bx-show ms-1'></i> معاينة الخطاب

            </button>

            {% endif %}

        {% endif %}

        {% if letter.file_path or letter.file_data %}

            <button type="button" class="btn btn-sm btn-info py-1 px-2 fs-7 text-white" onclick="previewFile('/view_letter_file/{{ letter.id }}', '{{ letter.title }}')">

                <i class='bx bx-show ms-1'></i> معاينة

            </button>

            <a href="/download_letter_file/{{ letter.id }}" class="btn btn-sm btn-outline-success py-1 px-2 fs-7">تحميل</a>

        {% endif %}

        <span class="priority-badge bg-fifa-green">{{ letter.priority }}</span>

        {% if can_delete == 1 %}

            <a href="/delete_letter/{{ letter.id }}" class="btn btn-sm btn-outline-danger py-1 px-2 fs-7" onclick="return confirm('حذف المعاملة؟');">حذف</a>

        {% endif %}

    </div>

</div>

{% endmacro %}

<!DOCTYPE html>

<html dir="rtl" lang="ar">

<head>

    <meta charset="UTF-8">

    <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

    <title>{{ page_title }} - نظام أرشفة نادي فيفا</title>

    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

    <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

    <link href="https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&family=Almarai:wght@300;400;700;800&family=Aref+Ruqaa:wght@400;700&family=Cairo:wght@400;700&family=Changa:wght@400;700&display=swap" rel="stylesheet">

    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>

    <style>

        :root {

            --fifa-green-primary: #123826;

            --fifa-green-light: #1e563b;

            --fifa-gold: #c5a059;

            --fifa-bg: #eaf3ec;

            --fifa-card-border: #d5e2d8;

        }

        body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

.top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }        .nav-logo { height: 42px; width: auto; object-fit: contain; }

        .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

        

        .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

        

        @media (max-width: 991.98px) {

            .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

            .sidebar.show-sidebar { right: 0; }

        }

 

        .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

        .mobile-overlay.active { display: block; }

 

        .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

        .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

        .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

        .content-body { flex: 1; padding: 1.25rem; width: 100%; overflow-x: hidden; }

        .modern-card { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-radius: 12px; border: 1px solid var(--fifa-card-border); box-shadow: 0 4px 15px rgba(18, 56, 38, 0.03); }

        .section-header { font-weight: 800; color: var(--fifa-green-primary); margin-bottom: 1.5rem; position: relative; padding-bottom: 10px; font-size: 1.3rem; }

        .section-header::after { content: ''; position: absolute; bottom: 0; right: 0; width: 55px; height: 3px; background-color: var(--fifa-gold); border-radius: 2px; }

        .letter-item { border-bottom: 1px solid #f0f4f2; padding: 1rem; }

        .letter-item:hover { background-color: rgba(244, 248, 246, 0.8); }

        .priority-badge { font-size: 0.75rem; padding: 4px 10px; border-radius: 20px; font-weight: 700; }

        .bg-fifa-green { background-color: var(--fifa-green-primary) !important; color: #fff; }

        .btn-fifa-primary { background-color: var(--fifa-green-primary); color: #ffffff; border-radius: 8px; padding: 0.6rem 1.2rem; font-weight: 700; border: none; }

        .btn-fifa-primary:hover { background-color: var(--fifa-green-light); color: #fff; }

 

        /* ================= ورقة خطاب Word رسمية مقاس A4 حقيقي مع أدوات التحكّم بالخط ================= */

        .paper-toolbar {

            background: #ffffff;

            border: 1px solid #c8d6cd;

            border-bottom: none;

            border-radius: 10px 10px 0 0;

            padding: 8px 12px;

            display: flex;

            flex-wrap: wrap;

            align-items: center;

            gap: 6px;

            width: 210mm;

            max-width: 100%;

            margin: 0 auto;

            box-shadow: 0 4px 10px rgba(0,0,0,0.05);

            box-sizing: border-box;

        }

        .paper-toolbar button, .paper-toolbar select, .paper-toolbar input[type="color"] {

            border: 1px solid #d5e2d8;

            background: #f8faf9;

            border-radius: 6px;

            padding: 4px 8px;

            font-size: 0.85rem;

            font-weight: bold;

            color: #123826;

            cursor: pointer;

            transition: all 0.2s;

        }

        .paper-toolbar button:hover {

            background: #123826;

            color: #ffffff;

        }

        .word-paper-container {

            display: flex;

            flex-direction: column;

            align-items: center;

            margin-bottom: 2rem;

            overflow-x: auto;

            padding-bottom: 8px;

        }

        /* مقاس A4 الحقيقي: 210مم × 297مم، تماماً مثل صفحة الوورد */

        .word-paper {

            background: #ffffff;

            width: 210mm;

            min-height: 297mm;

            max-width: 210mm;

            padding: 18mm 20mm;

            box-shadow: 0 10px 30px rgba(0,0,0,0.15);

            border: 1px solid #c8d6cd;

            border-radius: 0 0 4px 4px;

            position: relative;

            font-family: 'Amiri', 'Traditional Arabic', serif;

            color: #000;

            line-height: 1.8;

            box-sizing: border-box;

            flex-shrink: 0;

         }   

@media (max-width: 860px) {

    /* التصغير يتم بالكامل عبر JavaScript، لا حاجة لأي CSS هنا */

         }

        .word-paper.extra-page { margin-top: 18px; }

        /* زر حذف التوقيع الإلكتروني: يظهر بالتحويم على الكمبيوتر، ويبقى ظاهراً دائماً على الجوال/التاتش لأنه لا يوجد hover هناك */

        .draggable-signature .sig-remove-btn {

            display: none;

            align-items: center;

            justify-content: center;

        }

        .draggable-signature:hover .sig-remove-btn {

            display: flex;

        }

        @media (hover: none) {

            .draggable-signature .sig-remove-btn {

                display: flex !important;

            }

        }

        .page-number-badge {

            position: absolute;

            top: 6mm;

            left: 6mm;

            background: var(--fifa-gold);

            color: #ffffff;

            font-weight: 800;

            font-size: 0.8rem;

            padding: 3px 12px;

            border-radius: 20px;

            z-index: 5;

            font-family: 'Almarai', sans-serif;

        }

        

        .word-paper-header {

            display: flex;

            justify-content: space-between;

            align-items: flex-start;

            margin-bottom: 0.6rem;

            line-height: 1.4;

        }

.word-paper-header-rules { margin-bottom: 2.2rem; }

.word-paper-header-rules .rule-single {

    height: 2.5px;

    border-radius: 2px;

    background: linear-gradient(90deg, var(--fifa-green-primary) 0%, var(--fifa-gold) 50%, var(--fifa-green-primary) 100%);

}

        .word-paper-right {

            text-align: center !important;

            text-align-last: center !important;

            text-justify: none !important;

            white-space: normal;

            word-spacing: normal !important;

            letter-spacing: normal !important;

            font-size: 1.15rem;

            font-weight: bold;

            color: #000;

           flex: 0 1 auto;

           width: max-content;

           max-width: 260px;

           margin: 0 auto;

       }

        .word-paper-center {

            text-align: center;

            flex: 1;

            display: flex;

            flex-direction: column;

            align-items: center;

            justify-content: center;

        }

         .word-paper-center img {

             max-height: 125px;

             width: auto;

             object-fit: contain;

             margin-bottom: 2px;

       }

        .word-paper-center .brand-name-sub {

            font-weight: 800;

            font-size: 1.3rem;

            color: #000;

            letter-spacing: 1px;

            font-family: Arial, sans-serif;

            text-transform: uppercase;

        }

        .word-paper-left {

            text-align: right;

            font-size: 1.05rem;

            font-weight: bold;

            color: #000;

            flex: 1;

            display: flex;

            flex-direction: column;

            align-items: flex-end;

        }

        .word-paper-left-inner {

            text-align: right;

            min-width: 180px;

        }

        .word-paper-title {

            text-align: center;

            font-size: 1.35rem;

            font-weight: bold;

            margin-top: 1rem;

            margin-bottom: 0.5rem;

        }

        .word-paper-greeting {

            text-align: center;

            font-size: 1.2rem;

            font-weight: bold;

            margin-bottom: 1.5rem;

        }

        /* منطقة نص الخطاب القابلة للكتابة والتكبير والتصغير */

        .word-paper-body {

            font-size: 1.15rem;

            text-align: justify;

            text-justify: inter-word;

            margin-bottom: 2rem;

            min-height: 250px;

            outline: none;

            padding: 8px;

            border: 1px dashed transparent;

            border-radius: 6px;

            transition: border 0.2s;

        }

        .word-paper-body:hover, .word-paper-body:focus {

            border-color: #c5a059;

            background-color: #fafcfb;

        }

        .word-paper-footer-closing {

            text-align: center;

            font-size: 1.2rem;

            font-weight: bold;

            margin-bottom: 3rem;

        }

        .word-paper-signature {

            text-align: left;

            margin-left: 2rem;

            font-size: 1.15rem;

            font-weight: bold;

        }

        /* ===== تذييل الصفحة بشكل الموجة الذهبية مطابق لنموذج نادي فيفا ===== */

        .word-paper-footer-wave {

            position: absolute;

            bottom: 0;

            left: 0;

            right: 0;

            height: 26mm;

            pointer-events: none;

        }

        .word-paper-footer-wave svg {

            position: absolute;

            bottom: 0;

            left: 0;

            width: 100%;

            height: 100%;

            display: block;

        }

        .word-paper-footer-content {

            position: absolute;

            bottom: 4mm;

            left: 0;

            right: 0;

            display: flex;

            align-items: center;

            justify-content: center;

            gap: 22px;

            flex-wrap: wrap;

            direction: ltr;

            font-family: Arial, sans-serif;

            font-weight: bold;

            font-size: 0.92rem;

            color: #ffffff;

            text-shadow: 0 1px 2px rgba(0,0,0,0.25);

        }

        .word-paper-footer-content span {

            display: inline-flex;

            align-items: center;

            gap: 5px;

            white-space: nowrap;

        }

        .word-paper-footer-content i { font-size: 1.05rem; }

 

        .paper-editable-input {

            border: none;

            border-bottom: 1px dashed #aaa;

            background: transparent;

            font-weight: bold;

            font-family: inherit;

            font-size: inherit;

            padding: 0 4px;

        }

        .paper-editable-input:focus {

            outline: none;

            border-bottom: 1px solid var(--fifa-green-primary);

            background: #fdfdfd;

        }

 

        /* نافذة معاينة الخطاب A4 */

        #previewLetterModal .modal-body {

            background: #6b6f70;

            display: flex;

            justify-content: center;

            padding: 24px 10px;

            overflow: auto;

            max-height: 85vh;

        }

        #previewLetterContainer .word-paper {

            box-shadow: 0 15px 40px rgba(0,0,0,0.35);

        }

        #previewLetterContainer input {

            border: none !important;

            background: transparent !important;

            pointer-events: none;

        }

        @media print {

            body * { visibility: hidden; }

            #printAreaPaper, #printAreaPaper * { visibility: visible; }

            #printAreaPaper {

                position: absolute;

                top: 0;

                right: 0;

                left: 0;

                width: 210mm;

                margin: 0 auto;

            }

            #printAreaPaper .word-paper {

                width: 210mm !important;

                min-height: 297mm !important;

                margin: 0 auto !important;

                box-shadow: none !important;

                border: none !important;

                zoom: 1 !important;

                transform: none !important;

            }

            .word-paper-container { zoom: 1 !important; transform: none !important; }

            @page { size: A4; margin: 0; }

        }

 

    </style>

</head>

<body>

 

    <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

 

    <nav class="navbar top-navbar sticky-top">

        <div class="container-fluid">

            <div class="d-flex align-items-center gap-2">

                <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                    <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>

                </button>

                <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                    <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                    <div class="d-flex flex-column">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>

                        <span class="text-muted fs-8 d-none d-sm-block mt-1">نظام الأرشفة والخطابات الإلكتروني</span>

                    </div>

                </a>

            </div>

            

            <div class="d-flex align-items-center gap-2">

<button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                        <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                        <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>

                    </button>

                    <ul class="dropdown-menu dropdown-menu-start shadow">

                        <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                    </ul>

                </div>

            </div>

        </div>

    </nav>

 

    <div class="main-wrapper">

        <aside class="sidebar" id="sidebarMenu">

            <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                <span class="fw-bold text-white">قائمة التنقل</span>

                <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

            </div>

            {% if can_page_inbox == 1 or is_admin %}

            <a href="/dashboard" class="sidebar-link {{ 'active' if current_page == 'inbox' else '' }}"><i class='bx bxs-inbox'></i>الصندوق الوارد

                {% if unread_count and unread_count > 0 %}<span class="badge bg-danger rounded-pill ms-1" id="inboxUnreadBadge">{{ unread_count }}</span>{% endif %}

            </a>

            {% endif %}

            {% if can_page_outbox == 1 or is_admin %}

            <a href="/outbox" class="sidebar-link {{ 'active' if current_page == 'outbox' else '' }}"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

            {% endif %}

            {% if can_page_achievements == 1 or is_admin %}

            <a href="/monthly_achievements" class="sidebar-link {{ 'active' if current_page == 'achievements' else '' }}"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

            {% endif %}

            {% if can_page_archive == 1 or is_admin %}

            <a href="/archive" class="sidebar-link {{ 'active' if current_page == 'archive' else '' }}"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

            {% endif %}

            {% if can_page_quick_upload == 1 or is_admin %}

            <a href="/quick_upload" class="sidebar-link {{ 'active' if current_page == 'quick_upload' else '' }}"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

            {% endif %}

            {% if can_page_suggestions == 1 or is_admin %}

            <a href="/suggestions" class="sidebar-link {{ 'active' if current_page == 'suggestions' else '' }}"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>

            {% endif %}
            {% if can_page_chat == 1 or is_admin %}

            <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

            {% endif %}

            <script>
            (function(){
                function pollChatUnread(){
                    fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                        document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                            if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                            else { el.classList.add('d-none'); }
                        });
                    }).catch(function(){});
                }
                pollChatUnread();
                setInterval(pollChatUnread, 20000);
            })();
            </script>


            {% if is_admin or can_page_admin_tools == 1 %}

            <a href="/admin/dashboard" class="sidebar-link {{ 'active' if current_page == 'admin_dashboard' else '' }}" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

            <a href="/admin/permissions" class="sidebar-link {{ 'active' if current_page == 'permissions' else '' }}"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

            {% endif %}

            {% if can_add_user == 1 %}

            <a href="/register" class="sidebar-link {{ 'active' if current_page == 'register' else '' }}"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

            {% endif %}

            <div class="border-top border-secondary my-3 opacity-25"></div>

            <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

        </aside>

 

        <main class="content-body">

            {% if current_page == 'inbox' %}

            <div id="newLetterToast" class="alert alert-success d-none position-fixed top-0 start-50 translate-middle-x mt-3 shadow d-flex align-items-center gap-2" style="z-index: 2000;" role="alert">

                <i class='bx bx-envelope fs-5'></i>

                <span>وصلك خطاب جديد في الصندوق الوارد!</span>

                <button type="button" class="btn btn-sm btn-success" onclick="location.reload()">تحديث</button>

            </div>

            {% endif %}

            <div class="container-fluid p-0">

                <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-3">

                    <h4 class="section-header m-0">{{ page_title }}</h4>

                    {% if current_page == 'outbox' or current_page == 'inbox' %}

                    <div class="d-flex gap-2 flex-wrap">

                        <button type="button" class="btn btn-outline-dark d-flex align-items-center gap-2 shadow-sm fw-bold" onclick="previewLetterPaper()">

                            <i class='bx bx-show fs-5'></i> معاينة الخطاب

                        </button>

                        <button type="button" class="btn btn-danger d-flex align-items-center gap-2 shadow-sm fw-bold" onclick="downloadLetterPDF()">

                            <i class='bx bxs-file-pdf fs-5'></i> تحميل PDF

                        </button>

                    </div>

                    {% elif current_page == 'archive' %}

                    <div class="d-flex gap-2 flex-wrap">

                        <a href="/download_archive_zip?scope=own" class="btn btn-success d-flex align-items-center gap-2 shadow-sm fw-bold">

                            <i class='bx bx-download fs-5'></i> تحميل الكل (أرشيفي)

                        </a>

                        {% if can_view_all_archive == 1 %}

                        <a href="/download_archive_zip?scope=all" class="btn btn-outline-success d-flex align-items-center gap-2 shadow-sm fw-bold">

                            <i class='bx bx-download fs-5'></i> تحميل كل الأرشيف

                        </a>

                        {% endif %}

                    </div>

                    {% endif %}

                </div>

 

                {% if current_page == 'outbox' or current_page == 'inbox' %}

                <!-- ============ ورقة الخطاب الرسمية المباشرة مع شريط التنسيق ============ -->

                <div class="word-paper-container">

                    

                    <!-- شريط أدوات التنسيق وتكبير/تصغير الخط للنص المحدد -->

                    <div class="paper-toolbar">

                        <span class="fw-bold fs-8 text-muted me-1"><i class='bx bx-font'></i> تنسيق الخط المحدد:</span>

                        <button type="button" onmousedown="event.preventDefault()" onclick="changeFontSize(1)" title="تكبير النص المحدد"><i class='bx bx-font-plus fs-6'></i> A+</button>

                        <button type="button" onmousedown="event.preventDefault()" onclick="changeFontSize(-1)" title="تصغير النص المحدد"><i class='bx bx-font-minus fs-6'></i> A-</button>

                        <span id="currentFontSizeLabel" class="badge bg-light text-dark border fs-8">18px</span>

                        

                        <div class="vr mx-1"></div>

 

                        <select id="fontFamilySelect" onchange="changeFontFamily(this.value)" title="نوع الخط">

                            <option value="'Amiri', serif">خط الأميري النسخي</option>

                            <option value="'Almarai', sans-serif">خط المراعي العادي</option>

                            <option value="'Aref Ruqaa', serif">خط الرقعة</option>

                            <option value="'Cairo', sans-serif">خط كايرو</option>

                            <option value="'Changa', sans-serif">خط شانغا</option>

                        </select>

 

                        <div class="vr mx-1"></div>

 

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('bold')" title="تغميق (Bold)"><i class='bx bx-bold fs-6'></i></button>

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('underline')" title="تحته خط"><i class='bx bx-underline fs-6'></i></button>

                        <input type="color" id="textColorPicker" onchange="formatDoc('foreColor', this.value)" title="لون الخط" style="width: 32px; height: 28px; padding: 2px;">

 

                        <div class="vr mx-1"></div>

 

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('justifyRight')" title="محاذاة لليمين"><i class='bx bx-align-right fs-6'></i></button>

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('justifyCenter')" title="محاذاة للوسط"><i class='bx bx-align-middle fs-6'></i></button>

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('justifyLeft')" title="محاذاة لليصار"><i class='bx bx-align-left fs-6'></i></button>

                        <button type="button" onmousedown="event.preventDefault()" onclick="formatDoc('justifyFull')" title="ضبط المحاذاة"><i class='bx bx-align-justify fs-6'></i></button>

 

                        <div class="vr mx-1"></div>

 

                        <button type="button" onclick="addNewPage()" title="إضافة صفحة ثانية لإكمال الخطاب" style="background:#123826; color:#fff;"><i class='bx bx-file-plus fs-6'></i> صفحة جديدة</button>

                        <button type="button" id="removePageBtnToolbar" onclick="removeLastPage()" title="حذف آخر صفحة مضافة" style="background:#dc3545; color:#fff; display:none;"><i class='bx bx-file-minus fs-6'></i> حذف الصفحة</button>

                        <div class="vr mx-1"></div>

                        <button type="button" onclick="openSignaturePad()" title="إضافة توقيع إلكتروني على الخطاب" style="background:#0d6efd; color:#fff;"><i class='bx bx-pen fs-6'></i> توقيع إلكتروني</button>

 

                    </div>

 

                    <div class="word-paper" id="officialPaper">

                        <div class="word-paper-header">

                            <div class="word-paper-right">

                                المملكة العربية السعودية<br>

                                   وزارة الرياضة<br>

                                 فرع وزارة الرياضة بجازان<br>

                                   نادي فيفا الرياضي

                            </div>

                            <div class="word-paper-center">

                                <img src="{{ url_for('static', filename='logo.png') }}" alt="FAIFA" onerror="this.style.display='none'">

                                

                            </div>

                            <div class="word-paper-left">

                                <div class="word-paper-left-inner">

                                    الرقم : <input type="text" id="paperLetterNumInput" class="" value="{{ next_letter_number }}" style="width: 100px;"><br>

                                    التاريخ : <input type="text" id="paperLetterDateInput" class="" value="{{ now.strftime('%Y/%m/%dم') }}" style="width: 100px;"><br>

                                    المشفوعات : <input type="text" id="paperLetterAttachInput" class="" value="" style="width: 100px;">

                                </div>

                            </div>

                        </div>

                        

                          <div class="word-paper-header-rules">

                          <div class="rule-single"></div>

                      </div>

 

                        <!-- نص الخطاب المباشر القابل للتعديل والتكبير والتصغير للكتابة المباشرة -->

                        <div class="word-paper-body" id="paperBodyText" contenteditable="true" oninput="syncTextareaWithPaper()">

 

                        </div>

 

                       

 

                        

 

                        <div class="word-paper-footer-wave">

                            <svg viewBox="0 0 800 110" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">

                                <defs>

                                    <linearGradient id="fifaWaveGrad" x1="0" y1="0" x2="1" y2="0">

                                        <stop offset="0%" stop-color="#c5a059"/>

                                        <stop offset="45%" stop-color="#e3cd9c"/>

                                        <stop offset="100%" stop-color="#123826"/>

                                    </linearGradient>

                                </defs>

                                <path d="M0,55 C120,15 260,85 420,50 C560,20 660,70 800,40 L800,110 L0,110 Z" fill="url(#fifaWaveGrad)"/>

                            </svg>

                            <div class="word-paper-footer-content">

                                <span><i class='bx bx-envelope'></i> fifaclub1436@gmail.com</span>

                                <span><i class='bx bxl-twitter'></i> faifaclub1</span>

                                <span><i class='bx bxl-snapchat'></i> faifaclub1</span>

                                <span><i class='bx bxl-tiktok'></i> faifaclub1</span>

                            </div>

                        </div>

                    </div>

                </div>

 

                <!-- ============ نموذج أسفل ورقة الخطاب لتحديد البيانات والإرسال ============ -->

                <div class="modern-card p-4 mb-4">

                    <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-paper-plane ms-1'></i> تفاصيل إرسال المعاملة / الخطاب</h5>

                    <form id="letterSendForm" action="/send_letter" method="post" enctype="multipart/form-data">

                        <input type="hidden" name="letter_id" id="editLetterId" value="">

                        <input type="hidden" name="letter_number" id="hiddenLetterNumberInput" value="">

                        <div class="row g-3">

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">الإدارة المستلمة:</label>

                                <select name="receiver_id" id="receiverSelect" class="form-select fs-7" required onchange="updateReceiverTitle(this)">

                                    <option value="" selected disabled>اختر الإدارة المستلمة...</option>

                                    {% for d in depts %}

                                        <option value="{{ d.id }}" data-name="{{ d.name }}">{{ d.name }}</option>

                                    {% endfor %}

                                </select>

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">عنوان الخطاب (الموضوع):</label>

                                <input type="text" name="title" id="letterTitleInput" class="form-control fs-7" required placeholder="أدخل عنوان الخطاب...">

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">الأهمية:</label>

                                <select name="priority" id="letterPriority" class="form-select fs-7">

                                    <option value="عادي">عادي</option>

                                    <option value="عاجل">عاجل</option>

                                    <option value="سري للغاية">سري للغاية</option>

                                </select>

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">مرفق إضافي (اختياري):</label>

                                <input type="file" name="file" class="form-control fs-7">

                            </div>

                            <div class="col-12">

                                <label class="form-label fw-bold fs-7">صيغة ومحتوى الخطاب (مرتبط بالورقة أعلاه):</label>

                                <textarea name="content" id="letterContentInput" class="form-control fs-7" rows="5" placeholder="اكتب صيغة الخطاب هنا أو اكتبها مباشرة في الورقة أعلاه..." oninput="syncPaperWithTextarea(this.value)"></textarea>

                            </div>

                            <div class="col-12 text-end mt-3">

                                <button type="submit" class="btn btn-fifa-primary px-5 py-2 fw-bold shadow">

                                    <i class='bx bxs-paper-plane ms-1'></i> إرسال الخطاب الآن

                                </button>

                            </div>

                        </div>

                    </form>

                </div>

                <!-- ============ قسم مستقل لإرسال ملف من الجهاز مباشرة بدون نموذج الخطاب ============ -->

                <div class="modern-card p-4 mb-4">

                    <h5 class="fw-bold mb-2" style="color: var(--fifa-green-primary);"><i class='bx bxs-file-plus ms-1' style="color: var(--fifa-gold);"></i> إرسال ملف مباشر (بدون خطاب)</h5>

                    <p class="text-muted fs-7 mb-3">استخدم هذا الخيار لإرسال ملف أو مستند من جهازك مباشرة لإدارة أخرى، بدون تعبئة نموذج الخطاب الرسمي أعلاه.</p>

                    <form action="/send_file_direct" method="post" enctype="multipart/form-data">

                        <div class="row g-3">

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">الإدارة المستلمة:</label>

                                <select name="receiver_id" class="form-select fs-7" required>

                                    <option value="" selected disabled>اختر الإدارة المستلمة...</option>

                                    {% for d in depts %}

                                        <option value="{{ d.id }}">{{ d.name }}</option>

                                    {% endfor %}

                                </select>

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">عنوان / وصف الملف:</label>

                                <input type="text" name="title" class="form-control fs-7" required placeholder="مثال: تقرير الصيانة الشهري">

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">الأهمية:</label>

                                <select name="priority" class="form-select fs-7">

                                    <option value="عادي">عادي</option>

                                    <option value="عاجل">عاجل</option>

                                    <option value="سري للغاية">سري للغاية</option>

                                </select>

                            </div>

                            <div class="col-md-6">

                                <label class="form-label fw-bold fs-7">الملف:</label>

                                <input type="file" name="file" class="form-control fs-7" required>

                            </div>

                            <div class="col-12 text-end mt-2">

                                <button type="submit" class="btn btn-outline-dark px-4 py-2 fw-bold">

                                    <i class='bx bx-send ms-1'></i> إرسال الملف

                                </button>

                            </div>

                        </div>

                    </form>

                </div>

                {% endif %}

 

<div class="modern-card p-2 p-sm-3">

                    {% if current_page == 'archive' and is_admin and own_letters is not none %}

                        {% if own_letters or other_letters or own_monthly_letters or other_monthly_letters %}

                            {% if can_delete == 1 %}

                            <form id="bulkDeleteForm" action="/delete_selected_letters" method="post">

                                <input type="hidden" name="action_type" id="actionTypeInput" value="selected">

                                <div class="d-flex flex-wrap justify-content-between align-items-center bg-light p-2 rounded mb-3 gap-2 border">

                                    <div class="form-check m-0">

                                        <input class="form-check-input" type="checkbox" id="selectAllCheckbox" onclick="toggleSelectAll(this)">

                                        <label class="form-check-label fw-bold fs-7 text-dark" for="selectAllCheckbox">تحديد الكل</label>

                                    </div>

                                    <div class="d-flex gap-2">

                                        <button type="button" class="btn btn-sm btn-outline-danger fs-7" onclick="submitBulkDelete('selected')">

                                            <i class='bx bx-trash ms-1'></i>حذف الملفات المحددة

                                        </button>

                                        <button type="button" class="btn btn-sm btn-danger fs-7 fw-bold" onclick="submitBulkDelete('all')">

                                            <i class='bx bx-trash-alt ms-1'></i>حذف كل الأرشيف

                                        </button>

                                    </div>

                                </div>

                            {% endif %}

                            {% if own_monthly_letters %}

                            <div class="alert alert-light border mb-4">

                                <h6 class="fw-bold mb-2 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                    <i class='bx bxs-calendar-check' style="color: var(--fifa-gold);"></i> أرشيف إنجازات الشهر (أرشيفي الخاص)

                                    <span class="badge bg-warning text-dark">{{ own_monthly_letters|length }}</span>

                                </h6>

                                <div class="letters-list">

                                    {% for letter in own_monthly_letters %}{{ render_letter_item(letter) }}{% endfor %}

                                </div>

                            </div>

                            {% endif %}

                            <h6 class="fw-bold mb-2 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                <i class='bx bxs-folder-open' style="color: var(--fifa-gold);"></i> أرشيف الرفع الفوري

                                <span class="badge bg-success">{{ own_letters|length }}</span>

                            </h6>

                            <div class="letters-list mb-4">

                                {% if own_letters %}

                                    {% for letter in own_letters %}{{ render_letter_item(letter) }}{% endfor %}

                                {% else %}

                                    <div class="text-center py-3 text-muted"><p class="fs-8 m-0">لا توجد ملفات في أرشيفك الخاص.</p></div>

                                {% endif %}

                            </div>

                            {% if other_monthly_letters %}

                            <div class="alert alert-light border mb-4">

                                <h6 class="fw-bold mb-2 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                    <i class='bx bxs-calendar-check' style="color: var(--fifa-gold);"></i> أرشيف إنجازات الشهر (باقي الإدارات)

                                    <span class="badge bg-warning text-dark">{{ other_monthly_letters|length }}</span>

                                </h6>

                                <div class="letters-list">

                                    {% for letter in other_monthly_letters %}{{ render_letter_item(letter) }}{% endfor %}

                                </div>

                            </div>

                            {% endif %}

                            <h6 class="fw-bold mb-2 mt-3 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                <i class='bx bxs-buildings' style="color: var(--fifa-gold);"></i> أرشيف الرفع الفوري - باقي الإدارات

                                <span class="badge bg-secondary">{{ other_letters|length }}</span>

                            </h6>

                            <div class="letters-list">

                                {% if other_letters %}

                                    {% for letter in other_letters %}{{ render_letter_item(letter) }}{% endfor %}

                                {% else %}

                                    <div class="text-center py-3 text-muted"><p class="fs-8 m-0">لا توجد ملفات مؤرشفة لباقي الإدارات.</p></div>

                                {% endif %}

                            </div>

                            {% if can_delete == 1 %}

                            </form>

                            {% endif %}

                        {% else %}

                            <div class="text-center py-5 text-muted"><p class="fs-7">لا توجد خطابات حالياً.</p></div>

                        {% endif %}

                    {% else %}

                        {% if letters or monthly_letters %}

                            {% if current_page == 'archive' and can_delete == 1 %}

                            <form id="bulkDeleteForm" action="/delete_selected_letters" method="post">

                                <input type="hidden" name="action_type" id="actionTypeInput" value="selected">

                                <div class="d-flex flex-wrap justify-content-between align-items-center bg-light p-2 rounded mb-3 gap-2 border">

                                    <div class="form-check m-0">

                                        <input class="form-check-input" type="checkbox" id="selectAllCheckbox" onclick="toggleSelectAll(this)">

                                        <label class="form-check-label fw-bold fs-7 text-dark" for="selectAllCheckbox">تحديد الكل</label>

                                    </div>

                                    <div class="d-flex gap-2">

                                        <button type="button" class="btn btn-sm btn-outline-danger fs-7" onclick="submitBulkDelete('selected')">

                                            <i class='bx bx-trash ms-1'></i>حذف الملفات المحددة

                                        </button>

                                        <button type="button" class="btn btn-sm btn-danger fs-7 fw-bold" onclick="submitBulkDelete('all')">

                                            <i class='bx bx-trash-alt ms-1'></i>حذف كل الأرشيف

                                        </button>

                                    </div>

                                </div>

                            {% endif %}

                            {% if monthly_letters %}

                            <div class="alert alert-light border mb-4">

                                <h6 class="fw-bold mb-2 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                    <i class='bx bxs-calendar-check' style="color: var(--fifa-gold);"></i> أرشيف إنجازات الشهر

                                    <span class="badge bg-warning text-dark">{{ monthly_letters|length }}</span>

                                </h6>

                                <div class="letters-list">

                                    {% for letter in monthly_letters %}{{ render_letter_item(letter) }}{% endfor %}

                                </div>

                            </div>

                            {% endif %}

                            {% if letters %}

                            <h6 class="fw-bold mb-2 d-flex align-items-center gap-1" style="color: var(--fifa-green-primary);">

                                <i class='bx bxs-folder-open' style="color: var(--fifa-gold);"></i> أرشيف الرفع الفوري

                                <span class="badge bg-success">{{ letters|length }}</span>

                            </h6>

                            {% endif %}

                            <div class="letters-list">

                                {% for letter in letters %}{{ render_letter_item(letter) }}{% endfor %}

                            </div>

                            {% if current_page == 'archive' and can_delete == 1 %}

                            </form>

                            {% endif %}

                        {% else %}

                            <div class="text-center py-5 text-muted"><p class="fs-7">لا توجد خطابات حالياً.</p></div>

                        {% endif %}

                    {% endif %}

                </div>

            </div>

        </main>

    </div>

 

    <!-- نافذة معاينة الملفات الموحدة Modal -->

    <div class="modal fade" id="previewFileModal" tabindex="-1" aria-hidden="true">

      <div class="modal-dialog modal-xl modal-dialog-centered">

        <div class="modal-content">

          <div class="modal-header bg-dark text-white py-2">

            <h6 class="modal-title fw-bold" id="previewFileTitle">معاينة المستند</h6>

            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

          </div>

          <div class="modal-body p-0" style="height: 80vh; background: #525659;">

            <iframe id="previewFrame" src="" style="width:100%; height:100%; border:none;"></iframe>

          </div>

        </div>

      </div>

    </div>

 

    <!-- نافذة معاينة الخطاب نفسه (مقاس A4) قبل الطباعة أو التحميل -->

    <div class="modal fade" id="previewLetterModal" tabindex="-1" aria-hidden="true">

      <div class="modal-dialog modal-xl modal-dialog-centered">

        <div class="modal-content">

          <div class="modal-header bg-dark text-white py-2">

            <h6 class="modal-title fw-bold"><i class='bx bx-show ms-1'></i> معاينة الخطاب (مقاس A4)</h6>

            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

          </div>

          <div class="modal-body">

            <div id="previewLetterContainer"></div>

          </div>

          <div class="modal-footer">

            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">إغلاق</button>

            <button type="button" class="btn btn-danger fw-bold" onclick="downloadLetterPDF()"><i class='bx bxs-file-pdf ms-1'></i> تحميل PDF</button>

          </div>

        </div>

      </div>

    </div>

    <!-- ============ نافذة الرد السريع (منفصلة تماماً عن نموذج الخطاب الرسمي) ============ -->

    <div class="modal fade" id="quickReplyModal" tabindex="-1" aria-hidden="true">

      <div class="modal-dialog modal-lg modal-dialog-centered">

        <div class="modal-content">

          <div class="modal-header bg-dark text-white py-2">

            <h6 class="modal-title fw-bold"><i class='bx bx-reply ms-1'></i> رد سريع</h6>

            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

          </div>

          <form id="quickReplyForm" action="/reply_to_letter" method="post" enctype="multipart/form-data">

            <div class="modal-body">

                <input type="hidden" name="receiver_id" id="replyReceiverId" value="">

                <div class="mb-2">

                    <label class="form-label fw-bold fs-7">الرد إلى:</label>

                    <input type="text" id="replyToLabel" class="form-control fs-7" disabled>

                </div>

                <div class="mb-2">

                    <label class="form-label fw-bold fs-7">عنوان الرد:</label>

                    <input type="text" name="title" id="replyTitleInput" class="form-control fs-7" required>

                </div>

                <div class="mb-2">

                    <label class="form-label fw-bold fs-7">نص الرد:</label>

                    <textarea name="content" class="form-control fs-7" rows="6" required placeholder="اكتب ردك هنا..."></textarea>

                </div>

                <div class="mb-2">

                    <label class="form-label fw-bold fs-7">مرفق (اختياري):</label>

                    <input type="file" name="file" class="form-control fs-7">

                </div>

                <div class="mb-2">

                    <label class="form-label fw-bold fs-7">الأهمية:</label>

                    <select name="priority" class="form-select fs-7">

                        <option value="عادي">عادي</option>

                        <option value="عاجل">عاجل</option>

                        <option value="سري للغاية">سري للغاية</option>

                    </select>

                </div>

            </div>

            <div class="modal-footer">

                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">إلغاء</button>

                <button type="submit" class="btn btn-fifa-primary fw-bold"><i class='bx bx-send ms-1'></i> إرسال الرد</button>

            </div>

          </form>

        </div>

      </div>

    </div>

    <!-- ============ نافذة التوقيع الإلكتروني (تصوير بالكاميرا) ============ -->

    <div class="modal fade" id="signaturePadModal" tabindex="-1" aria-hidden="true">

      <div class="modal-dialog modal-dialog-centered">

        <div class="modal-content">

          <div class="modal-header bg-dark text-white py-2">

            <h6 class="modal-title fw-bold"><i class='bx bx-pen ms-1'></i> توقيع إلكتروني</h6>

            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

          </div>

          <div class="modal-body text-center">

            <input type="file" accept="image/*" capture="environment" id="signatureFileInput" style="display:none" onchange="handleSignatureFileSelected(event)">

            <input type="file" accept="image/*" id="signatureGalleryInput" style="display:none" onchange="handleSignatureFileSelected(event)">

            <canvas id="signatureProcessCanvas" style="display:none;"></canvas>

            <div id="signatureSavedArea" class="d-none">

                <p class="text-muted fs-8 mb-2">توقيعاتك المحفوظة (اضغط على أي توقيع لإدراجه في الخطاب) — الحد الأقصى 3 توقيعات:</p>

                <div id="signatureSavedGallery" class="d-flex flex-wrap gap-2 justify-content-center mb-2"></div>

                <p id="signatureSavedLimitMsg" class="fs-9 text-muted mb-3 d-none">وصلت للحد الأقصى، احذف توقيعاً لإضافة توقيع جديد.</p>

            </div>

            <div id="signatureCaptureArea">

                <p class="text-muted fs-7 mb-3">صوّر توقيعك بكاميرا الجوال أو اختر صورة له من المعرض — بنشيل خلفية الورقة تلقائياً ونبقي شكل التوقيع بس، وبعدها بتقدر تسحبه وتحطه بأي مكان في نموذج الخطاب.</p>

                <div class="d-flex flex-column flex-sm-row gap-2 justify-content-center">

                    <button type="button" class="btn btn-fifa-primary fw-bold px-4" onclick="document.getElementById('signatureFileInput').click()">

                        <i class='bx bx-camera fs-5 ms-1'></i> تصوير بالكاميرا

                    </button>

                    <button type="button" class="btn btn-outline-dark fw-bold px-4" onclick="document.getElementById('signatureGalleryInput').click()">

                        <i class='bx bx-images fs-5 ms-1'></i> اختيار من المعرض

                    </button>

                </div>

            </div>

            <div id="signaturePreviewArea" class="d-none">

                <p class="text-muted fs-8 mb-2">هذي معاينة التوقيع بعد إزالة الخلفية (المربعات الرمادية تعني شفافية):</p>

                <div style="border: 1px solid #d5e2d8; border-radius: 8px; padding: 8px; background-image: linear-gradient(45deg, #e9ecef 25%, transparent 25%), linear-gradient(-45deg, #e9ecef 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #e9ecef 75%), linear-gradient(-45deg, transparent 75%, #e9ecef 75%); background-size: 16px 16px; background-position: 0 0, 0 8px, 8px -8px, -8px 0px;">

                    <img id="signaturePreviewImg" src="" style="max-width: 100%; max-height: 220px; object-fit: contain;">

                </div>

                <div class="mt-3 text-start">

                    <label class="form-label fw-bold fs-8 mb-1">دقة إزالة الخلفية (لو لسه فيه بقايا خلفية أو التوقيع صار خفيف، حرّك المؤشر):</label>

                    <input type="range" class="form-range" id="signatureThresholdSlider" min="60" max="240" value="170" oninput="onSignatureThresholdChange(this.value)">

                </div>

                <div class="d-flex flex-column flex-sm-row gap-2 justify-content-center mt-2">

                    <button type="button" class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('signatureFileInput').click()">

                        <i class='bx bx-camera ms-1'></i> إعادة التصوير

                    </button>

                    <button type="button" class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('signatureGalleryInput').click()">

                        <i class='bx bx-images ms-1'></i> اختيار صورة أخرى من المعرض

                    </button>

                </div>

            </div>

          </div>

          <div class="modal-footer">

            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">إلغاء</button>

            <button type="button" id="insertSignatureBtn" class="btn btn-primary fw-bold d-none" onclick="applySignature()"><i class='bx bx-check ms-1'></i> إدراج التوقيع في الخطاب</button>

          </div>

        </div>

      </div>

    </div>

 

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

    <script>

        // دالة تنفيذ تنسيقات النص العامة

        function formatDoc(cmd, value = null) {

            document.execCommand(cmd, false, value);

            syncTextareaWithPaper();

        }

        // ==== دعم الصفحات المتعددة للخطاب ====

        var pageCounter = 1;

        function addNewPage(initialHTML) {

            var container = document.querySelector('.word-paper-container');

            if (!container) return;

            var pages = container.querySelectorAll('.word-paper');

            var lastPaper = pages[pages.length - 1];

            if (!lastPaper) return;

            pageCounter++;

            var clone = lastPaper.cloneNode(true);

            clone.removeAttribute('id');

            clone.classList.add('extra-page');

            var oldBadge = clone.querySelector('.page-number-badge');

            if (oldBadge) oldBadge.remove();

            var oldRemoveBtn = clone.querySelector('.remove-page-btn');

            if (oldRemoveBtn) oldRemoveBtn.remove();

            clone.querySelectorAll('input').forEach(function (inp) {

                inp.removeAttribute('id');

            });

            var bodyEl = clone.querySelector('.word-paper-body');

            if (bodyEl) {

                bodyEl.removeAttribute('id');

                bodyEl.setAttribute('contenteditable', 'true');

                bodyEl.innerHTML = initialHTML || '';

                bodyEl.oninput = syncTextareaWithPaper;

            }

            var badge = document.createElement('div');

            badge.className = 'page-number-badge';

            badge.innerText = 'صفحة ' + pageCounter;

            clone.appendChild(badge);

            container.appendChild(clone);

            renumberPages();

            updateRemovePageBtnVisibility();

            syncTextareaWithPaper();

            clone.scrollIntoView({ behavior: 'smooth', block: 'center' });

        }

        function removeLastPage() {

            var container = document.querySelector('.word-paper-container');

            if (!container) return;

            var extraPages = container.querySelectorAll('.word-paper.extra-page');

            if (extraPages.length === 0) return;

            var lastPage = extraPages[extraPages.length - 1];

            lastPage.remove();

            renumberPages();

            updateRemovePageBtnVisibility();

            syncTextareaWithPaper();

        }

        function updateRemovePageBtnVisibility() {

            var container = document.querySelector('.word-paper-container');

            var btn = document.getElementById('removePageBtnToolbar');

            if (!container || !btn) return;

            var extraPages = container.querySelectorAll('.word-paper.extra-page');

            btn.style.display = extraPages.length > 0 ? 'inline-flex' : 'none';

        }

        function renumberPages() {

            var extraPages = document.querySelectorAll('.word-paper.extra-page');

            pageCounter = 1;

            extraPages.forEach(function (p) {

                pageCounter++;

                var badge = p.querySelector('.page-number-badge');

                if (badge) badge.innerText = 'صفحة ' + pageCounter;

            });

        }

 

        // دالة تكبير وتصغير حجم الخط للنص المحدد فقط

        var currentPaperFontSize = 18;

        function changeFontSize(step) {

            var paperBody = document.getElementById('paperBodyText');

            if (!paperBody) return;

            var selection = window.getSelection();

            var range = (selection && selection.rangeCount) ? selection.getRangeAt(0) : null;

            var hasValidSelectionInPaper = range && paperBody.contains(range.commonAncestorContainer) && !range.collapsed;

            if (hasValidSelectionInPaper) {

                // تحديد الحجم الحالي التقريبي للنص المحدد

                var refNode = range.startContainer;

                var refElement = (refNode.nodeType === 3) ? refNode.parentElement : refNode;

                var currentSize = currentPaperFontSize;

                if (refElement) {

                    var computed = parseInt(window.getComputedStyle(refElement).fontSize);

                    if (!isNaN(computed)) currentSize = computed;

                }

                var newSize = currentSize + (step * 2);

                if (newSize < 10) newSize = 10;

                if (newSize > 72) newSize = 72;

                // تقنية موثوقة: نغلّف التحديد أولاً بعنصر <font size="7"> عبر execCommand

                // (يتعامل صح مع أي تحديد معقّد يمتد على أكثر من سطر/عنصر)، ثم نحوّله لحجم بالبكسل الفعلي

                document.execCommand('fontSize', false, '7');

                var fontElements = paperBody.querySelectorAll('font[size="7"]');

                fontElements.forEach(function (el) {

                    el.removeAttribute('size');

                    el.style.fontSize = newSize + 'px';

                });

                document.getElementById('currentFontSizeLabel').innerText = newSize + 'px';

            } else {

                // لا يوجد تحديد نص: يكبّر/يصغّر حاوية الورقة كاملة، ويعمل مهما تكررت الضغطات

                currentPaperFontSize += (step * 2);

                if (currentPaperFontSize < 10) currentPaperFontSize = 10;

                if (currentPaperFontSize > 72) currentPaperFontSize = 72;

                paperBody.style.fontSize = currentPaperFontSize + 'px';

                document.getElementById('currentFontSizeLabel').innerText = currentPaperFontSize + 'px';

            }

            syncTextareaWithPaper();

        }

 

        // دالة تغيير نوع الخط للنص المحدد

        function changeFontFamily(fontFamily) {

            var selection = window.getSelection();

            if (!selection.rangeCount) return;

 

            var range = selection.getRangeAt(0);

            var paperBody = document.getElementById('paperBodyText');

 

            if (range.collapsed) {

                paperBody.style.fontFamily = fontFamily;

            } else {

                var span = document.createElement('span');

                span.style.fontFamily = fontFamily;

                span.appendChild(range.extractContents());

                range.insertNode(span);

            }

            syncTextareaWithPaper();

        }

        // ==== التوقيع الإلكتروني (تصوير بالكاميرا + إزالة الخلفية تلقائياً + حفظ حتى 3 توقيعات على حساب الإدارة بالسيرفر + سحب وتحريك على الورقة) ====

        var SIG_MAX_SAVED = 3;

        var sigOriginalImage = null;

        var sigCapturedDataUrl = null;

        var sigSavedList = []; // كاش التواقيع المحفوظة بالسيرفر [{id, signature_data}, ...]

        var sigSelectedSavedId = null; // معرّف التوقيع المحفوظ المُختار حالياً من المعرض (لو موجود)، حتى لا يُعاد حفظه من جديد

        function fetchSavedSignatures(callback) {

            fetch('/api/get_signatures', { credentials: 'same-origin' })

                .then(function (r) { return r.json(); })

                .then(function (data) {

                    sigSavedList = data.signatures || [];

                    callback(sigSavedList);

                })

                .catch(function () { callback([]); });

        }

        function renderSavedSignatureGallery() {

            var gallery = document.getElementById('signatureSavedGallery');

            var savedArea = document.getElementById('signatureSavedArea');

            var limitMsg = document.getElementById('signatureSavedLimitMsg');

            gallery.innerHTML = '';

            if (!sigSavedList.length) {

                savedArea.classList.add('d-none');

                return;

            }

            savedArea.classList.remove('d-none');

            limitMsg.classList.toggle('d-none', sigSavedList.length < SIG_MAX_SAVED);

            sigSavedList.forEach(function (sig) {

                var thumbWrap = document.createElement('div');

                thumbWrap.style.cssText = 'position:relative; width:110px; border:2px solid ' +

                    (sig.id === sigSelectedSavedId ? '#0d6efd' : '#d5e2d8') +

                    '; border-radius:8px; padding:6px; cursor:pointer; background-image: linear-gradient(45deg, #e9ecef 25%, transparent 25%), linear-gradient(-45deg, #e9ecef 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #e9ecef 75%), linear-gradient(-45deg, transparent 75%, #e9ecef 75%); background-size: 16px 16px; background-position: 0 0, 0 8px, 8px -8px, -8px 0px;';

                thumbWrap.onclick = function () { selectSavedSignature(sig.id); };

                var img = document.createElement('img');

                img.src = sig.signature_data;

                img.style.cssText = 'width:100%; max-height:80px; object-fit:contain; display:block; pointer-events:none;';

                thumbWrap.appendChild(img);

                var delBtn = document.createElement('button');

                delBtn.type = 'button';

                delBtn.innerHTML = '&times;';

                delBtn.title = 'حذف هذا التوقيع';

                delBtn.style.cssText = 'position:absolute; top:-8px; left:-8px; width:20px; height:20px; border-radius:50%; border:none; background:#dc3545; color:#fff; font-weight:bold; line-height:1; cursor:pointer;';

                delBtn.onclick = function (e) {

                    e.stopPropagation();

                    deleteSavedSignature(sig.id);

                };

                thumbWrap.appendChild(delBtn);

                gallery.appendChild(thumbWrap);

            });

        }

        function selectSavedSignature(id) {

            var sig = sigSavedList.find(function (s) { return s.id === id; });

            if (!sig) return;

            sigSelectedSavedId = id;

            sigCapturedDataUrl = sig.signature_data;

            renderSavedSignatureGallery();

            document.getElementById('insertSignatureBtn').classList.remove('d-none');

        }

        function saveSignatureForReuse(dataUrl) {

            var formData = new FormData();

            formData.append('signature_data', dataUrl);

            fetch('/api/save_signature', { method: 'POST', body: formData, credentials: 'same-origin' })

                .then(function (r) { return r.json(); })

                .then(function (data) {

                    if (data.success) {

                        sigSavedList.push({ id: data.id, signature_data: dataUrl });

                        sigSelectedSavedId = data.id;

                        renderSavedSignatureGallery();

                    }

                })

                .catch(function () {});

        }

        function deleteSavedSignature(id) {

            fetch('/api/delete_signature/' + id, { method: 'POST', credentials: 'same-origin' })

                .then(function (r) { return r.json(); })

                .then(function (data) {

                    if (data.success) {

                        sigSavedList = sigSavedList.filter(function (s) { return s.id !== id; });

                        if (sigSelectedSavedId === id) {

                            sigSelectedSavedId = null;

                            sigCapturedDataUrl = null;

                            document.getElementById('insertSignatureBtn').classList.add('d-none');

                        }

                        renderSavedSignatureGallery();

                    }

                })

                .catch(function () {});

        }

        function handleSignatureFileSelected(event) {

            var file = event.target.files && event.target.files[0];

            if (!file) return;

            var reader = new FileReader();

            reader.onload = function (e) {

                var img = new Image();

                img.onload = function () {

                    sigOriginalImage = img;

                    sigSelectedSavedId = null; // توقيع جديد يُلتقط الآن، مو مختار من التواقيع المحفوظة

                    document.getElementById('signatureSavedArea').classList.add('d-none');

                    document.getElementById('signatureCaptureArea').classList.add('d-none');

                    document.getElementById('signaturePreviewArea').classList.remove('d-none');

                    document.getElementById('insertSignatureBtn').classList.remove('d-none');

                    var slider = document.getElementById('signatureThresholdSlider');

                    processSignatureBackground(slider ? slider.value : 170);

                };

                img.src = e.target.result;

            };

            reader.readAsDataURL(file);

            event.target.value = '';

        }

        // يشيل خلفية الورقة البيضاء/الفاتحة من الصورة تلقائياً ويبقي فقط أثر القلم (شكل التوقيع)

        // بجعل البكسلات الفاتحة شفافة بالكامل، والبكسلات الداكنة (الحبر) معتمة بلونها الأصلي.

        function processSignatureBackground(threshold) {

            if (!sigOriginalImage) return;

            threshold = parseInt(threshold, 10) || 170;

            var canvas = document.getElementById('signatureProcessCanvas');

            var maxDim = 900;

            var w = sigOriginalImage.naturalWidth || sigOriginalImage.width;

            var h = sigOriginalImage.naturalHeight || sigOriginalImage.height;

            if (Math.max(w, h) > maxDim) {

                var scale = maxDim / Math.max(w, h);

                w = Math.round(w * scale);

                h = Math.round(h * scale);

            }

            canvas.width = w;

            canvas.height = h;

            var ctx = canvas.getContext('2d');

            ctx.clearRect(0, 0, w, h);

            ctx.drawImage(sigOriginalImage, 0, 0, w, h);

            var imgData = ctx.getImageData(0, 0, w, h);

            var d = imgData.data;

            var softness = 35; // نطاق تدرّج ناعم حول الحد الفاصل لتفادي حواف مسننة

            for (var i = 0; i < d.length; i += 4) {

                var r = d[i], g = d[i + 1], b = d[i + 2];

                var lum = 0.299 * r + 0.587 * g + 0.114 * b;

                if (lum >= threshold) {

                    d[i + 3] = 0; // خلفية فاتحة -> شفافة بالكامل

                } else if (lum >= threshold - softness) {

                    // تدرّج ناعم بين الحبر والخلفية لتفادي حواف خشنة

                    var ratio = (threshold - lum) / softness;

                    d[i + 3] = Math.round(255 * ratio);

                } else {

                    d[i + 3] = 255; // حبر داكن -> معتم بالكامل

                }

            }

            ctx.putImageData(imgData, 0, 0);

            sigCapturedDataUrl = canvas.toDataURL('image/png');

            document.getElementById('signaturePreviewImg').src = sigCapturedDataUrl;

        }

        function onSignatureThresholdChange(value) {

            processSignatureBackground(value);

        }

        function openSignaturePad() {

            sigOriginalImage = null;

            sigCapturedDataUrl = null;

            sigSelectedSavedId = null;

            document.getElementById('signatureSavedArea').classList.add('d-none');

            document.getElementById('signatureCaptureArea').classList.remove('d-none');

            document.getElementById('signaturePreviewArea').classList.add('d-none');

            document.getElementById('insertSignatureBtn').classList.add('d-none');

            var slider = document.getElementById('signatureThresholdSlider');

            if (slider) slider.value = 170;

            var modalEl = document.getElementById('signaturePadModal');

            var modal = new bootstrap.Modal(modalEl);

            modal.show();

            // منطقة التصوير/الاختيار تظهر دائماً، وتُضاف فوقها معرض التواقيع المحفوظة (حتى 3) إن وُجدت

            fetchSavedSignatures(function () {

                renderSavedSignatureGallery();

            });

        }

        function applySignature() {

            if (!sigCapturedDataUrl) {

                alert('الرجاء تصوير أو اختيار صورة التوقيع أولاً.');

                return;

            }

            var bodies = document.querySelectorAll('.word-paper-container .word-paper-body');

            var targetBody = bodies.length ? bodies[bodies.length - 1] : document.getElementById('paperBodyText');

            if (!targetBody) return;

            if (getComputedStyle(targetBody).position === 'static') {

                targetBody.style.position = 'relative';

            }

            var wrapper = document.createElement('div');

            wrapper.className = 'draggable-signature';

            wrapper.setAttribute('contenteditable', 'false');

            wrapper.style.position = 'absolute';

            wrapper.style.top = '40px';

            wrapper.style.right = '40px';

            wrapper.style.width = '160px';

            wrapper.style.cursor = 'grab';

            wrapper.style.zIndex = '50';

            wrapper.style.userSelect = 'none';

            var img = document.createElement('img');

            img.src = sigCapturedDataUrl;

            img.style.width = '100%';

            img.style.display = 'block';

            img.style.pointerEvents = 'none';

            wrapper.appendChild(img);

            var removeBtn = document.createElement('button');

            removeBtn.type = 'button';

            removeBtn.className = 'sig-remove-btn';

            removeBtn.innerHTML = '&times;';

            removeBtn.title = 'حذف التوقيع';

            removeBtn.style.cssText = 'position:absolute; top:-10px; left:-10px; width:22px; height:22px; border-radius:50%; border:none; background:#dc3545; color:#fff; font-weight:bold; line-height:1; cursor:pointer;';

            removeBtn.onclick = function (e) {

                e.stopPropagation();

                wrapper.remove();

                syncTextareaWithPaper();

            };

            wrapper.appendChild(removeBtn);

            makeSignatureDraggable(wrapper);

            targetBody.appendChild(wrapper);

            // يحفظ التوقيع للاستخدام لاحقاً فقط لو كان التقاطاً جديداً (مو توقيع مُختار أصلاً من المحفوظات)

            // حتى لا يتكرر نفس التوقيع أكثر من مرة ضمن الحد الأقصى (3 توقيعات)

            if (!sigSelectedSavedId) {

                saveSignatureForReuse(sigCapturedDataUrl);

            }

            var modalEl = document.getElementById('signaturePadModal');

            var modal = bootstrap.Modal.getInstance(modalEl);

            if (modal) modal.hide();

            syncTextareaWithPaper();

        }

        function makeSignatureDraggable(el) {

            var dragging = false, startX = 0, startY = 0, startLeft = 0, startTop = 0, zoomFactor = 1;

            function getParentRect() { return el.offsetParent.getBoundingClientRect(); }

            // على الجوال يتم تصغير .word-paper-container عبر خاصية CSS "zoom" (fitWordPaperToScreen)

            // وهذه الخاصية تجعل كل قياسات getBoundingClientRect تُرجع القيم بعد التصغير، بينما القيم

            // التي نكتبها في style.left/top تُقرأ *قبل* التصغير ثم يتم ضربها بالـ zoom مرة أخرى عند الرسم.

            // فبدون تعويض هذا الفارق، أي سحب (خصوصاً نحو اليسار حيث تتحرك المسافة الأكبر) يضاعف

            // الإزاحة الفعلية على الجوال ويدفع التوقيع لمكان بعيد يكبّر عرض/حجم الصفحة (overflow-x: auto).

            function getZoomFactor() {

                var factor = 1;

                var node = el.parentElement;

                while (node) {

                    var z = node.style && node.style.zoom;

                    if (z) {

                        var zn = parseFloat(z);

                        if (!isNaN(zn) && zn > 0) factor *= zn;

                    }

                    node = node.parentElement;

                }

                return factor || 1;

            }

            function pointerDown(e) {

                e.preventDefault();

                e.stopPropagation();

                dragging = true;

                el.style.cursor = 'grabbing';

                zoomFactor = getZoomFactor();

                var point = e.touches ? e.touches[0] : e;

                startX = point.clientX;

                startY = point.clientY;

                var rect = el.getBoundingClientRect();

                var parentRect = getParentRect();

                startLeft = (rect.left - parentRect.left) / zoomFactor;

                startTop = (rect.top - parentRect.top) / zoomFactor;

                el.style.right = '';

                el.style.left = startLeft + 'px';

                el.style.top = startTop + 'px';

            }

            function pointerMove(e) {

                if (!dragging) return;

                e.preventDefault();

                var point = e.touches ? e.touches[0] : e;

                var dx = (point.clientX - startX) / zoomFactor;

                var dy = (point.clientY - startY) / zoomFactor;

                var newLeft = startLeft + dx;

                var newTop = startTop + dy;

                // تثبيت التوقيع داخل حدود الورقة حتى لو تم سحبه بعيداً بسرعة، لمنع تمدد/تكسر حجم الصفحة

                var parentRect = getParentRect();

                var parentWidth = parentRect.width / zoomFactor;

                var parentHeight = parentRect.height / zoomFactor;

                var elWidth = el.getBoundingClientRect().width / zoomFactor;

                var elHeight = el.getBoundingClientRect().height / zoomFactor;

                if (newLeft < 0) newLeft = 0;

                if (newLeft > parentWidth - elWidth) newLeft = Math.max(0, parentWidth - elWidth);

                if (newTop < 0) newTop = 0;

                if (newTop > parentHeight - elHeight) newTop = Math.max(0, parentHeight - elHeight);

                el.style.left = newLeft + 'px';

                el.style.top = newTop + 'px';

            }

            function pointerUp() {

                if (!dragging) return;

                dragging = false;

                el.style.cursor = 'grab';

                syncTextareaWithPaper();

            }

            el.addEventListener('mousedown', pointerDown);

            window.addEventListener('mousemove', pointerMove);

            window.addEventListener('mouseup', pointerUp);

            el.addEventListener('touchstart', pointerDown, { passive: false });

            window.addEventListener('touchmove', pointerMove, { passive: false });

            window.addEventListener('touchend', pointerUp);

        }

        function syncTextareaWithPaper() {

            var container = document.querySelector('.word-paper-container');

            var textarea = document.getElementById('letterContentInput');

            if (!container || !textarea) return;

            var bodies = container.querySelectorAll('.word-paper-body');

            var parts = [];

            bodies.forEach(function (b) { parts.push(b.innerHTML); });

            textarea.value = parts.join('<!--PAGE_BREAK-->');

        }

 

        function syncPaperWithTextarea(val) {

            // إزالة أي صفحات إضافية سابقة قبل إعادة البناء

            document.querySelectorAll('.word-paper.extra-page').forEach(function (p) { p.remove(); });

            pageCounter = 1;

            updateRemovePageBtnVisibility();

            var paperBody = document.getElementById('paperBodyText');

            if (!paperBody) return;

            if (!val || val.trim() === '') {

                paperBody.innerText = "أدخل نص الخطاب...";

                return;

            }

            var pages = val.split('<!--PAGE_BREAK-->');

            var firstPageVal = pages[0];

            if (/<[a-z][\\s\\S]*>/i.test(firstPageVal)) {

                // القيمة تحتوي وسوم HTML محفوظة مسبقاً (خطاب تم تحميله للتعديل)

                paperBody.innerHTML = firstPageVal;

                rebindDraggableSignatures(paperBody);

            } else {

                paperBody.innerText = firstPageVal;

            }

            for (var i = 1; i < pages.length; i++) {

                addNewPage(pages[i]);

            }

        }

        // إعادة تفعيل خاصية السحب على أي توقيعات موجودة مسبقاً في خطاب تم تحميله للتعديل

        function rebindDraggableSignatures(container) {

            var sigs = container.querySelectorAll('.draggable-signature');

            sigs.forEach(function (wrapper) {

                var removeBtn = wrapper.querySelector('button');

                if (removeBtn) {

                    removeBtn.className = 'sig-remove-btn';

                    removeBtn.style.display = '';

                    removeBtn.onclick = function (e) {

                        e.stopPropagation();

                        wrapper.remove();

                        syncTextareaWithPaper();

                    };

                }

                makeSignatureDraggable(wrapper);

            });

        }

 

        function previewFile(url, title) {

            document.getElementById('previewFileTitle').innerText = 'معاينة: ' + title;

            document.getElementById('previewFrame').src = url;

            var modal = new bootstrap.Modal(document.getElementById('previewFileModal'));

            modal.show();

        }

 

        // معاينة الخطاب الرسمي بمقاس A4 داخل نافذة منبثقة قبل الطباعة/التحميل (من ورقة/أوراق التحرير الحالية)

        function previewLetterPaper() {

            var sourcePages = document.querySelectorAll('.word-paper-container > .word-paper');

            var container = document.getElementById('previewLetterContainer');

            container.innerHTML = '';

            sourcePages.forEach(function (original, idx) {

                var clone = original.cloneNode(true);

                clone.removeAttribute('id');

                if (idx === 0) clone.id = 'previewOfficialPaper';

                clone.querySelectorAll('input').forEach(function (inp) {

                    var span = document.createElement('span');

                    span.innerText = inp.value || '';

                    span.style.fontWeight = 'bold';

                    inp.parentNode.replaceChild(span, inp);

                });

                var bodyEl = clone.querySelector('.word-paper-body');

                if (bodyEl) bodyEl.removeAttribute('contenteditable');

                var removeBtn = clone.querySelector('.remove-page-btn');

                if (removeBtn) removeBtn.remove();

                clone.style.marginBottom = '20px';

                container.appendChild(clone);

            });

 

            var modalEl = document.getElementById('previewLetterModal');

            var modal = new bootstrap.Modal(modalEl);

            modal.show();

 

            // ضبط مقياس العرض بحيث تظهر كل صفحة كاملة داخل النافذة (بدون تغيير مقاسها الحقيقي A4)

            setTimeout(function () {

                var modalBodyWidth = modalEl.querySelector('.modal-body').clientWidth - 20;

                container.querySelectorAll('.word-paper').forEach(function (clone) {

                    var paperWidthPx = clone.getBoundingClientRect().width;

                    var scale = Math.min(1, modalBodyWidth / paperWidthPx);

                    clone.style.transform = 'scale(' + scale + ')';

                    clone.style.transformOrigin = 'top center';

                    clone.style.marginBottom = (paperWidthPx * (scale - 1) + 20) + 'px';

                });

            }, 150);

        }

 

        // معاينة خطاب مؤرشف (من الوارد أو الصادر) بنفس شكل الورقة الرسمية A4، مع دعم كل صفحاته

        function previewArchivedLetter(btn) {

            var title = btn.getAttribute('data-title') || '';

            var contentId = btn.getAttribute('data-content-id');

            var dateVal = btn.getAttribute('data-date') || '';

            var numberVal = btn.getAttribute('data-number') || '';

 

            var textElem = contentId ? document.getElementById(contentId) : null;

            var contentHTML = textElem ? textElem.innerHTML : '';

            var pagesHTML = contentHTML ? contentHTML.split('<!--PAGE_BREAK-->') : [];

            if (pagesHTML.length === 0) pagesHTML = [title];

 

            var original = document.getElementById('officialPaper');

            if (!original) { return; }

            var container = document.getElementById('previewLetterContainer');

            container.innerHTML = '';

            pagesHTML.forEach(function (pageHtml, idx) {

                var clone = original.cloneNode(true);

                clone.removeAttribute('id');

                if (idx === 0) clone.id = 'previewOfficialPaper';

                clone.querySelectorAll('input').forEach(function (inp) {

                    var span = document.createElement('span');

                    if (inp.id.indexOf('NumInput') !== -1) {

                        span.innerText = numberVal;

                    } else if (inp.id.indexOf('DateInput') !== -1) {

                        span.innerText = dateVal;

                    } else {

                        span.innerText = '';

                    }

                    span.style.fontWeight = 'bold';

                    inp.parentNode.replaceChild(span, inp);

                });

                var bodyEl = clone.querySelector('.word-paper-body');

                if (bodyEl) {

                    bodyEl.removeAttribute('contenteditable');

                    bodyEl.innerHTML = pageHtml || (idx === 0 ? title : '');

                }

                var removeBtn = clone.querySelector('.remove-page-btn');

                if (removeBtn) removeBtn.remove();

                clone.style.marginBottom = '20px';

                container.appendChild(clone);

            });

 

            var modalEl = document.getElementById('previewLetterModal');

            var modal = new bootstrap.Modal(modalEl);

            modal.show();

 

            setTimeout(function () {

                var modalBodyWidth = modalEl.querySelector('.modal-body').clientWidth - 20;

                container.querySelectorAll('.word-paper').forEach(function (clone) {

                    var paperWidthPx = clone.getBoundingClientRect().width;

                    var scale = Math.min(1, modalBodyWidth / paperWidthPx);

                    clone.style.transform = 'scale(' + scale + ')';

                    clone.style.transformOrigin = 'top center';

                    clone.style.marginBottom = (paperWidthPx * (scale - 1) + 20) + 'px';

                });

            }, 150);

        }

        function updateNavbarHeightVar() {

    var nav = document.querySelector('.top-navbar');

    if (nav) {

        document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px');

    }

}

updateNavbarHeightVar();

window.addEventListener('load', updateNavbarHeightVar);

window.addEventListener('resize', updateNavbarHeightVar);

 function toggleSidebar() {

    document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

    document.getElementById('mobileOverlay').classList.toggle('active');

}

        // تصغير ورقة الخطاب تلقائياً لتناسب عرض شاشة الجوال بدون قص أو سكرول أفقي

        function fitWordPaperToScreen() {

            var container = document.querySelector('.word-paper-container');

            if (!container) return;

            container.style.zoom = '';

            container.style.transform = '';

            container.style.marginBottom = '';

            if (window.innerWidth <= 860) {

                var wrapperWidth = container.parentElement.clientWidth;

                var naturalWidth = container.scrollWidth;

                var scale = Math.min(1, (wrapperWidth / naturalWidth) * 0.90);

                container.style.zoom = scale;

            }

        }

        function initPaperFit() {

            fitWordPaperToScreen();

            if (document.fonts && document.fonts.ready) {

                document.fonts.ready.then(fitWordPaperToScreen);

            }

            setTimeout(fitWordPaperToScreen, 500);

            setTimeout(fitWordPaperToScreen, 1200);

        }

        window.addEventListener('load', initPaperFit);

        window.addEventListener('resize', fitWordPaperToScreen);

(function() {

    var touchStartX = 0;

    var touchStartY = 0;

    var edgeThreshold = 25;

    var swipeThreshold = 60;

    document.addEventListener('touchstart', function(e) {

        touchStartX = e.touches[0].clientX;

        touchStartY = e.touches[0].clientY;

    }, { passive: true });

    document.addEventListener('touchend', function(e) {

        if (window.innerWidth > 991.98) return;

        var sidebarEl = document.getElementById('sidebarMenu');

        if (!sidebarEl) return;

        var touchEndX = e.changedTouches[0].clientX;

        var touchEndY = e.changedTouches[0].clientY;

        var deltaX = touchEndX - touchStartX;

        var deltaY = touchEndY - touchStartY;

        if (Math.abs(deltaY) > 60) return;

        var isOpen = sidebarEl.classList.contains('show-sidebar');

        if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

            toggleSidebar();

        }

        else if (isOpen && deltaX > swipeThreshold) {

            toggleSidebar();

        }

    }, { passive: true });

})();

 

        function toggleSelectAll(source) {

            checkboxes = document.querySelectorAll('.letter-checkbox');

            for(var i=0, n=checkboxes.length; i<n; i++) {

                checkboxes[i].checked = source.checked;

            }

        }

 

        function updateReceiverTitle(selectElem) {

            var selectedOption = selectElem.options[selectElem.selectedIndex];

            var deptName = selectedOption.getAttribute('data-name');

            if (deptName) {

                var el = document.getElementById('paperSalutationInput');

                if (el) el.value = deptName;

            }

        }

 

        function openQuickReply(btn) {

            var senderId = btn.getAttribute('data-sender-id') || '';

            var senderName = btn.getAttribute('data-sender-name') || '';

            var title = btn.getAttribute('data-title') || '';

            document.getElementById('replyReceiverId').value = senderId;

            document.getElementById('replyToLabel').value = senderName;

            document.getElementById('replyTitleInput').value = (title.indexOf('رد:') === 0) ? title : ('رد: ' + title);

            var modal = new bootstrap.Modal(document.getElementById('quickReplyModal'));

            modal.show();

        }

        function loadLetterToEditor(btn) {

            var id = btn.getAttribute('data-id');

            var title = btn.getAttribute('data-title') || '';

            var senderId = btn.getAttribute('data-sender-id') || '';

            var receiverId = btn.getAttribute('data-receiver-id') || '';

            var priority = btn.getAttribute('data-priority') || 'عادي';

            var page = btn.getAttribute('data-page');

            var letterNumber = btn.getAttribute('data-letter-number') || '';

            var senderName = btn.getAttribute('data-sender-name') || '';

            var letterDate = btn.getAttribute('data-date') || '';

 

            var textElem = document.getElementById('letter-text-' + id);

            var contentHTML = textElem ? textElem.innerHTML : '';

 

            document.getElementById('letterTitleInput').value = title;

            document.getElementById('letterPriority').value = priority;

 

            var receiverSelect = document.getElementById('receiverSelect');

            var editIdInput = document.getElementById('editLetterId');

            var isReply = false;

 

            if (page === 'outbox') {

                editIdInput.value = id;

                if (receiverId && receiverSelect) receiverSelect.value = receiverId;

                if (letterNumber) {

                    document.getElementById('paperLetterNumInput').value = letterNumber;

                }

            } else {

                isReply = true;

                editIdInput.value = '';

                if (senderId && receiverSelect) receiverSelect.value = senderId;

                if (title && title.indexOf('رد:') !== 0) {

                    document.getElementById('letterTitleInput').value = 'رد: ' + title;

                }

                // صفحة أولى فاضية لكتابة نص الرد + صفحة ثانية فيها الخطاب المستقبل الأصلي كاملاً كمرجع

                contentHTML = '' + '<!--PAGE_BREAK-->' + contentHTML;

            }

 

            document.getElementById('letterContentInput').value = contentHTML;

            syncPaperWithTextarea(contentHTML);

 

            if (isReply) {

                setTimeout(function () {

                    var pb = document.getElementById('paperBodyText');

                    if (pb) {

                        pb.focus();

                        var range = document.createRange();

                        var sel = window.getSelection();

                        range.setStart(pb, 0);

                        range.collapse(true);

                        sel.removeAllRanges();

                        sel.addRange(range);

                    }

                }, 250);

            }

 

            var paperBody = document.getElementById('officialPaper');

            if (paperBody) {

                paperBody.scrollIntoView({ behavior: 'smooth' });

            }

        }

 

function downloadLetterPDF() {

    var previewContainer = document.getElementById('previewLetterContainer');

    var sourcePages;

    if (previewContainer && previewContainer.querySelector('.word-paper')) {

        sourcePages = previewContainer.querySelectorAll('.word-paper');

    } else {

        sourcePages = document.querySelectorAll('.word-paper-container > .word-paper');

    }

    if (!sourcePages || sourcePages.length === 0) {

        alert("لا توجد ورقة خطاب نشطة للتحميل!");

        return;

    }

    var styles = '';

    document.querySelectorAll('style, link[rel="stylesheet"]').forEach(function (styleNode) {

        styles += styleNode.outerHTML;

    });

    var oldFrame = document.getElementById('hiddenPrintFrame');

    if (oldFrame) oldFrame.remove();

    var printFrame = document.createElement('iframe');

    printFrame.id = 'hiddenPrintFrame';

    printFrame.style.position = 'fixed';

    printFrame.style.right = '-10000px';

    printFrame.style.bottom = '-10000px';

    printFrame.style.width = '0';

    printFrame.style.height = '0';

    printFrame.style.border = '0';

    document.body.appendChild(printFrame);

    var doc = printFrame.contentWindow.document;

    doc.open();

    doc.write('<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><title>طباعة الخطاب الرسمي</title>' + styles + '<style>' +

        '* { box-sizing: border-box; } ' +

        'html, body { margin:0 !important; padding:0 !important; width:210mm; background:#fff !important; } ' +

        '@page { size: A4; margin: 0mm; } ' +

        '.word-paper-container { margin:0 !important; padding:0 !important; overflow:visible !important; display:block !important; } ' +

        '.word-paper { width:210mm !important; height:297mm !important; min-height:297mm !important; max-height:297mm !important; max-width:210mm !important; margin:0 auto !important; padding:18mm 20mm !important; border:none !important; box-shadow:none !important; border-radius:0 !important; zoom:1 !important; transform:none !important; page-break-after: always; page-break-inside:avoid; overflow:hidden; } ' +

        '.word-paper:last-child { page-break-after: auto; } ' +

        '.word-paper-body { border:none !important; background:transparent !important; padding:0 !important; outline:none !important; } ' +

        '.page-number-badge, .remove-page-btn { display:none !important; } ' +

        'body * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }' +

        '</style></head><body></body></html>');

    doc.close();

    var wrapper = doc.createElement('div');

    wrapper.id = 'printAreaPaper';

    wrapper.className = 'word-paper-container';

    sourcePages.forEach(function (source) {

        var clone = source.cloneNode(true);

        clone.removeAttribute('id');

        clone.style.zoom = '1';

        clone.style.transform = 'none';

        clone.style.marginBottom = '0';

        clone.querySelectorAll('input').forEach(function (inp) {

            var span = document.createElement('span');

            span.innerText = inp.value || inp.getAttribute('value') || '';

            span.style.fontWeight = 'bold';

            inp.parentNode.replaceChild(span, inp);

        });

        var originalBody = source.querySelector('.word-paper-body');

        var cloneBody = clone.querySelector('.word-paper-body');

        if (originalBody && cloneBody) {

            cloneBody.removeAttribute('contenteditable');

            cloneBody.innerHTML = originalBody.innerHTML;

        }

        var removeBtn = clone.querySelector('.remove-page-btn');

        if (removeBtn) removeBtn.remove();

        wrapper.appendChild(clone);

    });

    doc.body.appendChild(wrapper);

    setTimeout(function () {

        printFrame.contentWindow.focus();

        printFrame.contentWindow.print();

    }, 500);

}

        function submitBulkDelete(type) {

            document.getElementById('actionTypeInput').value = type;

            if (type === 'all') {

                if (confirm('تحذير شديد: هل أنت متأكد من حذف كافة الملفات الموجودة في الأرشيف نهائياً؟')) {

                    document.getElementById('bulkDeleteForm').submit();

                }

            } else {

                var checkedCount = document.querySelectorAll('.letter-checkbox:checked').length;

                if (checkedCount === 0) {

                    alert('الرجاء تحديد ملف واحد على الأقل للحذف.');

                    return;

                }

                if (confirm('هل أنت متأكد من حذف الملفات المحددة؟')) {

                    document.getElementById('bulkDeleteForm').submit();

                }

            }

        }

 

        document.getElementById('letterSendForm').addEventListener('submit', function() {

            var numEl = document.getElementById('paperLetterNumInput');

            var hiddenEl = document.getElementById('hiddenLetterNumberInput');

            if (numEl && hiddenEl) {

                hiddenEl.value = numEl.value;

            }

        });

        window.addEventListener('DOMContentLoaded', function() {

            syncTextareaWithPaper();

        });

        {% if current_page == 'inbox' %}

        // فحص دوري لوجود خطابات جديدة وصلت أثناء تصفح صفحة الوارد

        if ('Notification' in window && Notification.permission === 'default') {

            Notification.requestPermission();

        }

        function checkForNewLetters() {

            fetch('/api/unread_count').then(function (r) { return r.json(); }).then(function (data) {

                if (data.count > 0) {

                    var toastEl = document.getElementById('newLetterToast');

                    if (toastEl) toastEl.classList.remove('d-none');

                    var badgeEl = document.getElementById('inboxUnreadBadge');

                    if (badgeEl) { badgeEl.innerText = data.count; badgeEl.classList.remove('d-none'); }

                    if ('Notification' in window && Notification.permission === 'granted') {

                        new Notification('نظام أرشفة نادي فيفا', { body: 'وصلك خطاب جديد بالصندوق الوارد' });

                    }

                }

            }).catch(function () {});

        }

        setInterval(checkForNewLetters, 20000);

        {% endif %}

    </script>

</body>

</html>

'''

 

@app.route('/api/unread_count')

def api_unread_count():

    if 'dept_id' not in session:

        return {'count': 0}

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) as count FROM letters WHERE receiver_id = %s AND is_read = 0', (session['dept_id'],))

    row = cursor.fetchone()

    cursor.close()

    conn.close()

    return {'count': row['count'] if row else 0}

MAX_SAVED_SIGNATURES_PER_DEPT = 3

@app.route('/api/get_signatures')

def api_get_signatures():

    if 'dept_id' not in session:

        return {'signatures': []}

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute(

        'SELECT id, signature_data FROM saved_signatures WHERE dept_id = %s ORDER BY id ASC',

        (session['dept_id'],)

    )

    rows = cursor.fetchall()

    cursor.close()

    conn.close()

    return {'signatures': [{'id': r['id'], 'signature_data': r['signature_data']} for r in rows]}

@app.route('/api/save_signature', methods=['POST'])

def api_save_signature():

    if 'dept_id' not in session:

        return {'success': False}, 401

    signature_data = request.form.get('signature_data', '')

    if not signature_data.startswith('data:image/'):

        return {'success': False, 'error': 'صيغة الصورة غير صالحة'}, 400

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) as count FROM saved_signatures WHERE dept_id = %s', (session['dept_id'],))

    current_count = cursor.fetchone()['count']

    if current_count >= MAX_SAVED_SIGNATURES_PER_DEPT:

        cursor.close()

        conn.close()

        return {'success': False, 'error': f'وصلت للحد الأقصى ({MAX_SAVED_SIGNATURES_PER_DEPT} توقيعات محفوظة). احذف واحداً منها أولاً لإضافة توقيع جديد.'}, 400

    cursor.execute(

        'INSERT INTO saved_signatures (dept_id, signature_data, created_at) VALUES (%s, %s, %s) RETURNING id',

        (session['dept_id'], signature_data, datetime.now().strftime('%Y-%m-%d %H:%M'))

    )

    new_id = cursor.fetchone()['id']

    conn.commit()

    cursor.close()

    conn.close()

    return {'success': True, 'id': new_id}

@app.route('/api/delete_signature/<int:signature_id>', methods=['POST'])

def api_delete_signature(signature_id):

    if 'dept_id' not in session:

        return {'success': False}, 401

    conn = get_db_connection()

    cursor = conn.cursor()

    # يتأكد إن التوقيع فعلاً يخص الإدارة الحالية قبل الحذف، حتى لا تقدر إدارة تحذف توقيع إدارة ثانية

    cursor.execute(

        'DELETE FROM saved_signatures WHERE id = %s AND dept_id = %s',

        (signature_id, session['dept_id'])

    )

    deleted = cursor.rowcount > 0

    conn.commit()

    cursor.close()

    conn.close()

    return {'success': deleted}

@app.route('/dashboard')

def dashboard():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    dept_id = session['dept_id']

    conn = get_db_connection()

    cursor = conn.cursor()

    

    cursor.execute('SELECT * FROM departments WHERE id = %s', (dept_id,))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_page_inbox'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول للصندوق الوارد."); window.location.href="/";</script>'''

    

    depts = get_allowed_receiver_depts(cursor, dept_id, current_dept['can_send_all'])

    next_letter_number = peek_next_letter_number(cursor)

    

    cursor.execute('''

        SELECT l.*, d.name as sender_name 

        FROM letters l 

        JOIN departments d ON l.sender_id = d.id 

        WHERE l.receiver_id = %s 

        ORDER BY l.id DESC

    ''', (dept_id,))

    letters = cursor.fetchall()

    cursor.execute('SELECT COUNT(*) as count FROM letters WHERE receiver_id = %s AND is_read = 0', (dept_id,))

    unread_count = cursor.fetchone()['count']

    cursor.execute('UPDATE letters SET is_read = 1 WHERE receiver_id = %s AND is_read = 0', (dept_id,))

    conn.commit()

    

    cursor.close()

    conn.close()

    

    return render_template_string(DASHBOARD_HTML, 

                                  page_title="الصندوق الوارد",

                                  current_page="inbox",

                                  letters=letters, 

                                  unread_count=unread_count,

                                  depts=depts, 

                                  dept_name=session['dept_name'],

                                  can_delete=current_dept['can_delete'],

                                  can_add_user=current_dept['can_add_user'],

                                  can_page_quick_upload=current_dept['can_page_quick_upload'],

                                  can_page_inbox=current_dept['can_page_inbox'],

                                  can_page_outbox=current_dept['can_page_outbox'],

                                  can_page_achievements=current_dept['can_page_achievements'],

                                  can_page_archive=current_dept['can_page_archive'],

                                  is_admin=is_admin,

                                  can_view_all_archive=current_dept['can_view_all_archive'],

                                  can_page_suggestions=current_dept['can_page_suggestions'],
                                  can_page_chat=current_dept['can_page_chat'],


                                  can_page_admin_tools=current_dept['can_page_admin_tools'],

                                  next_letter_number=next_letter_number,

                                  now=datetime.now())

 

@app.route('/outbox')

def outbox():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    dept_id = session['dept_id']

    conn = get_db_connection()

    cursor = conn.cursor()

    

    cursor.execute('SELECT * FROM departments WHERE id = %s', (dept_id,))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_page_outbox'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول للخطابات الصادرة."); window.location.href="/dashboard";</script>'''

    

    depts = get_allowed_receiver_depts(cursor, dept_id, current_dept['can_send_all'])

    next_letter_number = peek_next_letter_number(cursor)

    

    cursor.execute('''

        SELECT l.*, d.name as receiver_name 

        FROM letters l 

        JOIN departments d ON l.receiver_id = d.id 

        WHERE l.sender_id = %s 

        ORDER BY l.id DESC

    ''', (dept_id,))

    letters = cursor.fetchall()

    cursor.execute('SELECT COUNT(*) as count FROM letters WHERE receiver_id = %s AND is_read = 0', (dept_id,))

    unread_count = cursor.fetchone()['count']

    

    cursor.close()

    conn.close()

    

    return render_template_string(DASHBOARD_HTML, 

                                  page_title="الخطابات الصادرة",

                                  current_page="outbox",

                                  letters=letters, 

                                  unread_count=unread_count,

                                  depts=depts, 

                                  dept_name=session['dept_name'],

                                  can_delete=current_dept['can_delete'],

                                  can_add_user=current_dept['can_add_user'],

                                  can_page_quick_upload=current_dept['can_page_quick_upload'],

                                  can_page_inbox=current_dept['can_page_inbox'],

                                  can_page_outbox=current_dept['can_page_outbox'],

                                  can_page_achievements=current_dept['can_page_achievements'],

                                  can_page_archive=current_dept['can_page_archive'],

                                  is_admin=is_admin,

                                  can_view_all_archive=current_dept['can_view_all_archive'],

                                  can_page_suggestions=current_dept['can_page_suggestions'],
                                  can_page_chat=current_dept['can_page_chat'],


                                  can_page_admin_tools=current_dept['can_page_admin_tools'],

                                  next_letter_number=next_letter_number,

                                  now=datetime.now())

 

@app.route('/send_letter', methods=['POST'])

def send_letter():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    

    sender_id = session['dept_id']

    letter_id = request.form.get('letter_id')

    receiver_id = request.form.get('receiver_id')

    title = request.form.get('title')

    priority = request.form.get('priority', 'عادي')

    content = request.form.get('content', '')

    

    file = request.files.get('file')

    

    conn = get_db_connection()

    cursor = conn.cursor()

    if not is_receiver_allowed(cursor, sender_id, receiver_id):

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية إرسال خطاب لهذه الإدارة."); window.history.back();</script>'''

    

    if letter_id and letter_id.isdigit():

        if file and file.filename != '':

            file_name, file_path, file_mimetype = upload_file_to_supabase(file, subfolder='letters', dept_folder=session.get('dept_username'))

            

            cursor.execute('''

                UPDATE letters 

                SET title = %s, content = %s, priority = %s, receiver_id = %s, file_name = %s, file_path = %s, file_mimetype = %s, created_at = %s, is_read = 0

                WHERE id = %s AND sender_id = %s

            ''', (title, content, priority, receiver_id, file_name, file_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M'), letter_id, sender_id))

        else:

            cursor.execute('''

                UPDATE letters 

                SET title = %s, content = %s, priority = %s, receiver_id = %s, created_at = %s, is_read = 0

                WHERE id = %s AND sender_id = %s

            ''', (title, content, priority, receiver_id, datetime.now().strftime('%Y-%m-%d %H:%M'), letter_id, sender_id))

    else:

        file_name = ''

        file_path = None

        file_mimetype = None

        if file and file.filename != '':

            file_name, file_path, file_mimetype = upload_file_to_supabase(file, subfolder='letters', dept_folder=session.get('dept_username'))

        letter_number = str(consume_next_letter_number(cursor))

            

        cursor.execute('''

            INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, letter_number)

            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

        ''', (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M'), letter_number))

        

    conn.commit()

    cursor.close()

    conn.close()

    

    return redirect(url_for('outbox'))

@app.route('/send_file_direct', methods=['POST'])

def send_file_direct():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    sender_id = session['dept_id']

    receiver_id = request.form.get('receiver_id')

    title = request.form.get('title')

    priority = request.form.get('priority', 'عادي')

    file = request.files.get('file')

    if not file or file.filename == '':

        return '''<script>alert("الرجاء اختيار ملف للإرسال."); window.history.back();</script>'''

    if not receiver_id:

        return '''<script>alert("الرجاء اختيار الإدارة المستلمة."); window.history.back();</script>'''

    conn = get_db_connection()

    cursor = conn.cursor()

    if not is_receiver_allowed(cursor, sender_id, receiver_id):

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية إرسال ملف لهذه الإدارة."); window.history.back();</script>'''

    file_name, file_path, file_mimetype = upload_file_to_supabase(file, subfolder='letters', dept_folder=session.get('dept_username'))

    cursor.execute('''

        INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, letter_number)

        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

    ''', (title, '', priority, sender_id, receiver_id, file_name, file_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M'), None))

    conn.commit()

    cursor.close()

    conn.close()

    return redirect(url_for('outbox'))

@app.route('/reply_to_letter', methods=['POST'])

def reply_to_letter():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    sender_id = session['dept_id']

    receiver_id = request.form.get('receiver_id')

    title = request.form.get('title')

    content = request.form.get('content', '')

    priority = request.form.get('priority', 'عادي')

    file = request.files.get('file')

    if not receiver_id:

        return '''<script>alert("تعذر تحديد الجهة المستلمة للرد."); window.history.back();</script>'''

    file_name = ''

    file_path = None

    file_mimetype = None

    if file and file.filename != '':

        file_name, file_path, file_mimetype = upload_file_to_supabase(file, subfolder='letters', dept_folder=session.get('dept_username'))

    conn = get_db_connection()

    cursor = conn.cursor()

    letter_number = str(consume_next_letter_number(cursor))

    cursor.execute('''

        INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, letter_number)

        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

    ''', (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, datetime.now().strftime('%Y-%m-%d %H:%M'), letter_number))

    conn.commit()

    cursor.close()

    conn.close()

    return redirect(url_for('outbox'))

@app.route('/archive')

def archive():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    dept_id = session['dept_id']

    conn = get_db_connection()

    cursor = conn.cursor()

    

    cursor.execute('SELECT * FROM departments WHERE id = %s', (dept_id,))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_page_archive'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول لأرشيف الإدارة."); window.location.href="/dashboard";</script>'''

    

    cursor.execute('SELECT id, name FROM departments WHERE id != %s', (dept_id,))

    depts = cursor.fetchall()

 

    own_letters = None

    other_letters = None

    if is_admin and current_dept['can_view_all_archive'] == 1:

        cursor.execute('''

            SELECT l.*, s.name as sender_name, r.name as receiver_name, ad.name as archive_dept_name 

            FROM letters l 

            LEFT JOIN departments s ON l.sender_id = s.id 

            LEFT JOIN departments r ON l.receiver_id = r.id 

            LEFT JOIN departments ad ON l.archive_dept_id = ad.id 

            WHERE ((l.sender_id = l.receiver_id AND l.sender_id IS NOT NULL AND l.sender_id != %s)

                OR (l.sender_id IS NULL AND l.receiver_id IS NULL AND (l.archive_dept_id IS NULL OR l.archive_dept_id != %s)))

            ORDER BY l.id DESC

        ''', (dept_id, dept_id))

        other_letters = cursor.fetchall()

        letters = own_letters + other_letters

    elif current_dept['can_view_all_archive'] == 1:

        cursor.execute('''

            SELECT l.*, s.name as sender_name, r.name as receiver_name, ad.name as archive_dept_name 

            FROM letters l 

            LEFT JOIN departments s ON l.sender_id = s.id 

            LEFT JOIN departments r ON l.receiver_id = r.id 

            LEFT JOIN departments ad ON l.archive_dept_id = ad.id 

            WHERE (l.sender_id = l.receiver_id AND l.sender_id IS NOT NULL) OR (l.sender_id IS NULL AND l.receiver_id IS NULL)

            ORDER BY l.id DESC

        ''')

        letters = cursor.fetchall()

    else:

        cursor.execute('''

            SELECT l.*, s.name as sender_name, r.name as receiver_name, ad.name as archive_dept_name 

            FROM letters l 

            LEFT JOIN departments s ON l.sender_id = s.id 

            LEFT JOIN departments r ON l.receiver_id = r.id 

            LEFT JOIN departments ad ON l.archive_dept_id = ad.id 

            WHERE ((l.sender_id = l.receiver_id AND l.sender_id = %s) OR (l.sender_id IS NULL AND l.receiver_id IS NULL AND l.archive_dept_id = %s))

            ORDER BY l.id DESC

        ''', (dept_id, dept_id))

        letters = cursor.fetchall()

    cursor.execute('SELECT COUNT(*) as count FROM letters WHERE receiver_id = %s AND is_read = 0', (dept_id,))

    unread_count = cursor.fetchone()['count']

    cursor.close()

    conn.close()

    MONTHLY_ARCHIVE_PREFIXES = ('أرشيف إنجازات شهرية:', 'أرشيف شهادات دورات:', 'أرشيف شواهد:')

    def split_monthly(letters_list):

        if letters_list is None:

            return None, None

        monthly = [l for l in letters_list if l.get('title') and l['title'].startswith(MONTHLY_ARCHIVE_PREFIXES)]

        rest = [l for l in letters_list if not (l.get('title') and l['title'].startswith(MONTHLY_ARCHIVE_PREFIXES))]

        return monthly, rest

    own_monthly_letters = None

    other_monthly_letters = None

    monthly_letters = None

    if is_admin:

        own_monthly_letters, own_letters = split_monthly(own_letters)

        other_monthly_letters, other_letters = split_monthly(other_letters)

    else:

        monthly_letters, letters = split_monthly(letters)

    

    return render_template_string(DASHBOARD_HTML, 

                                  page_title="أرشيف الإدارة",

                                  current_page="archive",

                                  letters=letters,

                                  unread_count=unread_count,

                                  own_letters=own_letters,

                                  other_letters=other_letters,

                                  own_monthly_letters=own_monthly_letters,

                                  other_monthly_letters=other_monthly_letters,

                                  monthly_letters=monthly_letters,

                                  depts=depts, 

                                  dept_name=session['dept_name'],

                                  can_delete=current_dept['can_delete'],

                                  can_add_user=current_dept['can_add_user'],

                                  can_page_quick_upload=current_dept['can_page_quick_upload'],

                                  can_page_inbox=current_dept['can_page_inbox'],

                                  can_page_outbox=current_dept['can_page_outbox'],

                                  can_page_achievements=current_dept['can_page_achievements'],

                                  can_page_archive=current_dept['can_page_archive'],

                                  is_admin=is_admin,

                                  can_view_all_archive=current_dept['can_view_all_archive'],

                                  can_page_suggestions=current_dept['can_page_suggestions'],
                                  can_page_chat=current_dept['can_page_chat'],


                                  can_page_admin_tools=current_dept['can_page_admin_tools'],

                                  now=datetime.now())

 

@app.route('/quick_upload', methods=['GET', 'POST'])

def quick_upload():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_page_quick_upload'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الرفع الفوري."); window.location.href="/dashboard";</script>'''

 

    if request.method == 'POST':

        dept_id = session['dept_id']

        document_title = request.form.get('document_title')

        archive_category = request.form.get('archive_category')

        notes = request.form.get('notes', '')

        

        files = request.files.getlist('archive_files')

        uploaded_count = 0

 

        for file in files:

            if file and file.filename != '':

                original_name, storage_path, content_type = upload_file_to_supabase(file, subfolder='quick_upload', dept_folder=session.get('dept_username'))

                

                file_title = f"{document_title} - {original_name}" if len(files) > 1 else document_title

                

                cursor.execute('''

                    INSERT INTO letters (title, content, priority, sender_id, receiver_id, file_name, file_path, file_mimetype, created_at, archive_dept_id)

                    VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s)

                ''', (

                    file_title, 

                    f"التصنيف: {archive_category} | ملاحظات: {notes}", 

                    "عادي", 

                    original_name, 

                    storage_path,

                    content_type,

                    datetime.now().strftime('%Y-%m-%d %H:%M'),

                    dept_id

                ))

                uploaded_count += 1

                

        conn.commit()

        cursor.close()

        conn.close()

        

        if uploaded_count > 0:

            return f'''<script>alert("تم رفع وأرشفة {uploaded_count} ملف بنجاح إلى أرشيف الإدارة حصرياً!"); window.location.href="/archive";</script>'''

        else:

            return '''<script>alert("الرجاء التأكد من رفع الملفات بشكل صحيح."); window.location.href="/quick_upload";</script>'''

    

    cursor.close()

    conn.close()

    

    html_code = '''

    <!DOCTYPE html>

    <html lang="ar" dir="rtl">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>رفع ملفات متعددة للأرشفة - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green-primary: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

.top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            

            .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; display: flex; align-items: center; justify-content: center; }

            .upload-card { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-radius: 16px; border: 1px solid #d5e2d8; box-shadow: 0 10px 30px rgba(18, 56, 38, 0.08); width: 100%; max-width: 650px; padding: 1.5rem; position: relative; }

            .btn-fifa-primary { background-color: var(--fifa-green-primary); color: #ffffff; border-radius: 10px; padding: 0.75rem; font-weight: 700; border: none; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

<button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                            <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                            <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>

                        </button>

                        <ul class="dropdown-menu dropdown-menu-start shadow">

                            <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                        </ul>

                    </div>

                </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                {% if current_dept['can_page_inbox'] == 1 or is_admin %}

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                {% endif %}

                {% if current_dept['can_page_outbox'] == 1 or is_admin %}

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                {% endif %}

                {% if current_dept['can_page_achievements'] == 1 or is_admin %}

                <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                {% endif %}

                {% if current_dept['can_page_archive'] == 1 or is_admin %}

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                {% endif %}

                {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}

                <a href="/quick_upload" class="sidebar-link active"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                {% endif %}

                {% if current_dept['can_page_suggestions'] == 1 or is_admin %}

                <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>

                {% endif %}
                {% if current_dept['can_page_chat'] == 1 or is_admin %}

                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                {% endif %}

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                {% if is_admin or current_dept['can_page_admin_tools'] == 1 %}

                <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                {% endif %}

                {% if current_dept['can_add_user'] == 1 %}

                <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                {% endif %}

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

                <div class="upload-card">

                    <div class="text-center mb-4">

                        <h3 class="fw-bold fs-5" style="color: var(--fifa-green-primary);">رفع وتوثيق فوري (تظهر في أرشيف الإدارة فقط)</h3>

                    </div>

                    <form action="/quick_upload" method="post" enctype="multipart/form-data">

                        <div class="mb-3">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green-primary);">عنوان رئيسي للملفات المرفوعة</label>

                            <input type="text" name="document_title" required class="form-control py-2 fs-7" placeholder="مثال: فواتير وعقود قسم الصيانة">

                        </div>

                        <div class="mb-3">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green-primary);">تصنيف الأرشيف (تسجيل يدوي)</label>

                            <input type="text" name="archive_category" required class="form-control py-2 fs-7 bg-white" placeholder="أدخل تصنيف الأرشيف يدوياً...">

                        </div>

                        <div class="mb-3">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green-primary);">اختر الملفات</label>

                            <input type="file" name="archive_files" multiple required class="form-control fs-7">

                        </div>

                        <div class="mb-4">

                            <label class="form-label fw-bold fs-7" style="color: var(--fifa-green-primary);">ملاحظات وصفية (اختياري)</label>

                            <textarea name="notes" rows="2" class="form-control fs-7" placeholder="أدخل تفاصيل..."></textarea>

                        </div>

                        <button type="submit" class="btn btn-fifa-primary w-100 shadow-sm py-2 fs-7">رفع وأرشفة الملفات الآن</button>

                    </form>

                </div>

            </main>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

        function updateNavbarHeightVar() {

    var nav = document.querySelector('.top-navbar');

    if (nav) {

        document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px');

    }

}

updateNavbarHeightVar();

window.addEventListener('load', updateNavbarHeightVar);

window.addEventListener('resize', updateNavbarHeightVar);

        function toggleSidebar() {

    document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

    document.getElementById('mobileOverlay').classList.toggle('active');

}

(function() {

    var touchStartX = 0;

    var touchStartY = 0;

    var edgeThreshold = 25;

    var swipeThreshold = 60;

    document.addEventListener('touchstart', function(e) {

        touchStartX = e.touches[0].clientX;

        touchStartY = e.touches[0].clientY;

    }, { passive: true });

    document.addEventListener('touchend', function(e) {

        if (window.innerWidth > 991.98) return;

        var sidebarEl = document.getElementById('sidebarMenu');

        if (!sidebarEl) return;

        var touchEndX = e.changedTouches[0].clientX;

        var touchEndY = e.changedTouches[0].clientY;

        var deltaX = touchEndX - touchStartX;

        var deltaY = touchEndY - touchStartY;

        if (Math.abs(deltaY) > 60) return;

        var isOpen = sidebarEl.classList.contains('show-sidebar');

        if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

            toggleSidebar();

        }

        else if (isOpen && deltaX > swipeThreshold) {

            toggleSidebar();

        }

    }, { passive: true });

})();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, is_admin=is_admin, current_dept=current_dept, dept_name=session['dept_name'])

@app.route('/monthly_achievements')

def monthly_achievements():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    

    if current_dept['can_page_achievements'] != 1 and not is_admin:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة إنجازات الشهر."); window.location.href="/dashboard";</script>'''

    

    can_view_all_ach = current_dept['can_view_all_achievements'] == 1

    if can_view_all_ach:

        cursor.execute('SELECT * FROM departments')

        depts = cursor.fetchall()

    else:

        cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

        depts = cursor.fetchall()

    

    cursor.execute('''

        SELECT ma.*, d.name as dept_name 

        FROM monthly_achievements ma

        JOIN departments d ON ma.dept_id = d.id

        ORDER BY ma.id DESC

    ''')

    achievements = cursor.fetchall()

    cursor.execute('''

        SELECT cc.*, d.name as dept_name 

        FROM course_certificates cc

        JOIN departments d ON cc.dept_id = d.id

        ORDER BY cc.id DESC

    ''')

    certificates = cursor.fetchall()

    cursor.execute('''

        SELECT sh.*, d.name as dept_name 

        FROM shawahid sh

        JOIN departments d ON sh.dept_id = d.id

        ORDER BY sh.id DESC

    ''')

    shawahid_list = cursor.fetchall()

    

    cursor.close()

    conn.close()

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>إنجازات وشهادات الدورات - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green-primary: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; --fifa-card-border: #d5e2d8; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

.top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            

            .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; width: 100%; min-width: 0; overflow-x: hidden; }

            .dept-card { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-radius: 12px; border: 1px solid #d5e2d8; box-shadow: 0 4px 12px rgba(0,0,0,0.03); margin-bottom: 1.5rem; }

            .dept-header { background-color: var(--fifa-green-primary); color: #fff; border-radius: 11px 11px 0 0; padding: 0.8rem 1rem; }

            .btn-fifa-gold { background-color: var(--fifa-gold); color: #ffffff; font-weight: 700; border: none; }

            .sub-section-title { font-weight: 700; font-size: 0.85rem; color: var(--fifa-green-primary); border-bottom: 2px solid var(--fifa-gold); padding-bottom: 3px; margin-bottom: 10px; }

            .scroll-list-box { max-height: 320px; overflow-y: auto; padding-left: 4px; border: 1px solid #eef2ef; border-radius: 8px; }

            .scroll-list-box::-webkit-scrollbar { width: 6px; }

            .scroll-list-box::-webkit-scrollbar-track { background: #f4f8f6; border-radius: 10px; }

            .scroll-list-box::-webkit-scrollbar-thumb { background: #c5a059; border-radius: 10px; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

<button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                            <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                            <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>

                        </button>

                        <ul class="dropdown-menu dropdown-menu-start shadow">

                            <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                        </ul>

                    </div>

                </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                {% if current_dept['can_page_inbox'] == 1 or is_admin %}

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                {% endif %}

                {% if current_dept['can_page_outbox'] == 1 or is_admin %}

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                {% endif %}

                {% if current_dept['can_page_achievements'] == 1 or is_admin %}

                <a href="/monthly_achievements" class="sidebar-link active"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                {% endif %}

                {% if current_dept['can_page_archive'] == 1 or is_admin %}

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                {% endif %}

                {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}

                <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                {% endif %}

                {% if current_dept['can_page_suggestions'] == 1 or is_admin %}

                <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>

                {% endif %}
                {% if current_dept['can_page_chat'] == 1 or is_admin %}

                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                {% endif %}

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                {% if is_admin or current_dept['can_page_admin_tools'] == 1 %}

                <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                {% endif %}

                {% if current_dept['can_add_user'] == 1 %}

                <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                {% endif %}

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

                <div class="container-fluid p-0">

                    <div class="mb-4">

                        <h4 class="fw-bold fs-5" style="color: var(--fifa-green-primary);"><i class='bx bxs-trophy ms-2' style="color: var(--fifa-gold);"></i>إنجازات وشهادات دورات الإدارات</h4>

                    </div>

                    <div class="row">

                        {% for d in depts %}

                        <div class="col-lg-6">

                            <div class="dept-card">

                                <div class="dept-header d-flex flex-wrap justify-content-between align-items-center gap-2">

                                    <span class="fw-bold fs-7"><i class='bx bxs-folder-open ms-2' style="color: var(--fifa-gold);"></i>{{ d.name }}</span>

                                    <div class="d-flex gap-2">

                                        {% if can_delete == 1 %}

                                            <a href="/admin/clear_monthly_files/{{ d.id }}" class="btn btn-sm btn-outline-light fs-8" onclick="return confirm('تأكيد تفريغ وأرشفة الإنجازات والشهادات لهذا الشهر ونقلها لأرشيف الإدارة؟');">

                                                <i class='bx bx-archive-in ms-1'></i>تفريغ وأرشفة

                                            </a>

                                        {% endif %}

                                    </div>

                                </div>

                                <div class="p-3">

                                    <div class="sub-section-title d-flex justify-content-between align-items-center flex-wrap gap-1">

                                         <span><i class='bx bxs-award ms-1'></i> ملفات الإنجازات الشهرية</span>

                                         <div class="d-flex align-items-center gap-2 flex-wrap">

                                         {% if achievements|selectattr('dept_id', 'equalto', d.id)|list|length > 0 %}

                                         <a href="/download_all_achievements/{{ d.id }}" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">

                                             <i class='bx bx-download ms-1'></i> تحميل الكل

                                         </a>

                                         {% if can_delete == 1 %}

                                         <div class="form-check form-check-inline m-0">

                                             <input class="form-check-input" type="checkbox" id="selectAllAch_{{ d.id }}" onclick="toggleAllCheckboxes('achForm_{{ d.id }}', this)">

                                             <label class="form-check-label fs-8" for="selectAllAch_{{ d.id }}">تحديد الكل</label>

                                         </div>

                                         <button type="button" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="submitDeleteSelected('achForm_{{ d.id }}')">

                                             <i class='bx bx-trash-alt ms-1'></i> حذف المحدد

                                         </button>

                                         {% endif %}

                                         {% endif %}

                                         </div>

                                 </div>

                                    <form id="achForm_{{ d.id }}" action="/delete_selected_achievements" method="post">

                                    <input type="hidden" name="dept_id" value="{{ d.id }}">

                                    <div class="list-group mb-3 fs-7 scroll-list-box" id="dept-files-{{ d.id }}">

                                        {% set ns = namespace(found=false) %}

                                        {% for a in achievements %}

                                            {% if a.dept_id == d.id %}

                                                {% set ns.found = true %}

                                                <div class="list-group-item d-flex justify-content-between align-items-center gap-2 bg-transparent p-2">

                                                    <div class="d-flex align-items-start gap-2" style="min-width:0; flex:1 1 auto; overflow:hidden;">

                                                        {% if can_delete == 1 %}

                                                        <input class="form-check-input item-checkbox mt-1 flex-shrink-0" type="checkbox" name="item_ids" value="{{ a.id }}">

                                                        {% endif %}

                                                        <i class='bx bxs-file-pdf text-danger fs-5 align-middle ms-1 flex-shrink-0'></i>

                                                        <div style="min-width:0; overflow:hidden;">

                                                            <strong class="text-dark fs-7 d-block text-truncate" title="{{ a.title }}"><bdi>{{ a.title }}</bdi></strong>

                                                            <span class="text-muted d-block fs-8" dir="ltr">{{ a.uploaded_at }}</span>

                                                        </div>

                                                    </div>

                                                    <div class="d-flex gap-1 flex-shrink-0">

                                                        <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_ach_file/{{ a.id }}', '{{ a.title }}')">معاينة</button>

                                                        <a href="/download_ach_file/{{ a.id }}" target="_blank" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">تنزيل</a>

                                                        {% if can_delete == 1 %}

                                                        <a href="/delete_achievement/{{ a.id }}" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="return confirm('حذف هذا الملف؟');">حذف</a>

                                                        {% endif %}

                                                    </div>

                                                </div>

                                            {% endif %}

                                        {% endfor %}

                                        {% if not ns.found %}

                                            <div class="text-center py-2 text-muted fs-8">لا توجد إنجازات مرفوعة.</div>

                                        {% endif %}

                                    </div>

                                    </form>

                                    {% if session['dept_id'] == d.id or is_admin %}

                                    <form action="/upload_achievement" method="post" enctype="multipart/form-data" class="bg-white p-2 rounded border mb-3">

                                        <input type="hidden" name="dept_id" value="{{ d.id }}">

                                        <div class="d-flex flex-column flex-sm-row gap-2">

                                            <input type="text" name="title" class="form-control fs-8" placeholder="عنوان الإنجاز..." required>

                                            <input type="file" name="file" class="form-control fs-8" multiple required>

                                            <button class="btn btn-fifa-gold fs-8 text-nowrap" type="submit">رفع إنجاز</button>

                                        </div>

                                    </form>

                                    {% endif %}

                                    <div class="sub-section-title d-flex justify-content-between align-items-center flex-wrap gap-1">

                                         <span><i class='bx bxs-certification ms-1'></i> شهادات الدورات التدريبية</span>

                                         <div class="d-flex align-items-center gap-2 flex-wrap">

                                         {% if certificates|selectattr('dept_id', 'equalto', d.id)|list|length > 0 %}

                                         <a href="/download_all_certificates/{{ d.id }}" class="btn btn-sm btn-outline-primary py-0 px-2 fs-8">

                                             <i class='bx bx-download ms-1'></i> تحميل الكل

                                         </a>

                                         {% if can_delete == 1 %}

                                         <div class="form-check form-check-inline m-0">

                                             <input class="form-check-input" type="checkbox" id="selectAllCert_{{ d.id }}" onclick="toggleAllCheckboxes('certForm_{{ d.id }}', this)">

                                             <label class="form-check-label fs-8" for="selectAllCert_{{ d.id }}">تحديد الكل</label>

                                         </div>

                                         <button type="button" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="submitDeleteSelected('certForm_{{ d.id }}')">

                                             <i class='bx bx-trash-alt ms-1'></i> حذف المحدد

                                         </button>

                                         {% endif %}

                                         {% endif %}

                                         </div>

                                    </div>

                                    <form id="certForm_{{ d.id }}" action="/delete_selected_certificates" method="post">

                                    <input type="hidden" name="dept_id" value="{{ d.id }}">

                                    <div class="list-group mb-3 fs-7 scroll-list-box" id="dept-certs-{{ d.id }}">

                                        {% set ns_c = namespace(found=false) %}

                                        {% for c in certificates %}

                                            {% if c.dept_id == d.id %}

                                                {% set ns_c.found = true %}

                                                <div class="list-group-item d-flex justify-content-between align-items-center gap-2 bg-transparent p-2">

                                                    <div class="d-flex align-items-start gap-2" style="min-width:0; flex:1 1 auto; overflow:hidden;">

                                                        {% if can_delete == 1 %}

                                                        <input class="form-check-input item-checkbox mt-1 flex-shrink-0" type="checkbox" name="item_ids" value="{{ c.id }}">

                                                        {% endif %}

                                                        <i class='bx bxs-certification text-primary fs-5 align-middle ms-1 flex-shrink-0'></i>

                                                        <div style="min-width:0; overflow:hidden;">

                                                            <strong class="text-dark fs-7 d-block text-truncate" title="{{ c.title }}"><bdi>{{ c.title }}</bdi></strong>

                                                            <span class="text-muted d-block fs-8" dir="ltr">{{ c.uploaded_at }}</span>

                                                        </div>

                                                    </div>

                                                    <div class="d-flex gap-1 flex-shrink-0">

                                                        <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_cert_file/{{ c.id }}', '{{ c.title }}')">معاينة</button>

                                                        <a href="/download_cert_file/{{ c.id }}" target="_blank" class="btn btn-sm btn-outline-primary py-0 px-2 fs-8">تنزيل</a>

                                                        {% if can_delete == 1 %}

                                                        <a href="/delete_certificate/{{ c.id }}" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="return confirm('حذف هذا الملف؟');">حذف</a>

                                                        {% endif %}

                                                    </div>

                                                </div>

                                            {% endif %}

                                        {% endfor %}

                                        {% if not ns_c.found %}

                                            <div class="text-center py-2 text-muted fs-8">لا توجد شهادات دورات مرفوعة.</div>

                                        {% endif %}

                                    </div>

                                    </form>

                                    {% if session['dept_id'] == d.id or is_admin %}

                                    <form action="/upload_certificate" method="post" enctype="multipart/form-data" class="bg-light p-2 rounded border">

                                        <input type="hidden" name="dept_id" value="{{ d.id }}">

                                        <div class="d-flex flex-column flex-sm-row gap-2">

                                            <input type="text" name="title" class="form-control fs-8" placeholder="عنوان أو اسم شهادة الدورة..." required>

                                            <input type="file" name="file" class="form-control fs-8" multiple required>

                                            <button class="btn btn-primary fs-8 text-nowrap" type="submit">رفع شهادة</button>

                                        </div>

                                    </form>

                                    {% endif %}

                                    <div class="sub-section-title d-flex justify-content-between align-items-center flex-wrap gap-1">

                                         <span><i class='bx bxs-badge-check ms-1'></i> شواهد</span>

                                         <div class="d-flex align-items-center gap-2 flex-wrap">

                                         {% if shawahid_list|selectattr('dept_id', 'equalto', d.id)|list|length > 0 %}

                                         <a href="/download_all_shawahid/{{ d.id }}" class="btn btn-sm btn-outline-dark py-0 px-2 fs-8">

                                             <i class='bx bx-download ms-1'></i> تحميل الكل

                                         </a>

                                         {% if can_delete == 1 %}

                                         <div class="form-check form-check-inline m-0">

                                             <input class="form-check-input" type="checkbox" id="selectAllShahid_{{ d.id }}" onclick="toggleAllCheckboxes('shahidForm_{{ d.id }}', this)">

                                             <label class="form-check-label fs-8" for="selectAllShahid_{{ d.id }}">تحديد الكل</label>

                                         </div>

                                         <button type="button" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="submitDeleteSelected('shahidForm_{{ d.id }}')">

                                             <i class='bx bx-trash-alt ms-1'></i> حذف المحدد

                                         </button>

                                         {% endif %}

                                         {% endif %}

                                         </div>

                                    </div>

                                    <form id="shahidForm_{{ d.id }}" action="/delete_selected_shawahid" method="post">

                                    <input type="hidden" name="dept_id" value="{{ d.id }}">

                                    <div class="list-group mb-3 fs-7 scroll-list-box" id="dept-shawahid-{{ d.id }}">

                                        {% set ns_s = namespace(found=false) %}

                                        {% for s in shawahid_list %}

                                            {% if s.dept_id == d.id %}

                                                {% set ns_s.found = true %}

                                                <div class="list-group-item d-flex justify-content-between align-items-center gap-2 bg-transparent p-2">

                                                    <div class="d-flex align-items-start gap-2" style="min-width:0; flex:1 1 auto; overflow:hidden;">

                                                        {% if can_delete == 1 %}

                                                        <input class="form-check-input item-checkbox mt-1 flex-shrink-0" type="checkbox" name="item_ids" value="{{ s.id }}">

                                                        {% endif %}

                                                        <i class='bx bxs-badge-check text-dark fs-5 align-middle ms-1 flex-shrink-0'></i>

                                                        <div style="min-width:0; overflow:hidden;">

                                                            <strong class="text-dark fs-7 d-block text-truncate" title="{{ s.title }}"><bdi>{{ s.title }}</bdi></strong>

                                                            <span class="text-muted d-block fs-8" dir="ltr">{{ s.uploaded_at }}</span>

                                                        </div>

                                                    </div>

                                                    <div class="d-flex gap-1 flex-shrink-0">

                                                        <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_shahid_file/{{ s.id }}', '{{ s.title }}')">معاينة</button>

                                                        <a href="/download_shahid_file/{{ s.id }}" target="_blank" class="btn btn-sm btn-outline-dark py-0 px-2 fs-8">تنزيل</a>

                                                        {% if can_delete == 1 %}

                                                        <a href="/delete_shahid/{{ s.id }}" class="btn btn-sm btn-outline-danger py-0 px-2 fs-8" onclick="return confirm('حذف هذا الشاهد؟');">حذف</a>

                                                        {% endif %}

                                                    </div>

                                                </div>

                                            {% endif %}

                                        {% endfor %}

                                        {% if not ns_s.found %}

                                            <div class="text-center py-2 text-muted fs-8">لا توجد شواهد مرفوعة.</div>

                                        {% endif %}

                                    </div>

                                    </form>

                                    {% if session['dept_id'] == d.id or is_admin %}

                                    <form action="/upload_shahid" method="post" enctype="multipart/form-data" class="bg-white p-2 rounded border">

                                        <input type="hidden" name="dept_id" value="{{ d.id }}">

                                        <div class="d-flex flex-column flex-sm-row gap-2">

                                            <input type="text" name="title" class="form-control fs-8" placeholder="عنوان الشاهد..." required>

                                            <input type="file" name="file" class="form-control fs-8" multiple required>

                                            <button class="btn btn-dark fs-8 text-nowrap" type="submit">رفع شاهد</button>

                                        </div>

                                    </form>

                                    {% endif %}

                                </div>

                            </div>

                        </div>

                        {% endfor %}

                    </div>

                </div>

            </main>

        </div>

        <div class="modal fade" id="previewFileModal" tabindex="-1" aria-hidden="true">

          <div class="modal-dialog modal-xl modal-dialog-centered">

            <div class="modal-content">

              <div class="modal-header bg-dark text-white py-2">

                <h6 class="modal-title fw-bold" id="previewFileTitle">معاينة المستند</h6>

                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

              </div>

              <div class="modal-body p-0" style="height: 80vh; background: #525659;">

                <iframe id="previewFrame" src="" style="width:100%; height:100%; border:none;"></iframe>

              </div>

            </div>

          </div>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

            function previewFile(url, title) {

                document.getElementById('previewFileTitle').innerText = 'معاينة: ' + title;

                document.getElementById('previewFrame').src = url;

                var modal = new bootstrap.Modal(document.getElementById('previewFileModal'));

                modal.show();

            }

            function toggleAllCheckboxes(formId, sourceCheckbox) {

                var form = document.getElementById(formId);

                if (!form) return;

                var boxes = form.querySelectorAll('.item-checkbox');

                boxes.forEach(function (cb) { cb.checked = sourceCheckbox.checked; });

            }

            function submitDeleteSelected(formId) {

                var form = document.getElementById(formId);

                if (!form) return;

                var checked = form.querySelectorAll('.item-checkbox:checked');

                if (checked.length === 0) {

                    alert('الرجاء تحديد ملف واحد على الأقل للحذف.');

                    return;

                }

                if (confirm('هل أنت متأكد من حذف الملفات المحددة؟ (' + checked.length + ' ملف)')) {

                    form.submit();

                }

            }

            function updateNavbarHeightVar() {

    var nav = document.querySelector('.top-navbar');

    if (nav) {

        document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px');

    }

}

updateNavbarHeightVar();

window.addEventListener('load', updateNavbarHeightVar);

window.addEventListener('resize', updateNavbarHeightVar);

            function toggleSidebar() {

    document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

    document.getElementById('mobileOverlay').classList.toggle('active');

}

(function() {

    var touchStartX = 0;

    var touchStartY = 0;

    var edgeThreshold = 25;

    var swipeThreshold = 60;

    document.addEventListener('touchstart', function(e) {

        touchStartX = e.touches[0].clientX;

        touchStartY = e.touches[0].clientY;

    }, { passive: true });

    document.addEventListener('touchend', function(e) {

        if (window.innerWidth > 991.98) return;

        var sidebarEl = document.getElementById('sidebarMenu');

        if (!sidebarEl) return;

        var touchEndX = e.changedTouches[0].clientX;

        var touchEndY = e.changedTouches[0].clientY;

        var deltaX = touchEndX - touchStartX;

        var deltaY = touchEndY - touchStartY;

        if (Math.abs(deltaY) > 60) return;

        var isOpen = sidebarEl.classList.contains('show-sidebar');

        if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

            toggleSidebar();

        }

        else if (isOpen && deltaX > swipeThreshold) {

            toggleSidebar();

        }

    }, { passive: true });

})();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, depts=depts, achievements=achievements, certificates=certificates, shawahid_list=shawahid_list, dept_name=session['dept_name'], can_delete=current_dept['can_delete'], can_add_user=current_dept['can_add_user'], current_dept=current_dept, is_admin=is_admin)

@app.route('/delete_achievement/<int:ach_id>')

def delete_achievement(ach_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM monthly_achievements WHERE id = %s', (ach_id,))

    file_row = cursor.fetchone()

    cursor.execute('DELETE FROM monthly_achievements WHERE id = %s', (ach_id,))

    conn.commit()

    cursor.close()

    conn.close()

    if file_row and file_row.get('file_path'):

        delete_file_from_supabase(file_row['file_path'])

    return '''<script>alert("تم حذف الملف بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_certificate/<int:cert_id>')

def delete_certificate(cert_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM course_certificates WHERE id = %s', (cert_id,))

    file_row = cursor.fetchone()

    cursor.execute('DELETE FROM course_certificates WHERE id = %s', (cert_id,))

    conn.commit()

    cursor.close()

    conn.close()

    if file_row and file_row.get('file_path'):

        delete_file_from_supabase(file_row['file_path'])

    return '''<script>alert("تم حذف الملف بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_all_achievements/<int:dept_id>')

def delete_all_achievements(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    file_rows = cursor.fetchall()

    cursor.execute('DELETE FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    conn.commit()

    cursor.close()

    conn.close()

    for fr in file_rows:

        if fr.get('file_path'):

            delete_file_from_supabase(fr['file_path'])

    return '''<script>alert("تم حذف كل ملفات الإنجازات لهذه الإدارة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_selected_achievements', methods=['POST'])

def delete_selected_achievements():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    item_ids_raw = request.form.getlist('item_ids')

    item_ids = [int(i) for i in item_ids_raw if i.isdigit()]

    if item_ids:

        cursor.execute('SELECT file_path FROM monthly_achievements WHERE id = ANY(%s)', (item_ids,))

        file_rows = cursor.fetchall()

        cursor.execute('DELETE FROM monthly_achievements WHERE id = ANY(%s)', (item_ids,))

        conn.commit()

        for fr in file_rows:

            if fr.get('file_path'):

                delete_file_from_supabase(fr['file_path'])

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف الملفات المحددة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_all_certificates/<int:dept_id>')

def delete_all_certificates(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    cursor.execute('SELECT file_path FROM course_certificates WHERE dept_id = %s', (dept_id,))

    file_rows = cursor.fetchall()

    cursor.execute('DELETE FROM course_certificates WHERE dept_id = %s', (dept_id,))

    conn.commit()

    cursor.close()

    conn.close()

    for fr in file_rows:

        if fr.get('file_path'):

            delete_file_from_supabase(fr['file_path'])

    return '''<script>alert("تم حذف كل شهادات الدورات لهذه الإدارة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/delete_selected_certificates', methods=['POST'])

def delete_selected_certificates():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if current_dept['can_delete'] != 1:

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، لا تملك صلاحية الحذف."); window.location.href="/monthly_achievements";</script>'''

    item_ids_raw = request.form.getlist('item_ids')

    item_ids = [int(i) for i in item_ids_raw if i.isdigit()]

    if item_ids:

        cursor.execute('SELECT file_path FROM course_certificates WHERE id = ANY(%s)', (item_ids,))

        file_rows = cursor.fetchall()

        cursor.execute('DELETE FROM course_certificates WHERE id = ANY(%s)', (item_ids,))

        conn.commit()

        for fr in file_rows:

            if fr.get('file_path'):

                delete_file_from_supabase(fr['file_path'])

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف الملفات المحددة بنجاح"); window.location.href="/monthly_achievements";</script>'''

@app.route('/admin/dashboard')

def admin_dashboard():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

        

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    is_admin = is_admin_user(session.get('dept_name'))

    if not is_admin and (not current_dept or current_dept['can_page_admin_tools'] != 1):

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، هذه الصفحة مخصصة لمدير النظام فقط."); window.location.href="/dashboard";</script>'''

    cursor.execute('SELECT * FROM departments')

    depts = cursor.fetchall()

    

    cursor.execute('SELECT COUNT(*) as count FROM letters')

    total_letters = cursor.fetchone()['count']

    

    cursor.execute('SELECT COUNT(*) as count FROM monthly_achievements')

    total_ach = cursor.fetchone()['count']

    

    cursor.execute('SELECT COUNT(*) as count FROM course_certificates')

    total_certs = cursor.fetchone()['count']

    cursor.execute('SELECT COUNT(*) as count FROM shawahid')

    total_shawahid = cursor.fetchone()['count']

    # --- تحسين أداء الصفحة ---

    # الكود القديم كان يسوي أكثر من 9 استعلامات منفصلة لكل إدارة (يعني مئات الاستعلامات إذا زاد عدد

    # الإدارات)، وبعضها يجلب كل الأعمدة (SELECT *) من جداول فيها عمود BYTEA (file_data) يخزّن أحيانًا

    # محتوى الملف كامل كبيانات ثنائية داخل الصف نفسه - وهذا يتم تحميله من قاعدة البيانات لكل خطاب من كل

    # إدارة في كل مرة تُفتح فيها الصفحة، رغم إن القالب لا يستخدمه إلا للتأكد من وجوده فقط (true/false).

    # الحل: نجلب كل جدول مرة واحدة فقط بعدد أعمدة محدود (بدون بيانات الملف الثنائية)، ثم نجمّع

    # النتائج حسب كل إدارة داخل بايثون. هذا يخفّض عدد الاستعلامات من مئات إلى حوالي 8 استعلامات ثابتة

    # بغض النظر عن عدد الإدارات، ويمنع تحميل بيانات ملفات ثنائية ضخمة لا داعي لها إطلاقاً.

    dept_name_by_id = {d['id']: d['name'] for d in depts}

    inbox_by_dept = defaultdict(list)

    outbox_by_dept = defaultdict(list)

    archive_by_dept = defaultdict(list)

    cursor.execute('''

        SELECT id, title, created_at, sender_id, receiver_id, archive_dept_id,

               file_path, file_mimetype, (file_data IS NOT NULL) as file_data

        FROM letters

        ORDER BY id DESC

    ''')

    for l in cursor.fetchall():

        sid, rid, aid = l['sender_id'], l['receiver_id'], l['archive_dept_id']

        if rid is not None and sid != rid:

            row = dict(l)

            row['sender_name'] = dept_name_by_id.get(sid)

            inbox_by_dept[rid].append(row)

        if sid is not None and rid != sid:

            row = dict(l)

            row['receiver_name'] = dept_name_by_id.get(rid)

            outbox_by_dept[sid].append(row)

        archive_dept_ids = set()

        if sid is not None and rid is not None and sid == rid:

            archive_dept_ids.add(sid)

        if aid is not None:

            archive_dept_ids.add(aid)

        for adid in archive_dept_ids:

            archive_by_dept[adid].append(dict(l))

    ach_by_dept = defaultdict(list)

    cursor.execute('SELECT id, dept_id, title, uploaded_at FROM monthly_achievements ORDER BY id DESC')

    for a in cursor.fetchall():

        ach_by_dept[a['dept_id']].append(a)

    cert_by_dept = defaultdict(list)

    cursor.execute('SELECT id, dept_id, title, uploaded_at FROM course_certificates ORDER BY id DESC')

    for c in cursor.fetchall():

        cert_by_dept[c['dept_id']].append(c)

    shahid_by_dept = defaultdict(list)

    cursor.execute('SELECT id, dept_id, title, uploaded_at FROM shawahid ORDER BY id DESC')

    for s in cursor.fetchall():

        shahid_by_dept[s['dept_id']].append(s)

    dept_stats = []

    for d in depts:

        d_id = d['id']

        dept_stats.append({

            'id': d_id,

            'name': d['name'],

            'inbox_count': len(inbox_by_dept.get(d_id, [])),

            'outbox_count': len(outbox_by_dept.get(d_id, [])),

            'archive_count': len(archive_by_dept.get(d_id, [])),

            'ach_count': len(ach_by_dept.get(d_id, [])),

            'ach_files': ach_by_dept.get(d_id, []),

            'cert_count': len(cert_by_dept.get(d_id, [])),

            'cert_files': cert_by_dept.get(d_id, []),

            'shahid_count': len(shahid_by_dept.get(d_id, [])),

            'shahid_files': shahid_by_dept.get(d_id, []),

            'inbox_files': inbox_by_dept.get(d_id, []),

            'outbox_files': outbox_by_dept.get(d_id, []),

            'archive_files': archive_by_dept.get(d_id, [])

        })

    cursor.close()

    conn.close()

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>لوحة التحكم الشاملة - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green-primary: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; --fifa-card-border: #d5e2d8; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

.top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            

            .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; width: 100%; min-width: 0; overflow-x: hidden; }

            .stat-box { background: rgba(255, 255, 255, 0.95); border-radius: 12px; border: 1px solid var(--fifa-card-border); padding: 1.2rem; box-shadow: 0 4px 15px rgba(0,0,0,0.03); text-align: center; }

            .modern-card { background: rgba(255, 255, 255, 0.95); border-radius: 12px; border: 1px solid var(--fifa-card-border); padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 4px 15px rgba(0,0,0,0.03); }

            .scroll-list-box { max-height: 320px; overflow-y: auto; padding-left: 4px; border: 1px solid #eef2ef; border-radius: 8px; }

            .scroll-list-box::-webkit-scrollbar { width: 6px; }

            .scroll-list-box::-webkit-scrollbar-track { background: #f4f8f6; border-radius: 10px; }

            .scroll-list-box::-webkit-scrollbar-thumb { background: #c5a059; border-radius: 10px; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

<button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                            <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                            <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>

                        </button>

                        <ul class="dropdown-menu dropdown-menu-start shadow">

                            <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                        </ul>

                    </div>

                </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>
                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                <a href="/admin/dashboard" class="sidebar-link active" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

                <div class="container-fluid p-0">

                    <div class="mb-4">

                        <h4 class="fw-bold fs-5" style="color: var(--fifa-green-primary);"><i class='bx bxs-cog ms-2' style="color: var(--fifa-gold);"></i>لوحة التحكم والإحصائيات الشاملة</h4>

                    </div>

                    <div class="row g-3 mb-4">

                        <div class="col-md-4">

                            <div class="stat-box">

                                <h3 class="fw-bold text-success">{{ depts|length }}</h3>

                                <p class="text-muted fs-7 mb-0">إجمالي الإدارات والأقسام</p>

                            </div>

                        </div>

                        <div class="col-md-4">

                            <div class="stat-box">

                                <h3 class="fw-bold text-primary">{{ total_letters }}</h3>

                                <p class="text-muted fs-7 mb-0">إجمالي الخطابات والمعاملات</p>

                            </div>

                        </div>

                        <div class="col-md-4">

                            <div class="stat-box">

                                <h3 class="fw-bold text-warning">{{ total_ach + total_certs + total_shawahid }}</h3>

                                <p class="text-muted fs-7 mb-0">إجمالي الإنجازات والشهادات والشواهد</p>

                            </div>

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-group ms-1'></i> إجمالي الإدارات والأقسام وتفصيل ملفاتها</h5>

                        <div class="table-responsive">

                            <table class="table table-bordered table-hover align-middle fs-7">

                                <thead class="table-success text-dark">

                                    <tr>

                                        <th>اسم الإدارة / القسم</th>

                                        <th class="text-center">الصندوق الوارد</th>

                                        <th class="text-center">الخطابات الصادرة</th>

                                        <th class="text-center">أرشيف الإدارة</th>

                                        <th class="text-center">إنجازات الشهر</th>

                                        <th class="text-center">شهادات الدورات</th>

                                        <th class="text-center">شواهد</th>

                                    </tr>

                                </thead>

                                <tbody>

                                    {% for stat in dept_stats %}

                                    <tr>

                                        <td class="fw-bold text-success"><i class='bx bxs-folder ms-1 text-warning'></i> {{ stat.name }}</td>

                                        <td class="text-center"><span class="badge bg-secondary px-2 py-1">{{ stat.inbox_count }} ملفات</span></td>

                                        <td class="text-center"><span class="badge bg-primary px-2 py-1">{{ stat.outbox_count }} ملفات</span></td>

                                        <td class="text-center"><span class="badge bg-success px-2 py-1">{{ stat.archive_count }} ملفات</span></td>

                                        <td class="text-center"><span class="badge bg-warning text-dark px-2 py-1">{{ stat.ach_count }} ملفات</span></td>

                                        <td class="text-center"><span class="badge bg-info text-white px-2 py-1">{{ stat.cert_count }} ملفات</span></td>

                                        <td class="text-center"><span class="badge bg-dark px-2 py-1">{{ stat.shahid_count }} ملفات</span></td>

                                    </tr>

                                    {% endfor %}

                                </tbody>

                            </table>

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-inbox ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم الصندوق الوارد لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                    <h6 class="fw-bold text-success border-bottom pb-2">{{ stat.name }} ({{ stat.inbox_count }} خطابات واردة)</h6>

                                    {% if stat.inbox_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for l in stat.inbox_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ l.title }}"><i class='bx bxs-envelope text-secondary ms-1'></i> <bdi>{{ l.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ l.created_at }}</bdi>) - من: {{ l.sender_name or '-' }}</small></span>

                                                {% if l.file_path or l.file_data %}

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_letter_file/{{ l.id }}', '{{ l.title }}')">معاينة</button>

                                                    <a href="/download_letter_file/{{ l.id }}" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                                {% endif %}

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد خطابات واردة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-paper-plane ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم الخطابات الصادرة لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                    <h6 class="fw-bold text-primary border-bottom pb-2">{{ stat.name }} ({{ stat.outbox_count }} خطابات صادرة)</h6>

                                    {% if stat.outbox_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for l in stat.outbox_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ l.title }}"><i class='bx bxs-send text-primary ms-1'></i> <bdi>{{ l.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ l.created_at }}</bdi>) - إلى: {{ l.receiver_name or '-' }}</small></span>

                                                {% if l.file_path or l.file_data %}

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_letter_file/{{ l.id }}', '{{ l.title }}')">معاينة</button>

                                                    <a href="/download_letter_file/{{ l.id }}" class="btn btn-sm btn-outline-primary py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                                {% endif %}

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد خطابات صادرة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-file-archive ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم أرشيف الإدارة لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                    <h6 class="fw-bold text-success border-bottom pb-2">{{ stat.name }} ({{ stat.archive_count }} ملفات مؤرشفة)</h6>

                                    {% if stat.archive_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for l in stat.archive_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ l.title }}"><i class='bx bxs-file-archive text-success ms-1'></i> <bdi>{{ l.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ l.created_at }}</bdi>)</small></span>

                                                {% if l.file_path or l.file_data %}

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_letter_file/{{ l.id }}', '{{ l.title }}')">معاينة</button>

                                                    <a href="/download_letter_file/{{ l.id }}" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                                {% endif %}

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد ملفات مؤرشفة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-trophy ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم إنجازات الشهر لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                    <h6 class="fw-bold text-success border-bottom pb-2 d-flex justify-content-between align-items-center flex-wrap gap-1">

                                        <span>{{ stat.name }} ({{ stat.ach_count }} إنجازات)</span>

                                        {% if stat.ach_count > 0 %}

                                        <a href="/download_all_achievements/{{ stat.id }}" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">

                                            <i class='bx bx-download ms-1'></i> تحميل الكل

                                        </a>

                                        {% endif %}

                                    </h6>

                                    {% if stat.ach_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for ach in stat.ach_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ ach.title }}"><i class='bx bxs-file-pdf text-danger ms-1'></i> <bdi>{{ ach.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ ach.uploaded_at }}</bdi>)</small></span>

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_ach_file/{{ ach.id }}', '{{ ach.title }}')">معاينة</button>

                                                    <a href="/download_ach_file/{{ ach.id }}" class="btn btn-sm btn-outline-success py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد إنجازات مرفوعة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-certification ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم شهادات ودورات لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                   <h6 class="fw-bold text-primary border-bottom pb-2 d-flex justify-content-between align-items-center flex-wrap gap-1">

                                       <span>{{ stat.name }} ({{ stat.cert_count }} شهادات)</span>

                                       {% if stat.cert_count > 0 %}

                                       <a href="/download_all_certificates/{{ stat.id }}" class="btn btn-sm btn-outline-primary py-0 px-2 fs-8">

                                           <i class='bx bx-download ms-1'></i> تحميل الكل

                                       </a>

                                       {% endif %}

                                    </h6>

                                    {% if stat.cert_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for cert in stat.cert_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ cert.title }}"><i class='bx bxs-file-pdf text-primary ms-1'></i> <bdi>{{ cert.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ cert.uploaded_at }}</bdi>)</small></span>

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_cert_file/{{ cert.id }}', '{{ cert.title }}')">معاينة</button>

                                                    <a href="/download_cert_file/{{ cert.id }}" class="btn btn-sm btn-outline-primary py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد شهادات دورات مرفوعة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                    <!-- قسم تفصيل الشواهد لكل إدارة -->

                    <div class="modern-card">

                        <h5 class="fw-bold mb-3" style="color: var(--fifa-green-primary);"><i class='bx bxs-badge-check ms-1' style="color: var(--fifa-gold);"></i> تفصيل قسم شواهد لكل إدارة</h5>

                        <div class="row g-3">

                            {% for stat in dept_stats %}

                            <div class="col-md-6">

                                <div class="border rounded p-3 bg-light">

                                   <h6 class="fw-bold text-dark border-bottom pb-2 d-flex justify-content-between align-items-center flex-wrap gap-1">

                                       <span>{{ stat.name }} ({{ stat.shahid_count }} شواهد)</span>

                                       {% if stat.shahid_count > 0 %}

                                       <a href="/download_all_shawahid/{{ stat.id }}" class="btn btn-sm btn-outline-dark py-0 px-2 fs-8">

                                           <i class='bx bx-download ms-1'></i> تحميل الكل

                                       </a>

                                       {% endif %}

                                    </h6>

                                    {% if stat.shahid_files %}

                                        <ul class="list-unstyled mb-0 fs-8 mt-2 scroll-list-box">

                                            {% for sh in stat.shahid_files %}

                                            <li class="d-flex justify-content-between align-items-center gap-2 mb-1 bg-white p-2 rounded border">

                                                <span class="text-truncate" style="min-width:0; flex:1 1 auto;" title="{{ sh.title }}"><i class='bx bxs-badge-check text-dark ms-1'></i> <bdi>{{ sh.title }}</bdi> <small class="text-muted">(<bdi dir="ltr">{{ sh.uploaded_at }}</bdi>)</small></span>

                                                <div class="d-flex gap-1 flex-shrink-0">

                                                    <button type="button" class="btn btn-sm btn-info py-0 px-2 fs-8 text-white" onclick="previewFile('/view_shahid_file/{{ sh.id }}', '{{ sh.title }}')">معاينة</button>

                                                    <a href="/download_shahid_file/{{ sh.id }}" class="btn btn-sm btn-outline-dark py-0 px-2 fs-8">تنزيل</a>

                                                </div>

                                            </li>

                                            {% endfor %}

                                        </ul>

                                    {% else %}

                                        <p class="text-muted fs-8 mb-0 mt-2">لا توجد شواهد مرفوعة لهذه الإدارة.</p>

                                    {% endif %}

                                </div>

                            </div>

                            {% endfor %}

                        </div>

                    </div>

                </div>

            </main>

        </div>

        <div class="modal fade" id="previewFileModal" tabindex="-1" aria-hidden="true">

          <div class="modal-dialog modal-xl modal-dialog-centered">

            <div class="modal-content">

              <div class="modal-header bg-dark text-white py-2">

                <h6 class="modal-title fw-bold" id="previewFileTitle">معاينة المستند</h6>

                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>

              </div>

              <div class="modal-body p-0" style="height: 80vh; background: #525659;">

                <iframe id="previewFrame" src="" style="width:100%; height:100%; border:none;"></iframe>

              </div>

            </div>

          </div>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

            function previewFile(url, title) {

                document.getElementById('previewFileTitle').innerText = 'معاينة: ' + title;

                document.getElementById('previewFrame').src = url;

                var modal = new bootstrap.Modal(document.getElementById('previewFileModal'));

                modal.show();

            }

            function updateNavbarHeightVar() {

    var nav = document.querySelector('.top-navbar');

    if (nav) {

        document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px');

    }

}

updateNavbarHeightVar();

window.addEventListener('load', updateNavbarHeightVar);

window.addEventListener('resize', updateNavbarHeightVar);

            function toggleSidebar() {

    document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

    document.getElementById('mobileOverlay').classList.toggle('active');

}

(function() {

    var touchStartX = 0;

    var touchStartY = 0;

    var edgeThreshold = 25;

    var swipeThreshold = 60;

    document.addEventListener('touchstart', function(e) {

        touchStartX = e.touches[0].clientX;

        touchStartY = e.touches[0].clientY;

    }, { passive: true });

    document.addEventListener('touchend', function(e) {

        if (window.innerWidth > 991.98) return;

        var sidebarEl = document.getElementById('sidebarMenu');

        if (!sidebarEl) return;

        var touchEndX = e.changedTouches[0].clientX;

        var touchEndY = e.changedTouches[0].clientY;

        var deltaX = touchEndX - touchStartX;

        var deltaY = touchEndY - touchStartY;

        if (Math.abs(deltaY) > 60) return;

        var isOpen = sidebarEl.classList.contains('show-sidebar');

        if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

            toggleSidebar();

        }

        else if (isOpen && deltaX > swipeThreshold) {

            toggleSidebar();

        }

    }, { passive: true });

})();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, depts=depts, total_letters=total_letters, total_ach=total_ach, total_certs=total_certs, total_shawahid=total_shawahid, dept_stats=dept_stats, dept_name=session['dept_name'])

@app.route('/admin/delete_department/<int:dept_id>')

def delete_department(dept_id):

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    if not is_admin:

        return '''<script>alert("عذراً، هذه الصلاحية للمسؤولين فقط."); window.location.href="/dashboard";</script>'''

    if int(dept_id) == int(session['dept_id']):

        return '''<script>alert("لا يمكنك حذف حسابك الحالي الذي تستخدمه للدخول."); window.location.href="/admin/permissions";</script>'''

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT name FROM departments WHERE id = %s', (dept_id,))

    dept_row = cursor.fetchone()

    if not dept_row:

        cursor.close()

        conn.close()

        return '''<script>alert("الإدارة أو المستخدم غير موجود."); window.location.href="/admin/permissions";</script>'''

    if is_admin_user(dept_row['name']):

        cursor.close()

        conn.close()

        return '''<script>alert("لا يمكن حذف حساب مسؤول من هنا."); window.location.href="/admin/permissions";</script>'''

    cursor.execute('DELETE FROM letters WHERE sender_id = %s OR receiver_id = %s OR archive_dept_id = %s', (dept_id, dept_id, dept_id))

    cursor.execute('DELETE FROM monthly_achievements WHERE dept_id = %s', (dept_id,))

    cursor.execute('DELETE FROM course_certificates WHERE dept_id = %s', (dept_id,))

    cursor.execute('DELETE FROM suggestions WHERE dept_id = %s', (dept_id,))

    cursor.execute('DELETE FROM departments WHERE id = %s', (dept_id,))

    conn.commit()

    cursor.close()

    conn.close()

    return '''<script>alert("تم حذف الإدارة/المستخدم وكل بياناته المرتبطة بنجاح."); window.location.href="/admin/permissions";</script>'''

# --- إدارة الصلاحيات ---

@app.route('/admin/permissions', methods=['GET', 'POST'])

def admin_permissions():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    if not is_admin and (not current_dept or current_dept['can_page_admin_tools'] != 1):

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، صفحة إدارة الصلاحيات مخصصة للمسؤولين فقط."); window.location.href="/dashboard";</script>'''

    if request.method == 'POST':

        dept_id = request.form.get('dept_id')

        can_delete = 1 if request.form.get('can_delete') else 0

        can_view_all_archive = 1 if request.form.get('can_view_all_archive') else 0

        can_view_all_achievements = 1 if request.form.get('can_view_all_achievements') else 0

        can_add_user = 1 if request.form.get('can_add_user') else 0

        can_page_inbox = 1 if request.form.get('can_page_inbox') else 0

        can_page_outbox = 1 if request.form.get('can_page_outbox') else 0

        can_page_achievements = 1 if request.form.get('can_page_achievements') else 0

        can_page_archive = 1 if request.form.get('can_page_archive') else 0

        can_page_quick_upload = 1 if request.form.get('can_page_quick_upload') else 0

        can_page_suggestions = 1 if request.form.get('can_page_suggestions') else 0
        can_page_chat = 1 if request.form.get('can_page_chat') else 0


        can_send_all = 1 if request.form.get('can_send_all') else 0

        can_page_admin_tools = 1 if request.form.get('can_page_admin_tools') else 0

        allowed_recipients_raw = request.form.getlist('allowed_recipients')

        allowed_recipients = [int(i) for i in allowed_recipients_raw if i.isdigit()]

        new_password = request.form.get('new_password')

        new_username = request.form.get('new_username', '').strip()

        new_dept_name = request.form.get('new_dept_name', '').strip()

        new_email_raw = request.form.get('new_email', '').strip()

        new_email = new_email_raw.lower() if new_email_raw else None

        unlock_account = 1 if request.form.get('unlock_account') else 0

        if new_username:

            cursor.execute('SELECT id FROM departments WHERE username = %s AND id != %s', (new_username, dept_id))

            if cursor.fetchone():

                cursor.close()

                conn.close()

                return '''<script>alert("خطأ: اسم المستخدم الجديد مستخدم بالفعل من قبل إدارة أخرى."); window.location.href="/admin/permissions";</script>'''

        if new_dept_name:

            cursor.execute('SELECT id FROM departments WHERE name = %s AND id != %s', (new_dept_name, dept_id))

            if cursor.fetchone():

                cursor.close()

                conn.close()

                return '''<script>alert("خطأ: اسم الإدارة الجديد مستخدم بالفعل من قبل إدارة أخرى."); window.location.href="/admin/permissions";</script>'''

        if new_email:

            cursor.execute('SELECT id FROM departments WHERE LOWER(email) = %s AND id != %s', (new_email, dept_id))

            if cursor.fetchone():

                cursor.close()

                conn.close()

                return '''<script>alert("خطأ: البريد الإلكتروني مستخدم بالفعل من قبل إدارة أخرى."); window.location.href="/admin/permissions";</script>'''

        set_clauses = [

            'can_delete = %s', 'can_view_all_archive = %s', 'can_view_all_achievements = %s', 'can_add_user = %s',

            'can_page_inbox = %s', 'can_page_outbox = %s', 'can_page_achievements = %s', 'can_page_archive = %s',

            'can_page_quick_upload = %s', 'can_page_suggestions = %s', 'can_page_chat = %s', 'can_send_all = %s', 'can_page_admin_tools = %s',

            'email = %s'

        ]

        params = [can_delete, can_view_all_archive, can_view_all_achievements, can_add_user,

                  can_page_inbox, can_page_outbox, can_page_achievements, can_page_archive,

                  can_page_quick_upload, can_page_suggestions, can_page_chat, can_send_all, can_page_admin_tools,

                  new_email]

        if new_password and new_password.strip() != '':

            set_clauses.append('password = %s')

            params.append(generate_password_hash(new_password.strip()))

        if new_username:

            set_clauses.append('username = %s')

            params.append(new_username)

        if new_dept_name:

            set_clauses.append('name = %s')

            params.append(new_dept_name)

        if unlock_account == 1:

            set_clauses.append('is_locked = 0')

            set_clauses.append('failed_login_attempts = 0')

        params.append(dept_id)

        cursor.execute('UPDATE departments SET ' + ', '.join(set_clauses) + ' WHERE id = %s', tuple(params))

        # تحديث قائمة الإدارات المسموح لها بالإرسال إليها (تُستخدم فقط عند can_send_all = 0)

        cursor.execute('DELETE FROM send_permissions WHERE dept_id = %s', (dept_id,))

        if can_send_all == 0 and allowed_recipients:

            for allowed_id in allowed_recipients:

                cursor.execute('INSERT INTO send_permissions (dept_id, allowed_dept_id) VALUES (%s, %s)', (dept_id, allowed_id))

        conn.commit()

        cursor.close()

        conn.close()

        return '''<script>alert("تم تحديث الصلاحيات وبيانات الإدارة بنجاح!"); window.location.href="/admin/permissions";</script>'''

 

    cursor.execute('SELECT * FROM departments ORDER BY id ASC')

    departments = cursor.fetchall()

    cursor.execute('SELECT dept_id, allowed_dept_id FROM send_permissions')

    dept_allowed_map = {}

    for row in cursor.fetchall():

        dept_allowed_map.setdefault(row['dept_id'], []).append(row['allowed_dept_id'])

    cursor.execute('SELECT next_letter_number FROM system_settings ORDER BY id LIMIT 1')

    current_next_number = cursor.fetchone()['next_letter_number']

    cursor.close()

    conn.close()

 

    html_code = '''

    <!DOCTYPE html>

    <html dir="rtl" lang="ar">

    <head>

        <meta charset="UTF-8">

        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <script>

            (function () {

                try {

                    var t = localStorage.getItem('fifa_theme');

                    if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }

                    document.documentElement.setAttribute('data-theme', t);

                } catch (e) {}

            })();

        </script>

        <style>

            [data-theme="dark"] { color-scheme: dark; }

            [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }

            [data-theme="dark"] .modern-card, [data-theme="dark"] .login-card, [data-theme="dark"] .register-card,

            [data-theme="dark"] .upload-card, [data-theme="dark"] .perm-card, [data-theme="dark"] .dept-card,

            [data-theme="dark"] .stat-box, [data-theme="dark"] .paper-toolbar { background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .letter-item, [data-theme="dark"] .suggestion-item { border-bottom-color: #2a3a30 !important; }

            [data-theme="dark"] .letter-item:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .section-header, [data-theme="dark"] h3, [data-theme="dark"] h4, [data-theme="dark"] h5, [data-theme="dark"] h6,

            [data-theme="dark"] .fw-bold, [data-theme="dark"] label, [data-theme="dark"] .text-dark { color: #e7f0ea !important; }

            [data-theme="dark"] .text-muted, [data-theme="dark"] .text-secondary { color: #9fb0a7 !important; }

            [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {

                background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;

            }

            [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }

            [data-theme="dark"] .form-control:focus, [data-theme="dark"] .form-select:focus { background-color: #1b2620 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .btn-light { background-color: #24332a !important; color: #e7f0ea !important; border-color: #33463a !important; }

            [data-theme="dark"] .dropdown-menu { background-color: #16211a !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .dropdown-item { color: #dbe6e0 !important; }

            [data-theme="dark"] .dropdown-item:hover { background-color: #24332a !important; }

            [data-theme="dark"] .table { color: #dbe6e0 !important; }

            [data-theme="dark"] .table-bordered, [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color: #2a3a30 !important; }

            [data-theme="dark"] .table-hover tbody tr:hover { background-color: rgba(255,255,255,0.03) !important; }

            [data-theme="dark"] .table-success { background-color: #1c2c22 !important; color: #e7f0ea !important; }

            [data-theme="dark"] .bg-light { background-color: #1b2620 !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .border { border-color: #2a3a30 !important; }

            [data-theme="dark"] .modal-content { background-color: #16211a !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .scroll-list-box { border-color: #2a3a30 !important; }

            [data-theme="dark"] .list-group-item { background-color: transparent !important; color: #dbe6e0 !important; }

            [data-theme="dark"] .alert-light { background-color: #1b2620 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }

            [data-theme="dark"] .bg-white { background-color: #1b2620 !important; }

            /* ورقة الخطاب الرسمية تبقى بيضاء دائماً لأنها تمثل ورقة مطبوعة رسمية */

            [data-theme="dark"] .word-paper { background: #ffffff !important; color: #000 !important; }

            .theme-toggle-btn {

                border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;

                width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;

                color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;

            }

            [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }

            .theme-toggle-btn:hover { background: #123826; color: #fff; }

            [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }

        </style>

        <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">

        <title>إدارة الصلاحيات - نادي فيفا</title>

        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">

        <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>

        <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">

        <style>

            :root { --fifa-green: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; }

            body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }

            .top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }

            .nav-logo { height: 42px; width: auto; object-fit: contain; }

            .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }

            .sidebar { width: 260px; background-color: var(--fifa-green); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }

            @media (max-width: 991.98px) {

                .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }

                .sidebar.show-sidebar { right: 0; }

            }

            .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }

            .mobile-overlay.active { display: block; }

            .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }

            .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }

            .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }

            .content-body { flex: 1; padding: 1.25rem; width: 100%; min-width: 0; overflow-x: hidden; }

            .perm-card { background: #ffffff; border-radius: 12px; border: 1px solid #d5e2d8; box-shadow: 0 4px 15px rgba(0,0,0,0.04); margin-bottom: 1.5rem; overflow: hidden; }

            .perm-header { background-color: var(--fifa-green); color: #fff; padding: 1rem; font-weight: bold; font-size: 1.1rem; }

            .btn-fifa-gold { background-color: var(--fifa-gold); color: #ffffff; font-weight: 700; border: none; }

        </style>

    </head>

    <body>

        <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>

        <nav class="navbar top-navbar sticky-top">

            <div class="container-fluid">

                <div class="d-flex align-items-center gap-2">

                    <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">

                        <i class='bx bx-menu fs-2' style="color: var(--fifa-green);"></i>

                    </button>

                    <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">

                        <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">

                        <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green);">نادي فيفا الرياضي</span>

                    </a>

                </div>

                <div class="d-flex align-items-center gap-2">

<button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">

                    <i class='bx bxs-moon' id="themeToggleIcon"></i>

                </button>

                <div class="dropdown">

                    <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">

                            <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>

                            <span class="fw-bold fs-7" style="color: var(--fifa-green);">{{ dept_name }}</span>

                        </button>

                        <ul class="dropdown-menu dropdown-menu-start shadow">

                            <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>

                        </ul>

                    </div>

                </div>

            </div>

        </nav>

        <div class="main-wrapper">

            <aside class="sidebar" id="sidebarMenu">

                <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">

                    <span class="fw-bold text-white">قائمة التنقل</span>

                    <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>

                </div>

                {% if current_dept['can_page_inbox'] == 1 %}

                <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>

                {% endif %}

                {% if current_dept['can_page_outbox'] == 1 %}

                <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>

                {% endif %}

                {% if current_dept['can_page_achievements'] == 1 %}

                <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>

                {% endif %}

                {% if current_dept['can_page_archive'] == 1 %}

                <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>

                {% endif %}

                {% if current_dept['can_page_quick_upload'] == 1 %}

                <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>

                {% endif %}

                <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>
                {% if current_dept['can_page_chat'] == 1 or is_admin %}

                <a href="/chat" class="sidebar-link"><i class='bx bxs-chat'></i>الشات الداخلي<span class="badge bg-danger rounded-pill ms-1 d-none chat-unread-badge"></span></a>

                {% endif %}

                <script>
                (function(){
                    function pollChatUnread(){
                        fetch('/api/chat_unread_count').then(function(r){ return r.json(); }).then(function(data){
                            document.querySelectorAll('.chat-unread-badge').forEach(function(el){
                                if (data.count > 0) { el.innerText = data.count > 99 ? '99+' : data.count; el.classList.remove('d-none'); }
                                else { el.classList.add('d-none'); }
                            });
                        }).catch(function(){});
                    }
                    pollChatUnread();
                    setInterval(pollChatUnread, 20000);
                })();
                </script>


                <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>

                <a href="/admin/permissions" class="sidebar-link active"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>

                <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>

                <div class="border-top border-secondary my-3 opacity-25"></div>

                <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>

            </aside>

            <main class="content-body">

        <div class="container-fluid p-0">

            <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">

                <h4 class="fw-bold m-0" style="color: var(--fifa-green);"><i class='bx bxs-shield ms-2' style="color: var(--fifa-gold);"></i>لوحة إدارة صلاحيات الإدارات والشبكة</h4>

                <a href="/dashboard" class="btn btn-outline-success fw-bold fs-7"><i class='bx bx-right-arrow-alt ms-1'></i>العودة للنظام</a>

            </div>

            <div class="perm-card mb-4">

                <div class="perm-header d-flex justify-content-between align-items-center">

                    <span><i class='bx bx-list-ol ms-2'></i>ضبط ترقيم الصادر العام</span>

                </div>

                <div class="p-3">

                    <p class="text-muted fs-7 mb-3">

                        الرقم الحالي الذي سيُستخدم في أول خطاب صادر قادم (من أي إدارة): 

                        <strong class="text-success fs-6">{{ current_next_number }}</strong>

                    </p>

                    <form action="/admin/set_letter_number" method="post" class="d-flex flex-wrap gap-2 align-items-end">

                        <div>

                            <label class="form-label fw-bold fs-8 mb-1">تعيين الرقم التالي إلى:</label>

                            <input type="number" name="new_next_number" min="1" class="form-control fs-7" placeholder="مثال: 1 أو 50" required style="width: 160px;">

                        </div>

                        <button type="submit" class="btn btn-fifa-gold fs-7 px-4">حفظ الرقم</button>

                    </form>

                    <form action="/admin/set_letter_number" method="post" class="mt-2" onsubmit="return confirm('تصفير الترقيم والبدء من رقم 1 مجدداً؟');">

                        <input type="hidden" name="new_next_number" value="1">

                        <button type="submit" class="btn btn-outline-danger fs-8 px-3 py-1">

                            <i class='bx bx-reset ms-1'></i> تصفير الترقيم إلى 1

                        </button>

                    </form>

                </div>

            </div>

            <div class="perm-card mb-4 border-warning">

                <div class="perm-header d-flex justify-content-between align-items-center" style="background: linear-gradient(90deg, var(--fifa-gold) 0%, #d4a94a 100%);">

                    <span><i class='bx bx-layer ms-2'></i>تحديث صلاحيات كل الإدارات دفعة واحدة</span>

                </div>

                <div class="p-3">

                    <p class="text-muted fs-7 mb-3">

                        حدد الصلاحيات التي تريدها لجميع الإدارات <strong>دفعة واحدة</strong> بدل تعديل كل إدارة على حدة. عند الضغط على الزر سيتم استبدال هذه الصلاحيات لدى <strong>كل</strong> الإدارات بالقيم المحددة هنا فقط (لن تتأثر أسماء المستخدمين أو كلمات المرور أو أسماء الإدارات أو قوائم الإرسال المخصصة).

                    </p>

                    <form action="/admin/permissions/bulk_update" method="post" onsubmit="return confirm('سيتم استبدال هذه الصلاحيات لدى جميع الإدارات دفعة واحدة. هل أنت متأكد؟');">

                        <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-check-shield ms-1'></i>الصلاحيات العامة:</h6>

                        <div class="row g-2 mb-3">

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_delete" id="bulk_can_delete">

                                    <label class="form-check-label" for="bulk_can_delete">صلاحية الحذف</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_add_user" id="bulk_can_add_user">

                                    <label class="form-check-label" for="bulk_can_add_user">إضافة إدارات جديدة</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_view_all_archive" id="bulk_can_view_all_archive">

                                    <label class="form-check-label" for="bulk_can_view_all_archive">رؤية كامل أرشيف للنادي</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_view_all_achievements" id="bulk_can_view_all_achievements">

                                    <label class="form-check-label" for="bulk_can_view_all_achievements">رؤية إنجازات كافة الإدارات</label>

                                </div>

                            </div>

                        </div>

                        <h6 class="fw-bold text-danger mb-2 fs-7 border-bottom pb-1"><i class='bx bxs-shield-x ms-1'></i>صلاحيات إدارية متقدمة:</h6>

                        <div class="mb-3 border border-danger-subtle rounded p-2" style="background:#fff5f5;">

                            <div class="form-check form-switch fs-7 m-0">

                                <input class="form-check-input" type="checkbox" name="can_page_admin_tools" id="bulk_can_page_admin_tools">

                                <label class="form-check-label fw-bold text-danger" for="bulk_can_page_admin_tools">إظهار "لوحة التحكم الشاملة" و"إدارة الصلاحيات" لكل الإدارات</label>

                            </div>

                            <p class="fs-8 text-muted mb-0 mt-1">تنبيه: صلاحية قوية تمنح الجميع صلاحيات المسؤول الكاملة إن فُعّلت هنا.</p>

                        </div>

                        <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-layout ms-1'></i>صلاحيات فتح الصفحات:</h6>

                        <div class="row g-2 mb-3">

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_inbox" id="bulk_can_page_inbox">

                                    <label class="form-check-label" for="bulk_can_page_inbox">الصندوق الوارد</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_outbox" id="bulk_can_page_outbox">

                                    <label class="form-check-label" for="bulk_can_page_outbox">الخطابات الصادرة</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_achievements" id="bulk_can_page_achievements">

                                    <label class="form-check-label" for="bulk_can_page_achievements">إنجازات الشهر</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_archive" id="bulk_can_page_archive">

                                    <label class="form-check-label" for="bulk_can_page_archive">أرشيف الإدارة</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_quick_upload" id="bulk_can_page_quick_upload">

                                    <label class="form-check-label" for="bulk_can_page_quick_upload">رفع وتوثيق فوري</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_suggestions" id="bulk_can_page_suggestions">

                                    <label class="form-check-label" for="bulk_can_page_suggestions">مشاكل واقتراحات</label>

                                </div>

                            </div>

                            <div class="col-6 col-md-3">

                                <div class="form-check form-switch fs-7">

                                    <input class="form-check-input" type="checkbox" name="can_page_chat" id="bulk_can_page_chat">

                                    <label class="form-check-label" for="bulk_can_page_chat">الشات الداخلي</label>

                                </div>

                            </div>

                        </div>

                        <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-send ms-1'></i>صلاحيات الإرسال:</h6>

                        <div class="form-check form-switch fs-7 mb-3">

                            <input class="form-check-input" type="checkbox" name="can_send_all" id="bulk_can_send_all">

                            <label class="form-check-label" for="bulk_can_send_all">يمكنه إرسال الخطابات لجميع الإدارات (لكل الإدارات دفعة واحدة)</label>

                        </div>

                        <button type="submit" class="btn btn-danger w-100 fs-7 shadow-sm py-2">

                            <i class='bx bx-layer ms-1'></i> تحديث صلاحيات كل الإدارات دفعة واحدة

                        </button>

                    </form>

                </div>

            </div>

            <div class="row">

                {% for d in departments %}

                <div class="col-lg-6">

                    <div class="perm-card">

                        <div class="perm-header d-flex justify-content-between align-items-center">

                            <span><i class='bx bxs-building ms-2'></i>{{ d.name }}</span>

                            <span class="d-flex align-items-center gap-1">

                                <span class="badge bg-warning text-dark fs-8">{{ d.username }}</span>

                                {% if d.is_locked == 1 %}<span class="badge bg-danger fs-8">مقفل</span>{% endif %}

                            </span>

                        </div>

                        <div class="p-3">

                            <form action="/admin/permissions" method="post">

                                <input type="hidden" name="dept_id" value="{{ d.id }}">

                                

                                <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-check-shield ms-1'></i>الصلاحيات العامة:</h6>

                                <div class="row g-2 mb-3">

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_delete" {{ 'checked' if d.can_delete == 1 else '' }}>

                                            <label class="form-check-label">صلاحية الحذف</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_add_user" {{ 'checked' if d.can_add_user == 1 else '' }}>

                                            <label class="form-check-label">إضافة إدارات جديدة</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_view_all_archive" {{ 'checked' if d.can_view_all_archive == 1 else '' }}>

                                            <label class="form-check-label">رؤية كامل أرشيف للنادي</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_view_all_achievements" {{ 'checked' if d.can_view_all_achievements == 1 else '' }}>

                                            <label class="form-check-label">رؤية إنجازات كافة الإدارات</label>

                                        </div>

                                    </div>

                                </div>

                                <h6 class="fw-bold text-danger mb-2 fs-7 border-bottom pb-1"><i class='bx bxs-shield-x ms-1'></i>صلاحيات إدارية متقدمة:</h6>

                                <div class="mb-3 border border-danger-subtle rounded p-2" style="background:#fff5f5;">

                                    <div class="form-check form-switch fs-7 m-0">

                                        <input class="form-check-input" type="checkbox" name="can_page_admin_tools" id="canAdminTools_{{ d.id }}" {{ 'checked' if d.can_page_admin_tools == 1 else '' }}>

                                        <label class="form-check-label fw-bold text-danger" for="canAdminTools_{{ d.id }}">إظهار "لوحة التحكم الشاملة" و"إدارة الصلاحيات" لهذه الإدارة</label>

                                    </div>

                                    <p class="fs-8 text-muted mb-0 mt-1">تنبيه: صلاحية قوية - تتيح لهذه الإدارة رؤية إحصائيات كل الإدارات، وتعديل صلاحيات/كلمات مرور أي حساب، وحذف الحسابات. فعّلها فقط لمن تثق فيه.</p>

                                </div>

                                <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-layout ms-1'></i>صلاحيات فتح الصفحات:</h6>

                                <div class="row g-2 mb-3">

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_inbox" {{ 'checked' if d.can_page_inbox == 1 else '' }}>

                                            <label class="form-check-label">الصندوق الوارد</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_outbox" {{ 'checked' if d.can_page_outbox == 1 else '' }}>

                                            <label class="form-check-label">الخطابات الصادرة</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_achievements" {{ 'checked' if d.can_page_achievements == 1 else '' }}>

                                            <label class="form-check-label">إنجازات الشهر</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_archive" {{ 'checked' if d.can_page_archive == 1 else '' }}>

                                            <label class="form-check-label">أرشيف الإدارة</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_quick_upload" {{ 'checked' if d.can_page_quick_upload == 1 else '' }}>

                                            <label class="form-check-label">رفع وتوثيق فوري</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_suggestions" {{ 'checked' if d.can_page_suggestions == 1 else '' }}>

                                            <label class="form-check-label">مشاكل واقتراحات</label>

                                        </div>

                                    </div>

                                    <div class="col-6">

                                        <div class="form-check form-switch fs-7">

                                            <input class="form-check-input" type="checkbox" name="can_page_chat" {{ 'checked' if d.can_page_chat == 1 else '' }}>

                                            <label class="form-check-label">الشات الداخلي</label>

                                        </div>

                                    </div>

                                </div>

                                <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-send ms-1'></i>صلاحيات الإرسال:</h6>

                                <div class="form-check form-switch fs-7 mb-2">

                                    <input class="form-check-input" type="checkbox" name="can_send_all" id="canSendAll_{{ d.id }}" {{ 'checked' if d.can_send_all == 1 else '' }} onchange="document.getElementById('sendAllowedSection_{{ d.id }}').classList.toggle('d-none', this.checked)">

                                    <label class="form-check-label" for="canSendAll_{{ d.id }}">يمكنه إرسال الخطابات لجميع الإدارات</label>

                                </div>

                                <div id="sendAllowedSection_{{ d.id }}" class="mb-3 border rounded p-2 bg-light {{ 'd-none' if d.can_send_all == 1 else '' }}" style="max-height: 180px; overflow-y: auto;">

                                    <p class="fs-8 text-muted mb-2">أو حدد الإدارات المسموح لهذه الإدارة الإرسال إليها فقط (مثال: تقنية المعلومات أو الرئيس التنفيذي فقط):</p>

                                    {% for other in departments %}

                                        {% if other.id != d.id %}

                                        <div class="form-check fs-8">

                                            <input class="form-check-input" type="checkbox" name="allowed_recipients" value="{{ other.id }}" {{ 'checked' if other.id in dept_allowed_map.get(d.id, []) else '' }}>

                                            <label class="form-check-label">{{ other.name }}</label>

                                        </div>

                                        {% endif %}

                                    {% endfor %}

                                </div>

                                <h6 class="fw-bold text-success mb-2 fs-7 border-bottom pb-1"><i class='bx bx-user-circle ms-1'></i>بيانات الدخول:</h6>

                                <div class="mb-3">

                                    <label class="form-label fw-bold fs-8 mb-1" style="color: var(--fifa-green);">اسم الإدارة / القسم:</label>

                                    <input type="text" name="new_dept_name" class="form-control fs-8" value="{{ d.name }}">

                                </div>

                                <div class="mb-3">

                                    <label class="form-label fw-bold fs-8 mb-1" style="color: var(--fifa-green);">اسم المستخدم:</label>

                                    <input type="text" name="new_username" class="form-control fs-8" value="{{ d.username }}">

                                </div>

                                <div class="mb-3">

                                    <label class="form-label fw-bold fs-8 mb-1" style="color: var(--fifa-green);">تغيير كلمة المرور (اتركه فارغاً للإبقاء):</label>

                                    <input type="password" name="new_password" class="form-control fs-8" placeholder="كلمة مرور جديدة...">

                                </div>

                                <div class="mb-3">

                                    <label class="form-label fw-bold fs-8 mb-1" style="color: var(--fifa-green);">البريد الإلكتروني لهذه الإدارة (لاستعادة كلمة المرور):</label>

                                    <input type="email" name="new_email" class="form-control fs-8" value="{{ d.email or '' }}" placeholder="example@email.com">

                                    <p class="fs-9 text-muted mb-0 mt-1">هذا البريد يضيفه المسؤول فقط من هنا؛ المستخدم نفسه لا يقدر يعدّله. اتركه فارغاً للحذف.</p>

                                </div>

                                {% if d.is_locked == 1 %}

                                <div class="mb-3 bg-light border border-danger rounded p-2">

                                    <div class="form-check form-switch fs-7 m-0">

                                        <input class="form-check-input" type="checkbox" name="unlock_account" id="unlockCheck_{{ d.id }}">

                                        <label class="form-check-label text-danger fw-bold" for="unlockCheck_{{ d.id }}">فتح القفل عن هذا الحساب (مقفل حالياً بعد 5 محاولات دخول خاطئة)</label>

                                    </div>

                                </div>

                                {% endif %}

 

                                <button type="submit" class="btn btn-fifa-gold w-100 fs-7 shadow-sm py-2">تحديث صلاحيات {{ d.name }}</button>

                            </form>

                            {% if not (d.name == 'الرئيس التنفيذي' or d.name == 'رئيس تنفيذي' or d.name == 'CEO' or d.name == 'مدير تقنية المعلومات' or d.name == 'مدير تقنية معلومات' or d.name == 'تقنية المعلومات' or d.name == 'IT Manager' or d.name == 'IT' or 'تقنية' in d.name or 'تنفيذي' in d.name) %}

                            <form action="/admin/delete_department/{{ d.id }}" method="get" class="mt-2" onsubmit="return confirm('تحذير: سيتم حذف هذه الإدارة/المستخدم وكل خطاباته وملفاته نهائياً. هل أنت متأكد؟');">

                                <button type="submit" class="btn btn-outline-danger w-100 fs-7 py-2">

                                    <i class='bx bx-user-x ms-1'></i> حذف هذا المستخدم نهائياً

                                </button>

                            </form>

                            {% endif %}

                        </div>

                    </div>

                </div>

                {% endfor %}

            </div>

        </div>

            </main>

        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <script>

        function updateFifaThemeIcon() {

            var icon = document.getElementById('themeToggleIcon');

            if (!icon) return;

            var isDark = document.documentElement.getAttribute('data-theme') === 'dark';

            icon.className = isDark ? 'bx bxs-sun' : 'bx bxs-moon';

        }

        function toggleFifaTheme() {

            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';

            var next = current === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', next);

            try { localStorage.setItem('fifa_theme', next); } catch (e) {}

            updateFifaThemeIcon();

        }

        updateFifaThemeIcon();

    </script>

        <script>

            function updateNavbarHeightVar() {

                var nav = document.querySelector('.top-navbar');

                if (nav) { document.documentElement.style.setProperty('--navbar-height', nav.offsetHeight + 'px'); }

            }

            updateNavbarHeightVar();

            window.addEventListener('load', updateNavbarHeightVar);

            window.addEventListener('resize', updateNavbarHeightVar);

            function toggleSidebar() {

                document.getElementById('sidebarMenu').classList.toggle('show-sidebar');

                document.getElementById('mobileOverlay').classList.toggle('active');

            }

            (function() {

                var touchStartX = 0;

                var touchStartY = 0;

                var edgeThreshold = 25;

                var swipeThreshold = 60;

                document.addEventListener('touchstart', function(e) {

                    touchStartX = e.touches[0].clientX;

                    touchStartY = e.touches[0].clientY;

                }, { passive: true });

                document.addEventListener('touchend', function(e) {

                    if (window.innerWidth > 991.98) return;

                    var sidebarEl = document.getElementById('sidebarMenu');

                    if (!sidebarEl) return;

                    var touchEndX = e.changedTouches[0].clientX;

                    var touchEndY = e.changedTouches[0].clientY;

                    var deltaX = touchEndX - touchStartX;

                    var deltaY = touchEndY - touchStartY;

                    if (Math.abs(deltaY) > 60) return;

                    var isOpen = sidebarEl.classList.contains('show-sidebar');

                    if (!isOpen && touchStartX > (window.innerWidth - edgeThreshold) && deltaX < -swipeThreshold) {

                        toggleSidebar();

                    }

                    else if (isOpen && deltaX > swipeThreshold) {

                        toggleSidebar();

                    }

                }, { passive: true });

            })();

        </script>

    </body>

    </html>

    '''

    return render_template_string(html_code, departments=departments, dept_name=session['dept_name'], current_dept=current_dept, current_next_number=current_next_number, dept_allowed_map=dept_allowed_map)

# --- تحديث صلاحيات كل الإدارات دفعة واحدة (بدل تعديل كل إدارة على حدة) ---

@app.route('/admin/permissions/bulk_update', methods=['POST'])

def admin_permissions_bulk_update():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))

    current_dept = cursor.fetchone()

    if not is_admin and (not current_dept or current_dept['can_page_admin_tools'] != 1):

        cursor.close()

        conn.close()

        return '''<script>alert("عذراً، صفحة إدارة الصلاحيات مخصصة للمسؤولين فقط."); window.location.href="/dashboard";</script>'''

    can_delete = 1 if request.form.get('can_delete') else 0

    can_view_all_archive = 1 if request.form.get('can_view_all_archive') else 0

    can_view_all_achievements = 1 if request.form.get('can_view_all_achievements') else 0

    can_add_user = 1 if request.form.get('can_add_user') else 0

    can_page_inbox = 1 if request.form.get('can_page_inbox') else 0

    can_page_outbox = 1 if request.form.get('can_page_outbox') else 0

    can_page_achievements = 1 if request.form.get('can_page_achievements') else 0

    can_page_archive = 1 if request.form.get('can_page_archive') else 0

    can_page_quick_upload = 1 if request.form.get('can_page_quick_upload') else 0

    can_page_suggestions = 1 if request.form.get('can_page_suggestions') else 0
    can_page_chat = 1 if request.form.get('can_page_chat') else 0


    can_send_all = 1 if request.form.get('can_send_all') else 0

    can_page_admin_tools = 1 if request.form.get('can_page_admin_tools') else 0

    # يُطبَّق على كل الإدارات دفعة واحدة (بدون شرط WHERE) - لا يمس أسماء المستخدمين/كلمات المرور/أسماء الإدارات

    # ولا قوائم الإرسال المخصصة في send_permissions حتى لا تُفقد إعدادات دقيقة سابقة بالخطأ.

    cursor.execute('''

        UPDATE departments SET

            can_delete = %s, can_view_all_archive = %s, can_view_all_achievements = %s, can_add_user = %s,

            can_page_inbox = %s, can_page_outbox = %s, can_page_achievements = %s, can_page_archive = %s,

            can_page_quick_upload = %s, can_page_suggestions = %s, can_page_chat = %s, can_send_all = %s, can_page_admin_tools = %s

    ''', (can_delete, can_view_all_archive, can_view_all_achievements, can_add_user,

          can_page_inbox, can_page_outbox, can_page_achievements, can_page_archive,

          can_page_quick_upload, can_page_suggestions, can_page_chat, can_send_all, can_page_admin_tools))

    conn.commit()

    cursor.close()

    conn.close()

    return '''<script>alert("تم تحديث صلاحيات كل الإدارات دفعة واحدة بنجاح!"); window.location.href="/admin/permissions";</script>'''

@app.route('/admin/set_letter_number', methods=['POST'])

def set_letter_number():

    if 'dept_id' not in session:

        return redirect(url_for('login'))

    is_admin = is_admin_user(session.get('dept_name'))

    if not is_admin:

        return '''<script>alert("عذراً، هذه الصلاحية للمسؤولين فقط."); window.location.href="/dashboard";</script>'''

    new_number = request.form.get('new_next_number')

    if not new_number or not new_number.isdigit() or int(new_number) < 1:

        return '''<script>alert("الرجاء إدخال رقم صحيح أكبر من صفر."); window.location.href="/admin/permissions";</script>'''

    conn = get_db_connection()

    cursor = conn.cursor()

    cursor.execute('''

        UPDATE system_settings 

        SET next_letter_number = %s 

        WHERE id = (SELECT id FROM system_settings ORDER BY id LIMIT 1)

    ''', (int(new_number),))

    conn.commit()

    cursor.close()

    conn.close()

    return f'''<script>alert("تم تحديث رقم الصادر القادم ليصبح: {new_number}"); window.location.href="/admin/permissions";</script>'''


CHAT_HTML = '''
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script>
        (function () {
            try {
                var t = localStorage.getItem('fifa_theme');
                if (!t) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }
                document.documentElement.setAttribute('data-theme', t);
            } catch (e) {}
        })();
    </script>
    <style>
        [data-theme="dark"] { color-scheme: dark; }
        [data-theme="dark"] body { background: linear-gradient(135deg, #0e1712 0%, #131f19 100%) !important; background-color: #0f1712 !important; color: #dbe6e0 !important; }
        [data-theme="dark"] .top-navbar { background-color: rgba(20, 28, 24, 0.95) !important; border-bottom-color: #c5a059 !important; }
        [data-theme="dark"] .modern-card, [data-theme="dark"] .chat-shell, [data-theme="dark"] .chat-list-pane,
        [data-theme="dark"] .chat-window-pane, [data-theme="dark"] .modal-content {
            background: #16211a !important; border-color: #2a3a30 !important; color: #dbe6e0 !important;
        }
        [data-theme="dark"] .conv-item:hover, [data-theme="dark"] .conv-item.active { background-color: rgba(255,255,255,0.05) !important; }
        [data-theme="dark"] .conv-item { border-bottom-color: #2a3a30 !important; }
        [data-theme="dark"] .chat-window-header, [data-theme="dark"] .chat-input-bar { background: #16211a !important; border-color: #2a3a30 !important; }
        [data-theme="dark"] .msg-theirs .msg-bubble { background: #1f2b23 !important; color: #dbe6e0 !important; border-color: #2a3a30 !important; }
        [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea {
            background-color: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important;
        }
        [data-theme="dark"] .form-control::placeholder { color: #7c8c82 !important; }
        [data-theme="dark"] .text-muted { color: #9fb0a7 !important; }
        [data-theme="dark"] .chat-empty-state { color: #7c8c82 !important; }
        .theme-toggle-btn {
            border: 1px solid #d5e2d8; background: #f8faf9; border-radius: 8px;
            width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center;
            color: #123826; font-size: 1.15rem; transition: all 0.2s; cursor: pointer;
        }
        [data-theme="dark"] .theme-toggle-btn { background: #1b2620 !important; border-color: #33463a !important; color: #e7f0ea !important; }
        .theme-toggle-btn:hover { background: #123826; color: #fff; }
        [data-theme="dark"] .theme-toggle-btn:hover { background: #24332a !important; }
    </style>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo1.png') }}">
    <title>الشات الداخلي - نادي فيفا</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">
    <link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>
    <link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        :root { --fifa-green-primary: #123826; --fifa-gold: #c5a059; --fifa-bg: #eaf3ec; --fifa-card-border: #d5e2d8; }
        body { font-family: 'Almarai', sans-serif; background-color: var(--fifa-bg); color: #2b302e; overflow-x: hidden; }
        .top-navbar { background-color: rgba(255, 255, 255, 0.95); backdrop-filter: blur(5px); border-bottom: 3px solid var(--fifa-gold); padding: 0.6rem 1rem; box-shadow: 0 2px 10px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 1045; }
        .nav-logo { height: 42px; width: auto; object-fit: contain; }
        .main-wrapper { display: flex; min-height: calc(100vh - 76px); position: relative; }
        .sidebar { width: 260px; background-color: var(--fifa-green-primary); color: #ecf0f1; padding-top: 1rem; flex-shrink: 0; transition: all 0.3s ease; z-index: 1040; }
        @media (max-width: 991.98px) {
            .sidebar { position: fixed; top: var(--navbar-height, 76px); right: -260px; height: calc(100vh - var(--navbar-height, 76px)); box-shadow: -5px 0 15px rgba(0,0,0,0.2); overflow-y: auto; -webkit-overflow-scrolling: touch; }
            .sidebar.show-sidebar { right: 0; }
        }
        .mobile-overlay { display: none; position: fixed; top: var(--navbar-height, 76px); left: 0; right: 0; bottom: 0; background-color: rgba(0,0,0,0.5); z-index: 1030; }
        .mobile-overlay.active { display: block; }
        .sidebar-link { display: flex; align-items: center; color: #d1e0d8; text-decoration: none; padding: 12px 20px; border-right: 4px solid transparent; transition: all 0.25s; font-size: 0.95rem; }
        .sidebar-link:hover, .sidebar-link.active { background-color: rgba(255, 255, 255, 0.08); color: #ffffff; border-right-color: var(--fifa-gold); font-weight: 700; }
        .sidebar-link i { font-size: 1.35rem; margin-left: 12px; color: var(--fifa-gold); }
        .content-body { flex: 1; padding: 1.25rem; width: 100%; min-width: 0; overflow-x: hidden; }

        .chat-shell { display: flex; height: calc(100vh - 76px - 2.5rem); border: 1px solid var(--fifa-card-border); border-radius: 12px; overflow: hidden; background: #fff; box-shadow: 0 4px 15px rgba(0,0,0,0.03); }
        .chat-list-pane { width: 340px; flex-shrink: 0; display: flex; flex-direction: column; border-inline-start: 1px solid var(--fifa-card-border); background: #fff; }
        .chat-list-header { padding: 1rem; border-bottom: 1px solid var(--fifa-card-border); }
        .chat-list-search { padding: 0.6rem 1rem; border-bottom: 1px solid var(--fifa-card-border); }
        .chat-list-scroll { flex: 1; overflow-y: auto; }
        .conv-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.8rem 1rem; border-bottom: 1px solid #f0f4f2; cursor: pointer; text-decoration: none; color: inherit; }
        .conv-item:hover, .conv-item.active { background-color: rgba(18, 56, 38, 0.06); }
        .conv-avatar { width: 42px; height: 42px; border-radius: 50%; background: var(--fifa-green-primary); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; flex-shrink: 0; }
        .conv-avatar.group-avatar { background: var(--fifa-gold); color: #123826; }
        .conv-meta { flex: 1; min-width: 0; }
        .conv-name { font-weight: 700; font-size: 0.92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .conv-preview { font-size: 0.8rem; color: #8a978f; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .conv-side { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0; }
        .conv-time { font-size: 0.7rem; color: #9fb0a7; }
        .conv-unread-badge { background: var(--fifa-green-primary); color: #fff; font-size: 0.7rem; border-radius: 20px; min-width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; padding: 0 5px; }
        .conv-delete-btn { background: transparent; border: none; color: #b5c2bb; font-size: 1rem; padding: 2px; line-height: 1; display: flex; }
        .conv-delete-btn:hover { color: #d9534f; }
        [data-theme="dark"] .conv-delete-btn { color: #5a6b62; }
        [data-theme="dark"] .conv-delete-btn:hover { color: #e57373; }
        .chat-delete-btn { background: #f0f4f2; color: #8a978f; border: none; width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1.1rem; flex-shrink: 0; }
        .chat-delete-btn:hover { background: #fbe7e6; color: #d9534f; }

        .chat-window-pane { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #f4f8f5; }
        .chat-window-header { padding: 0.8rem 1rem; border-bottom: 1px solid var(--fifa-card-border); background: #fff; display: flex; align-items: center; gap: 0.7rem; }
        .chat-messages-scroll { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 0.55rem; }
        .msg-row { display: flex; flex-direction: column; max-width: 68%; }
        .msg-mine { align-self: flex-end; }
        .msg-theirs { align-self: flex-start; }
        .msg-sender-name { font-size: 0.72rem; color: var(--fifa-gold); font-weight: 700; margin-bottom: 2px; padding: 0 4px; }
        .msg-bubble { padding: 0.55rem 0.8rem; border-radius: 14px; font-size: 0.9rem; line-height: 1.5; word-break: break-word; }
        .msg-mine .msg-bubble { background: var(--fifa-green-primary); color: #fff; border-bottom-left-radius: 4px; }
        .msg-theirs .msg-bubble { background: #fff; border: 1px solid #e3e9e5; border-bottom-right-radius: 4px; }
        .msg-time { font-size: 0.65rem; color: #9fb0a7; margin-top: 2px; padding: 0 4px; }
        .msg-mine .msg-time { text-align: left; }
        .msg-file-card { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.6rem; border-radius: 10px; background: rgba(0,0,0,0.05); text-decoration: none; color: inherit; }
        .msg-mine .msg-file-card { background: rgba(255,255,255,0.15); color: #fff; }
        .msg-image-attach { max-width: 220px; border-radius: 10px; display: block; margin-top: 4px; }
        .chat-input-bar { padding: 0.7rem 1rem; border-top: 1px solid var(--fifa-card-border); background: #fff; display: flex; align-items: center; gap: 0.5rem; }
        .chat-input-bar textarea { resize: none; }
        .chat-empty-state { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #9fb0a7; gap: 0.5rem; }
        .chat-attach-btn, .chat-send-btn { width: 40px; height: 40px; border-radius: 50%; border: none; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 1.2rem; }
        .chat-attach-btn { background: #f0f4f2; color: var(--fifa-green-primary); }
        .chat-send-btn { background: var(--fifa-green-primary); color: #fff; }
        .file-preview-chip { display: inline-flex; align-items: center; gap: 6px; background: #f0f4f2; border-radius: 20px; padding: 4px 10px; font-size: 0.8rem; margin-bottom: 6px; }
        .contact-pick-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem; border-radius: 8px; cursor: pointer; }
        .contact-pick-item:hover { background: #f0f4f2; }
        .typing-indicator { font-size: 0.75rem; color: var(--fifa-gold); padding: 0 1rem 4px; min-height: 16px; }
        .chat-back-btn { display: none; }
        @media (max-width: 767.98px) {
            .chat-shell { position: relative; }
            .chat-list-pane { position: absolute; inset: 0; z-index: 2; width: 100%; }
            .chat-window-pane { position: absolute; inset: 0; z-index: 1; }
            .chat-shell.conv-open .chat-list-pane { display: none; }
            .chat-shell:not(.conv-open) .chat-window-pane { display: none; }
            .chat-back-btn { display: inline-flex; }
        }
    </style>
</head>
<body>
    <div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>
    <nav class="navbar top-navbar sticky-top">
        <div class="container-fluid">
            <div class="d-flex align-items-center gap-2">
                <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()">
                    <i class='bx bx-menu fs-2' style="color: var(--fifa-green-primary);"></i>
                </button>
                <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">
                    <img src="{{ url_for('static', filename='logo.png') }}" alt="نادي فيفا" class="nav-logo" onerror="this.style.display='none'">
                    <span class="fw-bold fs-6 lh-1" style="color: var(--fifa-green-primary);">نادي فيفا الرياضي</span>
                </a>
            </div>
            <div class="d-flex align-items-center gap-2">
            <button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" title="تبديل الوضع الليلي/النهاري" id="themeToggleBtn">
                <i class='bx bxs-moon' id="themeToggleIcon"></i>
            </button>
            <div class="dropdown">
                <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">
                    <i class='bx bxs-user-circle fs-4 ms-1' style="color: var(--fifa-gold);"></i>
                    <span class="fw-bold fs-7" style="color: var(--fifa-green-primary);">{{ dept_name }}</span>
                </button>
                <ul class="dropdown-menu dropdown-menu-start shadow">
                    <li><a class="dropdown-item text-danger py-2" href="/logout"><i class='bx bx-log-out ms-2'></i>تسجيل الخروج</a></li>
                </ul>
            </div>
        </div>
        </div>
    </nav>
    <div class="main-wrapper">
        <aside class="sidebar" id="sidebarMenu">
            <div class="d-flex justify-content-between align-items-center px-3 mb-2 d-lg-none">
                <span class="fw-bold text-white">قائمة التنقل</span>
                <button class="btn text-white fs-3 p-0" onclick="toggleSidebar()">&times;</button>
            </div>
            {% if current_dept['can_page_inbox'] == 1 or is_admin %}
            <a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>
            {% endif %}
            {% if current_dept['can_page_outbox'] == 1 or is_admin %}
            <a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>
            {% endif %}
            {% if current_dept['can_page_achievements'] == 1 or is_admin %}
            <a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>
            {% endif %}
            {% if current_dept['can_page_archive'] == 1 or is_admin %}
            <a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>
            {% endif %}
            {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}
            <a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color: var(--fifa-gold);"></i>رفع وتوثيق فوري</a>
            {% endif %}
            {% if current_dept['can_page_suggestions'] == 1 or is_admin %}
            <a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>
            {% endif %}
            {% if current_dept['can_page_chat'] == 1 or is_admin %}

            <a href="/chat" class="sidebar-link active"><i class='bx bxs-chat'></i>الشات الداخلي</a>

            {% endif %}
            {% if is_admin or current_dept['can_page_admin_tools'] == 1 %}
            <a href="/admin/dashboard" class="sidebar-link" style="background-color: rgba(197, 160, 89, 0.2);"><i class='bx bxs-cog' style="color: var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>
            <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>
            {% endif %}
            {% if current_dept['can_add_user'] == 1 %}
            <a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>
            {% endif %}
            <div class="border-top border-secondary my-3 opacity-25"></div>
            <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>
        </aside>
        <main class="content-body">
            <div class="container-fluid p-0">
                <div class="chat-shell" id="chatShell">
                    <div class="chat-list-pane">
                        <div class="chat-list-header d-flex justify-content-between align-items-center">
                            <h6 class="fw-bold mb-0" style="color: var(--fifa-green-primary);"><i class='bx bxs-chat ms-1' style="color: var(--fifa-gold);"></i>الشات الداخلي</h6>
                            <div class="d-flex gap-2">
                                <button class="btn btn-sm btn-light border" title="محادثة جديدة" onclick="openNewChatModal()"><i class='bx bx-message-square-add'></i></button>
                                <button class="btn btn-sm btn-light border" title="مجموعة جديدة" onclick="openNewGroupModal()"><i class='bx bx-group'></i></button>
                            </div>
                        </div>
                        <div class="chat-list-search">
                            <input type="text" class="form-control form-control-sm" placeholder="بحث في المحادثات..." id="convSearchInput" oninput="filterConversations()">
                        </div>
                        <div class="chat-list-scroll" id="convListScroll">
                            <div class="text-center text-muted small p-4" id="convListEmpty">لا توجد محادثات بعد. ابدأ محادثة جديدة من الأعلى.</div>
                        </div>
                    </div>
                    <div class="chat-window-pane" id="chatWindowPane">
                        <div class="chat-empty-state" id="chatEmptyState">
                            <i class='bx bxs-message-rounded-dots' style="font-size: 3rem;"></i>
                            <div>اختر محادثة من القائمة أو ابدأ محادثة جديدة</div>
                        </div>
                        <div id="chatActiveWindow" style="display:none; flex:1; min-height:0; display:flex; flex-direction:column;">
                            <div class="chat-window-header">
                                <button class="btn btn-sm btn-light border chat-back-btn" onclick="closeMobileConversation()"><i class='bx bx-arrow-back'></i></button>
                                <div class="conv-avatar" id="activeConvAvatar">؟</div>
                                <div class="flex-grow-1 min-width-0">
                                    <div class="fw-bold" id="activeConvName" style="font-size:0.95rem;">-</div>
                                    <div class="text-muted" id="activeConvSub" style="font-size:0.72rem;"></div>
                                </div>
                                <button type="button" class="chat-delete-btn" title="حذف المحادثة" onclick="deleteConversation(currentConversationId)"><i class='bx bx-trash'></i></button>
                            </div>
                            <div class="chat-messages-scroll" id="chatMessagesScroll">
                                <button class="btn btn-sm btn-light border w-100 mb-2 d-none" id="loadOlderBtn" onclick="loadOlderMessages()">تحميل رسائل أقدم</button>
                            </div>
                            <div class="typing-indicator" id="typingIndicator"></div>
                            <div class="chat-input-bar">
                                <input type="file" id="chatFileInput" style="display:none" onchange="onChatFileSelected()">
                                <button type="button" class="chat-attach-btn" title="إرفاق ملف/صورة" onclick="document.getElementById('chatFileInput').click()"><i class='bx bx-paperclip'></i></button>
                                <div class="flex-grow-1">
                                    <div id="filePreviewChip" style="display:none;" class="file-preview-chip">
                                        <i class='bx bx-file'></i><span id="filePreviewName"></span>
                                        <i class='bx bx-x' style="cursor:pointer;" onclick="clearChatFile()"></i>
                                    </div>
                                    <textarea class="form-control" id="chatTextInput" rows="1" placeholder="اكتب رسالتك هنا..." onkeydown="onChatInputKeydown(event)" oninput="onChatInputTyping()"></textarea>
                                </div>
                                <button type="button" class="chat-send-btn" onclick="sendChatMessage()"><i class='bx bx-send'></i></button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <div class="modal fade" id="newChatModal" tabindex="-1">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header">
                    <h6 class="modal-title fw-bold">بدء محادثة جديدة</h6>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <input type="text" class="form-control form-control-sm mb-2" placeholder="بحث عن إدارة..." oninput="filterContactList(this.value, 'newChatContactsList')">
                    <div id="newChatContactsList" style="max-height: 320px; overflow-y:auto;"></div>
                </div>
            </div>
        </div>
    </div>

    <div class="modal fade" id="newGroupModal" tabindex="-1">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header">
                    <h6 class="modal-title fw-bold">إنشاء مجموعة جديدة</h6>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <input type="text" class="form-control mb-2" id="newGroupNameInput" placeholder="اسم المجموعة">
                    <input type="text" class="form-control form-control-sm mb-2" placeholder="بحث عن إدارة..." oninput="filterContactList(this.value, 'newGroupContactsList')">
                    <div id="newGroupContactsList" style="max-height: 260px; overflow-y:auto;"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-fifa-primary" style="background-color: var(--fifa-green-primary); color:#fff; border:none;" onclick="submitNewGroup()">إنشاء المجموعة</button>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function toggleSidebar() {
            document.getElementById('sidebarMenu').classList.toggle('show-sidebar');
            document.getElementById('mobileOverlay').classList.toggle('active');
        }
        function toggleFifaTheme() {
            var cur = document.documentElement.getAttribute('data-theme');
            var next = cur === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            try { localStorage.setItem('fifa_theme', next); } catch (e) {}
        }

        var socket = io();
        var currentConversationId = null;
        var conversationsCache = {};
        var selectedGroupMembers = new Set();
        var pendingFile = null;
        var oldestLoadedMessageId = null;
        var typingTimeoutHandle = null;

        function escapeHtml(str) {
            if (!str) return '';
            var div = document.createElement('div');
            div.innerText = str;
            return div.innerHTML;
        }

        function avatarLetter(name) {
            return (name || '?').trim().charAt(0);
        }

        function formatTime(t) {
            if (!t) return '';
            var parts = t.split(' ');
            return parts.length > 1 ? parts[1] : t;
        }

        function loadConversations(preserveSelection) {
            fetch('/chat/api/conversations').then(function (r) { return r.json(); }).then(function (data) {
                var list = data.conversations || [];
                var scrollBox = document.getElementById('convListScroll');
                var emptyMsg = document.getElementById('convListEmpty');
                scrollBox.querySelectorAll('.conv-item').forEach(function (el) { el.remove(); });
                if (list.length === 0) {
                    emptyMsg.style.display = 'block';
                } else {
                    emptyMsg.style.display = 'none';
                }
                var totalUnread = 0;
                list.forEach(function (conv) {
                    conversationsCache[conv.id] = conv;
                    totalUnread += conv.unread || 0;
                    var el = document.createElement('a');
                    el.href = 'javascript:void(0)';
                    el.className = 'conv-item' + (conv.id === currentConversationId ? ' active' : '');
                    el.setAttribute('data-name', conv.name || '');
                    el.setAttribute('data-conv-id', conv.id);
                    el.onclick = function () { openConversation(conv.id); };
                    var avatarClass = conv.type === 'group' ? 'conv-avatar group-avatar' : 'conv-avatar';
                    var icon = conv.type === 'group' ? "<i class='bx bx-group'></i>" : escapeHtml(avatarLetter(conv.name));
                    var previewPrefix = (conv.type === 'group' && conv.last_sender_name) ? escapeHtml(conv.last_sender_name) + ': ' : '';
                    el.innerHTML =
                        '<div class="' + avatarClass + '">' + icon + '</div>' +
                        '<div class="conv-meta">' +
                            '<div class="conv-name">' + escapeHtml(conv.name) + (conv.type === 'group' ? ' <span class="text-muted fs-8">(' + conv.participants_count + ')</span>' : '') + '</div>' +
                            '<div class="conv-preview">' + previewPrefix + escapeHtml(conv.last_message || 'لا توجد رسائل بعد') + '</div>' +
                        '</div>' +
                        '<div class="conv-side">' +
                            '<div class="conv-time">' + formatTime(conv.last_at) + '</div>' +
                            (conv.unread > 0 ? '<div class="conv-unread-badge">' + conv.unread + '</div>' : '') +
                            '<button type="button" class="conv-delete-btn" title="حذف المحادثة" onclick="deleteConversation(' + conv.id + ', event)"><i class="bx bx-trash"></i></button>' +
                        '</div>';
                    scrollBox.appendChild(el);
                });
                updateGlobalChatBadges(totalUnread);
            }).catch(function () {});
        }

        function updateGlobalChatBadges(count) {
            document.querySelectorAll('.chat-unread-badge').forEach(function (el) {
                if (count > 0) { el.innerText = count > 99 ? '99+' : count; el.classList.remove('d-none'); }
                else { el.classList.add('d-none'); }
            });
        }

        function filterConversations() {
            var q = document.getElementById('convSearchInput').value.trim();
            document.querySelectorAll('#convListScroll .conv-item').forEach(function (el) {
                var name = el.getAttribute('data-name') || '';
                el.style.display = name.indexOf(q) !== -1 ? 'flex' : 'none';
            });
        }

        function openConversation(id) {
            if (currentConversationId) {
                socket.emit('leave_conversation', { conversation_id: currentConversationId });
            }
            currentConversationId = id;
            oldestLoadedMessageId = null;
            document.getElementById('chatShell').classList.add('conv-open');
            document.getElementById('chatEmptyState').style.display = 'none';
            document.getElementById('chatActiveWindow').style.display = 'flex';
            document.getElementById('typingIndicator').innerText = '';
            var conv = conversationsCache[id];
            if (conv) {
                document.getElementById('activeConvName').innerText = conv.name;
                document.getElementById('activeConvSub').innerText = conv.type === 'group' ? (conv.participants_count + ' أعضاء') : '';
                var avatarEl = document.getElementById('activeConvAvatar');
                avatarEl.className = conv.type === 'group' ? 'conv-avatar group-avatar' : 'conv-avatar';
                avatarEl.innerHTML = conv.type === 'group' ? "<i class='bx bx-group'></i>" : escapeHtml(avatarLetter(conv.name));
            }
            document.querySelectorAll('#convListScroll .conv-item').forEach(function (el) { el.classList.remove('active'); });
            socket.emit('join_conversation', { conversation_id: id });
            var msgBox = document.getElementById('chatMessagesScroll');
            msgBox.querySelectorAll('.msg-row').forEach(function (el) { el.remove(); });
            fetch('/chat/api/messages/' + id).then(function (r) { return r.json(); }).then(function (data) {
                var messages = data.messages || [];
                document.getElementById('loadOlderBtn').classList.toggle('d-none', !data.has_more);
                messages.forEach(function (m) { appendMessageBubble(m, false); });
                if (messages.length > 0) oldestLoadedMessageId = messages[0].id;
                scrollMessagesToBottom();
                markConversationRead(id);
            });
            loadConversations();
        }

        function closeMobileConversation() {
            document.getElementById('chatShell').classList.remove('conv-open');
        }

        function loadOlderMessages() {
            if (!currentConversationId || !oldestLoadedMessageId) return;
            var msgBox = document.getElementById('chatMessagesScroll');
            var prevHeight = msgBox.scrollHeight;
            fetch('/chat/api/messages/' + currentConversationId + '?before_id=' + oldestLoadedMessageId).then(function (r) { return r.json(); }).then(function (data) {
                var messages = data.messages || [];
                document.getElementById('loadOlderBtn').classList.toggle('d-none', !data.has_more);
                var afterBtn = document.getElementById('loadOlderBtn');
                messages.slice().reverse().forEach(function (m) {
                    var row = buildMessageRow(m);
                    afterBtn.insertAdjacentElement('afterend', row);
                });
                if (messages.length > 0) oldestLoadedMessageId = messages[0].id;
                msgBox.scrollTop = msgBox.scrollHeight - prevHeight;
            });
        }

        function markConversationRead(id) {
            fetch('/chat/api/mark_read/' + id, { method: 'POST' }).then(function () {
                if (conversationsCache[id]) conversationsCache[id].unread = 0;
                var totalUnread = 0;
                Object.keys(conversationsCache).forEach(function (k) { totalUnread += conversationsCache[k].unread || 0; });
                updateGlobalChatBadges(totalUnread);
            });
        }

        function deleteConversation(id, ev) {
            if (ev) { ev.stopPropagation(); ev.preventDefault(); }
            if (!id) return;
            var conv = conversationsCache[id];
            var name = conv ? conv.name : 'هذه المحادثة';
            if (!confirm('حذف محادثة "' + name + '" من قائمتك؟\\nلن تُحذف عند الطرف الآخر، وستظهر تلقائياً من جديد لو وصلتك رسالة جديدة فيها.')) return;
            fetch('/chat/api/delete_conversation/' + id, { method: 'POST' }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) { alert(data.error || 'تعذر حذف المحادثة'); return; }
                removeConversationFromUi(id);
            });
        }

        function removeConversationFromUi(id) {
            delete conversationsCache[id];
            var el = document.querySelector('#convListScroll .conv-item[data-conv-id="' + id + '"]');
            if (el) el.remove();
            if (currentConversationId === id) {
                currentConversationId = null;
                document.getElementById('chatShell').classList.remove('conv-open');
                document.getElementById('chatEmptyState').style.display = 'flex';
                document.getElementById('chatActiveWindow').style.display = 'none';
            }
            var totalUnread = 0;
            Object.keys(conversationsCache).forEach(function (k) { totalUnread += conversationsCache[k].unread || 0; });
            updateGlobalChatBadges(totalUnread);
            if (document.getElementById('convListScroll').querySelectorAll('.conv-item').length === 0) {
                document.getElementById('convListEmpty').style.display = 'block';
            }
        }

        function buildMessageRow(m) {
            var row = document.createElement('div');
            row.className = 'msg-row ' + (m.is_mine ? 'msg-mine' : 'msg-theirs');
            row.setAttribute('data-msg-id', m.id);
            var senderLabel = (!m.is_mine && conversationsCache[currentConversationId] && conversationsCache[currentConversationId].type === 'group') ?
                '<div class="msg-sender-name">' + escapeHtml(m.sender_name) + '</div>' : '';
            var body = '';
            if (m.content) { body += escapeHtml(m.content).replace(/\\n/g, '<br>'); }
            if (m.has_file) {
                var isImage = m.file_mimetype && m.file_mimetype.indexOf('image/') === 0;
                if (isImage) {
                    body += (body ? '<br>' : '') + '<a href="/chat/view/' + m.id + '" target="_blank"><img class="msg-image-attach" src="/chat/view/' + m.id + '"></a>';
                } else {
                    body += (body ? '<br>' : '') + '<a href="/chat/download/' + m.id + '" class="msg-file-card"><i class="bx bxs-file-blank fs-5"></i><span>' + escapeHtml(m.file_name || 'ملف') + '</span></a>';
                }
            }
            row.innerHTML = senderLabel + '<div class="msg-bubble">' + body + '</div><div class="msg-time">' + formatTime(m.created_at) + '</div>';
            return row;
        }

        function appendMessageBubble(m, animate) {
            var msgBox = document.getElementById('chatMessagesScroll');
            if (msgBox.querySelector('[data-msg-id="' + m.id + '"]')) { return; }
            var row = buildMessageRow(m);
            msgBox.appendChild(row);
        }

        function scrollMessagesToBottom() {
            var msgBox = document.getElementById('chatMessagesScroll');
            msgBox.scrollTop = msgBox.scrollHeight;
        }

        function onChatFileSelected() {
            var input = document.getElementById('chatFileInput');
            if (input.files && input.files[0]) {
                pendingFile = input.files[0];
                document.getElementById('filePreviewName').innerText = pendingFile.name;
                document.getElementById('filePreviewChip').style.display = 'inline-flex';
            }
        }
        function clearChatFile() {
            pendingFile = null;
            document.getElementById('chatFileInput').value = '';
            document.getElementById('filePreviewChip').style.display = 'none';
        }

        function onChatInputKeydown(ev) {
            if (ev.key === 'Enter' && !ev.shiftKey) {
                ev.preventDefault();
                sendChatMessage();
            }
        }
        function onChatInputTyping() {
            if (!currentConversationId) return;
            if (typingTimeoutHandle) return;
            socket.emit('typing', { conversation_id: currentConversationId });
            typingTimeoutHandle = setTimeout(function () { typingTimeoutHandle = null; }, 2000);
        }

        function sendChatMessage() {
            if (!currentConversationId) return;
            var textInput = document.getElementById('chatTextInput');
            var content = textInput.value.trim();
            if (!content && !pendingFile) return;
            var fd = new FormData();
            fd.append('conversation_id', currentConversationId);
            fd.append('content', content);
            if (pendingFile) fd.append('file', pendingFile);
            textInput.value = '';
            clearChatFile();
            var sentToConversationId = currentConversationId;
            fetch('/chat/api/send', { method: 'POST', body: fd }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) { alert(data.error || 'تعذر إرسال الرسالة'); return; }
                if (sentToConversationId === currentConversationId) {
                    appendMessageBubble({
                        id: data.message.id, sender_id: data.message.sender_id, sender_name: data.message.sender_name,
                        content: data.message.content, file_name: data.message.file_name, file_mimetype: data.message.file_mimetype,
                        has_file: data.message.has_file, created_at: data.message.created_at,
                        is_mine: true
                    });
                    scrollMessagesToBottom();
                }
                loadConversations();
            });
        }

        socket.on('new_message', function (payload) {
            if (payload.conversation_id === currentConversationId) {
                appendMessageBubble({
                    id: payload.id, sender_id: payload.sender_id, sender_name: payload.sender_name,
                    content: payload.content, file_name: payload.file_name, file_mimetype: payload.file_mimetype,
                    has_file: payload.has_file, created_at: payload.created_at,
                    is_mine: false
                }, true);
                scrollMessagesToBottom();
                markConversationRead(currentConversationId);
            }
            loadConversations();
        });
        socket.on('conversation_updated', function () { loadConversations(); });
        socket.on('conversation_removed', function (data) { removeConversationFromUi(data.conversation_id); });
        socket.on('typing', function (data) {
            if (data.conversation_id === currentConversationId) {
                var el = document.getElementById('typingIndicator');
                el.innerText = data.dept_name + ' يكتب الآن...';
                clearTimeout(window._typingClearTimer);
                window._typingClearTimer = setTimeout(function () { el.innerText = ''; }, 2500);
            }
        });

        var allContactsCache = [];
        function loadContactsInto(containerId, mode) {
            fetch('/chat/api/contacts').then(function (r) { return r.json(); }).then(function (data) {
                allContactsCache = data.contacts || [];
                renderContactList(containerId, mode, allContactsCache);
            });
        }
        function renderContactList(containerId, mode, contacts) {
            var box = document.getElementById(containerId);
            box.innerHTML = '';
            if (contacts.length === 0) {
                box.innerHTML = '<div class="text-muted small p-2">لا توجد إدارات يمكن مراسلتها</div>';
                return;
            }
            contacts.forEach(function (c) {
                var item = document.createElement('div');
                item.className = 'contact-pick-item';
                item.setAttribute('data-name', c.name);
                if (mode === 'direct') {
                    item.onclick = function () { startDirectChat(c.id); };
                    item.innerHTML = '<div class="conv-avatar">' + escapeHtml(avatarLetter(c.name)) + '</div><div class="fw-bold" style="font-size:0.9rem;">' + escapeHtml(c.name) + '</div>';
                } else {
                    var checked = selectedGroupMembers.has(c.id) ? 'checked' : '';
                    item.innerHTML = '<input type="checkbox" class="form-check-input" ' + checked + ' onchange="toggleGroupMember(' + c.id + ', this.checked)"> <div class="conv-avatar">' + escapeHtml(avatarLetter(c.name)) + '</div><div class="fw-bold" style="font-size:0.9rem;">' + escapeHtml(c.name) + '</div>';
                }
                box.appendChild(item);
            });
        }
        function filterContactList(q, containerId) {
            document.querySelectorAll('#' + containerId + ' .contact-pick-item').forEach(function (el) {
                var name = el.getAttribute('data-name') || '';
                el.style.display = name.indexOf(q) !== -1 ? 'flex' : 'none';
            });
        }
        function toggleGroupMember(id, checked) {
            if (checked) selectedGroupMembers.add(id); else selectedGroupMembers.delete(id);
        }

        function openNewChatModal() {
            loadContactsInto('newChatContactsList', 'direct');
            new bootstrap.Modal(document.getElementById('newChatModal')).show();
        }
        function openNewGroupModal() {
            selectedGroupMembers = new Set();
            document.getElementById('newGroupNameInput').value = '';
            loadContactsInto('newGroupContactsList', 'group');
            new bootstrap.Modal(document.getElementById('newGroupModal')).show();
        }
        function startDirectChat(deptId) {
            var fd = new FormData();
            fd.append('dept_id', deptId);
            fetch('/chat/api/start_direct', { method: 'POST', body: fd }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) { alert(data.error || 'تعذر بدء المحادثة'); return; }
                bootstrap.Modal.getInstance(document.getElementById('newChatModal')).hide();
                loadConversations();
                setTimeout(function () { openConversation(data.conversation_id); }, 200);
            });
        }
        function submitNewGroup() {
            var name = document.getElementById('newGroupNameInput').value.trim();
            if (!name) { alert('الرجاء إدخال اسم المجموعة'); return; }
            if (selectedGroupMembers.size === 0) { alert('اختر عضواً واحداً على الأقل'); return; }
            var fd = new FormData();
            fd.append('name', name);
            selectedGroupMembers.forEach(function (id) { fd.append('member_ids', id); });
            fetch('/chat/api/create_group', { method: 'POST', body: fd }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) { alert(data.error || 'تعذر إنشاء المجموعة'); return; }
                bootstrap.Modal.getInstance(document.getElementById('newGroupModal')).hide();
                loadConversations();
                setTimeout(function () { openConversation(data.conversation_id); }, 200);
            });
        }

        loadConversations();
    </script>
</body>
</html>
'''

# ============================================================
# ===============  الشات الداخلي السريع بين الإدارات  ===============
# ============================================================
# ميزة شات لحظي (WebSocket عبر Flask-SocketIO) بين الإدارات:
#  - محادثات فردية (بين إدارتين) ومحادثات جماعية (مجموعة تضم عدة إدارات).
#  - إرسال نصوص وصور وملفات (تُخزَّن على نفس Supabase Storage bucket المستخدم في باقي النظام).
#  - نفس قيود "صلاحيات الإرسال" المستخدمة حالياً في الخطابات (can_send_all / send_permissions):
#    أي إدارة لا تقدر تبدأ محادثة أو تضيف عضو لمجموعة إلا مع إدارة يسمح لها النظام بمراسلتها
#    حسب نفس الإعدادات الموجودة في صفحة "إدارة الصلاحيات" حالياً.
#
# ملاحظة تشغيل مهمة (Render / الاستضافة):
#  - هذه الميزة تحتاج مكتبتين إضافيتين في requirements.txt:  flask-socketio   و   simple-websocket
#  - أمر التشغيل (Procfile / Start Command على Render) يجب أن يصير:
#        gunicorn --worker-class gthread --threads 8 -w 1 app:app
#    (نستخدم gthread بدل eventlet لأن eventlet لا يدعم بايثون 3.14+ بعد. gthread مدمج أصلاً
#     داخل gunicorn نفسه ولا يحتاج أي مكتبة خارجية، ويعمل مع أي نسخة بايثون. عامل واحد -w 1
#     لازم يبقى كما هو، لأن غرف الشات محفوظة في ذاكرة العملية - لو احتجتوا أكثر من عامل
#     مستقبلاً، هذا يحتاج ربط Socket.IO بـ Redis كـ message queue).
# ============================================================

CHAT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20 ميجا حد أقصى لحجم أي ملف/صورة يُرفع بالشات


def _chat_participant_ids(cursor, conversation_id):
    cursor.execute('SELECT dept_id FROM chat_participants WHERE conversation_id = %s', (conversation_id,))
    return [r['dept_id'] for r in cursor.fetchall()]


def _chat_is_participant(cursor, conversation_id, dept_id):
    cursor.execute('SELECT 1 FROM chat_participants WHERE conversation_id = %s AND dept_id = %s', (conversation_id, dept_id))
    return cursor.fetchone() is not None


def _chat_conversation_display_name(cursor, conversation, viewer_dept_id):
    """للمحادثة الفردية يرجع اسم الطرف الآخر، وللمجموعة يرجع اسم المجموعة."""
    if conversation['type'] == 'group':
        return conversation['name'] or 'مجموعة بدون اسم'
    cursor.execute('''
        SELECT d.name FROM chat_participants cp
        JOIN departments d ON d.id = cp.dept_id
        WHERE cp.conversation_id = %s AND cp.dept_id != %s
        LIMIT 1
    ''', (conversation['id'], viewer_dept_id))
    row = cursor.fetchone()
    return row['name'] if row else (conversation['name'] or 'محادثة')


def _chat_find_existing_direct(cursor, dept_a, dept_b):
    cursor.execute('''
        SELECT cp1.conversation_id AS id
        FROM chat_participants cp1
        JOIN chat_participants cp2 ON cp2.conversation_id = cp1.conversation_id
        JOIN chat_conversations c ON c.id = cp1.conversation_id
        WHERE c.type = 'direct' AND cp1.dept_id = %s AND cp2.dept_id = %s
        LIMIT 1
    ''', (dept_a, dept_b))
    return cursor.fetchone()


@app.route('/chat')
def chat_page():
    if 'dept_id' not in session:
        return redirect(url_for('login'))
    is_admin = is_admin_user(session.get('dept_name'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))
    current_dept = cursor.fetchone()
    if current_dept['can_page_chat'] != 1 and not is_admin:
        cursor.close()
        conn.close()
        return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الشات الداخلي."); window.location.href="/dashboard";</script>'''
    cursor.close()
    conn.close()
    return render_template_string(CHAT_HTML, dept_name=session['dept_name'], current_dept=current_dept, is_admin=is_admin)


@app.route('/chat/api/contacts')
def chat_api_contacts():
    if 'dept_id' not in session:
        return {'contacts': []}, 401
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT can_send_all FROM departments WHERE id = %s', (session['dept_id'],))
    can_send_all_row = cursor.fetchone()
    can_send_all = can_send_all_row['can_send_all'] if can_send_all_row else 1
    depts = get_allowed_receiver_depts(cursor, session['dept_id'], can_send_all)
    cursor.close()
    conn.close()
    return {'contacts': [{'id': d['id'], 'name': d['name']} for d in depts]}


@app.route('/chat/api/conversations')
def chat_api_conversations():
    if 'dept_id' not in session:
        return {'conversations': []}, 401
    dept_id = session['dept_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT c.id, c.type, c.name, cp.last_read_message_id
        FROM chat_conversations c
        JOIN chat_participants cp ON cp.conversation_id = c.id
        WHERE cp.dept_id = %s AND cp.is_deleted = 0
    ''', (dept_id,))
    convs = cursor.fetchall()
    result = []
    for conv in convs:
        cursor.execute('''
            SELECT cm.id, cm.sender_id, cm.content, cm.file_name, cm.created_at, d.name AS sender_name
            FROM chat_messages cm
            JOIN departments d ON d.id = cm.sender_id
            WHERE cm.conversation_id = %s
            ORDER BY cm.id DESC LIMIT 1
        ''', (conv['id'],))
        last_msg = cursor.fetchone()
        cursor.execute('''
            SELECT COUNT(*) AS c FROM chat_messages
            WHERE conversation_id = %s AND id > %s
        ''', (conv['id'], conv['last_read_message_id'] or 0))
        unread = cursor.fetchone()['c']
        display_name = _chat_conversation_display_name(cursor, conv, dept_id)
        participants_count = None
        if conv['type'] == 'group':
            cursor.execute('SELECT COUNT(*) AS c FROM chat_participants WHERE conversation_id = %s', (conv['id'],))
            participants_count = cursor.fetchone()['c']
        last_preview = ''
        if last_msg:
            if last_msg['content']:
                last_preview = last_msg['content']
            elif last_msg['file_name']:
                last_preview = '📎 ' + last_msg['file_name']
        result.append({
            'id': conv['id'],
            'type': conv['type'],
            'name': display_name,
            'participants_count': participants_count,
            'last_message': last_preview,
            'last_sender_name': last_msg['sender_name'] if last_msg else None,
            'last_at': last_msg['created_at'] if last_msg else None,
            'last_id': last_msg['id'] if last_msg else 0,
            'unread': unread,
        })
    cursor.close()
    conn.close()
    result.sort(key=lambda r: r['last_id'], reverse=True)
    return {'conversations': result}


@app.route('/chat/api/messages/<int:conversation_id>')
def chat_api_messages(conversation_id):
    if 'dept_id' not in session:
        return {'messages': []}, 401
    dept_id = session['dept_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    if not _chat_is_participant(cursor, conversation_id, dept_id):
        cursor.close()
        conn.close()
        return {'error': 'غير مصرح'}, 403
    before_id = request.args.get('before_id', type=int)
    if before_id:
        cursor.execute('''
            SELECT cm.*, d.name AS sender_name FROM chat_messages cm
            JOIN departments d ON d.id = cm.sender_id
            WHERE cm.conversation_id = %s AND cm.id < %s
            ORDER BY cm.id DESC LIMIT 50
        ''', (conversation_id, before_id))
    else:
        cursor.execute('''
            SELECT cm.*, d.name AS sender_name FROM chat_messages cm
            JOIN departments d ON d.id = cm.sender_id
            WHERE cm.conversation_id = %s
            ORDER BY cm.id DESC LIMIT 50
        ''', (conversation_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    messages = [{
        'id': r['id'],
        'sender_id': r['sender_id'],
        'sender_name': r['sender_name'],
        'content': r['content'],
        'file_name': r['file_name'],
        'file_mimetype': r['file_mimetype'],
        'has_file': bool(r['file_path']),
        'created_at': r['created_at'],
        'is_mine': r['sender_id'] == dept_id,
    } for r in reversed(rows)]
    return {'messages': messages, 'has_more': len(rows) == 50}


@app.route('/chat/api/mark_read/<int:conversation_id>', methods=['POST'])
def chat_api_mark_read(conversation_id):
    if 'dept_id' not in session:
        return {'success': False}, 401
    dept_id = session['dept_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    if not _chat_is_participant(cursor, conversation_id, dept_id):
        cursor.close()
        conn.close()
        return {'success': False}, 403
    cursor.execute('SELECT MAX(id) AS max_id FROM chat_messages WHERE conversation_id = %s', (conversation_id,))
    max_id = cursor.fetchone()['max_id'] or 0
    cursor.execute('''
        UPDATE chat_participants SET last_read_message_id = %s
        WHERE conversation_id = %s AND dept_id = %s
    ''', (max_id, conversation_id, dept_id))
    conn.commit()
    cursor.close()
    conn.close()
    return {'success': True}


@app.route('/chat/api/delete_conversation/<int:conversation_id>', methods=['POST'])
def chat_api_delete_conversation(conversation_id):
    """حذف المحادثة من قائمة الإدارة الحالية فقط (بنفس منطق واتساب) - لا يحذفها عند بقية
    الأطراف، ولا يحذف الرسائل فعلياً، وتظهر تلقائياً من جديد لو وصلت رسالة جديدة بها."""
    if 'dept_id' not in session:
        return {'success': False}, 401
    dept_id = session['dept_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    if not _chat_is_participant(cursor, conversation_id, dept_id):
        cursor.close()
        conn.close()
        return {'success': False, 'error': 'غير مصرح لك بهذه المحادثة'}, 403
    cursor.execute('''
        UPDATE chat_participants SET is_deleted = 1
        WHERE conversation_id = %s AND dept_id = %s
    ''', (conversation_id, dept_id))
    conn.commit()
    cursor.close()
    conn.close()
    socketio.emit('conversation_removed', {'conversation_id': conversation_id}, room=f'chat_dept_{dept_id}')
    return {'success': True}


@app.route('/chat/api/start_direct', methods=['POST'])
def chat_api_start_direct():
    if 'dept_id' not in session:
        return {'success': False}, 401
    dept_id = session['dept_id']
    other_id = request.form.get('dept_id', type=int)
    if not other_id or other_id == dept_id:
        return {'success': False, 'error': 'إدارة غير صالحة'}, 400
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM departments WHERE id = %s', (other_id,))
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        return {'success': False, 'error': 'الإدارة غير موجودة'}, 404
    if not is_receiver_allowed(cursor, dept_id, other_id):
        cursor.close()
        conn.close()
        return {'success': False, 'error': 'لا تملك صلاحية مراسلة هذه الإدارة'}, 403
    existing = _chat_find_existing_direct(cursor, dept_id, other_id)
    if existing:
        cursor.close()
        conn.close()
        return {'success': True, 'conversation_id': existing['id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute(
        "INSERT INTO chat_conversations (type, name, created_by, created_at) VALUES ('direct', NULL, %s, %s) RETURNING id",
        (dept_id, now)
    )
    conv_id = cursor.fetchone()['id']
    cursor.execute('INSERT INTO chat_participants (conversation_id, dept_id, joined_at) VALUES (%s, %s, %s)', (conv_id, dept_id, now))
    cursor.execute('INSERT INTO chat_participants (conversation_id, dept_id, joined_at) VALUES (%s, %s, %s)', (conv_id, other_id, now))
    conn.commit()
    cursor.close()
    conn.close()
    socketio.emit('conversation_updated', {'conversation_id': conv_id}, room=f'chat_dept_{other_id}')
    return {'success': True, 'conversation_id': conv_id}


@app.route('/chat/api/create_group', methods=['POST'])
def chat_api_create_group():
    if 'dept_id' not in session:
        return {'success': False}, 401
    dept_id = session['dept_id']
    name = request.form.get('name', '').strip()
    member_ids = request.form.getlist('member_ids', type=int)
    if not name:
        return {'success': False, 'error': 'اسم المجموعة مطلوب'}, 400
    member_ids = [m for m in set(member_ids) if m != dept_id]
    if not member_ids:
        return {'success': False, 'error': 'اختر عضواً واحداً على الأقل'}, 400
    conn = get_db_connection()
    cursor = conn.cursor()
    for m in member_ids:
        if not is_receiver_allowed(cursor, dept_id, m):
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'لا تملك صلاحية إضافة إحدى الإدارات المختارة'}, 403
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute(
        "INSERT INTO chat_conversations (type, name, created_by, created_at) VALUES ('group', %s, %s, %s) RETURNING id",
        (name, dept_id, now)
    )
    conv_id = cursor.fetchone()['id']
    cursor.execute('INSERT INTO chat_participants (conversation_id, dept_id, joined_at) VALUES (%s, %s, %s)', (conv_id, dept_id, now))
    for m in member_ids:
        cursor.execute('INSERT INTO chat_participants (conversation_id, dept_id, joined_at) VALUES (%s, %s, %s)', (conv_id, m, now))
    conn.commit()
    cursor.close()
    conn.close()
    for m in member_ids:
        socketio.emit('conversation_updated', {'conversation_id': conv_id}, room=f'chat_dept_{m}')
    return {'success': True, 'conversation_id': conv_id}


@app.route('/chat/api/send', methods=['POST'])
def chat_api_send():
    if 'dept_id' not in session:
        return {'success': False}, 401
    dept_id = session['dept_id']
    conversation_id = request.form.get('conversation_id', type=int)
    content = (request.form.get('content') or '').strip()
    file = request.files.get('file')
    if not conversation_id:
        return {'success': False, 'error': 'محادثة غير صالحة'}, 400
    if not content and not (file and file.filename):
        return {'success': False, 'error': 'الرسالة فارغة'}, 400
    conn = get_db_connection()
    cursor = conn.cursor()
    if not _chat_is_participant(cursor, conversation_id, dept_id):
        cursor.close()
        conn.close()
        return {'success': False, 'error': 'غير مصرح لك بهذه المحادثة'}, 403
    file_name = file_path = file_mimetype = None
    if file and file.filename:
        file.seek(0, io.SEEK_END)
        size = file.tell()
        file.seek(0)
        if size > CHAT_UPLOAD_MAX_BYTES:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'حجم الملف أكبر من الحد المسموح (20 ميجا)'}, 400
        file_name, file_path, file_mimetype = upload_file_to_supabase(file, subfolder='chat', dept_folder=session.get('dept_username'))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute('''
        INSERT INTO chat_messages (conversation_id, sender_id, content, file_name, file_path, file_mimetype, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    ''', (conversation_id, dept_id, content or None, file_name, file_path, file_mimetype, now))
    msg_id = cursor.fetchone()['id']
    # أي رسالة جديدة "تُعيد إظهار" المحادثة لأي مشارك كان حذفها من قائمته سابقاً (بنفس منطق واتساب)
    cursor.execute('UPDATE chat_participants SET is_deleted = 0 WHERE conversation_id = %s', (conversation_id,))
    conn.commit()
    participant_ids = _chat_participant_ids(cursor, conversation_id)
    cursor.close()
    conn.close()
    payload = {
        'id': msg_id,
        'conversation_id': conversation_id,
        'sender_id': dept_id,
        'sender_name': session['dept_name'],
        'content': content or None,
        'file_name': file_name,
        'file_mimetype': file_mimetype,
        'has_file': bool(file_path),
        'created_at': now,
    }
    socketio.emit('new_message', payload, room=f'chat_conv_{conversation_id}')
    for pid in participant_ids:
        socketio.emit('conversation_updated', {'conversation_id': conversation_id}, room=f'chat_dept_{pid}')
    return {'success': True, 'message': payload}


@app.route('/chat/download/<int:message_id>')
def chat_download_file(message_id):
    if 'dept_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM chat_messages WHERE id = %s', (message_id,))
    msg = cursor.fetchone()
    if not msg or not _chat_is_participant(cursor, msg['conversation_id'], session['dept_id']):
        cursor.close()
        conn.close()
        return 'غير مصرح', 403
    cursor.close()
    conn.close()
    return send_supabase_file(msg['file_name'], msg['file_path'], msg['file_mimetype'], True)


@app.route('/chat/view/<int:message_id>')
def chat_view_file(message_id):
    if 'dept_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM chat_messages WHERE id = %s', (message_id,))
    msg = cursor.fetchone()
    if not msg or not _chat_is_participant(cursor, msg['conversation_id'], session['dept_id']):
        cursor.close()
        conn.close()
        return 'غير مصرح', 403
    cursor.close()
    conn.close()
    return send_supabase_file(msg['file_name'], msg['file_path'], msg['file_mimetype'], False)


@app.route('/api/chat_unread_count')
def api_chat_unread_count():
    if 'dept_id' not in session:
        return {'count': 0}
    dept_id = session['dept_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT cp.conversation_id, cp.last_read_message_id,
               (SELECT COUNT(*) FROM chat_messages cm
                WHERE cm.conversation_id = cp.conversation_id AND cm.id > cp.last_read_message_id) AS unread
        FROM chat_participants cp WHERE cp.dept_id = %s
    ''', (dept_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    total = sum(r['unread'] for r in rows)
    return {'count': total}


# --- أحداث Socket.IO (اتصال لحظي) ---

@socketio.on('connect')
def chat_socket_connect():
    if 'dept_id' not in session:
        return False
    join_room(f"chat_dept_{session['dept_id']}")


@socketio.on('join_conversation')
def chat_socket_join(data):
    if 'dept_id' not in session:
        return
    conversation_id = (data or {}).get('conversation_id')
    if not conversation_id:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    allowed = _chat_is_participant(cursor, conversation_id, session['dept_id'])
    cursor.close()
    conn.close()
    if allowed:
        join_room(f"chat_conv_{conversation_id}")


@socketio.on('leave_conversation')
def chat_socket_leave(data):
    conversation_id = (data or {}).get('conversation_id')
    if conversation_id:
        leave_room(f"chat_conv_{conversation_id}")


@socketio.on('typing')
def chat_socket_typing(data):
    if 'dept_id' not in session:
        return
    conversation_id = (data or {}).get('conversation_id')
    if not conversation_id:
        return
    emit('typing', {'conversation_id': conversation_id, 'dept_name': session.get('dept_name')},
         room=f"chat_conv_{conversation_id}", include_self=False)

if __name__ == '__main__':

    socketio.run(app, host='0.0.0.0', port=5000)
