import psycopg2

# بيانات الاتصال الخاصة بـ Supabase (قم بتعديلها بالبيانات الفعلية الخاصة بك)
DB_HOST = "db.ievhntskoenaqfcunwby.supabase.co"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASSWORD = "Essa12121313$$$$"
DB_PORT = 5432

def update_database():
    try:
        # الاتصال بقاعدة بيانات Supabase
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT
        )
        conn.autocommit = False # لإدارة المعاملات (Transactions) يدوياً
        cursor = conn.cursor()

        # 1. إضافة عمود الصلاحية الخاصة بالأرشيف الخاص إذا لم يكن موجوداً
        try:
            cursor.execute('ALTER TABLE departments ADD COLUMN IF NOT EXISTS can_access_archive INTEGER DEFAULT 0')
            print("✅ تم التحقق من/إضافة عمود الأرشيف الخاص بنجاح!")
        except Exception as e:
            conn.rollback()
            print(f"ℹ️ تنبيه بخصوص عمود الأرشيف الخاص: {e}")

        # 2. إضافة عمود صلاحية الاطلاع على كامل الأرشيف لكل الإدارات إذا لم يكن موجوداً
        try:
            cursor.execute('ALTER TABLE departments ADD COLUMN IF NOT EXISTS can_view_full_archive INTEGER DEFAULT 0')
            print("✅ تم التحقق من/إضافة عمود الاطلاع على كامل الأرشيف بنجاح!")
        except Exception as e:
            conn.rollback()
            print(f"ℹ️ تنبيه بخصوص عمود الاطلاع على كامل الأرشيف: {e}")

        # 3. إعطاء صلاحية الاطلاع على كامل الأرشيف تلقائياً للمديرين
        cursor.execute("""
            UPDATE departments 
            SET can_view_full_archive = 1 
            WHERE username IN ('it_manager', 'ceo') 
               OR name LIKE '%مدير تقنية المعلومات%' 
               OR name LIKE '%الرئيس التنفيذي%'
        """)

        conn.commit()
        cursor.close()
        conn.close()

        print("✅ تم تحديث قاعدة البيانات وصلاحيات الإدارات في Supabase بنجاح!")

    except Exception as e:
        print(f"❌ حدث خطأ أثناء الاتصال أو تحديث قاعدة البيانات: {e}")

if __name__ == '__main__':
    update_database()