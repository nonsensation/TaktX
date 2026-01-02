import http.server
import socketserver
import os
import json
import subprocess
import threading
import time
import uuid
import glob
import shutil
import random
import re
from datetime import datetime
import uuid6

# --- Configuration ---
PORT = 8000
DOWNLOAD_DIR = "library"
SETTINGS_FILE = "settings.json"
GROUPS_FILE = "groups.json"
YT_DLP_CMD = "yt-dlp"

# Ensure download directory exists
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# --- Global State ---
active_downloads_info = {}
active_processes = {}

# Regex for parsing standard yt-dlp progress
PROGRESS_REGEX = re.compile(r'\[download\]\s+(\d+\.?\d*)%\s+of\s+~?(\S+)\s+at\s+(.+?)\s+ETA\s+(\S+)')
# Regex for ffmpeg progress (used during range downloads/cuts)
# Matches: size=   20224kB time=00:00:30.09 bitrate=5505.4kbits/s speed=1.73x
FFMPEG_REGEX = re.compile(r'size=\s*(\S+)\s+time=\s*(\S+).*?speed=\s*(\S+)')

# --- Helper Functions ---
def load_json(filepath, default):
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def generate_uuid_v7():
    return str(uuid6.uuid7())

def parse_time_str(t_str):
    """Parses HH:MM:SS.mm string into seconds"""
    try:
        parts = t_str.split(':')
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    except:
        return 0.0

def load_settings():
    defaults = {
        "tags": ["gameplay", "tutorial", "highlight"],
        "profiles": [{"id": "default", "name": "Main Library"}],
        "current_profile": "default"
    }
    data = load_json(SETTINGS_FILE, defaults)
    if "profiles" not in data: data["profiles"] = defaults["profiles"]
    if "tags" not in data: data["tags"] = defaults["tags"]
    return data

def load_groups():
    return load_json(GROUPS_FILE, {})

def save_groups(data):
    save_json(GROUPS_FILE, data)

def sync_data_on_startup():
    settings = load_settings()
    existing_tags = set(settings.get("tags", []))
    
    meta_files = glob.glob(os.path.join(DOWNLOAD_DIR, "*.json"))
    for meta_path in meta_files:
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                tags = data.get('tags', '')
                if tags:
                    for tag in tags.split(','):
                        clean = tag.strip()
                        if clean: existing_tags.add(clean)
                if 'profile_id' not in data:
                    data['profile_id'] = 'default'
                    with open(meta_path, 'w', encoding='utf-8') as fw:
                        json.dump(data, fw, indent=4)
        except: pass
    settings['tags'] = list(existing_tags)
    save_json(SETTINGS_FILE, settings)
    print(f"Data synced. Tags: {len(settings['tags'])}")

sync_data_on_startup()

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/library':
            self.send_json(self.get_library_data())
            return
        if self.path == '/api/settings':
            self.send_json(load_settings())
            return
        if self.path == '/api/groups':
            self.send_json(load_groups())
            return
        if self.path == '/api/status':
            self.send_json(list(active_downloads_info.values()))
            return

        try:
            if self.path.startswith('/library/'):
                super().do_GET()
            elif self.path == '/' or self.path == '/index.html':
                self.path = 'index.html'
                super().do_GET()
            else:
                super().do_GET()
        except ConnectionResetError:
            pass 
        except Exception as e:
            print(f"Server Error: {e}")

    def do_POST(self):
        length = int(self.headers['Content-Length'])
        data = json.loads(self.rfile.read(length).decode('utf-8'))

        if self.path == '/api/analyze':
            cmd = [YT_DLP_CMD, '--no-colors', '-J', '--flat-playlist', data.get('url')]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
                if res.returncode == 0:
                    self.send_json(json.loads(res.stdout))
                else:
                    self.send_error(400, "yt-dlp error")
            except Exception as e:
                self.send_error(500, str(e))
            return

        if self.path == '/api/download':
            threading.Thread(target=start_download_process, args=(data,)).start()
            self.send_json({"status": "started"})
            return

        if self.path == '/api/cancel':
            clip_id = data.get('id')
            if clip_id in active_downloads_info:
                active_downloads_info[clip_id]['status'] = 'cancelled'
            
            if clip_id in active_processes:
                try:
                    active_processes[clip_id].kill()
                except: pass
            
            self.send_json({"status": "cancelled"})
            return

        if self.path == '/api/update':
            update_metadata(data)
            self.send_json({"status": "success"})
            return

        if self.path == '/api/settings':
            save_json(SETTINGS_FILE, data)
            self.send_json({"status": "saved"})
            return

        if self.path == '/api/groups':
            save_groups(data)
            self.send_json({"status": "saved"})
            return

        if self.path == '/api/check_source':
            cmd = [YT_DLP_CMD, '--simulate', data.get('url')]
            try:
                res = subprocess.run(cmd, capture_output=True)
                self.send_json({"available": (res.returncode == 0)})
            except:
                self.send_json({"available": False})
            return
            
        if self.path == '/api/delete':
            success = delete_clip(data.get('id'))
            if success:
                self.send_json({"status": "deleted"})
            else:
                self.send_error(500, "Could not delete file (locked)")
            return

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def get_library_data(self):
        clips = []
        meta_files = glob.glob(os.path.join(DOWNLOAD_DIR, "*.json"))
        for meta_path in meta_files:
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    filename = data.get('filename')
                    clip_id = data.get('id')
                    full_path = os.path.join(DOWNLOAD_DIR, filename) if filename else None
                    if filename and os.path.exists(full_path):
                        data['file_size'] = os.path.getsize(full_path)
                        data.setdefault('custom_title', "")
                        data.setdefault('description', "")
                        data.setdefault('group_id', "") 
                        data.setdefault('source_status', "unchecked")
                        data.setdefault('last_checked', "")
                        data.setdefault('profile_id', "default")
                        thumb_path = os.path.join(DOWNLOAD_DIR, f"{clip_id}.jpg")
                        if os.path.exists(thumb_path):
                            data['thumbnail'] = f"{clip_id}.jpg"
                        clips.append(data)
            except: pass
        clips.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return clips

