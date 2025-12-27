from flask import Flask, send_file
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

# 如果有安裝 eventlet，這行能提升效能
try:
    import eventlet
    eventlet.monkey_patch()
except ImportError:
    pass

app = Flask(__name__)
app.config['SECRET_KEY'] = 'galaxy_secret'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

current_process = None
master_fd_global = None

def log(msg):
    print(f"[系統] {msg}")
    sys.stdout.flush()

def kill_existing_process():
    """強制結束目前正在執行的進程"""
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

@socketio.on('run_code_v2')
def handle_run_code(data):
    global current_process, master_fd_global
    
    # 1. 先清理舊的執行狀態
    kill_existing_process()
    
    code = data.get('code')
    lang = data.get('lang', 'cpp')
    
    log(f"收到執行請求 ({lang})...")
    
    home_dir = os.environ.get('HOME', '/data/data/com.termux/files/home')
    if not os.path.exists(home_dir):
        home_dir = os.getcwd()

    # === 環境變數設定 ===
    my_env = os.environ.copy()
    my_env["PYTHONIOENCODING"] = "utf-8"
    my_env["LANG"] = "C.UTF-8"
    my_env["LC_ALL"] = "C.UTF-8"

    # 檢查是否可以使用 stdbuf
    stdbuf_exe = shutil.which("stdbuf")
    use_stdbuf = stdbuf_exe is not None

    if lang == 'python':
        source_file = os.path.join(home_dir, "galaxy_runner.py")
        python_exe = shutil.which("python3") or shutil.which("python")
        if not python_exe:
            emit('program_output', {'data': "❌ 錯誤: 找不到 'python' 指令。\r\n"})
            emit('program_status', {'status': 'error'})
            return
        run_cmd = [python_exe, '-u', source_file]
        
    else: # C++
        source_file = os.path.join(home_dir, "galaxy_runner.cpp")
        exe_file = os.path.join(home_dir, "galaxy_runner")
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            emit('program_output', {'data': "❌ 錯誤: 找不到 'clang++' 或 'g++'。\r\n"})
            emit('program_status', {'status': 'error'})
            return
        
        run_cmd = [exe_file]
        # 如果系統支援 stdbuf，強制 stdout 和 stderr 無緩衝
        if use_stdbuf:
            run_cmd = [stdbuf_exe, '-o0', '-e0'] + run_cmd

    # 寫入檔案
    try:
        with open(source_file, "w", encoding='utf-8') as f:
            f.write(code)
    except Exception as e:
        emit('program_output', {'data': f"❌ 寫入失敗: {e}\r\n"})
        emit('program_status', {'status': 'error'})
        return

    # C++ 編譯
    if lang == 'cpp':
        log(f"正在編譯: {compiler} {source_file}")
        compile_res = subprocess.run(
            [compiler, source_file, '-o', exe_file], 
            capture_output=True, 
            text=True, 
            env=my_env
        )
        if compile_res.returncode != 0:
            # 這裡也修正一下換行，以防編譯錯誤訊息格式跑掉
            err_msg = compile_res.stderr.replace('\r\n', '\n').replace('\n', '\r\n')
            emit('program_output', {'data': f"❌ 編譯錯誤:\r\n{err_msg}"})
            emit('program_status', {'status': 'error'})
            return

    # 執行程式
    try:
        master_fd_global, slave_fd = pty.openpty()
        
        # 設定終端機屬性 (關閉 Echo)
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
            env=my_env 
        )
        os.close(slave_fd)
        
        emit('program_output', {'data': ""})
        # 使用背景任務來讀取輸出
        socketio.start_background_task(target=read_output, fd=master_fd_global, proc=current_process)
        
    except Exception as e:
        emit('program_output', {'data': f"❌ 啟動失敗: {str(e)}\r\n"})
        emit('program_status', {'status': 'error'})
        kill_existing_process()

def read_output(fd, proc):
    """讀取輸出的核心邏輯 (格式修正版)"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors='replace')
    log("開始讀取輸出迴圈...")
    
    try:
        while True:
            # 一般讀取，Timeout 設為 0.1s
            r, _, _ = select.select([fd], [], [], 0.1)
            
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                    if data: 
                        text = decoder.decode(data, final=False)
                        # 修正: 確保換行符號為 \r\n，避免格式跑掉
                        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
                        socketio.emit('program_output', {'data': text})
                    else: 
                        break # EOF
                except OSError as e:
                    if e.errno == errno.EIO: # Linux PTY 關閉時的標準錯誤
                        break
                    break
            
            # 檢查進程是否已經結束
            if proc.poll() is not None:
                log("進程已結束，嘗試讀取殘留輸出...")
                time.sleep(0.2) 
                
                # 進入「貪婪讀取」模式
                while True:
                    try:
                        r, _, _ = select.select([fd], [], [], 0.1)
                        if fd not in r: 
                            break 
                        
                        data = os.read(fd, 4096)
                        if not data: 
                            break
                        
                        text = decoder.decode(data, final=True)
                        # 修正: 確保換行符號為 \r\n
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
            try: os.close(fd) 
            except: pass

@socketio.on('send_input')
def handle_input(data):
    global master_fd_global
    if master_fd_global:
        try: 
            input_text = data.get('input')
            # 確保輸入有換行符號
            if not input_text.endswith('\n'):
                input_text += '\n'
            
            msg = input_text.encode('utf-8')
            os.write(master_fd_global, msg)
        except Exception as e: log(f"寫入失敗: {e}")

@socketio.on('stop_code')
def handle_stop():
    kill_existing_process()
    emit('program_output', {'data': "\r\n[程式已停止]"})

if __name__ == '__main__':
    log("伺服器啟動中 (Cloud Version)...")
    # 讀取雲端平台分配的 PORT，如果沒有則預設 5000
    port = int(os.environ.get("PORT", 5000))
    # host 必須設為 0.0.0.0 才能讓外部存取
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)