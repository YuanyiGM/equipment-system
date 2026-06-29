#!/usr/bin/env python3
"""设备借还登记系统 - Flask 主应用"""

import os
import re
import json
import sys
import random
import socket
import secrets
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_from_directory, jsonify
)
import sqlite3
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer

# PyInstaller 兼容: 获取正确的资源路径
def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

app = Flask(__name__,
            template_folder=resource_path('templates'),
            static_folder=resource_path('static'))
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# 数据目录（云端使用环境变量，本地使用EXE目录或项目目录）
DATA_DIR = os.environ.get('DATA_DIR', '')
if DATA_DIR:
    BASE_DIR = DATA_DIR
elif getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'database.db')
QRCODES_DIR = os.path.join(BASE_DIR, 'qrcodes')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

if not os.path.exists(QRCODES_DIR):
    os.makedirs(QRCODES_DIR)
if not os.path.exists(os.path.dirname(DB_PATH)):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# ============================================================
# 配置 & IP检测
# ============================================================
def get_local_ip():
    """获取本机局域网IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def load_config():
    """加载配置文件"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    """保存配置文件"""
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_base_url():
    """获取用于二维码的基础URL（云端优先使用环境变量）"""
    # 云端部署：使用环境变量
    env_url = os.environ.get('BASE_URL', '').strip()
    if env_url:
        return env_url.rstrip('/')
    # 云端部署：从请求上下文自动获取
    if os.environ.get('RENDER'):
        try:
            from flask import request as req
            if req:
                return req.host_url.rstrip('/')
        except Exception:
            pass
    # 本地部署：读配置文件
    config = load_config()
    base_url = config.get('base_url', '').strip()
    if base_url:
        return base_url.rstrip('/')
    # 自动检测局域网IP
    local_ip = get_local_ip()
    port = config.get('port', 5000)
    return f"http://{local_ip}:{port}"


# ============================================================
# 数据库
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            department TEXT DEFAULT '',
            purchase_date TEXT DEFAULT '',
            status TEXT DEFAULT '在库',
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS borrow_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            borrower_name TEXT NOT NULL,
            borrower_dept TEXT NOT NULL,
            borrower_phone TEXT NOT NULL,
            return_code TEXT NOT NULL,
            borrow_time TEXT DEFAULT (datetime('now','localtime')),
            return_time TEXT,
            status TEXT DEFAULT '借出',
            FOREIGN KEY (device_id) REFERENCES devices(id)
        );
    """)
    conn.commit()
    conn.close()


def query_db(query, args=(), one=False):
    conn = get_db()
    cur = conn.execute(query, args)
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv


def execute_db(query, args=()):
    conn = get_db()
    cur = conn.execute(query, args)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id


# 模块加载时自动初始化数据库（云端gunicorn需要）
init_db()


# ============================================================
# 二维码
# ============================================================
def generate_qr_code(device_code):
    """生成设备二维码，返回文件名。二维码内容为完整URL"""
    filename = f"{device_code}.png"
    filepath = os.path.join(QRCODES_DIR, filename)

    base_url = get_base_url()
    qr_content = f"{base_url}/d/{device_code}"

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=2,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
        fill_color='#1a1a2e',
        back_color='#ffffff'
    )
    img.save(filepath, 'PNG')
    return filename


# ============================================================
# 工具
# ============================================================
def generate_return_code():
    return f"{random.randint(0, 999999):06d}"


def generate_device_code():
    """生成唯一设备编号: DEV + 年月日 + 4位随机数"""
    today = datetime.now().strftime('%Y%m%d')
    rand = f"{random.randint(0, 9999):04d}"
    return f"DEV{today}{rand}"


# ============================================================
# 管理员认证
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'success': False, 'error': '请先登录'}), 401
        return f(*args, **kwargs)
    return decorated_function


