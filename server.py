try:
    import eventlet
    eventlet.monkey_patch()
except ImportError:
    pass

from flask import Flask, send_file, request, jsonify, session
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import subprocess
import os
import pty
import select
import signal
import sys
import shutil
import codecs
import time
import errno
import pymongo
import bcrypt
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'galaxy_secret_key_888')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

MONGO_URI = os.environ.get("MONGO_URI", "")
db = None
users_collection = None

if MONGO_URI:
    try:
        client = pymongo.MongoClient(MONGO_URI)
        client.admin.command('ping')
        db = client.get_database("galaxy_compiler_db")
        users_collection = db.users
        print("[系統] MongoDB 連線成功！")
    except Exception as e:
        print(f"[系統]  MongoDB 連線失敗: {e}")
else:
    print("[系統]  警告: 未設定 MONGO_URI")

current_process = None
master_fd_global = None

def log(msg):
    print(f"[系統] {msg}")
    sys.stdout.flush()

def kill_existing_process():
    global current_process, master_fd_global
    if current_process:
        try:
            if current_process.poll() is None:
                os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
        except:
            pass
        current_process = None
   
    if master_fd_global:
        try: os.close(master_fd_global)
        except: pass
        master_fd_global = None

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/register', methods=['POST'])
def register():
    if not users_collection: return jsonify({'success': False, 'message': '資料庫未連線'}), 500
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password: return jsonify({'success': False, 'message': '請輸入帳號和密碼'}), 400
    if users_collection.find_one({'username': username}): return jsonify({'success': False, 'message': '此帳號已被註冊'}), 400
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    new_user = {'username': username, 'password': hashed_password, 'created_at': datetime.utcnow(), 'projects_cpp': [], 'projects_python': []}
    users_collection.insert_one(new_user)
    return jsonify({'success': True, 'message': '註冊成功！'})

@app.route('/login', methods=['POST'])
def login():
    if not users_collection: return jsonify({'success': False, 'message': '資料庫未連線'}), 500
    data = request.json
    username = data.get('username')
    password = data.get('password')
    user = users_collection.find_one({'username': username})
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
        session.permanent = True
        session['user'] = username
        return jsonify({'success': True, 'username': username})
    else:
        return jsonify({'success': False, 'message': '帳號或密碼錯誤'}), 401

@app.route('/get_user_data', methods=['GET'])
def get_user_data():
    if 'user' not in session: return jsonify({'success': False, 'is_logged_in': False})
    username = session['user']
    if not users_collection: return jsonify({'success': False, 'message': 'DB Error'}), 500
    user = users_collection.find_one({'username': username}, {'_id': 0, 'password': 0})
    if user: return jsonify({'success': True, 'is_logged_in': True, 'username': username, 'data': user})
    return jsonify({'success': False, 'is_logged_in': False})

@app.route('/save_projects', methods=['POST'])
def save_projects():
    if 'user' not in session: return jsonify({'success': False, 'message': '未登入'}), 401
    if not users_collection: return jsonify({'success': False, 'message': 'DB Error'}), 500
    data = request.json
    lang = data.get('lang')
    projects = data.get('projects')
    field_name = 'projects_cpp' if lang == 'cpp' else 'projects_python'
    users_collection.update_one({'username': session['user']}, {'$set': {field_name: projects}})
    return jsonify({'success': True})

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({'success': True})

@socketio.on('run_code_v2')
def handle_run_code(data):
    global current_process, master_fd_global
    kill_existing_process()
    code = data.get('code')
    lang = data.get('lang', 'cpp')
    home_dir = os.environ.get('HOME', '/app')
    if not os.path.exists(home_dir): home_dir = os.getcwd()
    my_env = os.environ.copy()
    my_env["PYTHONIOENCODING"] = "utf-8"
    my_env["LANG"] = "C.UTF-8"
    stdbuf_exe = shutil.which("stdbuf")
    use_stdbuf = stdbuf_exe is not None

    if lang == 'python':
        source_file = os.path.join(home_dir, "galaxy_runner.py")
        run_cmd = ['python', '-u', source_file]
    else:
        source_file = os.path.join(home_dir, "galaxy_runner.cpp")
        exe_file = os.path.join(home_dir, "galaxy_runner")
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            emit('program_output', {'data': " Error: C++ Compiler not found.\r\n"})
            emit('program_status', {'status': 'error'})
            return
        run_cmd = [exe_file]
        if use_stdbuf: run_cmd = [stdbuf_exe, '-o0', '-e0'] + run_cmd

    try:
        with open(source_file, "w", encoding='utf-8') as f: f.write(code)
    except Exception as e:
        emit('program_output', {'data': f" Write Error: {e}\r\n"}); return

    if lang == 'cpp':
        compile_res = subprocess.run([compiler, source_file, '-o', exe_file], capture_output=True, text=True, env=my_env)
        if compile_res.returncode != 0:
            err_msg = compile_res.stderr.replace('\r\n', '\n').replace('\n', '\r\n')
            emit('program_output', {'data': f" Compilation Error:\r\n{err_msg}"})
            emit('program_status', {'status': 'error'})
            return

    try:
        master_fd_global, slave_fd = pty.openpty()
        current_process = subprocess.Popen(run_cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, preexec_fn=os.setsid, close_fds=True, env=my_env)
        os.close(slave_fd)
        emit('program_output', {'data': ""})
        socketio.start_background_task(target=read_output, fd=master_fd_global, proc=current_process)
    except Exception as e:
        emit('program_output', {'data': f" Execution Error: {str(e)}\r\n"})
        emit('program_status', {'status': 'error'})
        kill_existing_process()

def read_output(fd, proc):
    decoder = codecs.getincrementaldecoder("utf-8")(errors='replace')
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                    if data:
                        text = decoder.decode(data, final=False)
                        socketio.emit('program_output', {'data': text.replace('\n', '\r\n')})
                    else: break
                except OSError: break
            if proc.poll() is not None:
                time.sleep(0.2)
                while True:
                    try:
                        r, _, _ = select.select([fd], [], [], 0.1)
                        if fd not in r: break
                        data = os.read(fd, 4096)
                        if not data: break
                        socketio.emit('program_output', {'data': decoder.decode(data, final=True).replace('\n', '\r\n')})
                    except OSError: break
                break
            socketio.sleep(0.01)
    except: pass
    finally:
        socketio.emit('program_status', {'status': 'finished'})
        if fd:
            try:
                os.close(fd)
            except:
                pass

@socketio.on('send_input')
def handle_input(data):
    global master_fd_global
    if master_fd_global:
        try: os.write(master_fd_global, (data.get('input')+'\n').encode('utf-8'))
        except: pass

@socketio.on('stop_code')
def handle_stop():
    kill_existing_process()
    emit('program_output', {'data': "\r\n[Stopped]"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