def start_download_process(data):
    clip_id = generate_uuid_v7()
    url = data.get('url')
    profile_id = data.get('profile_id', 'default')
    
    quality_setting = data.get('quality', 'best')
    quality_map = {
        'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]',
        '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]',
        'video_only': 'bestvideo[ext=mp4]',
        'audio_best': 'bestaudio[ext=m4a]/bestaudio',
        'audio_low': 'worstaudio[ext=m4a]/worstaudio'
    }
    fmt = quality_map.get(quality_setting, quality_map['best'])

    output_path = os.path.join(DOWNLOAD_DIR, f"{clip_id}.%(ext)s")
    
    cmd = [YT_DLP_CMD, '--no-colors', '--newline', '-f', fmt, '--write-thumbnail', '--convert-thumbnails', 'jpg', '-o', output_path]
    
    if os.path.exists("ffmpeg.exe"):
        cmd.extend(['--ffmpeg-location', '.'])

    start_time = data.get('start_time')
    end_time = data.get('end_time')
    
    # Calculate Expected Duration for Progress Bar
    total_duration_secs = 0.0
    
    if start_time or end_time:
        if not start_time: start_time = "00:00:00"
        if not end_time: end_time = "inf"
        section_arg = f"*{start_time}-{end_time}"
        cmd.extend(['--download-sections', section_arg, '--force-keyframes-at-cuts'])
        
        # Calculate duration if possible
        if end_time != "inf":
            s_sec = parse_time_str(start_time)
            e_sec = parse_time_str(end_time)
            total_duration_secs = e_sec - s_sec

    cmd.append(url)

    active_downloads_info[clip_id] = {
        "id": clip_id, 
        "title": data.get('title', 'Unknown'), 
        "status": "downloading",
        "percent": "0%",
        "speed": "0.00MiB/s",
        "eta": "--:--"
    }
    
    try:
        proc = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            encoding='utf-8', 
            errors='replace'
        )
        active_processes[clip_id] = proc
        
        while True:
            # FORCE STOP CHECK inside loop
            if active_downloads_info[clip_id].get('status') == 'cancelled':
                proc.kill()
                break

            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                line_str = line.strip()
                print(f"[yt-dlp] {line_str}") 
                
                # 1. Check Standard Progress
                match = PROGRESS_REGEX.search(line_str)
                if match:
                    percent, size, speed, eta = match.groups()
                    active_downloads_info[clip_id]['percent'] = f"{percent}%"
                    active_downloads_info[clip_id]['size'] = size
                    active_downloads_info[clip_id]['speed'] = speed.strip()
                    active_downloads_info[clip_id]['eta'] = eta
                
                # 2. Check FFmpeg Progress (Cut/Merge)
                elif "frame=" in line_str:
                    ff_match = FFMPEG_REGEX.search(line_str)
                    if ff_match:
                        size, time_str, speed = ff_match.groups()
                        
                        # Calculate Percentage if duration is known
                        if total_duration_secs > 0:
                            curr_secs = parse_time_str(time_str)
                            pct = (curr_secs / total_duration_secs) * 100
                            if pct > 100: pct = 100
                            active_downloads_info[clip_id]['percent'] = f"{pct:.1f}%"
                        else:
                            active_downloads_info[clip_id]['percent'] = "indeterminate"
                            
                        active_downloads_info[clip_id]['size'] = size
                        active_downloads_info[clip_id]['speed'] = speed.strip() + "x"
                        active_downloads_info[clip_id]['eta'] = "Processing"
                    else:
                        active_downloads_info[clip_id]['percent'] = "indeterminate"

        # Wait only if not cancelled to get return code
        if active_downloads_info[clip_id].get('status') != 'cancelled':
            proc.wait()
            if proc.returncode == 0:
                found = glob.glob(os.path.join(DOWNLOAD_DIR, f"{clip_id}.mp4"))
                if not found: found = glob.glob(os.path.join(DOWNLOAD_DIR, f"{clip_id}.*"))
                found = [f for f in found if not f.endswith('.jpg') and not f.endswith('.json')]
                filename = os.path.basename(found[0]) if found else f"{clip_id}.mp4"
                thumb_found = os.path.exists(os.path.join(DOWNLOAD_DIR, f"{clip_id}.jpg"))

                meta = {
                    "id": clip_id,
                    "group_id": data.get('group_id', ''),
                    "profile_id": profile_id,
                    "original_url": url,
                    "title": data.get('title'),
                    "custom_title": data.get('custom_title', ''),
                    "description": data.get('description', ''),
                    "filename": filename,
                    "range": f"{start_time}-{end_time}" if (start_time or end_time) else "Full Video",
                    "tags": data.get('tags', ''),
                    "quality_profile": quality_setting,
                    "source_status": "unchecked",
                    "last_checked": "",
                    "created_at": datetime.now().isoformat()
                }
                if thumb_found: meta['thumbnail'] = f"{clip_id}.jpg"
                
                with open(os.path.join(DOWNLOAD_DIR, f"{clip_id}.json"), 'w') as f:
                    json.dump(meta, f, indent=4)
                    
                active_downloads_info[clip_id]['status'] = "finished"
                active_downloads_info[clip_id]['percent'] = "100%"
            else:
                active_downloads_info[clip_id]['status'] = "error"

    except Exception as e:
        print(f"DL Exception: {e}")
        active_downloads_info[clip_id]['status'] = "error"
    
    finally:
        if clip_id in active_processes: del active_processes[clip_id]
        if active_downloads_info[clip_id]['status'] in ['cancelled', 'error']:
            time.sleep(1)
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{clip_id}*")):
                try: os.remove(f)
                except: pass
        time.sleep(5)
        if clip_id in active_downloads_info: del active_downloads_info[clip_id]

