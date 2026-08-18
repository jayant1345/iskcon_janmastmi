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
import time
import pymysql
import pymysql.cursors
from datetime import datetime, timedelta
import logging
import razorpay

app = Flask(__name__, static_folder='.', static_url_path='')

# ── RAZORPAY (online self-registration payment) ──
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET')
razorpay_client = (
    razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else None
)
# ONLINE_PAYMENT_MODE controls how the public /register flow collects payment:
#   'pending'      -- pay later (instant token, settle in person) -- the safe default
#   'razorpay'     -- embedded Checkout.js on this domain -- needs the website approved
#                      in Razorpay's dashboard (Checkout enforces a domain allowlist)
#   'payment_link' -- redirect to a Razorpay-hosted Payment Link page instead; since the
#                      payment page itself isn't embedded on this domain, it isn't subject
#                      to that same website-approval check, so it can work immediately
ONLINE_PAYMENT_MODE = os.environ.get('ONLINE_PAYMENT_MODE', 'pending').strip().lower()
RAZORPAY_ENABLED = razorpay_client is not None and ONLINE_PAYMENT_MODE == 'razorpay'
PAYMENT_LINK_ENABLED = razorpay_client is not None and ONLINE_PAYMENT_MODE == 'payment_link'

# Optional seva add-ons offered during public self-registration. Fixed server-side prices
# -- never trust an amount for these from the client, only which ones were selected.
SEVA_AARTI_PRICE = 500
SEVA_ABHISHEK_TIERS = {'abhishek': 1100, 'silver_kalash': 2500, 'golden_kalash': 5001}

# Volunteer-assisted Razorpay QR: how long the customer has to scan-and-pay before the
# link expires and the volunteer's app stops waiting. Enforced both on Razorpay's side
# (expire_by) and the frontend's poll timeout, from this single source of truth.
VOLUNTEER_QR_PAYMENT_WINDOW_SECONDS = 180

def parse_seva_selection(data):
    """Server-side source of truth for what a public registrant selected -- only the
    boolean/tier choice is read from the client, the rupee amounts always come from the
    fixed dicts above."""
    aarti_amount = SEVA_AARTI_PRICE if data.get('aarti') else 0
    abhishek_tier = str(data.get('abhishek_tier', 'none') or 'none').strip().lower()
    abhishek_amount = SEVA_ABHISHEK_TIERS.get(abhishek_tier, 0)
    return aarti_amount, abhishek_amount

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
    # The event is in India; without this every connection defaults to UTC, so
    # CURRENT_TIMESTAMP/NOW() (and every DATETIME column that relies on them)
    # silently store/report UTC instead of IST.
    'init_command': "SET time_zone = '+05:30'",
}

