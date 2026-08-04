# ============================================================
#  ISKCON JANMASTHAMI DEVOTEE GATHERING SYSTEM — Flask API Backend
#  File: app.py
# ============================================================

from flask import Flask, jsonify, request, send_from_directory, session, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
import re
import secrets
import pymysql
import pymysql.cursors
from datetime import datetime, timedelta
import logging

app = Flask(__name__, static_folder='.', static_url_path='')

# ── SECRET KEY & SESSION CONFIG ──
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SESSION_COOKIE_SECURE=os.environ.get('RAILWAY_ENVIRONMENT') is not None,
)

# ── CORS ──
# Reflects whatever Origin the browser sends (LAN IPs / ports vary by venue, and the
# deployed domain can change) instead of a hardcoded allowlist that breaks on new hosts.
# Safe alongside SESSION_COOKIE_SAMESITE='Lax', which already blocks cross-site fetch/XHR
# from carrying the session cookie.
CORS(app, supports_credentials=True, origins=re.compile(r'.*'))

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
#  DATABASE CONFIG
# ============================================================
DB_CONFIG = {
    'host':     os.environ.get('MYSQLHOST', 'localhost'),
    'port':     int(os.environ.get('MYSQLPORT') or 3306),
    'user':     os.environ.get('MYSQLUSER', 'root'),
    'password': os.environ.get('MYSQLPASSWORD', 'root'),
    'db':       os.environ.get('MYSQL_DATABASE', 'iskcon_janmastmi_db'),
    'charset':  'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
    'autocommit': True,
}

def get_db():
    return pymysql.connect(**DB_CONFIG)

def init_db():
    try:
        # Initial connection without database to create DB if needed
        conn_init_cfg = DB_CONFIG.copy()
        conn_init_cfg.pop('db', None)
        conn = pymysql.connect(**conn_init_cfg)
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['db']} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.close()
    except Exception as e:
        log.warning(f'DB auto-create warning: {e}')

    try:
        conn = pymysql.connect(**DB_CONFIG)
    except Exception as e:
        log.error(f'init_db: cannot connect to DB at startup: {e}')
        return
    try:
        with conn.cursor() as cur:
            # ── USERS table ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    username      VARCHAR(50)   NOT NULL UNIQUE,
                    password_hash VARCHAR(256)  NOT NULL,
                    name          VARCHAR(150)  NOT NULL,
                    mobile        VARCHAR(15)   NOT NULL,
                    role          ENUM('admin','user') NOT NULL DEFAULT 'user',
                    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_username (username)
                ) ENGINE=InnoDB CHARACTER SET utf8mb4
            """)

            # ── REGISTRATIONS table ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registrations (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    token         VARCHAR(10)  NOT NULL UNIQUE,
                    name          VARCHAR(150) NOT NULL,
                    address       TEXT         NOT NULL,
                    mobile        VARCHAR(15)  NOT NULL,
                    persons       INT          NOT NULL DEFAULT 1,
                    paid          INT          NOT NULL DEFAULT 0,
                    free_entry    TINYINT(1)   NOT NULL DEFAULT 0,
                    registered_by INT          NULL,
                    reg_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_token (token),
                    INDEX idx_mobile (mobile),
                    INDEX idx_reg_by (registered_by)
                ) ENGINE=InnoDB CHARACTER SET utf8mb4
            """)

            # ── ATTENDANCE table ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attendance (
                    id        INT AUTO_INCREMENT PRIMARY KEY,
                    token     VARCHAR(10)  NOT NULL UNIQUE,
                    name      VARCHAR(150) NOT NULL,
                    persons   INT          NOT NULL DEFAULT 1,
                    paid      INT          NOT NULL DEFAULT 0,
                    mobile    VARCHAR(15),
                    gate_time DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token) REFERENCES registrations(token) ON DELETE CASCADE,
                    INDEX idx_token (token)
                ) ENGINE=InnoDB CHARACTER SET utf8mb4
            """)

            # ── TOKEN COUNTER ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS token_counter (
                    id      INT PRIMARY KEY DEFAULT 1,
                    current INT NOT NULL DEFAULT 0
                ) ENGINE=InnoDB
            """)
            cur.execute("INSERT IGNORE INTO token_counter (id, current) VALUES (1, 0)")

            # ── ATTENDANCE LOG (per-scan-event log for accurate hourly footfall) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attendance_log (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    token      VARCHAR(10)  NOT NULL,
                    persons    INT          NOT NULL DEFAULT 1,
                    scanned_by INT          NULL,
                    scan_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_token (token),
                    INDEX idx_scan_time (scan_time)
                ) ENGINE=InnoDB CHARACTER SET utf8mb4
            """)

            # ── Migration Helpers ──
            for col_sql, col_name in [
                ("ALTER TABLE registrations ADD COLUMN registered_by INT NULL", "registered_by"),
                ("ALTER TABLE registrations ADD COLUMN free_entry TINYINT(1) NOT NULL DEFAULT 0", "free_entry"),
            ]:
                try:
                    cur.execute("""
                        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = 'registrations'
                          AND COLUMN_NAME = %s
                    """, (col_name,))
                    if cur.fetchone()['cnt'] == 0:
                        cur.execute(col_sql)
                        log.info(f'Migration: added column {col_name}')
                except Exception as me:
                    log.error(f'Migration error ({col_name}): {me}')

            try:
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'attendance'
                      AND COLUMN_NAME = 'scanned_by'
                """)
                if cur.fetchone()['cnt'] == 0:
                    cur.execute("ALTER TABLE attendance ADD COLUMN scanned_by INT NULL")
                    log.info('Migration: added scanned_by column to attendance table')
            except Exception as me:
                log.error(f'Migration error (scanned_by): {me}')

            # ── Seed default admin user ──
            admin_pw = os.environ.get('ADMIN_PASSWORD', 'admin123')
            admin_mob = os.environ.get('ADMIN_MOBILE', '0000000000')
            cur.execute("""
                INSERT IGNORE INTO users (username, password_hash, name, mobile, role)
                VALUES (%s, %s, %s, %s, 'admin')
            """, ('admin', generate_password_hash(admin_pw), 'Administrator', admin_mob))

            conn.commit()
        log.info('Database tables initialized successfully.')
    except Exception as e:
        log.error(f'init_db error: {e}')
    finally:
        conn.close()