def update_metadata(data):
    clip_id = data.get('id')
    path = os.path.join(DOWNLOAD_DIR, f"{clip_id}.json")
    if os.path.exists(path):
        with open(path, 'r') as f: meta = json.load(f)
        fields = ['tags', 'custom_title', 'description', 'group_id', 'source_status', 'last_checked', 'profile_id']
        for f in fields:
            if f in data: meta[f] = data[f]
        with open(path, 'w') as f: json.dump(meta, f, indent=4)

def delete_clip(clip_id):
    path = os.path.join(DOWNLOAD_DIR, f"{clip_id}.json")
    if os.path.exists(path):
        try:
            with open(path, 'r') as f: meta = json.load(f)
            vid_path = os.path.join(DOWNLOAD_DIR, meta.get('filename', ''))
            thumb_path = os.path.join(DOWNLOAD_DIR, f"{clip_id}.jpg")
            
            if os.path.exists(vid_path):
                deleted = False
                for i in range(20):
                    try:
                        os.remove(vid_path)
                        deleted = True
                        break
                    except PermissionError:
                        time.sleep(0.5)
                if not deleted: 
                    print(f"Failed to delete locked file: {vid_path}")
                    return False
            
            if os.path.exists(thumb_path):
                try: os.remove(thumb_path)
                except: pass
            
            os.remove(path)
            return True
        except Exception as e:
            print(f"Error deleting clip {clip_id}: {e}")
            return False
    return False

print(f"Server running on http://localhost:{PORT}")
with ThreadingTCPServer(("", PORT), RequestHandler) as httpd:
    httpd.serve_forever()