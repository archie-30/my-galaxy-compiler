try:
    import eventlet
    eventlet.monkey_patch(subprocess=False)
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
import uuid
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
        print(f"[系統] MongoDB 連線失敗: {e}")
else:
    print("[系統] 警告: 未設定 MONGO_URI")

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
        try:
            os.close(master_fd_global)
        except:
            pass
        master_fd_global = None

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/register', methods=['POST'])
def register():
    if users_collection is None:
        return jsonify({'success': False, 'message': '資料庫未連線'}), 500
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'success': False, 'message': '請輸入帳號和密碼'}), 400
    if users_collection.find_one({'username': username}):
        return jsonify({'success': False, 'message': '此帳號已被註冊'}), 400
    
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    new_user = {
        'username': username,
        'password': hashed_password,
        'created_at': datetime.utcnow(),
        'projects_cpp': [],
        'projects_python': []
    }
    users_collection.insert_one(new_user)
    return jsonify({'success': True, 'message': '註冊成功！'})

@app.route('/login', methods=['POST'])
def login():
    if users_collection is None:
        return jsonify({'success': False, 'message': '資料庫未連線'}), 500
    data = request.json
    username = data.get('username')
    password = data.get('password')
    user = users_collection.find_one({'username': username})
    
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
        session_token = str(uuid.uuid4())
        users_collection.update_one({'username': username}, {'$set': {'session_token': session_token}})
        
        session.permanent = True
        session['user'] = username
        session['token'] = session_token
        
        return jsonify({'success': True, 'username': username})
    else:
        return jsonify({'success': False, 'message': '帳號或密碼錯誤'}), 401

@app.route('/get_user_data', methods=['GET'])
def get_user_data():
    if 'user' not in session:
        return jsonify({'success': False, 'is_logged_in': False})
    
    username = session['user']
    if users_collection is None:
        return jsonify({'success': False, 'message': 'DB Error'}), 500
    
    user = users_collection.find_one({'username': username}, {'_id': 0, 'password': 0})
    current_token = session.get('token')
    
    if user and user.get('session_token') != current_token:
        session.clear()
        return jsonify({'success': False, 'is_logged_in': False, 'message': 'Logged in on another device'})

    if user:
        return jsonify({'success': True, 'is_logged_in': True, 'username': username, 'data': user})
    return jsonify({'success': False, 'is_logged_in': False})

