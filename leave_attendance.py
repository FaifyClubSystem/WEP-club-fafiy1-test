# -*- coding: utf-8 -*-
"""
وحدة "الإجازات" و"الحضور والانصراف" - نادي فيفا الرياضي
==========================================================
هذه الوحدة إضافية (Blueprint-style) مصممة لتُدمج داخل app.py الحالي بأقل قدر ممكن
من التعديل على الملف الأصلي، حفاظاً على استقرار نظام الأرشفة والخطابات القائم.

طريقة الدمج (خطوتان فقط داخل app.py):

    1) في أعلى الملف مع بقية الاستيرادات:
        from leave_attendance import init_leave_attendance

    2) بعد تعريف دالتي get_db_connection() و is_admin_user() مباشرة
       (أي بعد init_db() في نهاية القسم العلوي من الملف)، أضف سطر واحد:
        init_leave_attendance(app, get_db_connection, is_admin_user)

لا حاجة لتعديل أي كود آخر لتشغيل الصفحتين. لإظهار رابطيهما داخل القائمة الجانبية
للصفحات الحالية (الوارد/الصادر/الأرشيف...) استخدم سكربت patch_sidebar.py المرفق،
أو أضف الوصلتين يدوياً (راجع ملف INTEGRATION.md).

المنطق المطبّق (بحسب ما تم الاتفاق عليه):
- كل إدارة/حساب له "رصيد إجازات" رقمي (leave_balance) في جدول departments، يبدأ
  برصيد افتراضي موحّد (30 يوماً) ويمكن للإدمن تعديله يدوياً من صفحة الإعدادات.
- تقديم طلب إجازة: يحسب عدد الأيام تلقائياً من تاريخ البداية والنهاية، ولا يُخصم
  أي رصيد عند التقديم - الخصم يتم فقط لحظة موافقة الرئيس التنفيذي أو مدير تقنية
  المعلومات (نفس صلاحية is_admin_user المستخدمة في باقي النظام).
- عند رفض الطلب: لا يوجد خصم أصلاً فلا داعي لإرجاع شيء.
- عند إلغاء إجازة قبل بدايتها أو أثناءها: يُعاد فقط الرصيد "المتبقي غير المستخدم"
  (من تاريخ الإلغاء حتى نهاية الإجازة)، وليس كامل مدة الإجازة إذا كان قد مضى جزء منها.
- صفحة الحضور والانصراف: تعتمد على تحديد موقع الجوال (Geolocation API) وتحسب
  المسافة عن الموقع المستهدف (Haversine) وتقارنها بنصف قطر مسموح به، مع نافذة
  وقت قابلة للتعديل من لوحة الإدمن لكل من الحضور والانصراف.
- الصفحتان تظهران حالياً فقط لإدارة "تقنية المعلومات" (وللرئيس التنفيذي/الإدمن
  بحكم صلاحياتهم الشاملة الموجودة أصلاً في النظام) عبر عمودين جديدين في جدول
  departments: can_page_leave و can_page_attendance، الافتراضي لهما 0 لجميع
  الإدارات باستثناء "تقنية المعلومات" التي يتم تفعيلها تلقائياً أول مرة تُنشأ
  فيها الأعمدة. لاحقاً، فعّلها لبقية الإدارات من صفحة "إعدادات الإجازات والحضور"
  دون أي تعديل إضافي على الكود.
"""

import math
from datetime import datetime, date, timedelta

from flask import request, redirect, url_for, session, render_template_string

# ---------------------------------------------------------------------------
# أسماء/أنماط أدوار "تقنية المعلومات" فقط (بدون الرئيس التنفيذي) - تُستخدم فقط
# لتحديد من يحصل تلقائياً على تفعيل الصفحتين الجديدتين أول مرة (مرحلة الاختبار).
# ---------------------------------------------------------------------------
IT_ONLY_ROLE_NAMES = [
    'مدير تقنية المعلومات', 'مدير تقنية معلومات', 'تقنية المعلومات', 'IT Manager', 'IT'
]


def _is_it_only_name(dept_name):
    if not dept_name:
        return False
    dept_clean = dept_name.strip()
    if any(role.lower() == dept_clean.lower() for role in IT_ONLY_ROLE_NAMES):
        return True
    return ('تقنية' in dept_clean) and ('تنفيذي' not in dept_clean)