# ============================================================
# 管理员路由
# ============================================================
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session['admin_username'] = username
            return redirect(url_for('admin_dashboard'))
        flash('用户名或密码错误', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin')
@login_required
def admin_dashboard():
    devices = query_db("SELECT * FROM devices ORDER BY created_at DESC")
    total = len(devices)
    available = sum(1 for d in devices if d['status'] == '在库')
    borrowed = sum(1 for d in devices if d['status'] == '借出')
    records = query_db("SELECT * FROM borrow_records ORDER BY borrow_time DESC LIMIT 10")
    return render_template('admin_dashboard.html',
                           total=total, available=available, borrowed=borrowed,
                           devices=devices, records=records)


@app.route('/admin/devices')
@login_required
def admin_devices():
    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()

    sql = "SELECT * FROM devices WHERE 1=1"
    args = []
    if search:
        sql += " AND (name LIKE ? OR code LIKE ? OR department LIKE ?)"
        like = f"%{search}%"
        args.extend([like, like, like])
    if status_filter:
        sql += " AND status = ?"
        args.append(status_filter)

    sql += " ORDER BY created_at DESC"
    devices = query_db(sql, args)
    return render_template('admin_devices.html', devices=devices,
                           search=search, status_filter=status_filter)


@app.route('/admin/devices/add', methods=['GET', 'POST'])
@login_required
def admin_device_add():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        department = request.form.get('department', '').strip()
        purchase_date = request.form.get('purchase_date', '').strip()
        description = request.form.get('description', '').strip()
        code = request.form.get('code', '').strip()

        if not name:
            flash('设备名称不能为空', 'danger')
            return render_template('admin_device_form.html', device=None)

        if not code:
            code = generate_device_code()

        existing = query_db("SELECT id FROM devices WHERE code = ?", [code], one=True)
        if existing:
            flash(f'设备编号 {code} 已存在，请更换', 'danger')
            return render_template('admin_device_form.html', device=None)

        # 生成二维码
        try:
            qr_filename = generate_qr_code(code)
        except Exception as e:
            flash(f'二维码生成失败: {e}', 'danger')
            return render_template('admin_device_form.html', device=None)

        execute_db(
            "INSERT INTO devices (name, code, department, purchase_date, description) VALUES (?,?,?,?,?)",
            [name, code, department, purchase_date, description]
        )
        flash(f'设备 "{name}" 添加成功，二维码已生成', 'success')
        return redirect(url_for('admin_devices'))

    return render_template('admin_device_form.html', device=None)


@app.route('/admin/devices/edit/<int:device_id>', methods=['GET', 'POST'])
@login_required
def admin_device_edit(device_id):
    device = query_db("SELECT * FROM devices WHERE id = ?", [device_id], one=True)
    if not device:
        flash('设备不存在', 'danger')
        return redirect(url_for('admin_devices'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        department = request.form.get('department', '').strip()
        purchase_date = request.form.get('purchase_date', '').strip()
        description = request.form.get('description', '').strip()
        code = request.form.get('code', '').strip()

        if not name:
            flash('设备名称不能为空', 'danger')
            return render_template('admin_device_form.html', device=device)

        if not code:
            code = device['code']

        if code != device['code']:
            existing = query_db("SELECT id FROM devices WHERE code = ? AND id != ?",
                              [code, device_id], one=True)
            if existing:
                flash(f'设备编号 {code} 已存在', 'danger')
                return render_template('admin_device_form.html', device=device)

            # 删除旧二维码，生成新的
            old_qr = os.path.join(QRCODES_DIR, f"{device['code']}.png")
            if os.path.exists(old_qr):
                os.remove(old_qr)
            try:
                generate_qr_code(code)
            except Exception as e:
                flash(f'二维码更新失败: {e}', 'danger')
                return render_template('admin_device_form.html', device=device)

        execute_db(
            "UPDATE devices SET name=?, code=?, department=?, purchase_date=?, description=? WHERE id=?",
            [name, code, department, purchase_date, description, device_id]
        )
        flash(f'设备 "{name}" 更新成功', 'success')
        return redirect(url_for('admin_devices'))

    return render_template('admin_device_form.html', device=device)


@app.route('/admin/devices/delete/<int:device_id>', methods=['POST'])
@login_required
def admin_device_delete(device_id):
    device = query_db("SELECT * FROM devices WHERE id = ?", [device_id], one=True)
    if not device:
        flash('设备不存在', 'danger')
        return redirect(url_for('admin_devices'))

    if device['status'] == '借出':
        flash('设备正在借出中，无法删除', 'danger')
        return redirect(url_for('admin_devices'))

    execute_db("DELETE FROM borrow_records WHERE device_id = ?", [device_id])
    execute_db("DELETE FROM devices WHERE id = ?", [device_id])

    qr_path = os.path.join(QRCODES_DIR, f"{device['code']}.png")
    if os.path.exists(qr_path):
        os.remove(qr_path)

    flash('设备已删除', 'success')
    return redirect(url_for('admin_devices'))


@app.route('/admin/records')
@login_required
def admin_records():
    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()

    sql = """
        SELECT br.*, d.name as device_name, d.code as device_code
        FROM borrow_records br
        LEFT JOIN devices d ON br.device_id = d.id
        WHERE 1=1
    """
    args = []
    if search:
        sql += " AND (br.borrower_name LIKE ? OR br.borrower_dept LIKE ? OR d.name LIKE ? OR d.code LIKE ? OR br.return_code LIKE ?)"
        like = f"%{search}%"
        args.extend([like, like, like, like, like])
    if status_filter:
        sql += " AND br.status = ?"
        args.append(status_filter)

    sql += " ORDER BY br.borrow_time DESC"
    records = query_db(sql, args)
    return render_template('admin_records.html', records=records,
                           search=search, status_filter=status_filter)


@app.route('/admin/devices/qrcode/<device_code>')
@login_required
def admin_download_qr(device_code):
    """下载二维码图片"""
    filename = f"{device_code}.png"
    return send_from_directory(QRCODES_DIR, filename, as_attachment=True,
                               download_name=f"设备_{device_code}_二维码.png")


# ============================================================
# 移动端路由（扫码借还）
# ============================================================
@app.route('/d/<device_code>')
def mobile_device(device_code):
    """移动端扫码页面"""
    device = query_db("SELECT * FROM devices WHERE code = ?", [device_code], one=True)
    if not device:
        return render_template('mobile_error.html', message='设备不存在，请确认二维码是否正确')

    records = query_db(
        "SELECT * FROM borrow_records WHERE device_id = ? ORDER BY borrow_time DESC LIMIT 5",
        [device['id']]
    )
    return render_template('mobile_device.html', device=device, records=records)


@app.route('/api/borrow', methods=['POST'])
def api_borrow():
    """借出设备"""
    device_code = request.form.get('device_code', '').strip()
    borrower_name = request.form.get('borrower_name', '').strip()
    borrower_dept = request.form.get('borrower_dept', '').strip()
    borrower_phone = request.form.get('borrower_phone', '').strip()

    if not all([device_code, borrower_name, borrower_dept, borrower_phone]):
        return jsonify({'success': False, 'error': '请填写完整信息'})

    if not re.match(r'^1[3-9]\d{9}$', borrower_phone):
        return jsonify({'success': False, 'error': '手机号格式不正确'})

    device = query_db("SELECT * FROM devices WHERE code = ?", [device_code], one=True)
    if not device:
        return jsonify({'success': False, 'error': '设备不存在'})

    if device['status'] != '在库':
        recent = query_db(
            "SELECT * FROM borrow_records WHERE device_id = ? AND status = '借出' ORDER BY borrow_time DESC LIMIT 1",
            [device['id']], one=True
        )
        info = f"，当前借用人: {recent['borrower_name']}" if recent else ""
        return jsonify({'success': False, 'error': f'设备当前状态为"{device["status"]}"{info}'})

    return_code = generate_return_code()

    execute_db(
        "INSERT INTO borrow_records (device_id, borrower_name, borrower_dept, borrower_phone, return_code) VALUES (?,?,?,?,?)",
        [device['id'], borrower_name, borrower_dept, borrower_phone, return_code]
    )
    execute_db("UPDATE devices SET status = '借出' WHERE id = ?", [device['id']])

    return jsonify({
        'success': True,
        'return_code': return_code,
        'message': f'借出成功！请牢记归还码',
        'borrower_name': borrower_name
    })


@app.route('/api/return', methods=['POST'])
def api_return():
    """归还设备"""
    device_code = request.form.get('device_code', '').strip()
    return_code = request.form.get('return_code', '').strip()

    if not device_code:
        return jsonify({'success': False, 'error': '设备码不能为空'})
    if not return_code:
        return jsonify({'success': False, 'error': '请输入归还码'})
    if len(return_code) != 6 or not return_code.isdigit():
        return jsonify({'success': False, 'error': '归还码为6位数字'})

    device = query_db("SELECT * FROM devices WHERE code = ?", [device_code], one=True)
    if not device:
        return jsonify({'success': False, 'error': '设备不存在'})

    if device['status'] != '借出':
        return jsonify({'success': False, 'error': f'设备当前状态为"{device["status"]}"，无需归还'})

    record = query_db(
        "SELECT * FROM borrow_records WHERE device_id = ? AND return_code = ? AND status = '借出' ORDER BY borrow_time DESC LIMIT 1",
        [device['id'], return_code], one=True
    )

    if not record:
        return jsonify({'success': False, 'error': '归还码错误，请核实后重试'})

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    execute_db(
        "UPDATE borrow_records SET status = '已还', return_time = ? WHERE id = ?",
        [now, record['id']]
    )
    execute_db("UPDATE devices SET status = '在库' WHERE id = ?", [device['id']])

    return jsonify({
        'success': True,
        'message': f'归还成功！',
        'borrower_name': record['borrower_name']
    })


# ============================================================
# 静态文件 / 首页
# ============================================================
@app.route('/')
def index():
    return redirect(url_for('admin_login'))


@app.route('/qrcodes/<filename>')
def serve_qrcode(filename):
    return send_from_directory(QRCODES_DIR, filename)


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    init_db()

    # 如果是云端部署（有PORT环境变量），不进行本地检测
    is_cloud = bool(os.environ.get('RENDER') or os.environ.get('PORT'))

    if is_cloud:
        port = int(os.environ.get('PORT', 5000))
        base_url = os.environ.get('BASE_URL', f'http://0.0.0.0:{port}')
        print("=" * 55)
        print("     设备借还登记系统 v1.0 (云端版)")
        print("=" * 55)
        print(f"  服务端口:   {port}")
        print(f"  二维码域名: {base_url}")
        print("=" * 55)
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        local_ip = get_local_ip()
        port = 5000

        config = load_config()
        if not config.get('base_url'):
            config['base_url'] = f"http://{local_ip}:{port}"
            config['port'] = port
            save_config(config)

        base_url = config.get('base_url', f"http://{local_ip}:{port}")

        print("=" * 55)
        print("     设备借还登记系统 v1.0")
        print("=" * 55)
        print(f"  管理员后台: http://{local_ip}:{port}/admin")
        print(f"  本机访问:   http://127.0.0.1:{port}/admin")
        print(f"  默认账号:   admin / admin123")
        print(f"  二维码域名: {base_url}")
        print("=" * 55)
        print("  请确保手机与电脑在同一WiFi网络下扫码使用")
        print("  如IP地址变化，修改 config.json 中的 base_url 后重启")
        print("=" * 55)

        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/admin")

        app.run(host='0.0.0.0', port=port, debug=False)
