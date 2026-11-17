"""Steam Watch download website with server-protected editor.

Run locally using run_windows.bat or: pip install -r requirements.txt && python app.py
Public hosting: set SECRET_KEY, ADMIN_PASSWORD and persistent DATA_DIR first.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, session

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(ROOT / 'data'))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILE_PATH = DATA_DIR / 'site.json'
RELEASES = DATA_DIR / 'releases'
RELEASES.mkdir(parents=True, exist_ok=True)
DEFAULT_RELEASE = ROOT / 'downloads' / 'SteamWatch-v1.0.zip'
CURRENT_RELEASE = RELEASES / 'SteamWatch-current.zip'
if not CURRENT_RELEASE.exists() and DEFAULT_RELEASE.is_file():
    shutil.copyfile(DEFAULT_RELEASE, CURRENT_RELEASE)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=55 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SECURE_COOKIES') == '1',
    PERMANENT_SESSION_LIFETIME=3600,
)
PASSWORD = os.environ.get('ADMIN_PASSWORD', 'dev_mode')
MAX_TRIES = 6
WINDOW_SEC = 15 * 60
ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)

DEFAULT = {
    'site_name': 'STEAM WATCH',
    'badge': 'ПРОГРАММА ДЛЯ WINDOWS 10 / 11',
    'headline': 'Твои игры. Твои скидки.',
    'description': 'Отслеживай цены на игры Steam в гривнах и получай уведомления Windows, когда появляется выгодное предложение.',
    'release_version': '1.0',
    'release_note': 'Первая публичная версия · Python 3.11+',
    'tester_name': 'Нашему тестеру',
    'tester_note': 'Спасибо за помощь с проверкой проекта, внимание к деталям и обратную связь. Именно так Steam Watch становится лучше.',
    'author': 'MADE BY S1REX (and gpt)',
    'changelog': [
        {
            'version': 'v1.0',
            'date': '17.11.2026',
            'title': 'Первый релиз',
            'items': [
                'Поиск игр Steam по названию, без готового списка',
                'Текущая цена и скидка в гривнах для региона Украина',
                'Отслеживание нескольких игр и уведомления Windows',
                'Исторические скидки через API IsThereAnyDeal (по ключу)',
            ],
        }
    ],
}


def clean_text(value, length=500):
    if not isinstance(value, str):
        raise ValueError('Ожидался текст')
    value = value.strip()
    if not value or len(value) > length:
        raise ValueError(f'Текст должен содержать 1–{length} символов')
    return value


def validate(raw):
    if not isinstance(raw, dict):
        raise ValueError('Неверный формат')
    limits = {
        'site_name': 50, 'badge': 85, 'headline': 100,
        'description': 600, 'release_version': 24, 'release_note': 140,
        'tester_name': 100, 'tester_note': 500, 'author': 140,
    }
    safe = {field: clean_text(raw.get(field), cap) for field, cap in limits.items()}
    changelog = raw.get('changelog')
    if not isinstance(changelog, list) or not 1 <= len(changelog) <= 20:
        raise ValueError('Ченджлог должен содержать 1–20 записей')
    safe['changelog'] = []
    for entry in changelog:
        if not isinstance(entry, dict):
            raise ValueError('Ошибка записи ченджлога')
        items = entry.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 15:
            raise ValueError('Для каждой версии нужно 1–15 изменений')
        safe['changelog'].append({
            'version': clean_text(entry.get('version'), 24),
            'date': clean_text(entry.get('date'), 40),
            'title': clean_text(entry.get('title'), 100),
            'items': [clean_text(item, 250) for item in items],
        })
    return safe


def load_site():
    if FILE_PATH.exists():
        try:
            return validate(json.loads(FILE_PATH.read_text(encoding='utf-8')))
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            app.logger.warning('Ошибка чтения %s, загружены стандартные настройки', FILE_PATH)
    return validate(DEFAULT)


def save_site(data):
    # Atomic replace avoids truncated JSON when saving concurrently.
    tmp = FILE_PATH.with_name(FILE_PATH.name + '.' + secrets.token_hex(5) + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(FILE_PATH)
    finally:
        if tmp.exists():
            tmp.unlink()


def is_admin():
    return session.get('admin') is True


def require_admin():
    if not is_admin():
        abort(401)
    token = request.headers.get('X-CSRF-Token', '')
    if not token or not hmac.compare_digest(token, session.get('csrf', '')):
        abort(403)


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/admin') else 'no-cache'
    if request.path == '/':
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
    return response


@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/site')
def public_site():
    site = load_site()
    site['has_release'] = CURRENT_RELEASE.is_file()
    if site['has_release']:
        site['release_bytes'] = CURRENT_RELEASE.stat().st_size
    return jsonify(site)


@app.get('/download')
def download():
    if not CURRENT_RELEASE.is_file():
        abort(404, description='Архив программы ещё не загружен')
    version = re.sub(r'[^0-9A-Za-z._-]+', '-', load_site()['release_version'])
    return send_file(CURRENT_RELEASE, as_attachment=True, download_name=f'SteamWatch-v{version}.zip', mimetype='application/zip')


@app.get('/api/admin/session')
def admin_session():
    return jsonify({'authenticated': is_admin(), 'csrf': session.get('csrf') if is_admin() else None})


@app.post('/api/admin/login')
def login():
    # Intentionally use the network address, not user-supplied forwarding headers.
    ip = request.remote_addr or 'unknown'
    now = time.monotonic()
    recent = ATTEMPTS[ip]
    while recent and recent[0] < now - WINDOW_SEC:
        recent.popleft()
    if len(recent) >= MAX_TRIES:
        return jsonify({'error': 'Слишком много попыток. Повторите позже.'}), 429
    payload = request.get_json(silent=True)
    submitted = payload.get('password', '') if isinstance(payload, dict) else ''
    if not isinstance(submitted, str) or not hmac.compare_digest(submitted.encode('utf-8'), PASSWORD.encode('utf-8')):
        recent.append(now)
        return jsonify({'error': 'Неверный пароль'}), 401
    recent.clear()
    session.clear()
    session.permanent = True
    session['admin'] = True
    session['csrf'] = secrets.token_urlsafe(32)
    return jsonify({'authenticated': True, 'csrf': session['csrf']})


@app.post('/api/admin/logout')
def logout():
    require_admin()
    session.clear()
    return jsonify({'ok': True})


@app.post('/api/admin/site')
def edit_site():
    require_admin()
    try:
        config = validate(request.get_json(silent=True))
        save_site(config)
    except (ValueError, OSError) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'ok': True})


@app.post('/api/admin/release')
def upload_release():
    require_admin()
    upload = request.files.get('file')
    if not upload or not upload.filename or not upload.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Выберите архив ZIP'}), 400
    tmp = RELEASES / ('upload-' + secrets.token_hex(10) + '.tmp')
    try:
        upload.save(tmp)
        if tmp.stat().st_size < 30 or not zipfile.is_zipfile(tmp):
            return jsonify({'error': 'Файл не является корректным ZIP-архивом'}), 400
        with zipfile.ZipFile(tmp) as z:
            files = z.infolist()
            if len(files) > 10000 or sum(x.file_size for x in files) > 500 * 1024 * 1024:
                return jsonify({'error': 'Архив слишком большой'}), 400
            if z.testzip() is not None:
                return jsonify({'error': 'ZIP-архив повреждён'}), 400
        tmp.replace(CURRENT_RELEASE)
        return jsonify({'ok': True, 'size': CURRENT_RELEASE.stat().st_size})
    except (OSError, zipfile.BadZipFile):
        return jsonify({'error': 'Не удалось сохранить архив'}), 400
    finally:
        if tmp.exists():
            tmp.unlink()


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'Архив слишком большой (лимит 55 МБ)'}), 413


if __name__ == '__main__':
    print('Steam Watch: http://127.0.0.1:5000')
    print('Пароль администратора задаётся переменной ADMIN_PASSWORD (локально по умолчанию: dev_mode).')
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '5000')), debug=False)
