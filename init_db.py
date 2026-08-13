import psycopg2

# بيانات الاتصال الخاصة بـ Supabase
DB_HOST = "db.ievhntskoenaqfcunwby.supabase.co"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASSWORD = "Essa12121313$$$$"
DB_PORT = 5432

def init_database():
    try:
        # الاتصال بقاعدة بيانات Supabase
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT
        )
        cursor = conn.cursor()

        # 1. إنشاء جدول الإدارات (مع التأكد من قيد الفريدة لاسم المستخدم)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS departments (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
        ''')

        # 2. إنشاء جدول الخطابات
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS letters (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            priority TEXT DEFAULT 'عادي',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sender_id) REFERENCES departments (id) ON DELETE CASCADE,
            FOREIGN KEY (receiver_id) REFERENCES departments (id) ON DELETE CASCADE
        )
        ''')

        # 3. إنشاء جدول المرفقات
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            letter_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (letter_id) REFERENCES letters (id) ON DELETE CASCADE
        )
        ''')

        # 4. إضافة إدارات تجريبية وتحديث كلمة المرور تلقائياً إن وُجدت مسبقاً
        departments_data = [
            ('إدارة الموارد البشرية', 'hr', '123456'),
            ('الإدارة المالية', 'finance', '123456'),
            ('إدارة التقنية والأنظمة', 'it', '123456'),
            ('الاتصالات الإدارية', 'admin', '123456')
        ]

        for dept in departments_data:
            cursor.execute('''
                INSERT INTO departments (name, username, password) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (username) DO UPDATE 
                SET name = EXCLUDED.name, 
                    password = EXCLUDED.password
            ''', dept)

        conn.commit()
        cursor.close()
        conn.close()

        print("✅ تم إنشاء الجداول وتجهيز الإدارات في Supabase بنجاح!")

    except Exception as e:
        print(f"❌ حدث خطأ أثناء الاتصال أو إنشاء الجداول: {e}")

if __name__ == '__main__':
    init_database()