IST_OFFSET = timedelta(hours=5, minutes=30)
def ist_now():
    """IST wall-clock time, independent of the host container's local TZ."""
    return datetime.utcnow() + IST_OFFSET

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
                    category      VARCHAR(10)  NOT NULL DEFAULT 'volunteer',
                    payment_ref   VARCHAR(64)  NULL,
                    registered_by INT          NULL,
                    collected_by  INT          NULL,
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

            # ── SETTINGS (admin-adjustable pricing) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id             INT PRIMARY KEY DEFAULT 1,
                    token_rate     INT NOT NULL DEFAULT 20,
                    aarti_price    INT NOT NULL DEFAULT 101,
                    abhishek_price INT NOT NULL DEFAULT 251,
                    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
            """)
            cur.execute("INSERT IGNORE INTO settings (id, token_rate, aarti_price, abhishek_price) VALUES (1, 20, 101, 251)")

            # ── SETTLEMENTS (volunteer cash/UPI remittance to central admin) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settlements (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    volunteer_id INT          NOT NULL,
                    amount       INT          NOT NULL,
                    note         VARCHAR(255) NULL,
                    recorded_by  INT          NULL,
                    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (volunteer_id) REFERENCES users(id) ON DELETE CASCADE,
                    INDEX idx_volunteer (volunteer_id)
                ) ENGINE=InnoDB CHARACTER SET utf8mb4
            """)

            # ── Migration Helpers ──
            migrations = [
                ('registrations', 'registered_by',    "ALTER TABLE registrations ADD COLUMN registered_by INT NULL"),
                ('registrations', 'free_entry',        "ALTER TABLE registrations ADD COLUMN free_entry TINYINT(1) NOT NULL DEFAULT 0"),
                ('registrations', 'token_amount',       "ALTER TABLE registrations ADD COLUMN token_amount INT NOT NULL DEFAULT 0"),
                ('registrations', 'aarti_amount',       "ALTER TABLE registrations ADD COLUMN aarti_amount INT NOT NULL DEFAULT 0"),
                ('registrations', 'abhishek_amount',    "ALTER TABLE registrations ADD COLUMN abhishek_amount INT NOT NULL DEFAULT 0"),
                ('registrations', 'donation_amount',    "ALTER TABLE registrations ADD COLUMN donation_amount INT NOT NULL DEFAULT 0"),
                ('registrations', 'payment_mode',       "ALTER TABLE registrations ADD COLUMN payment_mode VARCHAR(10) NOT NULL DEFAULT 'cash'"),
                ('registrations', 'category',            "ALTER TABLE registrations ADD COLUMN category VARCHAR(10) NOT NULL DEFAULT 'volunteer'"),
                ('registrations', 'payment_ref',          "ALTER TABLE registrations ADD COLUMN payment_ref VARCHAR(64) NULL"),
                ('registrations', 'collected_by',         "ALTER TABLE registrations ADD COLUMN collected_by INT NULL"),
                ('attendance',    'scanned_by',          "ALTER TABLE attendance ADD COLUMN scanned_by INT NULL"),
                ('users',         'upi_id',              "ALTER TABLE users ADD COLUMN upi_id VARCHAR(100) NULL"),
                ('settings',      'tz_backfilled',       "ALTER TABLE settings ADD COLUMN tz_backfilled TINYINT(1) NOT NULL DEFAULT 0"),
            ]
            for table, col_name, col_sql in migrations:
                try:
                    cur.execute("""
                        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = %s
                          AND COLUMN_NAME = %s
                    """, (table, col_name))
                    if cur.fetchone()['cnt'] == 0:
                        cur.execute(col_sql)
                        log.info(f'Migration: added column {table}.{col_name}')
                except pymysql.err.OperationalError as me:
                    if me.args and me.args[0] == 1060:
                        pass  # Duplicate column: another gunicorn worker won the race, harmless
                    else:
                        log.error(f'Migration error ({table}.{col_name}): {me}')
                except Exception as me:
                    log.error(f'Migration error ({table}.{col_name}): {me}')

            # ── One-time IST backfill ──
            # Every DATETIME column here was written before this connection started
            # forcing session time_zone='+05:30', so existing rows hold UTC wall-clock
            # values (5:30 behind the intended IST). Shift them forward exactly once.
            # The conditional UPDATE below is an atomic claim: only the worker that
            # actually flips 0->1 runs the backfill, so concurrent gunicorn workers
            # racing here on startup can't double-shift the data.
            try:
                cur.execute("UPDATE settings SET tz_backfilled = 1 WHERE id = 1 AND tz_backfilled = 0")
                if cur.rowcount == 1:
                    for table, col in (
                        ('registrations', 'reg_at'),
                        ('attendance', 'gate_time'),
                        ('attendance_log', 'scan_time'),
                        ('settlements', 'created_at'),
                        ('users', 'created_at'),
                    ):
                        cur.execute(f"UPDATE {table} SET {col} = DATE_ADD({col}, INTERVAL 330 MINUTE)")
                    conn.commit()
                    log.info('Timezone backfill: shifted existing UTC timestamps to IST (+5:30).')
            except Exception as me:
                log.error(f'Timezone backfill error: {me}')

            # ── Seed/sync default admin user ──
            # ON DUPLICATE KEY UPDATE keeps the admin password in sync with the
            # ADMIN_PASSWORD env var on every restart, so changing it on Railway
            # and redeploying is enough to change the login (no manual DB access
            # needed). Note: this means an admin password changed later via the
            # Users UI reverts to ADMIN_PASSWORD on the next restart/deploy unless
            # the env var is also updated to match.
            admin_pw = os.environ.get('ADMIN_PASSWORD', 'admin123')
            admin_mob = os.environ.get('ADMIN_MOBILE', '0000000000')
            cur.execute("""
                INSERT INTO users (username, password_hash, name, mobile, role)
                VALUES (%s, %s, %s, %s, 'admin')
                ON DUPLICATE KEY UPDATE password_hash = VALUES(password_hash)
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

@app.route('/register')
def serve_public_register():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'register.html')

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
    session['upi_id']   = user.get('upi_id') or ''
    log.info(f"Login successful: {username} (Role: {user['role']})")
    return jsonify({
        'success': True,
        'user': {
            'id':       user['id'],
            'username': user['username'],
            'name':     user['name'],
            'mobile':   user['mobile'],
            'upi_id':   user.get('upi_id') or '',
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
            'upi_id':   session.get('upi_id') or '',
            'role':     session['role'],
        }
    })

# ============================================================
#  USER MANAGEMENT (ADMIN ONLY)
# ============================================================
UPI_ID_RE = re.compile(r'^[\w.\-]{2,256}@[a-zA-Z]{2,64}$')

@app.route('/api/users', methods=['GET'])
@admin_required
def list_users():
    users = db_query("SELECT id, username, name, mobile, upi_id, role, created_at FROM users ORDER BY id")
    for u in users:
        u['upi_id'] = u.get('upi_id') or ''
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
    upi_id   = str(data.get('upi_id', '')).strip()
    role     = data.get('role', 'user')

    if not all([username, password, name, mobile]):
        return jsonify({'error': 'All fields (username, password, name, mobile) are required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Invalid role specified'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile must be exactly 10 digits'}), 400
    if upi_id and not UPI_ID_RE.match(upi_id):
        return jsonify({'error': 'UPI ID must look like name@bank (e.g. 9876543210@ybl)'}), 400
    try:
        uid = db_execute(
            "INSERT INTO users (username, password_hash, name, mobile, upi_id, role) VALUES (%s,%s,%s,%s,%s,%s)",
            (username, generate_password_hash(password), name, mobile, upi_id or None, role)
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
    upi_id = str(data.get('upi_id', '')).strip()
    role   = data.get('role', 'user')

    if not name or not mobile:
        return jsonify({'error': 'Name and mobile are required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Invalid role'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile must be exactly 10 digits'}), 400
    if upi_id and not UPI_ID_RE.match(upi_id):
        return jsonify({'error': 'UPI ID must look like name@bank (e.g. 9876543210@ybl)'}), 400

    if data.get('password'):
        db_execute(
            "UPDATE users SET name=%s, mobile=%s, upi_id=%s, role=%s, password_hash=%s WHERE id=%s",
            (name, mobile, upi_id or None, role, generate_password_hash(data['password']), uid)
        )
    else:
        db_execute(
            "UPDATE users SET name=%s, mobile=%s, upi_id=%s, role=%s WHERE id=%s",
            (name, mobile, upi_id or None, role, uid)
        )
    log.info(f'Updated user id={uid}')
    return jsonify({'success': True})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
@admin_required
def delete_user(uid):
    admins = db_query("SELECT id FROM users WHERE role='admin'")
    if len(admins) == 1 and admins[0]['id'] == uid:
        return jsonify({'error': 'Cannot delete the primary admin account'}), 400

    # settlements.volunteer_id cascades on delete, so removing an account with
    # settlement history would silently erase the record of money already remitted --
    # block it instead of letting the financial trail vanish.
    settlement_count = db_query(
        "SELECT COUNT(*) AS cnt FROM settlements WHERE volunteer_id = %s", (uid,), fetch='one'
    )['cnt']
    registration_count = db_query(
        "SELECT COUNT(*) AS cnt FROM registrations WHERE registered_by = %s", (uid,), fetch='one'
    )['cnt']
    if settlement_count > 0 or registration_count > 0:
        return jsonify({
            'error': (
                f'Cannot delete: this account has {registration_count} registration(s) and '
                f'{settlement_count} settlement record(s). Deleting would destroy that financial '
                f'history. Keep the account (they can simply stop using it) instead of removing it.'
            )
        }), 400

    db_execute("DELETE FROM users WHERE id=%s", (uid,))
    log.info(f'Deleted user id={uid}')
    return jsonify({'success': True})

# ============================================================
#  PRICING SETTINGS (per-person token rate, Aarti/Abhishek suggested prices)
# ============================================================
def get_settings_row():
    row = db_query("SELECT token_rate, aarti_price, abhishek_price FROM settings WHERE id = 1", fetch='one')
    if not row:
        return {'token_rate': 20, 'aarti_price': 101, 'abhishek_price': 251}
    return row

@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    try:
        settings = get_settings_row()
        # Tells the volunteer app whether to offer the "Pay via Razorpay" QR option --
        # independent of ONLINE_PAYMENT_MODE, which only governs the public /register flow.
        settings['razorpay_configured'] = razorpay_client is not None
        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/settings', methods=['PUT'])
@admin_required
def update_settings():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data sent'}), 400
    try:
        current = get_settings_row()
        token_rate     = int(data.get('token_rate', current['token_rate']))
        aarti_price    = int(data.get('aarti_price', current['aarti_price']))
        abhishek_price = int(data.get('abhishek_price', current['abhishek_price']))
        if token_rate < 0 or aarti_price < 0 or abhishek_price < 0:
            return jsonify({'error': 'Prices cannot be negative'}), 400
        db_execute(
            "UPDATE settings SET token_rate=%s, aarti_price=%s, abhishek_price=%s WHERE id=1",
            (token_rate, aarti_price, abhishek_price)
        )
        log.info(f'Settings updated by {session.get("username")}: rate={token_rate} aarti={aarti_price} abhishek={abhishek_price}')
        return jsonify({'success': True, 'token_rate': token_rate, 'aarti_price': aarti_price, 'abhishek_price': abhishek_price})
    except (TypeError, ValueError):
        return jsonify({'error': 'Prices must be whole numbers'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
#  VOLUNTEER SETTLEMENTS (cash/UPI collected -> remitted to central admin)
# ============================================================
@app.route('/api/admin/settlements', methods=['GET'])
@admin_required
def list_settlements():
    try:
        volunteer_id = request.args.get('volunteer_id')
        if volunteer_id:
            rows = db_query("""
                SELECT s.*, u.name AS volunteer_name, u.username AS volunteer_username, r.name AS recorded_by_name
                FROM settlements s
                JOIN users u ON u.id = s.volunteer_id
                LEFT JOIN users r ON r.id = s.recorded_by
                WHERE s.volunteer_id = %s
                ORDER BY s.created_at DESC
            """, (volunteer_id,))
        else:
            rows = db_query("""
                SELECT s.*, u.name AS volunteer_name, u.username AS volunteer_username, r.name AS recorded_by_name
                FROM settlements s
                JOIN users u ON u.id = s.volunteer_id
                LEFT JOIN users r ON r.id = s.recorded_by
                ORDER BY s.created_at DESC
            """)
        for row in rows:
            if row.get('created_at'):
                row['created_at'] = row['created_at'].strftime('%d/%m/%Y %H:%M')
        return jsonify({'settlements': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/settlements', methods=['POST'])
@admin_required
def create_settlement():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data sent'}), 400
    try:
        volunteer_id = int(data.get('volunteer_id', 0))
        amount = int(data.get('amount', 0))
        note = str(data.get('note', '')).strip()[:255]
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid volunteer or amount'}), 400

    if amount <= 0:
        return jsonify({'error': 'Amount must be greater than zero'}), 400
    volunteer = db_query("SELECT id, name FROM users WHERE id = %s", (volunteer_id,), fetch='one')
    if not volunteer:
        return jsonify({'error': 'Volunteer not found'}), 404

    sid = db_execute(
        "INSERT INTO settlements (volunteer_id, amount, note, recorded_by) VALUES (%s, %s, %s, %s)",
        (volunteer_id, amount, note, session['user_id'])
    )
    log.info(f'Settlement recorded: volunteer={volunteer["name"]} amount={amount} by={session.get("username")}')
    return jsonify({'success': True, 'id': sid}), 201

# ============================================================
#  STATISTICS & ANALYTICS
# ============================================================
@app.route('/api/stats', methods=['GET'])
@login_required
def get_stats():
    try:
        reg_row = db_query("""
            SELECT COUNT(*) AS families, COALESCE(SUM(persons),0) AS persons, COALESCE(SUM(paid),0) AS collection,
                   COALESCE(SUM(token_amount),0) AS token_total, COALESCE(SUM(aarti_amount),0) AS aarti_total,
                   COALESCE(SUM(abhishek_amount),0) AS abhishek_total, COALESCE(SUM(donation_amount),0) AS donation_total
            FROM registrations
        """, fetch='one')
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
            result['token_total'] = int(reg_row['token_total'])
            result['aarti_total'] = int(reg_row['aarti_total'])
            result['abhishek_total'] = int(reg_row['abhishek_total'])
            result['donation_total'] = int(reg_row['donation_total'])
        return jsonify(result)
    except Exception as e:
        log.error(f'stats error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/user-stats', methods=['GET'])
@admin_required
def user_stats():
    try:
        # Registrations and attendance are aggregated in separate subqueries before
        # joining to users. Joining both tables directly in one GROUP BY fans out
        # matching rows (every registration x every attendance row for that user),
        # which silently inflates or (with the old SUM(DISTINCT) workaround)
        # undercounts the collection total whenever two amounts happen to match.
        rows = db_query("""
            SELECT
                u.id,
                u.name,
                u.username,
                u.mobile,
                COALESCE(reg.families_registered, 0) AS families_registered,
                COALESCE(reg.persons_registered, 0)  AS persons_registered,
                COALESCE(reg.collection, 0)          AS collection,
                COALESCE(reg.token_total, 0)         AS token_total,
                COALESCE(reg.aarti_total, 0)         AS aarti_total,
                COALESCE(reg.abhishek_total, 0)      AS abhishek_total,
                COALESCE(reg.donation_total, 0)      AS donation_total,
                COALESCE(att.families_scanned, 0)    AS families_scanned,
                COALESCE(att.persons_scanned, 0)     AS persons_scanned,
                COALESCE(sett.submitted, 0)          AS submitted
            FROM users u
            LEFT JOIN (
                SELECT registered_by,
                       COUNT(*)             AS families_registered,
                       SUM(persons)         AS persons_registered,
                       SUM(paid)            AS collection,
                       SUM(token_amount)    AS token_total,
                       SUM(aarti_amount)    AS aarti_total,
                       SUM(abhishek_amount) AS abhishek_total,
                       SUM(donation_amount) AS donation_total
                FROM registrations
                GROUP BY registered_by
            ) reg ON reg.registered_by = u.id
            LEFT JOIN (
                SELECT scanned_by, COUNT(*) AS families_scanned, SUM(persons) AS persons_scanned
                FROM attendance
                GROUP BY scanned_by
            ) att ON att.scanned_by = u.id
            LEFT JOIN (
                SELECT volunteer_id, SUM(amount) AS submitted
                FROM settlements
                GROUP BY volunteer_id
            ) sett ON sett.volunteer_id = u.id
            ORDER BY collection DESC, families_scanned DESC
        """)
        for row in rows:
            for key in ('families_registered', 'persons_registered', 'collection', 'token_total',
                        'aarti_total', 'abhishek_total', 'donation_total', 'families_scanned',
                        'persons_scanned', 'submitted'):
                row[key] = int(row[key])
            row['balance_due'] = row['collection'] - row['submitted']
        return jsonify({'user_stats': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/my-stats', methods=['GET'])
@login_required
def my_stats():
    try:
        uid = session['user_id']
        row = db_query("""
            SELECT COUNT(*) AS families, COALESCE(SUM(persons),0) AS persons,
                   COALESCE(SUM(paid),0) AS collection
            FROM registrations WHERE registered_by = %s
        """, (uid,), fetch='one')
        sett_row = db_query(
            "SELECT COALESCE(SUM(amount),0) AS submitted FROM settlements WHERE volunteer_id = %s",
            (uid,), fetch='one'
        )
        collection = int(row['collection'])
        submitted = int(sett_row['submitted'])
        return jsonify({
            'families': row['families'],
            'persons': int(row['persons']),
            'collection': collection,
            'submitted': submitted,
            'balance_due': collection - submitted,
        })
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
                   u.username AS registered_by_username,
                   c.name AS collected_by_name
            FROM registrations r
            LEFT JOIN attendance a ON r.token = a.token
            LEFT JOIN users u ON u.id = r.registered_by
            LEFT JOIN users c ON c.id = r.collected_by
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

@app.route('/api/registrations/<token>/collect-payment', methods=['POST'])
@login_required
def collect_pending_payment(token):
    """Any logged-in volunteer can settle an online registration's deferred
    ('pending') payment in person -- e.g. cash/UPI collected before the event."""
    data = request.get_json() or {}
    payment_mode = str(data.get('payment_mode', '')).strip().lower()
    if payment_mode not in ('cash', 'upi'):
        return jsonify({'error': "payment_mode must be 'cash' or 'upi'"}), 400

    row = db_query("SELECT * FROM registrations WHERE token=%s", (token,), fetch='one')
    if not row:
        return jsonify({'error': 'Registration not found'}), 404
    if row['payment_mode'] != 'pending':
        return jsonify({'error': 'This registration is not awaiting payment'}), 400

    paid = row['token_amount'] + row['aarti_amount'] + row['abhishek_amount'] + row['donation_amount']
    db_execute(
        "UPDATE registrations SET paid=%s, payment_mode=%s, collected_by=%s WHERE token=%s",
        (paid, payment_mode, session['user_id'], token)
    )
    log.info(f'Pending payment collected: Token={token} Mode={payment_mode} Amount={paid} By={session.get("username")}')
    return jsonify({'success': True, 'token': token, 'paid': paid, 'payment_mode': payment_mode})

@app.route('/api/registrations/<token>', methods=['DELETE'])
@admin_required
def delete_registration(token):
    """Admin-only: remove a single mistaken registration (e.g. duplicate/test entry)
    without wiping the whole event's data via the Danger Zone reset."""
    row = db_query("SELECT name FROM registrations WHERE token=%s", (token,), fetch='one')
    if not row:
        return jsonify({'error': 'Registration not found'}), 404
    db_execute("DELETE FROM attendance WHERE token=%s", (token,))
    db_execute("DELETE FROM registrations WHERE token=%s", (token,))
    log.warning(f'Registration deleted: Token={token} Name={row["name"]} By={session.get("username")}')
    return jsonify({'success': True, 'token': token})

def _create_registration(data, registered_by, category):
    name       = str(data.get('name', '')).strip()
    address    = str(data.get('address', '')).strip()
    mobile     = str(data.get('mobile', '')).strip()
    persons    = int(data.get('persons', 1))
    free_entry = bool(data.get('free_entry', False))
    payment_mode = str(data.get('payment_mode', 'cash')).strip().lower()
    payment_ref  = str(data.get('payment_ref', '') or '').strip()[:64] or None
    allowed_modes = ('cash', 'upi', 'razorpay', 'free') if category == 'volunteer' else ('razorpay', 'pending', 'free')
    if payment_mode not in allowed_modes:
        payment_mode = allowed_modes[0]

    try:
        aarti_amount    = max(0, int(data.get('aarti_amount', 0) or 0))
        abhishek_amount = max(0, int(data.get('abhishek_amount', 0) or 0))
        donation_amount = max(0, int(data.get('donation_amount', 0) or 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Aarti, Abhishek and Donation amounts must be whole numbers'}), 400

    if not name or not address or not mobile:
        return jsonify({'error': 'Name, address, and mobile number are required'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile number must be 10 digits'}), 400
    if persons < 1 or persons > 50:
        return jsonify({'error': 'Members count must be between 1 and 50'}), 400

    # Token/entry amount is never trusted from the client -- it's always persons x the
    # admin-configured rate, so the charge can't be tampered with in the request body.
    rate = get_settings_row()['token_rate']
    token_amount = 0 if free_entry else persons * rate
    if free_entry:
        payment_mode = 'free'
    # Online self-registrations aren't collected at the counter yet -- payment is settled
    # later once the payment number/QR is shared, so nothing is recorded as "paid" here.
    # It stays informational (token_amount) rather than feeding the collection totals.
    paid = 0 if payment_mode == 'pending' else (token_amount + aarti_amount + abhishek_amount + donation_amount)

    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE token_counter SET current = current + 1 WHERE id = 1")
                cur.execute("SELECT current FROM token_counter WHERE id = 1")
                tok_num = cur.fetchone()['current']
                token = str(tok_num).zfill(3)

                cur.execute("""
                    INSERT INTO registrations
                        (token, name, address, mobile, persons, paid, free_entry, registered_by, category,
                         token_amount, aarti_amount, abhishek_amount, donation_amount, payment_mode, payment_ref)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (token, name, address, mobile, persons, paid, int(free_entry), registered_by, category,
                      token_amount, aarti_amount, abhishek_amount, donation_amount, payment_mode, payment_ref))
                conn.commit()
        finally:
            conn.close()

        log.info(f'Janmashtami Registration: Token={token} Name={name} Members={persons} Category={category} '
                 f'Token={token_amount} Aarti={aarti_amount} Abhishek={abhishek_amount} '
                 f'Donation={donation_amount} Total={paid} Free={free_entry} PaymentMode={payment_mode}')
        return jsonify({
            'success':         True,
            'token':           token,
            'name':            name,
            'mobile':          mobile,
            'address':         address,
            'persons':         persons,
            'paid':            paid,
            'token_amount':    token_amount,
            'aarti_amount':    aarti_amount,
            'abhishek_amount': abhishek_amount,
            'donation_amount': donation_amount,
            'free_entry':      free_entry,
            'payment_mode':    payment_mode,
            'category':        category,
            'reg_at':          ist_now().strftime('%d/%m/%Y %H:%M'),
        }), 201
    except Exception as e:
        log.error(f'register error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/register', methods=['POST'])
@login_required
def register_family():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No payload submitted'}), 400
    return _create_registration(data, registered_by=session['user_id'], category='volunteer')

@app.route('/api/register/public/config', methods=['GET'])
def register_public_config():
    """Public, no-login -- tells the /register page which payment flow to run:
    embedded Checkout ('razorpay'), a redirect to a Razorpay Payment Link
    ('payment_link' -- works even while the Checkout domain approval is pending,
    since the payment page itself is hosted on Razorpay, not this domain), or
    the pay-later fallback ('pending')."""
    mode = 'razorpay' if RAZORPAY_ENABLED else ('payment_link' if PAYMENT_LINK_ENABLED else 'pending')
    return jsonify({
        'mode': mode,
        'razorpay_enabled': RAZORPAY_ENABLED,
        'token_rate': get_settings_row()['token_rate'],
        'aarti_price': SEVA_AARTI_PRICE,
        'abhishek_tiers': SEVA_ABHISHEK_TIERS,
    })

@app.route('/api/razorpay/order', methods=['POST'])
def create_razorpay_order():
    """Public, no-login -- creates a Razorpay order for the token amount of an
    online self-registration, ahead of opening the Checkout popup."""
    if not RAZORPAY_ENABLED:
        return jsonify({'error': 'Online payment is not available right now'}), 503

    data = request.get_json() or {}
    try:
        persons = int(data.get('persons', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid members count'}), 400
    if persons < 1 or persons > 50:
        return jsonify({'error': 'Members count must be between 1 and 50'}), 400

    rate = get_settings_row()['token_rate']
    aarti_amount, abhishek_amount = parse_seva_selection(data)
    amount_paise = (persons * rate + aarti_amount + abhishek_amount) * 100
    if amount_paise <= 0:
        return jsonify({'error': 'Nothing to pay'}), 400

    try:
        order = razorpay_client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'payment_capture': 1,
            'notes': {
                'event': 'ISKCON Janmashtami 2026 Online Registration',
                'name': str(data.get('name', '')).strip()[:100],
                'mobile': str(data.get('mobile', '')).strip()[:15],
                'persons': persons,
                'aarti_amount': aarti_amount,
                'abhishek_amount': abhishek_amount,
            },
        })
        return jsonify({'order_id': order['id'], 'amount': amount_paise, 'currency': 'INR', 'key_id': RAZORPAY_KEY_ID})
    except Exception as e:
        log.error(f'razorpay order create error: {e}')
        return jsonify({'error': 'Could not initiate payment. Please try again.'}), 500

@app.route('/api/razorpay/payment-link', methods=['POST'])
def create_razorpay_payment_link():
    """Public, no-login -- creates a Razorpay Payment Link for this registration and
    returns its hosted URL to redirect the browser to. Unlike embedded Checkout, this
    page is hosted on Razorpay's own domain, so it isn't blocked by the website/domain
    approval that gates the Checkout.js flow on this site."""
    if not PAYMENT_LINK_ENABLED:
        return jsonify({'error': 'Online payment is not available right now'}), 503

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    mobile = str(data.get('mobile', '')).strip()
    address = str(data.get('address', '')).strip()
    try:
        persons = int(data.get('persons', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid members count'}), 400

    if not name or not address or not mobile:
        return jsonify({'error': 'Name, address, and mobile number are required'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile number must be 10 digits'}), 400
    if persons < 1 or persons > 50:
        return jsonify({'error': 'Members count must be between 1 and 50'}), 400

    rate = get_settings_row()['token_rate']
    aarti_amount, abhishek_amount = parse_seva_selection(data)
    amount_paise = (persons * rate + aarti_amount + abhishek_amount) * 100
    if amount_paise <= 0:
        return jsonify({'error': 'Nothing to pay'}), 400

    seva_bits = []
    if aarti_amount:
        seva_bits.append('Aarti Seva')
    if abhishek_amount:
        seva_bits.append({1100: 'Abhishek Seva', 2500: 'Silver Kalash Abhishek Seva', 5001: 'Golden Kalash Abhishek Seva'}.get(abhishek_amount, 'Abhishek Seva'))
    description = f'ISKCON Janmashtami 2026 Entry Token ({persons} member{"s" if persons > 1 else ""})'
    if seva_bits:
        description += ' + ' + ' + '.join(seva_bits)

    reference_id = secrets.token_hex(12)
    callback_url = request.host_url.rstrip('/') + '/register'

    try:
        link = razorpay_client.payment_link.create({
            'amount': amount_paise,
            'currency': 'INR',
            'accept_partial': False,
            'description': description,
            'customer': {'name': name[:100], 'contact': mobile},
            'notify': {'sms': False, 'email': False},
            'reminder_enable': False,
            'reference_id': reference_id,
            'callback_url': callback_url,
            'callback_method': 'get',
            'notes': {
                'event': 'ISKCON Janmashtami 2026 Online Registration',
                'name': name[:255],
                'mobile': mobile[:15],
                'address': address[:500],
                'persons': persons,
                'aarti_amount': aarti_amount,
                'abhishek_amount': abhishek_amount,
            },
        })
        return jsonify({'short_url': link['short_url'], 'id': link['id'], 'amount': amount_paise // 100, 'persons': persons})
    except Exception as e:
        log.error(f'razorpay payment_link create error: {e}')
        return jsonify({'error': 'Could not initiate payment. Please try again.'}), 500

@app.route('/api/register/public/payment-link-complete', methods=['POST'])
def register_public_payment_link_complete():
    """Public, no-login -- called by /register after Razorpay redirects back from a
    Payment Link. Verifies the redirect's signature server-side (so it can't be forged),
    confirms the link is actually paid, then creates the registration from the details
    stashed in the link's notes at creation time. Idempotent on razorpay_payment_id, since
    a page refresh/back-navigation on the callback page would otherwise replay this.
    Deliberately not gated on PAYMENT_LINK_ENABLED -- a link created while that mode was
    on must still be honored even if the mode is switched before the payer returns."""
    if not razorpay_client:
        return jsonify({'error': 'Online payment is not configured'}), 503

    data = request.get_json() or {}
    payment_id = str(data.get('razorpay_payment_id', '')).strip()
    link_id = str(data.get('razorpay_payment_link_id', '')).strip()
    link_ref = str(data.get('razorpay_payment_link_reference_id', '')).strip()
    link_status = str(data.get('razorpay_payment_link_status', '')).strip()
    signature = str(data.get('razorpay_signature', '')).strip()
    if not (payment_id and link_id and link_status and signature):
        return jsonify({'error': 'Payment verification data missing'}), 400

    try:
        valid = razorpay_client.utility.verify_payment_link_signature({
            'razorpay_payment_id': payment_id,
            'payment_link_id': link_id,
            'payment_link_reference_id': link_ref,
            'payment_link_status': link_status,
            'razorpay_signature': signature,
        })
        if not valid:
            raise razorpay.errors.SignatureVerificationError('signature mismatch')
    except razorpay.errors.SignatureVerificationError:
        log.error(f'Razorpay payment-link signature verification failed for link {link_id}')
        return jsonify({'error': 'Payment verification failed'}), 400

    if link_status != 'paid':
        return jsonify({'error': f'Payment was not completed (status: {link_status}).'}), 400

    existing = db_query("SELECT * FROM registrations WHERE payment_ref=%s", (payment_id,), fetch='one')
    if existing:
        return jsonify({
            'success': True, 'token': existing['token'], 'name': existing['name'],
            'mobile': existing['mobile'], 'address': existing['address'],
            'persons': existing['persons'], 'paid': existing['paid'],
            'token_amount': existing['token_amount'], 'aarti_amount': existing['aarti_amount'],
            'abhishek_amount': existing['abhishek_amount'], 'donation_amount': existing['donation_amount'],
            'free_entry': bool(existing['free_entry']), 'payment_mode': existing['payment_mode'],
            'category': existing['category'],
        }), 200

    try:
        link = razorpay_client.payment_link.fetch(link_id)
    except Exception as e:
        log.error(f'razorpay payment_link fetch error: {e}')
        return jsonify({'error': 'Could not verify payment. Please contact support with your payment ID.'}), 500

    if link.get('status') != 'paid' or link.get('amount_paid') != link.get('amount'):
        log.error(f'Razorpay payment-link status/amount mismatch: {link}')
        return jsonify({'error': 'Payment amount mismatch. Please contact support with your payment ID.'}), 400

    notes = link.get('notes') or {}
    reg_data = {
        'name': notes.get('name', ''),
        'mobile': notes.get('mobile', ''),
        'address': notes.get('address', ''),
        'persons': notes.get('persons', 1),
        'free_entry': False,
        'payment_mode': 'razorpay',
        'payment_ref': payment_id,
        'aarti_amount': int(notes.get('aarti_amount', 0) or 0),
        'abhishek_amount': int(notes.get('abhishek_amount', 0) or 0),
        'donation_amount': 0,
    }
    return _create_registration(reg_data, registered_by=None, category='online')

@app.route('/api/razorpay/volunteer-payment-link', methods=['POST'])
@login_required
def create_volunteer_razorpay_link():
    """Logged-in volunteer flow -- creates a Razorpay Payment Link for an in-person
    registration and returns its short_url so the volunteer's app can render it as a QR
    code for the customer to scan with their own phone (instead of Checkout/redirect).
    No callback_url is set -- nothing needs to redirect anywhere; the volunteer's device
    polls /volunteer-payment-link/<id>/status instead."""
    if not razorpay_client:
        return jsonify({'error': 'Online payment is not available right now'}), 503

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    mobile = str(data.get('mobile', '')).strip()
    address = str(data.get('address', '')).strip()
    try:
        persons = int(data.get('persons', 1))
        aarti_amount = max(0, int(data.get('aarti_amount', 0) or 0))
        abhishek_amount = max(0, int(data.get('abhishek_amount', 0) or 0))
        donation_amount = max(0, int(data.get('donation_amount', 0) or 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid numeric field'}), 400

    if not name or not address or not mobile:
        return jsonify({'error': 'Name, address, and mobile number are required'}), 400
    if not re.match(r'^\d{10}$', mobile):
        return jsonify({'error': 'Mobile number must be 10 digits'}), 400
    if persons < 1 or persons > 50:
        return jsonify({'error': 'Members count must be between 1 and 50'}), 400

    rate = get_settings_row()['token_rate']
    token_amount = persons * rate
    amount_paise = (token_amount + aarti_amount + abhishek_amount + donation_amount) * 100
    if amount_paise <= 0:
        return jsonify({'error': 'Nothing to pay'}), 400

    reference_id = secrets.token_hex(12)
    try:
        link = razorpay_client.payment_link.create({
            'amount': amount_paise,
            'currency': 'INR',
            'accept_partial': False,
            'description': f'ISKCON Janmashtami 2026 Entry Token ({persons} member{"s" if persons > 1 else ""})',
            'customer': {'name': name[:100], 'contact': mobile},
            'notify': {'sms': False, 'email': False},
            'reminder_enable': False,
            'reference_id': reference_id,
            'expire_by': int(time.time()) + VOLUNTEER_QR_PAYMENT_WINDOW_SECONDS,
            'notes': {
                'event': 'ISKCON Janmashtami 2026 Volunteer-Assisted Registration',
                'name': name[:255],
                'mobile': mobile[:15],
                'address': address[:500],
                'persons': persons,
                'aarti_amount': aarti_amount,
                'abhishek_amount': abhishek_amount,
                'donation_amount': donation_amount,
                'registered_by': session['user_id'],
            },
        })
        return jsonify({
            'link_id': link['id'],
            'short_url': link['short_url'],
            'amount': amount_paise // 100,
            'expires_in': VOLUNTEER_QR_PAYMENT_WINDOW_SECONDS,
        })
    except Exception as e:
        log.error(f'razorpay volunteer payment_link create error: {e}')
        return jsonify({'error': 'Could not initiate payment. Please try again.'}), 500

@app.route('/api/razorpay/volunteer-payment-link/<link_id>/status', methods=['GET'])
@login_required
def volunteer_razorpay_link_status(link_id):
    """Polled by the volunteer's app after showing the Razorpay QR. Pulls status straight
    from Razorpay's API using our own secret-keyed client -- never trusts anything the
    browser reports -- so this is authoritative the moment Razorpay shows the link paid."""
    if not razorpay_client:
        return jsonify({'error': 'Online payment is not available right now'}), 503

    try:
        link = razorpay_client.payment_link.fetch(link_id)
    except Exception as e:
        log.error(f'razorpay volunteer payment_link fetch error: {e}')
        return jsonify({'error': 'Could not check payment status. Please try again.'}), 500

    if link.get('status') != 'paid':
        return jsonify({'status': link.get('status', 'created')})

    payments = link.get('payments') or []
    captured = next((p for p in payments if p.get('status') == 'captured'), None) or (payments[-1] if payments else None)
    payment_id = (captured or {}).get('payment_id') or (captured or {}).get('id')
    if not payment_id:
        return jsonify({'error': 'Payment marked paid but no payment record found. Please contact support.'}), 500

    # Idempotent: a previous poll tick (or a slow double-request) may have already
    # created the registration for this exact payment -- don't create a second one.
    existing = db_query("SELECT * FROM registrations WHERE payment_ref=%s", (payment_id,), fetch='one')
    if existing:
        return jsonify({
            'success': True, 'token': existing['token'], 'name': existing['name'],
            'mobile': existing['mobile'], 'address': existing['address'],
            'persons': existing['persons'], 'paid': existing['paid'],
            'token_amount': existing['token_amount'], 'aarti_amount': existing['aarti_amount'],
            'abhishek_amount': existing['abhishek_amount'], 'donation_amount': existing['donation_amount'],
            'free_entry': bool(existing['free_entry']), 'payment_mode': existing['payment_mode'],
            'category': existing['category'],
        }), 200

    if link.get('amount_paid') != link.get('amount'):
        log.error(f'Razorpay volunteer link amount mismatch: {link}')
        return jsonify({'error': 'Payment amount mismatch. Please contact support with your payment ID.'}), 400

    notes = link.get('notes') or {}
    reg_data = {
        'name': notes.get('name', ''),
        'mobile': notes.get('mobile', ''),
        'address': notes.get('address', ''),
        'persons': notes.get('persons', 1),
        'free_entry': False,
        'payment_mode': 'razorpay',
        'payment_ref': payment_id,
        'aarti_amount': int(notes.get('aarti_amount', 0) or 0),
        'abhishek_amount': int(notes.get('abhishek_amount', 0) or 0),
        'donation_amount': int(notes.get('donation_amount', 0) or 0),
    }
    registered_by = notes.get('registered_by') or session['user_id']
    return _create_registration(reg_data, registered_by=registered_by, category='volunteer')

@app.route('/api/register/public', methods=['POST'])
def register_public():
    """Public, no-login self-registration -- reachable via the /register QR code.
    When RAZORPAY_ENABLED, payment happens up front via Razorpay Checkout and the
    token is only created once the signature is verified and the captured amount
    matches what's owed. Otherwise falls back to pay-later (payment_mode='pending').
    Either way registered_by stays NULL and category is always 'online'."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No payload submitted'}), 400

    if not RAZORPAY_ENABLED:
        data = dict(data)
        aarti_amount, abhishek_amount = parse_seva_selection(data)
        data['free_entry'] = False
        data['payment_mode'] = 'pending'
        data['aarti_amount'] = aarti_amount
        data['abhishek_amount'] = abhishek_amount
        data['donation_amount'] = 0
        return _create_registration(data, registered_by=None, category='online')

    order_id   = str(data.get('razorpay_order_id', '')).strip()
    payment_id = str(data.get('razorpay_payment_id', '')).strip()
    signature  = str(data.get('razorpay_signature', '')).strip()
    if not order_id or not payment_id or not signature:
        return jsonify({'error': 'Payment verification data missing'}), 400

    try:
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })
    except razorpay.errors.SignatureVerificationError:
        log.error(f'Razorpay signature verification failed for order {order_id}')
        return jsonify({'error': 'Payment verification failed'}), 400

    try:
        order = razorpay_client.order.fetch(order_id)
    except Exception as e:
        log.error(f'razorpay order fetch error: {e}')
        return jsonify({'error': 'Could not verify payment. Please contact support with your payment ID.'}), 500

    try:
        persons = int(data.get('persons', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid members count'}), 400
    order_notes = order.get('notes') or {}
    aarti_amount = int(order_notes.get('aarti_amount', 0) or 0)
    abhishek_amount = int(order_notes.get('abhishek_amount', 0) or 0)
    rate = get_settings_row()['token_rate']
    expected_amount = (max(1, min(50, persons)) * rate + aarti_amount + abhishek_amount) * 100

    # The amount actually captured by Razorpay is the source of truth -- never trust
    # persons/amount fields from the request body for what gets charged.
    if order.get('status') != 'paid' or order.get('amount_paid') != expected_amount:
        log.error(f'Razorpay amount/status mismatch: order={order} expected={expected_amount}')
        return jsonify({'error': 'Payment amount mismatch. Please contact support with your payment ID.'}), 400

    data = dict(data)
    data['free_entry'] = False
    data['payment_mode'] = 'razorpay'
    data['payment_ref'] = payment_id
    data['aarti_amount'] = aarti_amount
    data['abhishek_amount'] = abhishek_amount
    data['donation_amount'] = 0
    return _create_registration(data, registered_by=None, category='online')

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
        # Janmashtami runs afternoon through midnight (Krishna's birth is marked at 00:00,
        # with the function continuing to ~00:30). A plain DATE(scan_time) = CURDATE() filter
        # would drop everything scanned after midnight, since those rows carry tomorrow's
        # calendar date. Shifting by 6 hours before comparing keeps the whole afternoon->past-
        # midnight window on one logical "event day".
        rows = db_query("""
            SELECT HOUR(scan_time) AS hr, COUNT(DISTINCT token) AS families, SUM(persons) AS persons
            FROM attendance_log
            WHERE DATE(DATE_SUB(scan_time, INTERVAL 6 HOUR)) = DATE(DATE_SUB(NOW(), INTERVAL 6 HOUR))
            GROUP BY HOUR(scan_time)
            ORDER BY hr
        """)
        counts = {int(r['hr']): {'families': int(r['families']), 'persons': int(r['persons'])} for r in rows}

        event_hours = list(range(12, 24)) + [0]  # 12 PM ... 11 PM, then 12 AM (midnight)
        hourly = []
        for h in event_hours:
            if h == 12:
                label = '12 PM'
            elif h == 0:
                label = '12 AM'
            elif h > 12:
                label = f'{h - 12} PM'
            else:
                label = f'{h} AM'
            slot = counts.get(h, {'families': 0, 'persons': 0})
            hourly.append({'hour': h, 'label': label, 'families': slot['families'], 'persons': slot['persons']})

        return jsonify({'hourly': hourly})
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

@app.route('/api/gate/lookup', methods=['GET'])
@login_required
def gate_lookup():
    """Read-only lookup used by the scan UI to show a confirm-before-entry screen,
    so gate staff can edit how many of the family are actually here right now --
    on the very first scan, not just on repeat scans for latecomers."""
    token_input = str(request.args.get('token', '')).strip()
    if not token_input:
        return jsonify({'error': 'Token is required'}), 400
    try:
        reg = db_query("SELECT * FROM registrations WHERE token = %s OR mobile = %s", (token_input, token_input), fetch='one')
        if not reg:
            return jsonify({'status': 'not_found', 'message': f'No registration found for token or mobile number "{token_input}".'}), 404

        existing = db_query("SELECT * FROM attendance WHERE token = %s", (reg['token'],), fetch='one')
        registered_total = reg['persons']
        already_in = existing['persons'] if existing else 0
        remaining = max(0, registered_total - already_in)
        gate_time = existing['gate_time'] if existing else None
        if gate_time and hasattr(gate_time, 'strftime'):
            gate_time = gate_time.strftime('%H:%M')

        return jsonify({
            'status': 'found',
            'token': reg['token'],
            'name': reg['name'],
            'mobile': reg['mobile'],
            'registered': registered_total,
            'already_in': already_in,
            'remaining': remaining,
            'gate_time': gate_time,
            'payment_mode': reg['payment_mode'],
            'amount_due': reg['token_amount'] + reg['aarti_amount'] + reg['abhishek_amount'] + reg['donation_amount'],
            'paid': reg['paid'],
        })
    except Exception as e:
        log.error(f'gate_lookup error: {e}')
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
        # Never let a client-supplied count push the checked-in total past what the
        # family actually registered for.
        actual_persons = min(persons, reg['persons']) if persons > 0 else reg['persons']
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
                persons = min(persons, remaining)
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

        now_str = ist_now().strftime('%H:%M:%S')
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
                r.token_amount,
                r.aarti_amount,
                r.abhishek_amount,
                r.donation_amount,
                r.payment_mode,
                r.payment_ref,
                r.free_entry,
                r.category,
                r.reg_at,
                COALESCE(u.name, 'Unknown')   AS registered_by_user,
                COALESCE(c.name, '')          AS collected_by_user,
                IF(a.token IS NOT NULL,'Yes','No') AS attended,
                COALESCE(a.persons, 0)             AS members_counted,
                COALESCE(DATE_FORMAT(a.gate_time,'%%d/%%m/%%Y %%H:%%i'), '') AS gate_time
            FROM registrations r
            LEFT JOIN users u ON u.id = r.registered_by
            LEFT JOIN users c ON c.id = r.collected_by
            LEFT JOIN attendance a ON r.token = a.token
            ORDER BY r.id ASC
        """)

        lines = ['Token,Family Head,Address,Mobile,Registered Members,Token Amount (Rs),Aarti (Rs),Abhishek (Rs),Donation (Rs) [80G],Total Paid (Rs),Payment Mode,Payment Reference,Free Entry,Category,Registered At,Registered By Volunteer,Payment Collected By,Gate Attended,Gate Members Counted,Gate Entry Time']
        for r in rows:
            addr   = str(r['address']).replace(',', ';').replace('\n', ' ')
            reg_at = r['reg_at'].strftime('%d/%m/%Y %H:%M') if hasattr(r['reg_at'], 'strftime') else r['reg_at']
            lines.append(
                f"{r['token']},{r['name']},{addr},{r['mobile']},"
                f"{r['registered_members']},{r['token_amount']},{r['aarti_amount']},"
                f"{r['abhishek_amount']},{r['donation_amount']},{r['paid']},"
                f"{r['payment_mode']},"
                f"{r['payment_ref'] or ''},"
                f"{'Yes' if r['free_entry'] else 'No'},"
                f"{r['category']},"
                f"{reg_at},{r['registered_by_user']},"
                f"{r['collected_by_user']},"
                f"{r['attended']},{r['members_counted']},{r['gate_time']}"
            )

        return Response(
            '\n'.join(lines),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=iskcon_janmastmi_report_2026.csv'}
        )
    except Exception as e:
        log.error(f'export_csv error: {e}')
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