init_db()

# ============================================================
#  DB HELPERS
# ============================================================
def db_query(sql, args=None, fetch='all'):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            if fetch == 'all':
                return cur.fetchall()
            elif fetch == 'one':
                return cur.fetchone()
            else:
                conn.commit()
                return cur.rowcount
    finally:
        conn.close()

def db_execute(sql, args=None):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()

# ============================================================
#  AUTH DECORATORS
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'code': 'AUTH_REQUIRED'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'code': 'AUTH_REQUIRED'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'Admin access required', 'code': 'FORBIDDEN'}), 403
        return f(*args, **kwargs)
    return decorated

# ============================================================
#  STATIC FILES & HEALTH CHECK
# ============================================================
@app.route('/')
@app.route('/index.html')
def serve_frontend():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')

@app.route('/<path:filename>')
def serve_static_file(filename):
    if filename.startswith('api/'):
        return jsonify({'error': 'Endpoint not found'}), 404
    dir_path = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(dir_path, filename)
    if os.path.exists(file_path):
        return send_from_directory(dir_path, filename)
    return jsonify({'error': f'File {filename} not found'}), 404

@app.route('/api/ping', methods=['GET'])
def ping():
    try:
        db_query("SELECT 1", fetch='one')
        return jsonify({'status': 'ok', 'message': 'ISKCON Janmashtami API is operational', 'event': 'Janmashtami 2026'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ============================================================
#  AUTH ROUTES
# ============================================================
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data sent'}), 400
    username = str(data.get('username', '')).strip().lower()
    password = str(data.get('password', ''))
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    user = db_query("SELECT * FROM users WHERE username = %s", (username,), fetch='one')
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid username or password'}), 401
    
    session.clear()
    session.permanent = True
    session['user_id']  = user['id']
    session['username'] = user['username']
    session['name']     = user['name']
    session['mobile']   = user['mobile']
    session['role']     = user['role']
    log.info(f"Login successful: {username} (Role: {user['role']})")
    return jsonify({
        'success': True,
        'user': {
            'id':       user['id'],
            'username': user['username'],
            'name':     user['name'],
            'mobile':   user['mobile'],
            'upi_id':   user['mobile'] + '@upi',
            'role':     user['role'],
        }
    })

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    log.info(f"Logout: {session.get('username', 'Guest')}")
    session.clear()
    return jsonify({'success': True})

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    if 'user_id' not in session:
        return jsonify({'authenticated': False}), 200
    return jsonify({
        'authenticated': True,
        'user': {
            'id':       session['user_id'],
            'username': session['username'],
            'name':     session['name'],
            'mobile':   session['mobile'],
            'upi_id':   session['mobile'] + '@upi',
            'role':     session['role'],
        }
    })

# ============================================================
#  USER MANAGEMENT (ADMIN ONLY)
# ============================================================
@app.route('/api/users', methods=['GET'])
@admin_required
def list_users():
    users = db_query("SELECT id, username, name, mobile, role, created_at FROM users ORDER BY id")
    for u in users:
        u['upi_id'] = u['mobile'] + '@upi'
        if u.get('created_at'):
            u['created_at'] = u['created_at'].strftime('%d/%m/%Y %H:%M')
    return jsonify({'users': users})

@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    data = request.get_json()
    username = str(data.get('username', '')).strip().lower()
    password = str(data.get('password', ''))
    name     = str(data.get('name', '')).strip()
    mobile   = str(data.get('mobile', '')).strip()
    role     = data.get('role', 'user')

    if not all([username, password, name, mobile]):
        return jsonify({'error': 'All fields (username, password, name, mobile) are required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Invalid role specified'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile must be exactly 10 digits'}), 400
    try:
        uid = db_execute(
            "INSERT INTO users (username, password_hash, name, mobile, role) VALUES (%s,%s,%s,%s,%s)",
            (username, generate_password_hash(password), name, mobile, role)
        )
        log.info(f'Created user: {username} role={role}')
        return jsonify({'success': True, 'id': uid}), 201
    except Exception as e:
        if 'Duplicate entry' in str(e):
            return jsonify({'error': 'Username already exists'}), 409
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:uid>', methods=['PUT'])
@admin_required
def update_user(uid):
    data = request.get_json()
    name   = str(data.get('name', '')).strip()
    mobile = str(data.get('mobile', '')).strip()
    role   = data.get('role', 'user')

    if not name or not mobile:
        return jsonify({'error': 'Name and mobile are required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Invalid role'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile must be exactly 10 digits'}), 400

    if data.get('password'):
        db_execute(
            "UPDATE users SET name=%s, mobile=%s, role=%s, password_hash=%s WHERE id=%s",
            (name, mobile, role, generate_password_hash(data['password']), uid)
        )
    else:
        db_execute(
            "UPDATE users SET name=%s, mobile=%s, role=%s WHERE id=%s",
            (name, mobile, role, uid)
        )
    log.info(f'Updated user id={uid}')
    return jsonify({'success': True})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
@admin_required
def delete_user(uid):
    admins = db_query("SELECT id FROM users WHERE role='admin'")
    if len(admins) == 1 and admins[0]['id'] == uid:
        return jsonify({'error': 'Cannot delete the primary admin account'}), 400
    db_execute("DELETE FROM users WHERE id=%s", (uid,))
    log.info(f'Deleted user id={uid}')
    return jsonify({'success': True})

# ============================================================
#  STATISTICS & ANALYTICS
# ============================================================
@app.route('/api/stats', methods=['GET'])
@login_required
def get_stats():
    try:
        reg_row = db_query(
            "SELECT COUNT(*) AS families, COALESCE(SUM(persons),0) AS persons, COALESCE(SUM(paid),0) AS collection FROM registrations",
            fetch='one'
        )
        att_row = db_query(
            "SELECT COUNT(*) AS families, COALESCE(SUM(persons),0) AS persons FROM attendance",
            fetch='one'
        )
        result = {
            'registered_families': reg_row['families'],
            'registered_persons':  int(reg_row['persons']),
            'attended_families':   att_row['families'],
            'attended_persons':    int(att_row['persons']),
            'pending_families':    max(0, reg_row['families'] - att_row['families']),
        }
        if session.get('role') == 'admin':
            result['collection'] = int(reg_row['collection'])
        return jsonify(result)
    except Exception as e:
        log.error(f'stats error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/user-stats', methods=['GET'])
@admin_required
def user_stats():
    try:
        rows = db_query("""
            SELECT
                u.id,
                u.name,
                u.username,
                u.mobile,
                COUNT(DISTINCT r.id)                        AS families_registered,
                COALESCE(SUM(DISTINCT r.persons), 0)        AS persons_registered,
                COALESCE(SUM(DISTINCT r.paid), 0)           AS collection,
                COUNT(DISTINCT a.id)                        AS families_scanned,
                COALESCE(SUM(a.persons), 0)                 AS persons_scanned
            FROM users u
            LEFT JOIN registrations r ON r.registered_by = u.id
            LEFT JOIN attendance a ON a.scanned_by = u.id
            GROUP BY u.id
            ORDER BY collection DESC, families_scanned DESC
        """)
        for row in rows:
            row['families_registered'] = int(row['families_registered'])
            row['persons_registered']  = int(row['persons_registered'])
            row['collection']          = int(row['collection'])
            row['families_scanned']     = int(row['families_scanned'])
            row['persons_scanned']      = int(row['persons_scanned'])
        return jsonify({'user_stats': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/my-stats', methods=['GET'])
@login_required
def my_stats():
    try:
        uid = session['user_id']
        row = db_query("""
            SELECT COUNT(*) AS families, COALESCE(SUM(persons),0) AS persons
            FROM registrations WHERE registered_by = %s
        """, (uid,), fetch='one')
        return jsonify({'families': row['families'], 'persons': int(row['persons'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
#  REGISTRATIONS
# ============================================================
@app.route('/api/registrations', methods=['GET'])
@login_required
def list_registrations():
    try:
        rows = db_query("""
            SELECT r.*,
                   IF(a.token IS NOT NULL, 1, 0) AS attended,
                   a.gate_time,
                   u.name AS registered_by_name,
                   u.username AS registered_by_username
            FROM registrations r
            LEFT JOIN attendance a ON r.token = a.token
            LEFT JOIN users u ON u.id = r.registered_by
            ORDER BY r.id DESC
        """)
        for row in rows:
            if row.get('reg_at'):
                row['reg_at'] = row['reg_at'].strftime('%d/%m/%Y %H:%M')
            if row.get('gate_time'):
                row['gate_time'] = row['gate_time'].strftime('%d/%m/%Y %H:%M')
        return jsonify({'registrations': rows})
    except Exception as e:
        log.error(f'list_registrations error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/register', methods=['POST'])
@login_required
def register_family():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No payload submitted'}), 400

    name       = str(data.get('name', '')).strip()
    address    = str(data.get('address', '')).strip()
    mobile     = str(data.get('mobile', '')).strip()
    persons    = int(data.get('persons', 1))
    free_entry = bool(data.get('free_entry', False))
    paid       = 0 if free_entry else int(data.get('paid', 0))
    registered_by = session['user_id']

    if not name or not address or not mobile:
        return jsonify({'error': 'Name, address, and mobile number are required'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile number must be 10 digits'}), 400
    if persons < 1 or persons > 50:
        return jsonify({'error': 'Members count must be between 1 and 50'}), 400
    if not free_entry and paid <= 0:
        return jsonify({'error': 'Amount paid is required, or mark this registration as Free Entry'}), 400

    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE token_counter SET current = current + 1 WHERE id = 1")
                cur.execute("SELECT current FROM token_counter WHERE id = 1")
                tok_num = cur.fetchone()['current']
                token = str(tok_num).zfill(3)

                cur.execute("""
                    INSERT INTO registrations (token, name, address, mobile, persons, paid, free_entry, registered_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (token, name, address, mobile, persons, paid, int(free_entry), registered_by))
                conn.commit()
        finally:
            conn.close()

        log.info(f'Janmashtami Registration: Token={token} Name={name} Members={persons} Paid={paid} Free={free_entry}')
        return jsonify({
            'success':    True,
            'token':      token,
            'name':       name,
            'persons':    persons,
            'paid':       paid,
            'free_entry': free_entry,
            'reg_at':     datetime.now().strftime('%d/%m/%Y %H:%M'),
        }), 201
    except Exception as e:
        log.error(f'register error: {e}')
        return jsonify({'error': str(e)}), 500

# ============================================================
#  ATTENDANCE & GATE GATEWAY
# ============================================================
@app.route('/api/attendance', methods=['GET'])
@login_required
def list_attendance():
    try:
        rows = db_query("""
            SELECT a.*, u.name AS scanned_by_name, u.username AS scanned_by_username
            FROM attendance a
            LEFT JOIN users u ON u.id = a.scanned_by
            ORDER BY a.gate_time DESC
        """)
        for row in rows:
            if row.get('gate_time'):
                row['gate_time'] = row['gate_time'].strftime('%d/%m/%Y %H:%M:%S')
        return jsonify({'attendance': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/attendance/hourly', methods=['GET'])
@login_required
def hourly_attendance():
    try:
        rows = db_query("""
            SELECT HOUR(scan_time) AS hr, COUNT(DISTINCT token) AS families, SUM(persons) AS persons
            FROM attendance_log
            WHERE DATE(scan_time) = CURDATE()
            GROUP BY HOUR(scan_time)
            ORDER BY hr
        """)
        slots = {h: {'families': 0, 'persons': 0} for h in range(8, 22)}
        for row in rows:
            h = int(row['hr'])
            if h in slots:
                slots[h] = {'families': int(row['families']), 'persons': int(row['persons'])}
        return jsonify({'hourly': slots})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/gate/search', methods=['GET'])
@login_required
def search_gate_tokens():
    try:
        q = str(request.args.get('q', '')).strip()
        clean_q = re.sub(r'[^\w\s]', '', q)
        
        if not clean_q:
            rows = db_query("""
                SELECT r.token, r.name, r.mobile, r.persons, r.paid, r.free_entry,
                       IF(a.token IS NOT NULL, 1, 0) AS attended,
                       a.gate_time
                FROM registrations r
                LEFT JOIN attendance a ON r.token = a.token
                ORDER BY r.id DESC
                LIMIT 10
            """)
        else:
            query_pattern = f'%{clean_q}%'
            rows = db_query("""
                SELECT r.token, r.name, r.mobile, r.persons, r.paid, r.free_entry,
                       IF(a.token IS NOT NULL, 1, 0) AS attended,
                       a.gate_time
                FROM registrations r
                LEFT JOIN attendance a ON r.token = a.token
                WHERE r.token LIKE %s OR r.mobile LIKE %s OR r.name LIKE %s
                ORDER BY r.id DESC
                LIMIT 10
            """, (query_pattern, query_pattern, query_pattern))

        for r in rows:
            if r.get('gate_time') and hasattr(r['gate_time'], 'strftime'):
                r['gate_time'] = r['gate_time'].strftime('%H:%M')
        return jsonify({'results': rows})
    except Exception as e:
        log.error(f'search_gate_tokens error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/gate/scan', methods=['POST'])
@login_required
def gate_scan():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data sent'}), 400

    token   = str(data.get('token', '')).strip()
    name    = str(data.get('name', '')).strip()
    # Default 0 (not 1) so a normal first scan without an explicit persons
    # override falls back to the family's full registered headcount below,
    # instead of only checking in a single person.
    persons = int(data.get('persons', 0))
    paid    = int(data.get('paid', 0))
    mobile  = str(data.get('mobile', '')).strip()

    if not token:
        return jsonify({'error': 'Registration Token is required'}), 400

    try:
        token_input = token
        reg = db_query("SELECT * FROM registrations WHERE token = %s OR mobile = %s", (token_input, token_input), fetch='one')
        if not reg:
            return jsonify({'status': 'not_found', 'message': f'No registration found for token or mobile number "{token_input}".'}), 404

        token = reg['token']
        actual_name = reg['name']
        actual_persons = persons if persons > 0 else reg['persons']
        actual_paid = reg['paid']
        actual_mobile = reg['mobile']

        existing = db_query("SELECT * FROM attendance WHERE token = %s", (token,), fetch='one')
        if existing:
            gate_time = existing['gate_time']
            if hasattr(gate_time, 'strftime'):
                gate_time = gate_time.strftime('%H:%M')
            registered_total = reg['persons']
            already_in = existing['persons']
            remaining = max(0, registered_total - already_in)

            if persons > 0 and data.get('add_more'):
                new_total = already_in + persons
                db_execute("UPDATE attendance SET persons = %s WHERE token = %s", (new_total, token))
                db_execute(
                    "INSERT INTO attendance_log (token, persons, scanned_by) VALUES (%s, %s, %s)",
                    (token, persons, session['user_id'])
                )
                log.info(f'Gate update: added {persons} more for token={token}, total={new_total}')
                return jsonify({
                    'status':    'success',
                    'message':   f'{persons} additional family member(s) logged. Total inside: {new_total}',
                    'token':     token,
                    'name':      actual_name,
                    'persons':   new_total,
                    'gate_time': gate_time,
                }), 200

            return jsonify({
                'status':     'duplicate',
                'message':    f'Token {token} ({actual_name}) already scanned at gate ({gate_time})',
                'token':      token,
                'name':       actual_name,
                'persons':    already_in,
                'registered': registered_total,
                'remaining':  remaining,
                'gate_time':  gate_time,
            }), 200

        scanned_by = session['user_id']
        db_execute("""
            INSERT INTO attendance (token, name, persons, paid, mobile, scanned_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (token, actual_name, actual_persons, actual_paid, actual_mobile, scanned_by))
        db_execute(
            "INSERT INTO attendance_log (token, persons, scanned_by) VALUES (%s, %s, %s)",
            (token, actual_persons, scanned_by)
        )

        now_str = datetime.now().strftime('%H:%M:%S')
        log.info(f'Janmashtami Gate Entry Granted: Token={token} Name={actual_name} Members={actual_persons} ScannedBy={session.get("username")}')
        return jsonify({
            'status':    'success',
            'message':   f'Gate entry granted! {actual_persons} devotee(s) checked in (Token #{token}).',
            'token':     token,
            'name':      actual_name,
            'persons':   actual_persons,
            'paid':      actual_paid,
            'gate_time': now_str,
        }), 200
    except Exception as e:
        log.error(f'gate_scan error: {e}')
        return jsonify({'error': str(e)}), 500

# ============================================================
#  CSV REPORT EXPORT
# ============================================================
@app.route('/api/export/csv', methods=['GET'])
@login_required
def export_csv():
    try:
        rows = db_query("""
            SELECT
                r.token,
                r.name,
                r.address,
                r.mobile,
                r.persons AS registered_members,
                r.paid,
                r.free_entry,
                r.reg_at,
                COALESCE(u.name, 'Unknown')   AS registered_by_user,
                IF(a.token IS NOT NULL,'Yes','No') AS attended,
                COALESCE(a.persons, 0)             AS members_counted,
                COALESCE(DATE_FORMAT(a.gate_time,'%%d/%%m/%%Y %%H:%%i'), '') AS gate_time
            FROM registrations r
            LEFT JOIN users u ON u.id = r.registered_by
            LEFT JOIN attendance a ON r.token = a.token
            ORDER BY r.id ASC
        """)

        lines = ['Token,Family Head,Address,Mobile,Registered Members,Paid (Rs),Free Entry,Registered At,Registered By Volunteer,Gate Attended,Gate Members Counted,Gate Entry Time']
        for r in rows:
            addr   = str(r['address']).replace(',', ';').replace('\n', ' ')
            reg_at = r['reg_at'].strftime('%d/%m/%Y %H:%M') if hasattr(r['reg_at'], 'strftime') else r['reg_at']
            lines.append(
                f"{r['token']},{r['name']},{addr},{r['mobile']},"
                f"{r['registered_members']},{r['paid']},{'Yes' if r['free_entry'] else 'No'},"
                f"{reg_at},{r['registered_by_user']},"
                f"{r['attended']},{r['members_counted']},{r['gate_time']}"
            )

        return Response(
            '\n'.join(lines),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=iskcon_janmastmi_report_2026.csv'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
#  ADMIN DATA RESET
# ============================================================
@app.route('/api/admin/clear', methods=['POST'])
@admin_required
def clear_all():
    secret = request.get_json().get('secret', '')
    if secret != 'ISKCON_JANMASTMI_CLEAR_2026':
        return jsonify({'error': 'Invalid confirmation secret code'}), 403
    try:
        db_execute("DELETE FROM attendance")
        db_execute("DELETE FROM registrations")
        db_execute("UPDATE token_counter SET current = 0 WHERE id = 1")
        log.warning('ALL JANMASTHAMI DATA CLEARED BY ADMIN')
        return jsonify({'success': True, 'message': 'All registration & gate data reset successfully.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
#  MAIN SERVER RUNNER
# ============================================================
if __name__ == '__main__':
    import socket
    hostname = socket.gethostname()
    try:
        lan_ip = socket.gethostbyname(hostname)
    except Exception:
        lan_ip = '127.0.0.1'

    port = int(os.environ.get('PORT', 5005))
    print('\n' + '='*65)
    print('  [+] ISKCON SOCIETY - JANMASTHAMI CELEBRATION 2026')
    print('  Devotee Gathering & Gate Attendance Gateway')
    print('='*65)
    print(f'  Local Access:   http://localhost:{port}')
    print(f'  LAN Access:     http://{lan_ip}:{port}')
    print(f'  Default Login:  admin / admin123')
    print('='*65 + '\n')

    app.run(host='0.0.0.0', port=port, debug=False)