@app.route('/save_projects', methods=['POST'])
def save_projects():
    if 'user' not in session:
        return jsonify({'success': False, 'message': '未登入'}), 401
    if users_collection is None:
        return jsonify({'success': False, 'message': 'DB Error'}), 500
    
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
    
    lang = data.get('lang', 'cpp')
    files = data.get('files', [])
    single_code = data.get('code')
    
    log(f"收到執行請求 ({lang})...")
    
    home_dir = os.environ.get('HOME', '/app')
    if not os.path.exists(home_dir):
        home_dir = os.getcwd()

    workspace = os.path.join(home_dir, "galaxy_workspace")
    os.makedirs(workspace, exist_ok=True)

    if not files and single_code:
        ext = '.cpp' if lang == 'cpp' else '.py'
        files = [{'name': f'main{ext}', 'code': single_code}]

    if not files:
        emit('program_output', {'data': "❌ 錯誤: 沒有收到任何程式碼。\r\n"})
        emit('program_status', {'status': 'error'})
        return

    cpp_sources = []
    main_python_file = None

    try:
        for f in files:
            fname = f.get('name', '')
            fcode = f.get('code', '')
            if not fname:
                continue
            
            fname = os.path.basename(fname)
            file_path = os.path.join(workspace, fname)
            
            with open(file_path, "w", encoding='utf-8') as fw:
                fw.write(fcode)
                
            if lang == 'cpp' and fname.endswith('.cpp'):
                cpp_sources.append(file_path)
            elif lang == 'python':
                if fname.lower() == 'main.py' or main_python_file is None:
                    main_python_file = file_path
    except Exception as e:
        emit('program_output', {'data': f"❌ 寫入失敗: {e}\r\n"})
        emit('program_status', {'status': 'error'})
        return

    my_env = os.environ.copy()
    my_env["PYTHONIOENCODING"] = "utf-8"
    my_env["LANG"] = "C.UTF-8"
    my_env["LC_ALL"] = "C.UTF-8"

    stdbuf_exe = shutil.which("stdbuf")
    use_stdbuf = stdbuf_exe is not None

    if lang == 'python':
        if not main_python_file:
            emit('program_output', {'data': "❌ 錯誤: 找不到 Python 執行檔 (.py)。\r\n"})
            emit('program_status', {'status': 'error'})
            return

        python_exe = shutil.which("python3") or shutil.which("python")
        if not python_exe:
            emit('program_output', {'data': "❌ 錯誤: 找不到 'python' 指令。\r\n"})
            emit('program_status', {'status': 'error'})
            return
        run_cmd = [python_exe, '-u', main_python_file]
        cwd = workspace
        
    else:
        if not cpp_sources:
            emit('program_output', {'data': "❌ 錯誤: 找不到 C++ 原始碼 (.cpp)。\r\n"})
            emit('program_status', {'status': 'error'})
            return

        exe_file = os.path.join(workspace, "galaxy_runner")
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            emit('program_output', {'data': "❌ 錯誤: 找不到 'clang++' 或 'g++'。\r\n"})
            emit('program_status', {'status': 'error'})
            return
        
        log(f"正在編譯: {compiler} {' '.join(cpp_sources)}")
        compile_cmd = [compiler] + cpp_sources + ['-o', exe_file]
        compile_res = subprocess.run(
            compile_cmd, 
            capture_output=True, 
            text=True, 
            env=my_env,
            cwd=workspace
        )
        if compile_res.returncode != 0:
            err_msg = compile_res.stderr.replace('\r\n', '\n').replace('\n', '\r\n')
            emit('program_output', {'data': f"❌ 編譯錯誤:\r\n{err_msg}"})
            emit('program_status', {'status': 'error'})
            return

        run_cmd = [exe_file]
        if use_stdbuf:
            run_cmd = [stdbuf_exe, '-o0', '-e0'] + run_cmd
        cwd = workspace

    try:
        master_fd_global, slave_fd = pty.openpty()
        
        try:
            import termios
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except:
            pass 

        log(f"啟動進程: {run_cmd}")
        current_process = subprocess.Popen(
            run_cmd, 
            stdin=slave_fd, 
            stdout=slave_fd, 
            stderr=slave_fd,
            preexec_fn=os.setsid, 
            close_fds=True,
            env=my_env,
            cwd=cwd
        )
        os.close(slave_fd)
        
        emit('program_output', {'data': ""})
        socketio.start_background_task(target=read_output, fd=master_fd_global, proc=current_process)
        
    except Exception as e:
        emit('program_output', {'data': f"❌ 啟動失敗: {str(e)}\r\n"})
        emit('program_status', {'status': 'error'})
        kill_existing_process()

def read_output(fd, proc):
    decoder = codecs.getincrementaldecoder("utf-8")(errors='replace')
    log("開始讀取輸出迴圈...")
    
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.02)
            
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                    if data: 
                        text = decoder.decode(data, final=False)
                        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
                        socketio.emit('program_output', {'data': text})
                    else: 
                        break
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    break
            
            if proc.poll() is not None:
                log("進程已結束，嘗試讀取殘留輸出...")
                time.sleep(0.1) 
                
                while True:
                    try:
                        r, _, _ = select.select([fd], [], [], 0.05)
                        if fd not in r: 
                            break 
                        
                        data = os.read(fd, 4096)
                        if not data: 
                            break
                        
                        text = decoder.decode(data, final=True)
                        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
                        socketio.emit('program_output', {'data': text})
                        
                    except OSError:
                        break
                break 
                
            socketio.sleep(0.01) 
            
    except Exception as e: 
        log(f"讀取異常: {e}")
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
        try: 
            input_text = data.get('input')
            if not input_text.endswith('\n'):
                input_text += '\n'
            
            msg = input_text.encode('utf-8')
            os.write(master_fd_global, msg)
        except Exception as e:
            log(f"寫入失敗: {e}")

@socketio.on('stop_code')
def handle_stop():
    kill_existing_process()
    emit('program_output', {'data': "\r\n[程式已停止]"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    log(f"伺服器啟動中 (雲端部署版本) - Port: {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