# ---------------------------------------------------------------------------
# حساب المسافة بين نقطتين جغرافيتين بالأمتار (Haversine)
# ---------------------------------------------------------------------------
def _haversine_meters(lat1, lng1, lat2, lng2):
    R = 6371000.0  # نصف قطر الأرض بالأمتار
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (math.sin(d_phi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_date(s):
    return datetime.strptime(s, '%Y-%m-%d').date()


def _riyadh_now():
    """يرجع الوقت الحالي بتوقيت السعودية (UTC+3 طوال العام، لا يوجد توقيت صيفي).
    السيرفر (Render) يشتغل بتوقيت UTC، فلازم نحول يدوياً بدل الاعتماد على توقيت
    النظام المحلي حتى لا تُسجَّل أوقات الحضور/الانصراف بفارق 3 ساعات عن الواقع."""
    return datetime.utcnow() + timedelta(hours=3)


def _now_hm():
    return _riyadh_now().strftime('%H:%M')


def _within_window(now_hm, start_hm, end_hm):
    """يدعم أيضاً نافذة تمتد بعد منتصف الليل (مثال: 22:00 -> 02:00)."""
    if not start_hm or not end_hm:
        return True
    if start_hm <= end_hm:
        return start_hm <= now_hm <= end_hm
    return now_hm >= start_hm or now_hm <= end_hm


# ---------------------------------------------------------------------------
# القالب المشترك (نفس هوية النظام البصرية: أخضر فيفا + ذهبي + خط Almarai + RTL)
# ---------------------------------------------------------------------------
PAGE_SHELL = '''
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
<title>{{ page_title }} - نظام نادي فيفا</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">
<link href='https://unpkg.com/boxicons@2.1.4/css/boxicons.min.css' rel='stylesheet'>
<link href="https://fonts.googleapis.com/css2?family=Almarai:wght@300;400;700;800&display=swap" rel="stylesheet">
<style>
    :root { --fifa-green-primary:#123826; --fifa-green-light:#1e563b; --fifa-gold:#c5a059; --fifa-bg:#eaf3ec; --fifa-card-border:#d5e2d8; }
    [data-theme="dark"] { color-scheme: dark; }
    [data-theme="dark"] body { background:#0f1712 !important; color:#dbe6e0 !important; }
    [data-theme="dark"] .top-navbar { background-color: rgba(20,28,24,.95) !important; border-bottom-color:#c5a059 !important; }
    [data-theme="dark"] .modern-card, [data-theme="dark"] .stat-box { background:#16211a !important; border-color:#2a3a30 !important; color:#dbe6e0 !important; }
    [data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] textarea { background-color:#1b2620 !important; border-color:#33463a !important; color:#e7f0ea !important; }
    [data-theme="dark"] .text-muted { color:#9fb0a7 !important; }
    [data-theme="dark"] .table { color:#dbe6e0 !important; }
    [data-theme="dark"] .table-bordered td, [data-theme="dark"] .table-bordered th { border-color:#2a3a30 !important; }
    [data-theme="dark"] .bg-light { background-color:#1b2620 !important; color:#dbe6e0 !important; }
    body { font-family:'Almarai', sans-serif; background-color: var(--fifa-bg); color:#2b302e; overflow-x:hidden; }
    .top-navbar { background-color: rgba(255,255,255,.95); border-bottom:3px solid var(--fifa-gold); padding:.6rem 1rem; position:sticky; top:0; z-index:1045; }
    .nav-logo { height:42px; }
    .main-wrapper { display:flex; min-height: calc(100vh - 76px); position:relative; }
    .sidebar { width:260px; background-color: var(--fifa-green-primary); color:#ecf0f1; padding-top:1rem; flex-shrink:0; z-index:1040; }
    @media (max-width: 991.98px) {
        .sidebar { position:fixed; top:var(--navbar-height,76px); right:-260px; height:calc(100vh - var(--navbar-height,76px)); overflow-y:auto; transition:.3s; }
        .sidebar.show-sidebar { right:0; }
    }
    .mobile-overlay { display:none; position:fixed; top:var(--navbar-height,76px); left:0; right:0; bottom:0; background:rgba(0,0,0,.5); z-index:1030; }
    .mobile-overlay.active { display:block; }
    .sidebar-link { display:flex; align-items:center; color:#d1e0d8; text-decoration:none; padding:12px 20px; border-right:4px solid transparent; font-size:.95rem; }
    .sidebar-link:hover, .sidebar-link.active { background-color:rgba(255,255,255,.08); color:#fff; border-right-color:var(--fifa-gold); font-weight:700; }
    .sidebar-link i { font-size:1.35rem; margin-left:12px; color:var(--fifa-gold); }
    .content-body { flex:1; padding:1.25rem; width:100%; min-width:0; overflow-x:hidden; }
    .modern-card { background:rgba(255,255,255,.95); border-radius:12px; border:1px solid var(--fifa-card-border); padding:1.5rem; margin-bottom:1.5rem; box-shadow:0 4px 15px rgba(0,0,0,.03); }
    .section-header { font-weight:800; color:var(--fifa-green-primary); margin-bottom:1.25rem; position:relative; padding-bottom:10px; font-size:1.25rem; }
    .section-header::after { content:''; position:absolute; bottom:0; right:0; width:55px; height:3px; background:var(--fifa-gold); border-radius:2px; }
    .btn-fifa-primary { background-color:var(--fifa-green-primary); color:#fff; border-radius:8px; padding:.6rem 1.2rem; font-weight:700; border:none; }
    .btn-fifa-primary:hover { background-color:var(--fifa-green-light); color:#fff; }
    .btn-fifa-gold { background-color:var(--fifa-gold); color:#fff; font-weight:700; border:none; }
    .stat-box { background:rgba(255,255,255,.95); border-radius:12px; border:1px solid var(--fifa-card-border); padding:1.2rem; text-align:center; }
    .status-badge { font-size:.75rem; padding:4px 12px; border-radius:20px; font-weight:700; }
    .st-pending { background:#fff3cd; color:#7a5b00; }
    .st-approved { background:#d1e7dd; color:#0f5132; }
    .st-rejected { background:#f8d7da; color:#842029; }
    .st-cancelled { background:#e2e3e5; color:#41464b; }
    .theme-toggle-btn { border:1px solid #d5e2d8; background:#f8faf9; border-radius:8px; width:38px; height:38px; display:inline-flex; align-items:center; justify-content:center; color:#123826; cursor:pointer; }
</style>
</head>
<body>
<div class="mobile-overlay" id="mobileOverlay" onclick="toggleSidebar()"></div>
<nav class="navbar top-navbar sticky-top">
    <div class="container-fluid">
        <div class="d-flex align-items-center gap-2">
            <button class="btn btn-outline-success d-lg-none py-1 px-2 border-0" type="button" onclick="toggleSidebar()"><i class='bx bx-menu fs-2' style="color:var(--fifa-green-primary);"></i></button>
            <a class="navbar-brand d-flex align-items-center gap-2 m-0" href="/dashboard">
                <span class="fw-bold fs-6 lh-1" style="color:var(--fifa-green-primary);">نادي فيفا الرياضي</span>
            </a>
        </div>
        <div class="d-flex align-items-center gap-2">
            <button type="button" class="theme-toggle-btn" onclick="toggleFifaTheme()" id="themeToggleBtn"><i class='bx bxs-moon' id="themeToggleIcon"></i></button>
            <div class="dropdown">
                <button class="btn btn-light dropdown-toggle border py-1 px-2" type="button" data-bs-toggle="dropdown">
                    <i class='bx bxs-user-circle fs-4 ms-1' style="color:var(--fifa-gold);"></i>
                    <span class="fw-bold fs-7" style="color:var(--fifa-green-primary);">{{ dept_name }}</span>
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
        {% if current_dept['can_page_inbox'] == 1 or is_admin %}<a href="/dashboard" class="sidebar-link"><i class='bx bxs-inbox'></i>الصندوق الوارد</a>{% endif %}
        {% if current_dept['can_page_outbox'] == 1 or is_admin %}<a href="/outbox" class="sidebar-link"><i class='bx bxs-paper-plane'></i>الخطابات الصادرة</a>{% endif %}
        {% if current_dept['can_page_achievements'] == 1 or is_admin %}<a href="/monthly_achievements" class="sidebar-link"><i class='bx bxs-trophy'></i>إنجازات الشهر</a>{% endif %}
        {% if current_dept['can_page_archive'] == 1 or is_admin %}<a href="/archive" class="sidebar-link"><i class='bx bxs-file-archive'></i>أرشيف الإدارة</a>{% endif %}
        {% if current_dept['can_page_quick_upload'] == 1 or is_admin %}<a href="/quick_upload" class="sidebar-link"><i class='bx bx-cloud-upload' style="color:var(--fifa-gold);"></i>رفع وتوثيق فوري</a>{% endif %}
        {% if current_dept['can_page_leave'] == 1 or is_admin %}<a href="/leave" class="sidebar-link {{ 'active' if current_page == 'leave' else '' }}"><i class='bx bx-calendar-minus' style="color:var(--fifa-gold);"></i>طلبات الإجازات</a>{% endif %}
        {% if current_dept['can_page_attendance'] == 1 or is_admin %}<a href="/attendance" class="sidebar-link {{ 'active' if current_page == 'attendance' else '' }}"><i class='bx bx-map-pin' style="color:var(--fifa-gold);"></i>الحضور والانصراف</a>{% endif %}
        {% if current_dept['can_page_suggestions'] == 1 or is_admin %}<a href="/suggestions" class="sidebar-link"><i class='bx bxs-message-square-detail'></i>مشاكل واقتراحات</a>{% endif %}
        {% if is_admin %}
        <a href="/admin/dashboard" class="sidebar-link" style="background-color:rgba(197,160,89,.2);"><i class='bx bxs-cog' style="color:var(--fifa-gold);"></i>لوحة التحكم الشاملة</a>
        <a href="/admin/permissions" class="sidebar-link"><i class='bx bxs-shield'></i>إدارة الصلاحيات</a>
        <a href="/admin/leave_attendance_settings" class="sidebar-link {{ 'active' if current_page == 'la_settings' else '' }}"><i class='bx bx-cog' style="color:var(--fifa-gold);"></i>إعدادات الإجازات والحضور</a>
        {% endif %}
        {% if current_dept['can_add_user'] == 1 %}<a href="/register" class="sidebar-link"><i class='bx bxs-user-plus'></i>إضافة إدارة جديدة</a>{% endif %}
        <div class="border-top border-secondary my-3 opacity-25"></div>
        <a href="/logout" class="sidebar-link text-danger"><i class='bx bx-log-out text-danger'></i>تسجيل الخروج</a>
    </aside>
    <main class="content-body">
        <div class="container-fluid p-0">
            <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-3">
                <h4 class="section-header m-0">{{ page_title }}</h4>
            </div>
            {{ body|safe }}
        </div>
    </main>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    function updateFifaThemeIcon(){var i=document.getElementById('themeToggleIcon');if(!i)return;var d=document.documentElement.getAttribute('data-theme')==='dark';i.className=d?'bx bxs-sun':'bx bxs-moon';}
    function toggleFifaTheme(){var c=document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';var n=c==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',n);try{localStorage.setItem('fifa_theme',n);}catch(e){}updateFifaThemeIcon();}
    updateFifaThemeIcon();
    function toggleSidebar(){document.getElementById('sidebarMenu').classList.toggle('show-sidebar');document.getElementById('mobileOverlay').classList.toggle('active');}
    function updateNavbarHeightVar(){var n=document.querySelector('.top-navbar');if(n){document.documentElement.style.setProperty('--navbar-height',n.offsetHeight+'px');}}
    updateNavbarHeightVar();
    window.addEventListener('load', updateNavbarHeightVar);
    window.addEventListener('resize', updateNavbarHeightVar);
</script>
{{ extra_script|safe }}
</body>
</html>
'''


# ---------------------------------------------------------------------------
# قالب صفحة "طلبات الإجازات"
# ---------------------------------------------------------------------------
LEAVE_BODY_HTML = '''
<div class="row g-3 mb-4">
    <div class="col-md-4">
        <div class="stat-box">
            <h3 class="fw-bold text-success mb-0">{{ current_dept['leave_balance'] if current_dept['leave_balance'] is not none else 0 }}</h3>
            <p class="text-muted fs-7 mb-0">رصيد الإجازات المتبقي (بالأيام)</p>
        </div>
    </div>
    <div class="col-md-4">
        <div class="stat-box">
            <h3 class="fw-bold text-primary mb-0">{{ own_requests|selectattr('status','equalto','قيد المراجعة')|list|length }}</h3>
            <p class="text-muted fs-7 mb-0">طلبات قيد المراجعة</p>
        </div>
    </div>
    <div class="col-md-4">
        <div class="stat-box">
            <h3 class="fw-bold text-warning mb-0">{{ own_requests|selectattr('status','equalto','موافق عليها')|list|length }}</h3>
            <p class="text-muted fs-7 mb-0">إجازات موافق عليها</p>
        </div>
    </div>
</div>

<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bx-calendar-plus ms-1'></i> تقديم طلب إجازة جديد</h5>
    <form action="/leave/request" method="post" class="row g-3">
        <div class="col-md-3">
            <label class="form-label fw-bold fs-7">نوع الإجازة</label>
            <select name="leave_type" class="form-select fs-7">
                <option value="سنوية">سنوية</option>
                <option value="مرضية">مرضية</option>
                <option value="اضطرارية">اضطرارية</option>
                <option value="أخرى">أخرى</option>
            </select>
        </div>
        <div class="col-md-3">
            <label class="form-label fw-bold fs-7">تاريخ البداية</label>
            <input type="date" name="start_date" id="leaveStartDate" class="form-control fs-7" required>
        </div>
        <div class="col-md-3">
            <label class="form-label fw-bold fs-7">تاريخ النهاية</label>
            <input type="date" name="end_date" id="leaveEndDate" class="form-control fs-7" required>
        </div>
        <div class="col-md-3">
            <label class="form-label fw-bold fs-7">عدد الأيام (تلقائي)</label>
            <input type="text" id="leaveDaysCount" class="form-control fs-7" disabled placeholder="—">
        </div>
        <div class="col-12">
            <label class="form-label fw-bold fs-7">السبب / ملاحظات (اختياري)</label>
            <textarea name="reason" class="form-control fs-7" rows="2"></textarea>
        </div>
        <div class="col-12 text-end">
            <button type="submit" class="btn btn-fifa-primary px-4 py-2 fw-bold"><i class='bx bx-send ms-1'></i> إرسال الطلب</button>
        </div>
    </form>
</div>

{% if is_admin and pending_all %}
<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bxs-check-shield ms-1' style="color:var(--fifa-gold);"></i> طلبات بانتظار الموافقة (كل الإدارات)</h5>
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark">
                <tr><th>الإدارة</th><th>النوع</th><th>من</th><th>إلى</th><th>عدد الأيام</th><th>السبب</th><th>إجراء</th></tr>
            </thead>
            <tbody>
                {% for r in pending_all %}
                <tr>
                    <td class="fw-bold">{{ r.dept_name }}</td>
                    <td>{{ r.leave_type }}</td>
                    <td dir="ltr">{{ r.start_date }}</td>
                    <td dir="ltr">{{ r.end_date }}</td>
                    <td class="text-center">{{ r.days_count }}</td>
                    <td>{{ r.reason or '-' }}</td>
                    <td class="d-flex gap-1 flex-wrap">
                        <a href="/leave/approve/{{ r.id }}" class="btn btn-sm btn-success py-1 px-2 fs-8" onclick="return confirm('تأكيد الموافقة على هذه الإجازة وخصم الرصيد؟');">موافقة</a>
                        <a href="/leave/reject/{{ r.id }}" class="btn btn-sm btn-outline-danger py-1 px-2 fs-8" onclick="return confirm('تأكيد رفض هذا الطلب؟');">رفض</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endif %}

{% if is_admin and cancel_requests_all %}
<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bxs-x-square ms-1' style="color:var(--fifa-gold);"></i> طلبات إلغاء بانتظار الموافقة (كل الإدارات)</h5>
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark">
                <tr><th>الإدارة</th><th>النوع</th><th>من</th><th>إلى</th><th>عدد الأيام</th><th>الحالة السابقة</th><th>إجراء</th></tr>
            </thead>
            <tbody>
                {% for r in cancel_requests_all %}
                <tr>
                    <td class="fw-bold">{{ r.dept_name }}</td>
                    <td>{{ r.leave_type }}</td>
                    <td dir="ltr">{{ r.start_date }}</td>
                    <td dir="ltr">{{ r.end_date }}</td>
                    <td class="text-center">{{ r.days_count }}</td>
                    <td>{{ r.pre_cancel_status or '-' }}</td>
                    <td class="d-flex gap-1 flex-wrap">
                        <a href="/leave/cancel_approve/{{ r.id }}" class="btn btn-sm btn-danger py-1 px-2 fs-8" onclick="return confirm('تأكيد إلغاء هذه الإجازة؟ سيتم استرجاع الأيام غير المستخدمة فقط إن وُجدت.');">تأكيد الإلغاء</a>
                        <a href="/leave/cancel_reject/{{ r.id }}" class="btn btn-sm btn-outline-secondary py-1 px-2 fs-8" onclick="return confirm('رفض طلب الإلغاء وإرجاع الطلب لحالته السابقة؟');">رفض الإلغاء</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endif %}

{% if is_admin and all_requests %}
<div class="modern-card">
    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
        <h5 class="fw-bold mb-0" style="color:var(--fifa-green-primary);"><i class='bx bxs-list-check ms-1' style="color:var(--fifa-gold);"></i> كل طلبات الإجازات (كل الإدارات)</h5>
        <form action="/leave/delete_all" method="post" onsubmit="return confirm('تأكيد حذف كل طلبات الإجازات لكل الإدارات؟ هذا يحذف السجلات فقط ولا يعيد حساب أي رصيد. لا يمكن التراجع.');">
            <button type="submit" class="btn btn-sm btn-danger"><i class='bx bx-trash'></i> حذف الكل</button>
        </form>
    </div>
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark">
                <tr><th>الإدارة</th><th>النوع</th><th>من</th><th>إلى</th><th>الأيام</th><th>الحالة</th><th>ملاحظات</th><th>حذف</th></tr>
            </thead>
            <tbody>
                {% for r in all_requests %}
                <tr>
                    <td class="fw-bold">{{ r.dept_name }}</td>
                    <td>{{ r.leave_type }}</td>
                    <td dir="ltr">{{ r.start_date }}</td>
                    <td dir="ltr">{{ r.end_date }}</td>
                    <td class="text-center">{{ r.days_count }}</td>
                    <td>
                        {% if r.status == 'قيد المراجعة' %}<span class="status-badge st-pending">قيد المراجعة</span>
                        {% elif r.status == 'موافق عليها' %}<span class="status-badge st-approved">موافق عليها</span>
                        {% elif r.status == 'مرفوضة' %}<span class="status-badge st-rejected">مرفوضة</span>
                        {% elif r.status == 'طلب إلغاء' %}<span class="status-badge st-pending">بانتظار موافقة الإلغاء</span>
                        {% elif r.status == 'ملغاة' %}<span class="status-badge st-cancelled">ملغاة{% if r.refunded_days %} (تم استرجاع {{ r.refunded_days }} يوم){% endif %}</span>
                        {% else %}{{ r.status }}{% endif %}
                    </td>
                    <td class="fs-8 text-muted">{{ r.reason or '-' }}</td>
                    <td>
                        <form action="/leave/delete/{{ r.id }}" method="post" onsubmit="return confirm('تأكيد حذف هذا الطلب؟');">
                            <button type="submit" class="btn btn-sm btn-outline-danger"><i class='bx bx-trash'></i></button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endif %}

<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bxs-list-check ms-1' style="color:var(--fifa-gold);"></i> طلباتي</h5>
    {% if own_requests %}
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark">
                <tr><th>النوع</th><th>من</th><th>إلى</th><th>الأيام</th><th>الحالة</th><th>ملاحظات</th><th>إجراء</th></tr>
            </thead>
            <tbody>
                {% for r in own_requests %}
                <tr>
                    <td>{{ r.leave_type }}</td>
                    <td dir="ltr">{{ r.start_date }}</td>
                    <td dir="ltr">{{ r.end_date }}</td>
                    <td class="text-center">{{ r.days_count }}</td>
                    <td>
                        {% if r.status == 'قيد المراجعة' %}<span class="status-badge st-pending">قيد المراجعة</span>
                        {% elif r.status == 'موافق عليها' %}<span class="status-badge st-approved">موافق عليها</span>
                        {% elif r.status == 'مرفوضة' %}<span class="status-badge st-rejected">مرفوضة</span>
                        {% elif r.status == 'طلب إلغاء' %}<span class="status-badge st-pending">بانتظار موافقة الإلغاء</span>
                        {% elif r.status == 'ملغاة' %}<span class="status-badge st-cancelled">ملغاة{% if r.refunded_days %} (تم استرجاع {{ r.refunded_days }} يوم){% endif %}</span>
                        {% else %}{{ r.status }}{% endif %}
                    </td>
                    <td class="fs-8 text-muted">{{ r.reason or '-' }}</td>
                    <td>
                        {% if r.status in ['قيد المراجعة', 'موافق عليها'] %}
                        <a href="/leave/cancel/{{ r.id }}" class="btn btn-sm btn-outline-danger py-1 px-2 fs-8" onclick="return confirm('هل أنت متأكد من إلغاء هذه الإجازة؟ سيتم استرجاع الأيام غير المستخدمة فقط.');">إلغاء</a>
                        {% elif r.status == 'طلب إلغاء' %}<span class="text-muted fs-8">بانتظار موافقة الإدارة</span>
                        {% else %}—{% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-muted fs-7 text-center py-3">لا توجد طلبات إجازة حتى الآن.</p>
    {% endif %}
</div>
<script>
(function(){
    var s = document.getElementById('leaveStartDate');
    var e = document.getElementById('leaveEndDate');
    var d = document.getElementById('leaveDaysCount');
    function recalc(){
        if (s.value && e.value){
            var sd = new Date(s.value), ed = new Date(e.value);
            var diff = Math.round((ed - sd) / 86400000) + 1;
            d.value = diff > 0 ? (diff + ' يوم') : 'تاريخ غير صحيح';
        } else { d.value = ''; }
    }
    s.addEventListener('change', recalc);
    e.addEventListener('change', recalc);
})();
</script>
'''


# ---------------------------------------------------------------------------
# قالب صفحة "الحضور والانصراف"
# ---------------------------------------------------------------------------
ATTENDANCE_BODY_HTML = '''
<div class="row g-3 mb-4">
    <div class="col-md-6">
        <div class="stat-box">
            <h5 class="fw-bold mb-1" style="color:var(--fifa-green-primary);"><i class='bx bx-map-pin' style="color:var(--fifa-gold);"></i> {{ settings.location_label or 'موقع الدوام' }}</h5>
            <p class="text-muted fs-8 mb-0">نطاق مسموح: {{ settings.radius_meters }} متر</p>
            <p class="text-muted fs-8 mb-0">وقت الحضور: {{ settings.checkin_start }} - {{ settings.checkin_end }} | وقت الانصراف: {{ settings.checkout_start }} - {{ settings.checkout_end }}</p>
        </div>
    </div>
    <div class="col-md-6">
        <div class="stat-box" id="gpsStatusBox">
            <p class="fw-bold mb-1" id="gpsStatusText"><i class='bx bx-loader-alt bx-spin'></i> جاري تحديد موقعك...</p>
            <p class="text-muted fs-8 mb-0" id="gpsDistanceText"></p>
        </div>
    </div>
</div>

<div class="modern-card text-center">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);">تسجيل اليوم ({{ today_record.record_date if today_record else '' }})</h5>
    <div class="row g-3 justify-content-center">
        <div class="col-md-5">
            <div class="border rounded p-3 bg-light">
                <p class="fw-bold mb-2"><i class='bx bx-log-in-circle text-success'></i> الحضور</p>
                {% if today_record and today_record.check_in_time %}
                    <p class="text-success fw-bold fs-6 mb-0">تم الساعة {{ today_record.check_in_time }}</p>
                {% else %}
                    <form action="/attendance/checkin" method="post" id="checkinForm">
                        <input type="hidden" name="lat" id="checkinLat">
                        <input type="hidden" name="lng" id="checkinLng">
                        <button type="submit" class="btn btn-fifa-primary w-100 fw-bold" id="checkinBtn" disabled>تسجيل حضور</button>
                    </form>
                {% endif %}
            </div>
        </div>
        <div class="col-md-5">
            <div class="border rounded p-3 bg-light">
                <p class="fw-bold mb-2"><i class='bx bx-log-out-circle text-danger'></i> الانصراف</p>
                {% if today_record and today_record.check_out_time %}
                    <p class="text-danger fw-bold fs-6 mb-0">تم الساعة {{ today_record.check_out_time }}</p>
                {% elif today_record and today_record.check_in_time %}
                    <form action="/attendance/checkout" method="post" id="checkoutForm">
                        <input type="hidden" name="lat" id="checkoutLat">
                        <input type="hidden" name="lng" id="checkoutLng">
                        <button type="submit" class="btn btn-outline-danger w-100 fw-bold" id="checkoutBtn" disabled>تسجيل انصراف</button>
                    </form>
                {% else %}
                    <p class="text-muted fs-8 mb-0">سجّل الحضور أولاً</p>
                {% endif %}
            </div>
        </div>
    </div>
</div>

<div class="modern-card">
    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
        <h5 class="fw-bold mb-0" style="color:var(--fifa-green-primary);"><i class='bx bxs-calendar ms-1' style="color:var(--fifa-gold);"></i> سجل آخر 30 يوم{% if is_admin %} (كل الإدارات){% endif %}</h5>
        {% if is_admin and history %}
        <form action="/attendance/delete_all" method="post" onsubmit="return confirm('تأكيد حذف كل سجلات الحضور والانصراف لكل الإدارات؟ لا يمكن التراجع عن هذا الإجراء.');">
            <button type="submit" class="btn btn-sm btn-danger"><i class='bx bx-trash'></i> حذف الكل</button>
        </form>
        {% endif %}
    </div>
    {% if history %}
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark"><tr>{% if is_admin %}<th>الإدارة</th>{% endif %}<th>التاريخ</th><th>الحضور</th><th>الانصراف</th>{% if is_admin %}<th>حذف</th>{% endif %}</tr></thead>
            <tbody>
            {% for h in history %}
                <tr>
                    {% if is_admin %}<td class="fw-bold">{{ h.dept_name }}</td>{% endif %}
                    <td dir="ltr">{{ h.record_date }}</td><td>{{ h.check_in_time or '-' }}</td><td>{{ h.check_out_time or '-' }}</td>
                    {% if is_admin %}
                    <td>
                        <form action="/attendance/delete/{{ h.id }}" method="post" onsubmit="return confirm('تأكيد حذف هذا السجل؟');">
                            <button type="submit" class="btn btn-sm btn-outline-danger"><i class='bx bx-trash'></i></button>
                        </form>
                    </td>
                    {% endif %}
                </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <p class="text-muted fs-7 text-center py-3">لا يوجد سجل حضور بعد.</p>
    {% endif %}
</div>

{% if is_admin and admin_today %}
<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bxs-group ms-1' style="color:var(--fifa-gold);"></i> حضور جميع الإدارات اليوم</h5>
    <div class="table-responsive">
        <table class="table table-bordered table-hover align-middle fs-7">
            <thead class="table-success text-dark"><tr><th>الإدارة</th><th>الحضور</th><th>الانصراف</th><th>حذف</th></tr></thead>
            <tbody>
            {% for h in admin_today %}
                <tr>
                    <td class="fw-bold">{{ h.dept_name }}</td><td>{{ h.check_in_time or '-' }}</td><td>{{ h.check_out_time or '-' }}</td>
                    <td>
                        <form action="/attendance/delete/{{ h.id }}" method="post" onsubmit="return confirm('تأكيد حذف هذا السجل؟');">
                            <button type="submit" class="btn btn-sm btn-outline-danger"><i class='bx bx-trash'></i></button>
                        </form>
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endif %}
'''

ATTENDANCE_SCRIPT = '''
<script>
(function () {
    var targetLat = {{ (settings.target_lat if settings.target_lat is not none else 'null') }};
    var targetLng = {{ (settings.target_lng if settings.target_lng is not none else 'null') }};
    var radius = {{ settings.radius_meters or 200 }};
    var statusText = document.getElementById('gpsStatusText');
    var distanceText = document.getElementById('gpsDistanceText');
    var checkinBtn = document.getElementById('checkinBtn');
    var checkoutBtn = document.getElementById('checkoutBtn');

    function haversine(lat1, lng1, lat2, lng2) {
        var R = 6371000;
        var toRad = function (d) { return d * Math.PI / 180; };
        var dLat = toRad(lat2 - lat1), dLng = toRad(lng2 - lng1);
        var a = Math.sin(dLat/2)*Math.sin(dLat/2) + Math.cos(toRad(lat1))*Math.cos(toRad(lat2))*Math.sin(dLng/2)*Math.sin(dLng/2);
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    if (targetLat === null || targetLng === null) {
        statusText.innerHTML = '<i class="bx bx-error text-danger"></i> لم يتم تحديد موقع الدوام بعد من الإدمن';
        return;
    }

    if (!navigator.geolocation) {
        statusText.innerHTML = '<i class="bx bx-error text-danger"></i> جهازك لا يدعم تحديد الموقع (GPS)';
        return;
    }

    navigator.geolocation.getCurrentPosition(function (pos) {
        var lat = pos.coords.latitude, lng = pos.coords.longitude;
        var dist = Math.round(haversine(lat, lng, targetLat, targetLng));
        var within = dist <= radius;
        if (within) {
            statusText.innerHTML = '<i class="bx bx-check-circle text-success"></i> أنت داخل نطاق موقع الدوام';
        } else {
            statusText.innerHTML = '<i class="bx bx-x-circle text-danger"></i> أنت خارج نطاق موقع الدوام';
        }
        distanceText.innerText = 'المسافة عن الموقع المحدد: ' + dist + ' متر (المسموح: ' + radius + ' متر)';

        var checkinLat = document.getElementById('checkinLat');
        var checkinLng = document.getElementById('checkinLng');
        if (checkinLat) { checkinLat.value = lat; checkinLng.value = lng; }
        var checkoutLat = document.getElementById('checkoutLat');
        var checkoutLng = document.getElementById('checkoutLng');
        if (checkoutLat) { checkoutLat.value = lat; checkoutLng.value = lng; }

        if (checkinBtn) { checkinBtn.disabled = !within; }
        if (checkoutBtn) { checkoutBtn.disabled = !within; }
    }, function (err) {
        statusText.innerHTML = '<i class="bx bx-error text-danger"></i> تعذر تحديد موقعك، فعّل خدمة الموقع (GPS) واسمح للمتصفح بالوصول إليه';
        distanceText.innerText = '';
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
})();
</script>
'''


# ---------------------------------------------------------------------------
# قالب صفحة إعدادات الإدمن (الموقع/الأوقات + رصيد وصلاحيات كل إدارة)
# ---------------------------------------------------------------------------
SETTINGS_BODY_HTML = '''
<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bx-map-pin ms-1' style="color:var(--fifa-gold);"></i> موقع الدوام وأوقات تسجيل الحضور/الانصراف</h5>
    <form action="/admin/leave_attendance_settings" method="post" class="row g-3">
        <input type="hidden" name="form_type" value="location">
        <div class="col-md-4">
            <label class="form-label fw-bold fs-7">خط العرض (Latitude)</label>
            <input type="text" name="target_lat" id="settingsLat" class="form-control fs-7" value="{{ settings.target_lat if settings.target_lat is not none else '' }}" required>
        </div>
        <div class="col-md-4">
            <label class="form-label fw-bold fs-7">خط الطول (Longitude)</label>
            <input type="text" name="target_lng" id="settingsLng" class="form-control fs-7" value="{{ settings.target_lng if settings.target_lng is not none else '' }}" required>
        </div>
        <div class="col-md-4">
            <label class="form-label fw-bold fs-7">نصف القطر المسموح (متر)</label>
            <input type="number" name="radius_meters" class="form-control fs-7" value="{{ settings.radius_meters or 200 }}" min="10" required>
        </div>
        <div class="col-md-12">
            <button type="button" class="btn btn-outline-dark fs-7" onclick="useMyLocation()"><i class='bx bx-current-location ms-1'></i> استخدم موقعي الحالي (قف عند موقع الدوام أولاً)</button>
            <span id="useLocStatus" class="fs-8 text-muted ms-2"></span>
        </div>
        <div class="col-md-6">
            <label class="form-label fw-bold fs-7">اسم/وصف الموقع (اختياري)</label>
            <input type="text" name="location_label" class="form-control fs-7" value="{{ settings.location_label or '' }}" placeholder="مثال: مقر النادي الرئيسي">
        </div>
        <div class="col-md-3 col-6">
            <label class="form-label fw-bold fs-7">بداية وقت الحضور</label>
            <input type="time" name="checkin_start" class="form-control fs-7" value="{{ settings.checkin_start }}" required>
        </div>
        <div class="col-md-3 col-6">
            <label class="form-label fw-bold fs-7">نهاية وقت الحضور</label>
            <input type="time" name="checkin_end" class="form-control fs-7" value="{{ settings.checkin_end }}" required>
        </div>
        <div class="col-md-3 col-6">
            <label class="form-label fw-bold fs-7">بداية وقت الانصراف</label>
            <input type="time" name="checkout_start" class="form-control fs-7" value="{{ settings.checkout_start }}" required>
        </div>
        <div class="col-md-3 col-6">
            <label class="form-label fw-bold fs-7">نهاية وقت الانصراف</label>
            <input type="time" name="checkout_end" class="form-control fs-7" value="{{ settings.checkout_end }}" required>
        </div>
        <div class="col-12 text-end">
            <button type="submit" class="btn btn-fifa-gold px-4 py-2 fw-bold">حفظ إعدادات الموقع والأوقات</button>
        </div>
    </form>
</div>

<div class="modern-card">
    <h5 class="fw-bold mb-3" style="color:var(--fifa-green-primary);"><i class='bx bxs-group ms-1' style="color:var(--fifa-gold);"></i> رصيد الإجازات وإتاحة الصفحتين لكل إدارة</h5>
    <p class="text-muted fs-8">حالياً مفعّلتان تلقائياً فقط لإدارة "تقنية المعلومات" لأغراض الاختبار. فعّل المربعين لأي إدارة أخرى هنا عند الجاهزية للتعميم على الجميع.</p>
    <div class="table-responsive">
        <table class="table table-bordered align-middle fs-7">
            <thead class="table-success text-dark"><tr><th>الإدارة</th><th>رصيد الإجازات</th><th>صفحة الإجازات</th><th>صفحة الحضور</th><th></th></tr></thead>
            <tbody>
            {% for d in departments %}
                <tr>
                    <form action="/admin/leave_attendance_settings" method="post" class="d-contents">
                    <input type="hidden" name="form_type" value="dept_flags">
                    <input type="hidden" name="dept_id" value="{{ d.id }}">
                    <td class="fw-bold">{{ d.name }}</td>
                    <td style="max-width:120px;"><input type="number" name="leave_balance" class="form-control form-control-sm" value="{{ d.leave_balance if d.leave_balance is not none else 30 }}"></td>
                    <td class="text-center"><input type="checkbox" name="can_page_leave" class="form-check-input" {{ 'checked' if d.can_page_leave == 1 else '' }}></td>
                    <td class="text-center"><input type="checkbox" name="can_page_attendance" class="form-check-input" {{ 'checked' if d.can_page_attendance == 1 else '' }}></td>
                    <td><button type="submit" class="btn btn-sm btn-fifa-primary fs-8">حفظ</button></td>
                    </form>
                </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
</div>
'''

SETTINGS_SCRIPT = '''
<script>
function useMyLocation() {
    var statusEl = document.getElementById('useLocStatus');
    if (!navigator.geolocation) { statusEl.innerText = 'المتصفح لا يدعم تحديد الموقع'; return; }
    statusEl.innerText = 'جاري التحديد...';
    navigator.geolocation.getCurrentPosition(function (pos) {
        document.getElementById('settingsLat').value = pos.coords.latitude;
        document.getElementById('settingsLng').value = pos.coords.longitude;
        statusEl.innerText = 'تم تحديد موقعك الحالي، لا تنسَ الضغط على "حفظ".';
    }, function () {
        statusEl.innerText = 'تعذر تحديد الموقع، تأكد من السماح للمتصفح بالوصول لموقعك.';
    }, { enableHighAccuracy: true, timeout: 15000 });
}
</script>
'''


# ---------------------------------------------------------------------------
# إنشاء/ترحيل الجداول والأعمدة الجديدة
# ---------------------------------------------------------------------------
def _init_tables(get_db_connection):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='departments' AND table_schema = current_schema()")
    dept_columns = [c['column_name'] for c in cursor.fetchall()]

    newly_added_flags = False
    if 'leave_balance' not in dept_columns:
        cursor.execute('ALTER TABLE departments ADD COLUMN leave_balance INTEGER DEFAULT 30')
    if 'can_page_leave' not in dept_columns:
        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_leave INTEGER DEFAULT 0')
        newly_added_flags = True
    if 'can_page_attendance' not in dept_columns:
        cursor.execute('ALTER TABLE departments ADD COLUMN can_page_attendance INTEGER DEFAULT 0')
        newly_added_flags = True

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leave_requests (
            id SERIAL PRIMARY KEY,
            dept_id INTEGER NOT NULL,
            leave_type TEXT DEFAULT 'سنوية',
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            days_count INTEGER NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'قيد المراجعة',
            requested_at TEXT,
            decided_at TEXT,
            decided_by TEXT,
            cancelled_at TEXT,
            refunded_days INTEGER DEFAULT 0,
            balance_deducted INTEGER DEFAULT 0,
            pre_cancel_status TEXT
        )
    ''')
    # لجدول موجود مسبقاً (تم إنشاؤه قبل إضافة نظام "طلب الإلغاء"): نضيف العمود
    # الناقص بأمان دون الاعتماد على فحص information_schema (لتفادي مشاكل تعدد الـ schema).
    cursor.execute('ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS pre_cancel_status TEXT')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_settings (
            id SERIAL PRIMARY KEY,
            target_lat DOUBLE PRECISION,
            target_lng DOUBLE PRECISION,
            radius_meters INTEGER DEFAULT 200,
            location_label TEXT DEFAULT '',
            checkin_start TEXT DEFAULT '07:00',
            checkin_end TEXT DEFAULT '09:00',
            checkout_start TEXT DEFAULT '14:00',
            checkout_end TEXT DEFAULT '16:00',
            updated_at TEXT
        )
    ''')
    cursor.execute('SELECT COUNT(*) as c FROM attendance_settings')
    if cursor.fetchone()['c'] == 0:
        cursor.execute('''
            INSERT INTO attendance_settings
                (target_lat, target_lng, radius_meters, location_label, checkin_start, checkin_end, checkout_start, checkout_end, updated_at)
            VALUES (NULL, NULL, 200, '', '07:00', '09:00', '14:00', '16:00', %s)
        ''', (_riyadh_now().strftime('%Y-%m-%d %H:%M'),))

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_records (
            id SERIAL PRIMARY KEY,
            dept_id INTEGER NOT NULL,
            record_date TEXT NOT NULL,
            check_in_time TEXT,
            check_in_lat DOUBLE PRECISION,
            check_in_lng DOUBLE PRECISION,
            check_in_distance_m DOUBLE PRECISION,
            check_in_on_time INTEGER,
            check_out_time TEXT,
            check_out_lat DOUBLE PRECISION,
            check_out_lng DOUBLE PRECISION,
            check_out_distance_m DOUBLE PRECISION,
            check_out_on_time INTEGER,
            UNIQUE(dept_id, record_date)
        )
    ''')

    if newly_added_flags:
        # أول مرة تُنشأ فيها الأعمدة: فعّل الصفحتين تلقائياً لإدارة "تقنية المعلومات" فقط (اختبار)
        cursor.execute('SELECT id, name FROM departments')
        for row in cursor.fetchall():
            if _is_it_only_name(row['name']):
                cursor.execute(
                    'UPDATE departments SET can_page_leave = 1, can_page_attendance = 1 WHERE id = %s',
                    (row['id'],)
                )

    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# تسجيل المسارات (Routes) داخل تطبيق Flask الرئيسي
# ---------------------------------------------------------------------------
def init_leave_attendance(app, get_db_connection, is_admin_user):
    """استدعِ هذه الدالة مرة واحدة من app.py بعد تعريف get_db_connection و is_admin_user:

        from leave_attendance import init_leave_attendance
        init_leave_attendance(app, get_db_connection, is_admin_user)
    """
    _init_tables(get_db_connection)

    def _shell(page_title, current_page, body_html, current_dept, is_admin, extra_script=''):
        return render_template_string(
            PAGE_SHELL, page_title=page_title, current_page=current_page, body=body_html,
            dept_name=session.get('dept_name'), current_dept=current_dept, is_admin=is_admin,
            extra_script=extra_script
        )

    def _get_settings(cursor):
        cursor.execute('SELECT * FROM attendance_settings ORDER BY id LIMIT 1')
        return cursor.fetchone()

    def _has_attendance_access(dept_id):
        """يعيد فحص صلاحية الوصول على مستوى السيرفر (وليس فقط الواجهة) قبل تنفيذ
        أي تسجيل حضور/انصراف فعلي، حتى لا يمكن تجاوز الصلاحية بإرسال الطلب مباشرة."""
        if is_admin_user(session.get('dept_name')):
            return True
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT can_page_attendance FROM departments WHERE id = %s', (dept_id,))
        row = cursor.fetchone()
        cursor.close(); conn.close()
        return bool(row and row.get('can_page_attendance') == 1)

    # ============================== الإجازات ==============================

    @app.route('/leave', methods=['GET'], endpoint='leave_page')
    def leave_page():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))
        current_dept = cursor.fetchone()
        is_admin = is_admin_user(session.get('dept_name'))
        if current_dept.get('can_page_leave') != 1 and not is_admin:
            cursor.close(); conn.close()
            return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الإجازات."); window.location.href="/dashboard";</script>'''

        cursor.execute('SELECT * FROM leave_requests WHERE dept_id = %s ORDER BY id DESC', (session['dept_id'],))
        own_requests = cursor.fetchall()

        pending_all = []
        cancel_requests_all = []
        all_requests = []
        if is_admin:
            cursor.execute('''
                SELECT lr.*, d.name as dept_name FROM leave_requests lr
                JOIN departments d ON lr.dept_id = d.id
                WHERE lr.status = 'قيد المراجعة' ORDER BY lr.id ASC
            ''')
            pending_all = cursor.fetchall()

            cursor.execute('''
                SELECT lr.*, d.name as dept_name FROM leave_requests lr
                JOIN departments d ON lr.dept_id = d.id
                WHERE lr.status = 'طلب إلغاء' ORDER BY lr.id ASC
            ''')
            cancel_requests_all = cursor.fetchall()

            cursor.execute('''
                SELECT lr.*, d.name as dept_name FROM leave_requests lr
                JOIN departments d ON lr.dept_id = d.id
                ORDER BY lr.id DESC
            ''')
            all_requests = cursor.fetchall()

        cursor.close(); conn.close()
        body = render_template_string(
            LEAVE_BODY_HTML, current_dept=current_dept, is_admin=is_admin,
            own_requests=own_requests, pending_all=pending_all,
            cancel_requests_all=cancel_requests_all, all_requests=all_requests
        )
        return _shell('طلبات الإجازات', 'leave', body, current_dept, is_admin)

    @app.route('/leave/request', methods=['POST'], endpoint='leave_submit')
    def leave_submit():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        conn0 = get_db_connection(); cursor0 = conn0.cursor()
        cursor0.execute('SELECT can_page_leave FROM departments WHERE id = %s', (session['dept_id'],))
        perm_row = cursor0.fetchone()
        cursor0.close(); conn0.close()
        if (not perm_row or perm_row.get('can_page_leave') != 1) and not is_admin_user(session.get('dept_name')):
            return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الإجازات."); window.location.href="/dashboard";</script>'''
        dept_id = session['dept_id']
        leave_type = request.form.get('leave_type', 'سنوية')
        start_date_s = request.form.get('start_date')
        end_date_s = request.form.get('end_date')
        reason = request.form.get('reason', '').strip()
        if not start_date_s or not end_date_s:
            return '''<script>alert("الرجاء تحديد تاريخ البداية والنهاية."); window.location.href="/leave";</script>'''
        try:
            start_d = _parse_date(start_date_s)
            end_d = _parse_date(end_date_s)
        except ValueError:
            return '''<script>alert("صيغة التاريخ غير صحيحة."); window.location.href="/leave";</script>'''
        if end_d < start_d:
            return '''<script>alert("تاريخ النهاية لا يمكن أن يكون قبل تاريخ البداية."); window.location.href="/leave";</script>'''
        days_count = (end_d - start_d).days + 1

        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT leave_balance FROM departments WHERE id = %s', (dept_id,))
        row = cursor.fetchone()
        current_balance = row['leave_balance'] if row and row.get('leave_balance') is not None else 0
        if days_count > current_balance:
            cursor.close(); conn.close()
            return f'''<script>alert("رصيدك الحالي {current_balance} يوم فقط، ولا يكفي لتغطية {days_count} يوم المطلوبة."); window.location.href="/leave";</script>'''

        cursor.execute('''
            INSERT INTO leave_requests (dept_id, leave_type, start_date, end_date, days_count, reason, status, requested_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'قيد المراجعة', %s)
        ''', (dept_id, leave_type, start_date_s, end_date_s, days_count, reason, _riyadh_now().strftime('%Y-%m-%d %H:%M')))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم إرسال طلب الإجازة بنجاح، بانتظار موافقة الرئيس التنفيذي / مدير تقنية المعلومات."); window.location.href="/leave";</script>'''

    @app.route('/leave/approve/<int:req_id>', methods=['GET'], endpoint='leave_approve')
    def leave_approve(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("هذه الصلاحية للرئيس التنفيذي / مدير تقنية المعلومات فقط."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM leave_requests WHERE id = %s', (req_id,))
        lr = cursor.fetchone()
        if not lr or lr['status'] != 'قيد المراجعة':
            cursor.close(); conn.close()
            return '''<script>alert("هذا الطلب لم يعد قيد المراجعة."); window.location.href="/leave";</script>'''
        cursor.execute('SELECT leave_balance FROM departments WHERE id = %s', (lr['dept_id'],))
        d_row = cursor.fetchone()
        current_balance = d_row['leave_balance'] if d_row and d_row.get('leave_balance') is not None else 0
        if lr['days_count'] > current_balance:
            cursor.close(); conn.close()
            return f'''<script>alert("رصيد صاحب الطلب الحالي {current_balance} يوم فقط ولا يكفي لتغطية هذا الطلب ({lr['days_count']} يوم). لا يمكن الموافقة."); window.location.href="/leave";</script>'''
        new_balance = current_balance - lr['days_count']
        cursor.execute('UPDATE departments SET leave_balance = %s WHERE id = %s', (new_balance, lr['dept_id']))
        cursor.execute('''
            UPDATE leave_requests SET status='موافق عليها', decided_at=%s, decided_by=%s, balance_deducted=%s
            WHERE id = %s
        ''', (_riyadh_now().strftime('%Y-%m-%d %H:%M'), session.get('dept_name'), lr['days_count'], req_id))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تمت الموافقة على الإجازة وخصم الرصيد بنجاح."); window.location.href="/leave";</script>'''

    @app.route('/leave/reject/<int:req_id>', methods=['GET'], endpoint='leave_reject')
    def leave_reject(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("هذه الصلاحية للرئيس التنفيذي / مدير تقنية المعلومات فقط."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM leave_requests WHERE id = %s', (req_id,))
        lr = cursor.fetchone()
        if not lr or lr['status'] != 'قيد المراجعة':
            cursor.close(); conn.close()
            return '''<script>alert("هذا الطلب لم يعد قيد المراجعة."); window.location.href="/leave";</script>'''
        cursor.execute('''
            UPDATE leave_requests SET status='مرفوضة', decided_at=%s, decided_by=%s WHERE id = %s
        ''', (_riyadh_now().strftime('%Y-%m-%d %H:%M'), session.get('dept_name'), req_id))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم رفض طلب الإجازة."); window.location.href="/leave";</script>'''

    def _do_actual_cancel(cursor, lr, base_status):
        """ينفذ الإلغاء الفعلي (مع حساب الاسترجاع إن لزم) بالاعتماد على base_status
        كحالة أساس (قد تكون lr['status'] للإلغاء المباشر من الإدمن، أو
        lr['pre_cancel_status'] عند تأكيد طلب إلغاء سبق تقديمه)."""
        refund_days = 0
        if base_status == 'موافق عليها':
            today = _riyadh_now().date()
            end_d = lr['end_date'] if isinstance(lr['end_date'], date) else _parse_date(str(lr['end_date']))
            start_d = lr['start_date'] if isinstance(lr['start_date'], date) else _parse_date(str(lr['start_date']))
            if today < start_d:
                refund_days = lr['balance_deducted'] or 0
            elif today > end_d:
                refund_days = 0
            else:
                remaining = (end_d - today).days + 1  # من اليوم (شامل) حتى نهاية الإجازة
                refund_days = min(remaining, lr['balance_deducted'] or 0)
            if refund_days > 0:
                cursor.execute('SELECT leave_balance FROM departments WHERE id = %s', (lr['dept_id'],))
                d_row = cursor.fetchone()
                current_balance = d_row['leave_balance'] if d_row and d_row.get('leave_balance') is not None else 0
                cursor.execute('UPDATE departments SET leave_balance = %s WHERE id = %s',
                                (current_balance + refund_days, lr['dept_id']))
        cursor.execute('''
            UPDATE leave_requests SET status='ملغاة', cancelled_at=%s, refunded_days=%s, pre_cancel_status=NULL WHERE id = %s
        ''', (_riyadh_now().strftime('%Y-%m-%d %H:%M'), refund_days, lr['id']))
        return refund_days

    @app.route('/leave/cancel/<int:req_id>', methods=['GET'], endpoint='leave_cancel')
    def leave_cancel(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        is_admin = is_admin_user(session.get('dept_name'))
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM leave_requests WHERE id = %s', (req_id,))
        lr = cursor.fetchone()
        if not lr:
            cursor.close(); conn.close()
            return '''<script>alert("الطلب غير موجود."); window.location.href="/leave";</script>'''
        if str(lr['dept_id']) != str(session['dept_id']) and not is_admin:
            cursor.close(); conn.close()
            return '''<script>alert("لا تملك صلاحية إلغاء طلب إجازة لإدارة أخرى."); window.location.href="/leave";</script>'''
        if lr['status'] not in ('قيد المراجعة', 'موافق عليها'):
            cursor.close(); conn.close()
            return '''<script>alert("لا يمكن إلغاء هذا الطلب في حالته الحالية."); window.location.href="/leave";</script>'''

        if is_admin:
            # الإدمن (الرئيس التنفيذي / مدير تقنية المعلومات) يملك صلاحية الإلغاء الفوري مباشرة
            refund_days = _do_actual_cancel(cursor, lr, lr['status'])
            conn.commit(); cursor.close(); conn.close()
            msg = "تم إلغاء طلب الإجازة." if refund_days == 0 else f"تم إلغاء الإجازة وإرجاع {refund_days} يوم إلى الرصيد."
            return f'''<script>alert("{msg}"); window.location.href="/leave";</script>'''

        # الموظف العادي لا يملك صلاحية الإلغاء المباشر - يُرسل طلب إلغاء فقط،
        # وينتظر تأكيد الرئيس التنفيذي / مدير تقنية المعلومات لتنفيذه فعلياً.
        cursor.execute('''
            UPDATE leave_requests SET status='طلب إلغاء', pre_cancel_status=%s WHERE id = %s
        ''', (lr['status'], req_id))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم إرسال طلب إلغاء الإجازة، بانتظار موافقة الرئيس التنفيذي / مدير تقنية المعلومات."); window.location.href="/leave";</script>'''

    @app.route('/leave/cancel_approve/<int:req_id>', methods=['GET'], endpoint='leave_cancel_approve')
    def leave_cancel_approve(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("هذه الصلاحية للرئيس التنفيذي / مدير تقنية المعلومات فقط."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM leave_requests WHERE id = %s', (req_id,))
        lr = cursor.fetchone()
        if not lr or lr['status'] != 'طلب إلغاء':
            cursor.close(); conn.close()
            return '''<script>alert("لا يوجد طلب إلغاء بانتظار الموافقة لهذا الطلب."); window.location.href="/leave";</script>'''
        refund_days = _do_actual_cancel(cursor, lr, lr.get('pre_cancel_status') or 'قيد المراجعة')
        conn.commit(); cursor.close(); conn.close()
        msg = "تم تأكيد إلغاء الإجازة." if refund_days == 0 else f"تم تأكيد الإلغاء وإرجاع {refund_days} يوم إلى رصيد الإدارة."
        return f'''<script>alert("{msg}"); window.location.href="/leave";</script>'''

    @app.route('/leave/cancel_reject/<int:req_id>', methods=['GET'], endpoint='leave_cancel_reject')
    def leave_cancel_reject(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("هذه الصلاحية للرئيس التنفيذي / مدير تقنية المعلومات فقط."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM leave_requests WHERE id = %s', (req_id,))
        lr = cursor.fetchone()
        if not lr or lr['status'] != 'طلب إلغاء':
            cursor.close(); conn.close()
            return '''<script>alert("لا يوجد طلب إلغاء بانتظار الموافقة لهذا الطلب."); window.location.href="/leave";</script>'''
        cursor.execute('''
            UPDATE leave_requests SET status=%s, pre_cancel_status=NULL WHERE id = %s
        ''', (lr.get('pre_cancel_status') or 'قيد المراجعة', req_id))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم رفض طلب الإلغاء، رجع الطلب لحالته السابقة."); window.location.href="/leave";</script>'''

    @app.route('/leave/delete/<int:req_id>', methods=['POST'], endpoint='leave_delete')
    def leave_delete(req_id):
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("عذراً، حذف طلبات الإجازات متاح فقط للمسؤول."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('DELETE FROM leave_requests WHERE id = %s', (req_id,))
        conn.commit(); cursor.close(); conn.close()
        return redirect(url_for('leave_page'))

    @app.route('/leave/delete_all', methods=['POST'], endpoint='leave_delete_all')
    def leave_delete_all():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("عذراً، حذف كل الطلبات متاح فقط للمسؤول."); window.location.href="/leave";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('DELETE FROM leave_requests')
        conn.commit(); cursor.close(); conn.close()
        return redirect(url_for('leave_page'))

    # ========================= الحضور والانصراف =========================

    @app.route('/attendance', methods=['GET'], endpoint='attendance_page')
    def attendance_page():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))
        current_dept = cursor.fetchone()
        is_admin = is_admin_user(session.get('dept_name'))
        if current_dept.get('can_page_attendance') != 1 and not is_admin:
            cursor.close(); conn.close()
            return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الحضور والانصراف."); window.location.href="/dashboard";</script>'''

        settings = _get_settings(cursor)
        today_s = _riyadh_now().date().isoformat()
        cursor.execute('SELECT * FROM attendance_records WHERE dept_id = %s AND record_date = %s',
                        (session['dept_id'], today_s))
        today_record = cursor.fetchone()
        if is_admin:
            # المسؤول يشوف سجل كل الإدارات لآخر 30 يوم (مو بس سجله الشخصي)
            thirty_days_ago_s = (_riyadh_now().date() - timedelta(days=30)).isoformat()
            cursor.execute('''
                SELECT ar.*, d.name as dept_name FROM attendance_records ar
                JOIN departments d ON ar.dept_id = d.id
                WHERE ar.record_date >= %s
                ORDER BY ar.record_date DESC, d.name
            ''', (thirty_days_ago_s,))
            history = cursor.fetchall()
        else:
            cursor.execute('SELECT * FROM attendance_records WHERE dept_id = %s ORDER BY record_date DESC LIMIT 30',
                            (session['dept_id'],))
            history = cursor.fetchall()

        admin_today = []
        if is_admin:
            cursor.execute('''
                SELECT ar.*, d.name as dept_name FROM attendance_records ar
                JOIN departments d ON ar.dept_id = d.id
                WHERE ar.record_date = %s ORDER BY d.name
            ''', (today_s,))
            admin_today = cursor.fetchall()

        cursor.close(); conn.close()
        body = render_template_string(
            ATTENDANCE_BODY_HTML, settings=settings, today_record=today_record,
            history=history, is_admin=is_admin, admin_today=admin_today
        )
        extra = render_template_string(ATTENDANCE_SCRIPT, settings=settings)
        return _shell('الحضور والانصراف', 'attendance', body, current_dept, is_admin, extra_script=extra)

    @app.route('/attendance/checkin', methods=['POST'], endpoint='attendance_checkin')
    def attendance_checkin():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not _has_attendance_access(session['dept_id']):
            return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الحضور والانصراف."); window.location.href="/dashboard";</script>'''
        try:
            lat = float(request.form.get('lat'))
            lng = float(request.form.get('lng'))
        except (TypeError, ValueError):
            return '''<script>alert("تعذر تحديد موقعك. فعّل خدمة الموقع (GPS) في جوالك وحاول مجدداً."); window.location.href="/attendance";</script>'''

        conn = get_db_connection(); cursor = conn.cursor()
        settings = _get_settings(cursor)
        if not settings or settings.get('target_lat') is None or settings.get('target_lng') is None:
            cursor.close(); conn.close()
            return '''<script>alert("لم يتم تحديد موقع الدوام بعد من قبل الإدمن."); window.location.href="/attendance";</script>'''

        now_hm = _now_hm()
        if not _within_window(now_hm, settings.get('checkin_start'), settings.get('checkin_end')):
            cursor.close(); conn.close()
            return f'''<script>alert("خارج وقت تسجيل الحضور المسموح ({settings.get('checkin_start')} - {settings.get('checkin_end')})."); window.location.href="/attendance";</script>'''

        distance = _haversine_meters(lat, lng, settings['target_lat'], settings['target_lng'])
        if distance > (settings.get('radius_meters') or 200):
            cursor.close(); conn.close()
            return f'''<script>alert("أنت خارج نطاق موقع الدوام المسموح (المسافة الحالية {int(distance)} متر)."); window.location.href="/attendance";</script>'''

        today_s = _riyadh_now().date().isoformat()
        cursor.execute('SELECT * FROM attendance_records WHERE dept_id = %s AND record_date = %s',
                        (session['dept_id'], today_s))
        existing = cursor.fetchone()
        if existing and existing.get('check_in_time'):
            cursor.close(); conn.close()
            return '''<script>alert("تم تسجيل حضورك مسبقاً اليوم."); window.location.href="/attendance";</script>'''

        now_time = _riyadh_now().strftime('%H:%M:%S')
        if existing:
            cursor.execute('''
                UPDATE attendance_records SET check_in_time=%s, check_in_lat=%s, check_in_lng=%s, check_in_distance_m=%s, check_in_on_time=1
                WHERE id = %s
            ''', (now_time, lat, lng, distance, existing['id']))
        else:
            cursor.execute('''
                INSERT INTO attendance_records (dept_id, record_date, check_in_time, check_in_lat, check_in_lng, check_in_distance_m, check_in_on_time)
                VALUES (%s, %s, %s, %s, %s, %s, 1)
            ''', (session['dept_id'], today_s, now_time, lat, lng, distance))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم تسجيل الحضور بنجاح."); window.location.href="/attendance";</script>'''

    @app.route('/attendance/checkout', methods=['POST'], endpoint='attendance_checkout')
    def attendance_checkout():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not _has_attendance_access(session['dept_id']):
            return '''<script>alert("عذراً، لا تملك صلاحية الوصول لصفحة الحضور والانصراف."); window.location.href="/dashboard";</script>'''
        try:
            lat = float(request.form.get('lat'))
            lng = float(request.form.get('lng'))
        except (TypeError, ValueError):
            return '''<script>alert("تعذر تحديد موقعك. فعّل خدمة الموقع (GPS) في جوالك وحاول مجدداً."); window.location.href="/attendance";</script>'''

        conn = get_db_connection(); cursor = conn.cursor()
        settings = _get_settings(cursor)
        if not settings or settings.get('target_lat') is None or settings.get('target_lng') is None:
            cursor.close(); conn.close()
            return '''<script>alert("لم يتم تحديد موقع الدوام بعد من قبل الإدمن."); window.location.href="/attendance";</script>'''

        now_hm = _now_hm()
        if not _within_window(now_hm, settings.get('checkout_start'), settings.get('checkout_end')):
            cursor.close(); conn.close()
            return f'''<script>alert("خارج وقت تسجيل الانصراف المسموح ({settings.get('checkout_start')} - {settings.get('checkout_end')})."); window.location.href="/attendance";</script>'''

        distance = _haversine_meters(lat, lng, settings['target_lat'], settings['target_lng'])
        if distance > (settings.get('radius_meters') or 200):
            cursor.close(); conn.close()
            return f'''<script>alert("أنت خارج نطاق موقع الدوام المسموح (المسافة الحالية {int(distance)} متر)."); window.location.href="/attendance";</script>'''

        today_s = _riyadh_now().date().isoformat()
        cursor.execute('SELECT * FROM attendance_records WHERE dept_id = %s AND record_date = %s',
                        (session['dept_id'], today_s))
        existing = cursor.fetchone()
        if not existing or not existing.get('check_in_time'):
            cursor.close(); conn.close()
            return '''<script>alert("لا يمكن تسجيل الانصراف قبل تسجيل الحضور."); window.location.href="/attendance";</script>'''
        if existing.get('check_out_time'):
            cursor.close(); conn.close()
            return '''<script>alert("تم تسجيل انصرافك مسبقاً اليوم."); window.location.href="/attendance";</script>'''

        now_time = _riyadh_now().strftime('%H:%M:%S')
        cursor.execute('''
            UPDATE attendance_records SET check_out_time=%s, check_out_lat=%s, check_out_lng=%s, check_out_distance_m=%s, check_out_on_time=1
            WHERE id = %s
        ''', (now_time, lat, lng, distance, existing['id']))
        conn.commit(); cursor.close(); conn.close()
        return '''<script>alert("تم تسجيل الانصراف بنجاح."); window.location.href="/attendance";</script>'''

    @app.route('/attendance/delete/<int:record_id>', methods=['POST'], endpoint='attendance_delete')
    def attendance_delete(record_id):
        """حذف سجل حضور/انصراف - للمسؤول (الرئيس التنفيذي/مدير تقنية المعلومات) فقط،
        يُستخدم لتنظيف سجلات تجريبية أو تصحيح أخطاء تسجيل."""
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("عذراً، حذف سجلات الحضور متاح فقط للمسؤول."); window.location.href="/attendance";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('DELETE FROM attendance_records WHERE id = %s', (record_id,))
        conn.commit(); cursor.close(); conn.close()
        return redirect(url_for('attendance_page'))

    @app.route('/attendance/delete_all', methods=['POST'], endpoint='attendance_delete_all')
    def attendance_delete_all():
        """حذف كل سجلات الحضور والانصراف لكل الإدارات دفعة واحدة - للمسؤول فقط."""
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("عذراً، حذف كل السجلات متاح فقط للمسؤول."); window.location.href="/attendance";</script>'''
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('DELETE FROM attendance_records')
        conn.commit(); cursor.close(); conn.close()
        return redirect(url_for('attendance_page'))

    # ============================ إعدادات الإدمن ============================

    @app.route('/admin/leave_attendance_settings', methods=['GET', 'POST'], endpoint='leave_attendance_settings')
    def leave_attendance_settings_route():
        if 'dept_id' not in session:
            return redirect(url_for('login'))
        if not is_admin_user(session.get('dept_name')):
            return '''<script>alert("هذه الصفحة مخصصة للمسؤولين فقط."); window.location.href="/dashboard";</script>'''

        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute('SELECT * FROM departments WHERE id = %s', (session['dept_id'],))
        current_dept = cursor.fetchone()

        if request.method == 'POST':
            form_type = request.form.get('form_type')
            if form_type == 'location':
                try:
                    target_lat_f = float(request.form.get('target_lat'))
                    target_lng_f = float(request.form.get('target_lng'))
                    radius_i = int(request.form.get('radius_meters'))
                except (TypeError, ValueError):
                    cursor.close(); conn.close()
                    return '''<script>alert("الرجاء إدخال إحداثيات ونصف قطر صحيحة."); window.location.href="/admin/leave_attendance_settings";</script>'''
                location_label = request.form.get('location_label', '')
                checkin_start = request.form.get('checkin_start')
                checkin_end = request.form.get('checkin_end')
                checkout_start = request.form.get('checkout_start')
                checkout_end = request.form.get('checkout_end')
                cursor.execute('SELECT id FROM attendance_settings ORDER BY id LIMIT 1')
                srow = cursor.fetchone()
                cursor.execute('''
                    UPDATE attendance_settings SET target_lat=%s, target_lng=%s, radius_meters=%s, location_label=%s,
                        checkin_start=%s, checkin_end=%s, checkout_start=%s, checkout_end=%s, updated_at=%s
                    WHERE id = %s
                ''', (target_lat_f, target_lng_f, radius_i, location_label, checkin_start, checkin_end,
                      checkout_start, checkout_end, _riyadh_now().strftime('%Y-%m-%d %H:%M'), srow['id']))
                conn.commit(); cursor.close(); conn.close()
                return '''<script>alert("تم حفظ إعدادات الموقع وأوقات الدوام بنجاح."); window.location.href="/admin/leave_attendance_settings";</script>'''

            elif form_type == 'dept_flags':
                dept_id = request.form.get('dept_id')
                can_page_leave = 1 if request.form.get('can_page_leave') else 0
                can_page_attendance = 1 if request.form.get('can_page_attendance') else 0
                try:
                    leave_balance_i = int(request.form.get('leave_balance'))
                except (TypeError, ValueError):
                    leave_balance_i = None
                if leave_balance_i is not None:
                    cursor.execute(
                        'UPDATE departments SET can_page_leave=%s, can_page_attendance=%s, leave_balance=%s WHERE id=%s',
                        (can_page_leave, can_page_attendance, leave_balance_i, dept_id)
                    )
                else:
                    cursor.execute(
                        'UPDATE departments SET can_page_leave=%s, can_page_attendance=%s WHERE id=%s',
                        (can_page_leave, can_page_attendance, dept_id)
                    )
                conn.commit(); cursor.close(); conn.close()
                return '''<script>alert("تم تحديث إعدادات الإدارة بنجاح."); window.location.href="/admin/leave_attendance_settings";</script>'''

        settings = _get_settings(cursor)
        cursor.execute('SELECT * FROM departments ORDER BY id ASC')
        departments = cursor.fetchall()
        cursor.close(); conn.close()

        body = render_template_string(SETTINGS_BODY_HTML, settings=settings, departments=departments)
        return _shell('إعدادات الإجازات والحضور', 'la_settings', body, current_dept, True,
                       extra_script=SETTINGS_SCRIPT)
