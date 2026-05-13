from flask import Flask, render_template, jsonify, request, send_file
import psutil
import platform
import pandas as pd
import os
import time
import socket
import subprocess
import getpass
import threading
import json
import os
import sys
import webbrowser
import shutil

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def writable_path(relative_path):
    if getattr(sys, "frozen", False):
        base_path = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "PowerMonitorForEfficientLearning"
        )
    else:
        base_path = os.path.abspath(".")
    os.makedirs(base_path, exist_ok=True)
    return os.path.join(base_path, relative_path)

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static")
)

RECENT_DATA_DIR = writable_path("recent_data")
os.makedirs(RECENT_DATA_DIR, exist_ok=True)

def get_desktop_power_monitor_dir():
    candidates = []

    home_dir = os.path.expanduser("~")
    user_profile = os.environ.get("USERPROFILE", home_dir)
    one_drive = os.environ.get("OneDrive", "")

    candidates.append(os.path.join(home_dir, "Desktop"))
    candidates.append(os.path.join(user_profile, "Desktop"))

    if one_drive:
        candidates.append(os.path.join(one_drive, "Desktop"))

    candidates.append(os.path.join(home_dir, "OneDrive", "Desktop"))
    candidates.append(os.path.join(user_profile, "OneDrive", "Desktop"))

    desktop_dir = None
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            desktop_dir = candidate
            break

    if not desktop_dir:
        desktop_dir = os.path.join(home_dir, "Desktop")
        os.makedirs(desktop_dir, exist_ok=True)

    power_monitor_dir = os.path.join(desktop_dir, "Power Monitor")
    recent_data_dir = os.path.join(power_monitor_dir, "recent_data")
    os.makedirs(recent_data_dir, exist_ok=True)
    return power_monitor_dir, recent_data_dir

def ensure_export_dirs():
    os.makedirs(RECENT_DATA_DIR, exist_ok=True)
    _, desktop_recent_dir = get_desktop_power_monitor_dir()
    os.makedirs(desktop_recent_dir, exist_ok=True)
    return desktop_recent_dir

DESKTOP_POWER_MONITOR_DIR, DESKTOP_RECENT_DATA_DIR = get_desktop_power_monitor_dir()
os.makedirs(DESKTOP_RECENT_DATA_DIR, exist_ok=True)

CARBON_FACTOR_KG_PER_KWH = 0.82
energy_total_wh = 0.0
carbon_total_kg = 0.0
session_start_epoch = time.time()
session_start_text = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_start_epoch))
cpu_hist = []
gpu_hist = []
power_hist = []
energy_total_hist = []
energy_rate_hist = []
carbon_rate_hist = []
ram_hist = []
disk_hist = []
internet_hist = []
time_hist = []
epoch_hist = []
battery_percent_hist = []
battery_time_hist = []
prev_recv = None
prev_time = None
latest_dynamic_data = {}
data_lock = threading.Lock()

cached_gpu_usage = 0.0
last_gpu_scan_time = 0.0
gpu_method = "warming up"
gpu_lock = threading.Lock()
last_gpu_debug_samples = []

cached_top_processes = []
last_process_scan_time = 0.0

session_sample_count = 0
session_sums = {
    'cpu': 0.0,
    'gpu': 0.0,
    'ram': 0.0,
    'disk': 0.0,
    'power': 0.0,
    'energy_total': 0.0,
    'energy_rate': 0.0,
    'carbon_rate': 0.0,
    'internet': 0.0,
}

SYSTEM_INFO = {
    "owner_name": "User",
    "device_name": "Device",
    "model": "Unknown Model",
    "os": "Unknown OS",
    "cpu_name": "Unknown CPU",
    "gpu_name": "Unknown GPU",
    "has_battery": False,
}

IGNORED_PROCESS_NAMES = {"system idle process", "idle", "registry", "memory compression", "secure system"}
TOOLTIPS = {
    "power": "Power = Base System + Display + Keyboard/Touchpad + Motherboard/Fans + CPU Part + GPU Part + RAM Part + Disk Part + Network Part + Charging Overhead",
    "co2_rate": "Carbon Emission Rate = (Power Consumption in W / 1000) × 0.82 kg CO₂/kWh",
    "energy": "Energy Consumed (Wh) = accumulated sum of Power Consumption / 3600 every second",
    "battery": "On laptops: shows battery percent, charging state, full/backup estimate. On desktops without battery: shows Line Connected.",
    "co2_total": "Current total CO₂ is accumulated from per-second carbon estimates",
    "energy_rate": "Energy Consumption Rate = Power Consumption / 3600 per second. Next Sec shows estimated energy to be consumed in the next 1 second, not total accumulated energy.",
    "green_score": "Green Score = 100 - (CPU×0.18) - (GPU×0.12) - max(Power-22,0)×0.72 - max(RAM-70,0)×0.16 - low battery/charging adjustments",
}


def bounded_tail(values, limit):
    if limit <= 0:
        return []
    return values[-limit:] if len(values) > limit else list(values)


def rolling_average(values, seconds):
    vals = bounded_tail(values, seconds)
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def rolling_total(values, seconds):
    vals = bounded_tail(values, seconds)
    return round(sum(vals), 4) if vals else 0.0


def trim_battery_history(max_points=600):
    if len(battery_percent_hist) > max_points:
        del battery_percent_hist[:-max_points]
    if len(battery_time_hist) > max_points:
        del battery_time_hist[:-max_points]

def safe_run(cmd, timeout=3):
    try:
        startupinfo = None
        creationflags = 0

        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=creationflags
        )

        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""


def get_cpu_name():
    out = safe_run(["wmic", "cpu", "get", "name"])
    lines = [x.strip() for x in out.splitlines() if x.strip() and x.strip().lower() != 'name']
    return lines[0] if lines else (platform.processor() or 'Unknown CPU')


def get_gpu_name():
    out = safe_run(["wmic", "path", "win32_VideoController", "get", "name"])
    lines = [x.strip() for x in out.splitlines() if x.strip() and x.strip().lower() != 'name']
    return ', '.join(lines) if lines else 'Unknown GPU'


def get_model_name():
    out = safe_run(["wmic", "computersystem", "get", "model"])
    lines = [x.strip() for x in out.splitlines() if x.strip() and x.strip().lower() != 'model']
    return lines[0] if lines else 'Unknown Model'


def get_os_name():
    caption = safe_run(["wmic", "os", "get", "Caption"])
    lines = [x.strip() for x in caption.splitlines() if x.strip() and x.strip().lower() != 'caption']
    if lines:
        return lines[0].replace('Microsoft ', '').strip()
    return f"{platform.system()} {platform.release()}"


def init_static_info():
    SYSTEM_INFO["owner_name"] = getpass.getuser() if hasattr(getpass, 'getuser') else 'User'
    SYSTEM_INFO["device_name"] = platform.node() or socket.gethostname() or 'Device'
    SYSTEM_INFO["model"] = get_model_name()
    SYSTEM_INFO["os"] = get_os_name()
    SYSTEM_INFO["cpu_name"] = get_cpu_name()
    SYSTEM_INFO["gpu_name"] = get_gpu_name()
    SYSTEM_INFO["has_battery"] = psutil.sensors_battery() is not None


def get_disk_percent():
    try:
        drive = os.environ.get('SystemDrive', 'C:') + '\\'
        return round(psutil.disk_usage(drive).percent, 2)
    except Exception:
        return 0.0


def get_ram_percent():
    try:
        return round(psutil.virtual_memory().percent, 2)
    except Exception:
        return 0.0


def format_duration(seconds):
    if seconds is None or seconds <= 0:
        return 'Estimating...'
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h} hr {m} min" if h > 0 else f"{m} min"


def estimate_battery_time_from_history(battery_percent, plugged_in):
    try:
        if len(battery_percent_hist) < 10 or len(battery_time_hist) < 10:
            return None
        delta_percent = battery_percent_hist[-1] - battery_percent_hist[0]
        delta_time = battery_time_hist[-1] - battery_time_hist[0]
        if delta_time <= 0:
            return None
        rate = delta_percent / delta_time
        if plugged_in:
            if rate <= 0:
                return None
            return max(100 - battery_percent, 0) / rate
        if rate >= 0:
            return None
        return max(battery_percent, 0) / abs(rate)
    except Exception:
        return None


def get_battery_info():
    try:
        battery = psutil.sensors_battery()
        if not battery:
            return {
                "percent": None,
                "status": "Line Connected",
                "time_text": "No battery installed.",
                "plugged_in": True,
            }
        percent = round(battery.percent, 2)
        plugged_in = bool(battery.power_plugged)
        status = 'Charger Connected' if plugged_in else 'On Battery'
        battery_percent_hist.append(percent)
        battery_time_hist.append(time.time())
        trim_battery_history()
        secsleft = getattr(battery, 'secsleft', None)
        seconds_value = secsleft if isinstance(secsleft, int) and secsleft > 0 else estimate_battery_time_from_history(percent, plugged_in)
        if plugged_in and percent >= 99.5:
            status = 'Fully Charged'
            time_text = 'Unplug charger.'
        elif seconds_value:
            time_text = f"Full in {format_duration(seconds_value)}" if plugged_in else f"On Battery • Backup ~ {format_duration(seconds_value)}"
        else:
            time_text = 'Full time estimating...' if plugged_in else 'On Battery • Backup estimating...'
        return {
            "percent": percent,
            "status": status,
            "time_text": time_text,
            "plugged_in": plugged_in,
        }
    except Exception:
        return {"percent": None, "status": "Unknown", "time_text": "Estimating...", "plugged_in": False}


def get_network_speed():
    global prev_recv, prev_time
    now = time.time()
    counters = psutil.net_io_counters()
    current_recv = counters.bytes_recv
    if prev_recv is None or prev_time is None:
        prev_recv = current_recv
        prev_time = now
        return 0.0
    delta_bytes = current_recv - prev_recv
    delta_time = max(now - prev_time, 1e-6)
    prev_recv = current_recv
    prev_time = now
    return round((delta_bytes / 1024.0) / delta_time, 2)


def get_interface_info():
    try:
        stats = psutil.net_if_stats()
        wifi = 'Not Connected'
        ethernet = 'Not Connected'
        for name, val in stats.items():
            lname = name.lower()
            if val.isup:
                if any(x in lname for x in ['wi-fi', 'wifi', 'wireless', 'wlan']):
                    wifi = 'Connected'
                if any(x in lname for x in ['ethernet', 'eth']):
                    ethernet = 'Connected'
        return wifi, ethernet
    except Exception:
        return 'Unknown', 'Unknown'


def parse_gpu_usage_nvidia():
    out = safe_run(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
        timeout=1.5,
    )
    if not out:
        return None
    try:
        value = float(out.splitlines()[0].strip())
        return round(min(max(value, 0.0), 100.0), 2)
    except Exception:
        return None


def parse_gpu_usage_powershell():
    ps_script = r"""
$ErrorActionPreference = 'Stop'
try {
    $rows = @()
    $counter = Get-Counter '\GPU Engine(*)\Utilization Percentage'
    foreach ($sample in $counter.CounterSamples) {
        $name = ''
        if ($null -ne $sample.InstanceName -and [string]::IsNullOrWhiteSpace([string]$sample.InstanceName) -eq $false) {
            $name = [string]$sample.InstanceName
        } elseif ($null -ne $sample.Path) {
            $name = [string]$sample.Path
        }

        $value = 0.0
        try { $value = [double]$sample.CookedValue } catch { $value = 0.0 }
        if ($value -lt 0) { continue }

        $rows += [pscustomobject]@{
            name = $name
            value = [math]::Round($value, 4)
        }
    }

    if (-not $rows -or $rows.Count -eq 0) {
        [pscustomobject]@{ usage = 0; samples = @() } | ConvertTo-Json -Compress -Depth 4
        exit
    }

    $interesting = $rows | Where-Object {
        $_.name -match 'engtype_3D|engtype_Compute|engtype_VideoDecode|engtype_VideoProcessing|engtype_Copy'
    }
    if (-not $interesting -or $interesting.Count -eq 0) {
        $interesting = $rows
    }

    $grouped = $interesting | Group-Object name
    $collapsed = @()
    foreach ($group in $grouped) {
        $maxVal = ($group.Group | Measure-Object -Property value -Maximum).Maximum
        if ($null -eq $maxVal) { $maxVal = 0 }
        $collapsed += [pscustomobject]@{
            name = $group.Name
            value = [math]::Round([double]$maxVal, 2)
        }
    }

    $sumVal = ($collapsed | Measure-Object -Property value -Sum).Sum
    $maxVal = ($collapsed | Measure-Object -Property value -Maximum).Maximum
    if ($null -eq $sumVal) { $sumVal = 0 }
    if ($null -eq $maxVal) { $maxVal = 0 }

    $usage = [math]::Round([math]::Max([double]$sumVal, [double]$maxVal), 2)
    if ($usage -gt 100) { $usage = 100 }
    if ($usage -lt 0) { $usage = 0 }

    [pscustomobject]@{
        usage = $usage
        samples = ($collapsed | Sort-Object value -Descending | Select-Object -First 12)
    } | ConvertTo-Json -Compress -Depth 4
}
catch {
    Write-Output ""
}
"""
    out = safe_run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        timeout=2.8,
    )
    if not out:
        return None, []
    try:
        payload = json.loads(out)
        usage = round(min(max(float(payload.get('usage', 0.0)), 0.0), 100.0), 2)
        samples = payload.get('samples', []) or []
        normalized = []
        if isinstance(samples, dict):
            samples = [samples]
        for item in samples:
            if not isinstance(item, dict):
                continue
            normalized.append({
                'name': str(item.get('name', '')).strip(),
                'value': round(float(item.get('value', 0.0)), 2),
            })
        return usage, normalized
    except Exception:
        return None, []


def parse_gpu_usage_cim():
    ps_script = r"""
$ErrorActionPreference = 'Stop'
try {
    $rows = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine |
        Select-Object Name, UtilizationPercentage
    if (-not $rows) {
        [pscustomobject]@{ usage = 0; samples = @() } | ConvertTo-Json -Compress -Depth 4
        exit
    }

    $filtered = $rows | Where-Object {
        $_.Name -match 'engtype_3D|engtype_Compute|engtype_VideoDecode|engtype_VideoProcessing|engtype_Copy'
    }
    if (-not $filtered -or $filtered.Count -eq 0) {
        $filtered = $rows
    }

    $collapsed = @()
    foreach ($group in ($filtered | Group-Object Name)) {
        $maxVal = ($group.Group | Measure-Object -Property UtilizationPercentage -Maximum).Maximum
        if ($null -eq $maxVal) { $maxVal = 0 }
        $collapsed += [pscustomobject]@{ name = $group.Name; value = [math]::Round([double]$maxVal, 2) }
    }

    $sumVal = ($collapsed | Measure-Object -Property value -Sum).Sum
    $maxVal = ($collapsed | Measure-Object -Property value -Maximum).Maximum
    if ($null -eq $sumVal) { $sumVal = 0 }
    if ($null -eq $maxVal) { $maxVal = 0 }
    $usage = [math]::Round([math]::Max([double]$sumVal, [double]$maxVal), 2)
    if ($usage -gt 100) { $usage = 100 }
    if ($usage -lt 0) { $usage = 0 }

    [pscustomobject]@{ usage = $usage; samples = ($collapsed | Sort-Object value -Descending | Select-Object -First 12) } | ConvertTo-Json -Compress -Depth 4
}
catch {
    Write-Output ""
}
"""
    out = safe_run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        timeout=2.8,
    )
    if not out:
        return None, []
    try:
        payload = json.loads(out)
        usage = round(min(max(float(payload.get('usage', 0.0)), 0.0), 100.0), 2)
        samples = payload.get('samples', []) or []
        normalized = []
        if isinstance(samples, dict):
            samples = [samples]
        for item in samples:
            if not isinstance(item, dict):
                continue
            normalized.append({
                'name': str(item.get('name', '')).strip(),
                'value': round(float(item.get('value', 0.0)), 2),
            })
        return usage, normalized
    except Exception:
        return None, []


def parse_gpu_usage_typeperf():
    out = safe_run(
        ["typeperf", r"\GPU Engine(*)\Utilization Percentage", "-sc", "1"],
        timeout=2.4,
    )
    if not out:
        return None
    try:
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 3:
            return None
        values = []
        for line in lines[2:]:
            parts = [p.strip().strip('"') for p in line.split(',')]
            for val in parts[1:]:
                try:
                    num = float(val)
                    if num >= 0:
                        values.append(num)
                except Exception:
                    pass
        if not values:
            return 0.0
        total = sum(values)
        return round(min(total, 100.0), 2)
    except Exception:
        return None


def update_gpu_usage_cache():
    global cached_gpu_usage, last_gpu_scan_time, gpu_method, last_gpu_debug_samples
    gpu_name = SYSTEM_INFO["gpu_name"].lower()
    gpu_usage = None
    method_used = "fallback_zero"
    samples = []

    if 'nvidia' in gpu_name:
        gpu_usage = parse_gpu_usage_nvidia()
        if gpu_usage is not None:
            method_used = 'nvidia-smi'

    if gpu_usage is None:
        gpu_usage, samples = parse_gpu_usage_powershell()
        if gpu_usage is not None:
            method_used = 'powershell-get-counter'

    if gpu_usage is None:
        gpu_usage, samples = parse_gpu_usage_cim()
        if gpu_usage is not None:
            method_used = 'powershell-cim-gpuengine'

    if gpu_usage is None:
        gpu_usage = parse_gpu_usage_typeperf()
        if gpu_usage is not None:
            method_used = 'typeperf'
            samples = []

    if gpu_usage is None:
        gpu_usage = 0.0

    with gpu_lock:
        cached_gpu_usage = round(min(max(gpu_usage, 0.0), 100.0), 2)
        last_gpu_scan_time = time.time()
        gpu_method = method_used
        last_gpu_debug_samples = samples


def get_gpu_usage_cached():
    with gpu_lock:
        return cached_gpu_usage


def calculate_power_components(cpu_percent, gpu_percent, ram_percent, disk_percent, internet_kbps, plugged_in):
    gpu_name = SYSTEM_INFO['gpu_name'].lower()
    has_battery = SYSTEM_INFO['has_battery']
    base_system = 5.5
    display = 4.0 if has_battery else 2.5
    keyboard_touchpad = 0.9 if has_battery else 0.3
    motherboard_fans = 1.8
    cpu_part = (cpu_percent / 100.0) * 32.0
    gpu_scale = 30.0 if any(x in gpu_name for x in ['nvidia', 'amd', 'radeon']) else 14.0
    gpu_part = (gpu_percent / 100.0) * gpu_scale
    ram_part = (ram_percent / 100.0) * 6.0
    disk_part = (disk_percent / 100.0) * 3.0
    network_part = min((internet_kbps / 1024.0) * 1.2, 2.5)
    charging_overhead = 1.5 if plugged_in else 0.0
    comp = {
        "base_system": round(base_system, 2),
        "display": round(display, 2),
        "keyboard_touchpad": round(keyboard_touchpad, 2),
        "motherboard_fans": round(motherboard_fans, 2),
        "cpu": round(cpu_part, 2),
        "gpu": round(gpu_part, 2),
        "ram": round(ram_part, 2),
        "disk": round(disk_part, 2),
        "network": round(network_part, 2),
        "charging_overhead": round(charging_overhead, 2),
    }
    comp['total'] = round(sum(comp.values()), 2)
    return comp


def get_power_status(power_w):
    return 'Optimized' if power_w < 25 else ('Good' if power_w < 45 else 'High')


def predict_next_energy_wh():
    data = list(energy_rate_hist)
    if not data:
        return 0.0
    recent = data[-5:] if len(data) >= 5 else data
    avg_rate = sum(recent) / len(recent)
    return round(max(avg_rate, 0.0), 6)


def predict_co2_for_minutes(minutes, current_power_w):
    recent_power = bounded_tail(power_hist, 30)
    avg_power = sum(recent_power) / len(recent_power) if recent_power else current_power_w
    return round((avg_power / 1000.0) * CARBON_FACTOR_KG_PER_KWH * (minutes / 60.0), 6)


def calculate_efficiency(cpu, gpu, power_w, ram, battery_percent, plugged_in):
    score = 100.0 - cpu * 0.18 - gpu * 0.12 - max(power_w - 22, 0) * 0.72 - max(ram - 70, 0) * 0.16
    if battery_percent is not None and battery_percent < 25:
        score -= 4
    if plugged_in:
        score -= 2
    score = max(0, min(100, round(score, 1)))
    if score >= 80:
        return score, 'Efficient', 'good'
    if score >= 50:
        return score, 'Moderate', 'moderate'
    return score, 'Poor', 'poor'


def detect_power_spike(current_power):
    recent = bounded_tail(power_hist, 12)
    if len(recent) < 8:
        return False
    return current_power > (sum(recent) / len(recent) + 6.0)


def get_top_processes_cached():
    global cached_top_processes, last_process_scan_time
    now = time.time()
    if now - last_process_scan_time < 5:
        return cached_top_processes

    rows = []
    cpu_count = max(psutil.cpu_count(logical=True) or 1, 1)
    for proc in psutil.process_iter(['pid', 'name', 'memory_percent']):
        try:
            name = (proc.info.get('name') or 'Unknown').strip()
            if name.lower() in IGNORED_PROCESS_NAMES:
                continue
            raw_cpu = proc.cpu_percent(None)
            cpu_val = round(min(max(raw_cpu / cpu_count, 0.0), 100.0), 2)
            ram_val = round(proc.info.get('memory_percent') or 0.0, 2)
            if cpu_val <= 0.2 and ram_val <= 0.5:
                continue
            rows.append({
                'name': name,
                'cpu': cpu_val,
                'ram': ram_val,
                'score': round(cpu_val * 0.72 + ram_val * 0.28, 2),
            })
        except Exception:
            pass

    rows.sort(key=lambda x: x['score'], reverse=True)
    cached_top_processes = rows[:3]
    last_process_scan_time = now
    return cached_top_processes


def build_dynamic_advisor(cpu, gpu, ram, power_w, battery_percent, plugged_in, top_processes):
    tips = []
    for proc in top_processes:
        pname, pcpu, pram = proc['name'], proc['cpu'], proc['ram']
        lname = pname.lower()
        if lname in ['chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe'] and pram >= 8:
            tips.append(f"{pname} is using {pram:.1f}% RAM -> Close unused tabs to save power.")
            continue
        if pcpu >= 18:
            tips.append(f"{pname} is using {pcpu:.1f}% CPU -> Close or restart it to reduce energy usage.")
            continue
        if pram >= 8:
            tips.append(f"{pname} is using {pram:.1f}% RAM -> Close non-working tabs or unused windows.")
    if cpu > 80:
        tips.append('CPU spike detected -> Close heavy apps or stop extra background tasks.')
    if gpu > 80:
        tips.append('GPU usage is very high -> Close graphics-heavy apps if not needed.')
    if ram > 85:
        tips.append('System RAM is very high -> Reduce memory-heavy apps for smoother performance.')
    if power_w > 50:
        tips.append('Power usage is high -> Lower brightness and close unnecessary apps.')
    if battery_percent is not None and battery_percent < 20 and not plugged_in:
        tips.append('Battery is low -> Turn on battery saver and close heavy apps now.')
    unique = []
    for tip in tips:
        if tip not in unique:
            unique.append(tip)
    return unique[:3] if unique else ['System is running efficiently right now. Keep unnecessary background apps closed.']


def generate_alerts(cpu, gpu, power_w, battery_percent, internet_kbps, power_spike):
    alerts = []
    if cpu > 80:
        alerts.append(f'High CPU alert: {cpu}% CPU usage detected.')
    if gpu > 80:
        alerts.append(f'High GPU alert: {gpu}% GPU usage detected.')
    if power_w > 50:
        alerts.append(f'High power usage alert: {power_w} W detected.')
    if power_spike:
        alerts.append('Power spike alert: sudden jump in power usage detected.')
    if battery_percent is not None and battery_percent < 20:
        alerts.append(f'Low battery alert: battery is at {battery_percent}%.')
    if internet_kbps > 4000:
        alerts.append('Heavy network activity detected.')
    return alerts if alerts else ['System status normal.']


def get_history_slice(seconds):
    seconds = max(1, min(seconds, 300))
    return {
        "labels": bounded_tail(time_hist, seconds),
        "cpu": bounded_tail(cpu_hist, seconds),
        "gpu": bounded_tail(gpu_hist, seconds),
        "power": bounded_tail(power_hist, seconds),
        "energy_total": bounded_tail(energy_total_hist, seconds),
        "energy_rate": bounded_tail(energy_rate_hist, seconds),
        "carbon_rate": bounded_tail(carbon_rate_hist, seconds),
        "ram": bounded_tail(ram_hist, seconds),
        "disk": bounded_tail(disk_hist, seconds),
        "internet": bounded_tail(internet_hist, seconds),
        "display_step_seconds": 1,
    }


def avg_from_start(key):
    if session_sample_count <= 0:
        return 0.0
    return round(session_sums[key] / session_sample_count, 4)


def build_summary_rows():
    rows = []
    summary_windows = [
        ('Last 1 Min Average', 60),
        ('Last 5 Min Average', 300),
        ('Last 10 Min Average', 600),
        ('Last 30 Min Average', 1800),
        ('Last 1 Hour Average', 3600),
    ]
    for label, seconds in summary_windows:
        rows.append({
            'record_type': label,
            'timestamp': '',
            'cpu_percent': rolling_average(cpu_hist, seconds),
            'gpu_percent': rolling_average(gpu_hist, seconds),
            'ram_percent': rolling_average(ram_hist, seconds),
            'disk_percent': rolling_average(disk_hist, seconds),
            'power_w': rolling_average(power_hist, seconds),
            'energy_total_wh': rolling_total(energy_rate_hist, seconds),
            'energy_rate_wh_per_sec': rolling_average(energy_rate_hist, seconds),
            'carbon_rate_kg_per_hr': rolling_average(carbon_rate_hist, seconds),
            'internet_kbps': rolling_average(internet_hist, seconds),
        })

    rows.append({
        'record_type': 'Average From Start Of App',
        'timestamp': '',
        'cpu_percent': avg_from_start('cpu'),
        'gpu_percent': avg_from_start('gpu'),
        'ram_percent': avg_from_start('ram'),
        'disk_percent': avg_from_start('disk'),
        'power_w': avg_from_start('power'),
        'energy_total_wh': round(energy_total_wh, 4),
        'energy_rate_wh_per_sec': avg_from_start('energy_rate'),
        'carbon_rate_kg_per_hr': avg_from_start('carbon_rate'),
        'internet_kbps': avg_from_start('internet'),
    })
    return rows


def sample_once():
    global energy_total_wh, carbon_total_kg, latest_dynamic_data, session_sample_count

    cpu = round(psutil.cpu_percent(interval=None), 2)
    gpu = get_gpu_usage_cached()
    ram = get_ram_percent()
    disk = get_disk_percent()

    battery_data = get_battery_info()
    battery_percent = battery_data['percent']
    battery_status = battery_data['status']
    battery_time_text = battery_data['time_text']
    plugged_in = battery_data['plugged_in']

    internet_kbps = get_network_speed()
    wifi_status, ethernet_status = get_interface_info()

    power_breakdown = calculate_power_components(cpu, gpu, ram, disk, internet_kbps, plugged_in)
    power_w = power_breakdown['total']
    power_status = get_power_status(power_w)

    energy_this_second_wh = power_w / 3600.0
    energy_total_wh += energy_this_second_wh

    carbon_rate_kg_per_hr = (power_w / 1000.0) * CARBON_FACTOR_KG_PER_KWH
    carbon_this_second_kg = carbon_rate_kg_per_hr / 3600.0
    carbon_total_kg += carbon_this_second_kg

    now_epoch = int(time.time())
    current_second = time.strftime('%H:%M:%S')
    power_spike = detect_power_spike(power_w)

    cpu_hist.append(cpu)
    gpu_hist.append(gpu)
    power_hist.append(round(power_w, 2))
    energy_total_hist.append(round(energy_total_wh, 4))
    energy_rate_hist.append(round(energy_this_second_wh, 6))
    carbon_rate_hist.append(round(carbon_rate_kg_per_hr, 6))
    ram_hist.append(ram)
    disk_hist.append(disk)
    internet_hist.append(internet_kbps)
    time_hist.append(current_second)
    epoch_hist.append(now_epoch)

    session_sample_count += 1
    session_sums['cpu'] += cpu
    session_sums['gpu'] += gpu
    session_sums['ram'] += ram
    session_sums['disk'] += disk
    session_sums['power'] += power_w
    session_sums['energy_total'] += energy_total_wh
    session_sums['energy_rate'] += energy_this_second_wh
    session_sums['carbon_rate'] += carbon_rate_kg_per_hr
    session_sums['internet'] += internet_kbps

    efficiency_score, efficiency_label, efficiency_class = calculate_efficiency(cpu, gpu, power_w, ram, battery_percent, plugged_in)
    top_processes = get_top_processes_cached()
    alerts = generate_alerts(cpu, gpu, power_w, battery_percent, internet_kbps, power_spike)
    advisor = build_dynamic_advisor(cpu, gpu, ram, power_w, battery_percent, plugged_in, top_processes)

    with data_lock:
        latest_dynamic_data = {
            **SYSTEM_INFO,
            'tooltips': TOOLTIPS,
            'cpu': cpu,
            'gpu': gpu,
            'gpu_method': gpu_method,
            'power': round(power_w, 2),
            'power_status': power_status,
            'energy_total': round(energy_total_wh, 4),
            'energy_rate': round(energy_this_second_wh, 6),
            'carbon_rate': round(carbon_rate_kg_per_hr, 6),
            'carbon_total': round(carbon_total_kg, 6),
            'predicted_co2_1min': predict_co2_for_minutes(1, power_w),
            'predicted_co2_5min': predict_co2_for_minutes(5, power_w),
            'ram': ram,
            'disk': disk,
            'battery': battery_percent,
            'battery_status': battery_status,
            'battery_time_text': battery_time_text,
            'internet': internet_kbps,
            'wifi_status': wifi_status,
            'ethernet_status': ethernet_status,
            'future_energy': predict_next_energy_wh(),
            'efficiency_score': efficiency_score,
            'efficiency_label': efficiency_label,
            'efficiency_class': efficiency_class,
            'alerts': alerts,
            'advisor': advisor,
            'top_processes': top_processes,
            'power_breakdown': power_breakdown,
            'last_updated': current_second,
            'last_updated_epoch': now_epoch,
        }


def monitor_loop():
    while True:
        start = time.time()
        try:
            sample_once()
        except Exception as e:
            print('Monitor loop error:', e)
        sleep_left = 1.0 - (time.time() - start)
        if sleep_left > 0:
            time.sleep(sleep_left)


def gpu_monitor_loop():
    while True:
        start = time.time()
        try:
            update_gpu_usage_cache()
        except Exception as e:
            print('GPU monitor error:', e)
        sleep_left = 1.0 - (time.time() - start)
        if sleep_left > 0:
            time.sleep(sleep_left)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/data')
def data():
    ensure_export_dirs()
    window = request.args.get('window', default=60, type=int)
    with data_lock:
        dynamic = dict(latest_dynamic_data)
    dynamic['history'] = get_history_slice(window)
    labels = dynamic['history'].get('labels', [])

    metric_key = request.args.get('metric', default='cpu', type=str)
    history = dynamic['history']
    if metric_key == 'gpu':
        metric_data = history.get('gpu', [])
    elif metric_key == 'memory_disk':
        metric_data = [max(history.get('ram', [0])[i], history.get('disk', [0])[i]) for i in range(min(len(history.get('ram', [])), len(history.get('disk', []))))]
    elif metric_key == 'power':
        metric_data = history.get('power', [])
    elif metric_key == 'energy_total':
        metric_data = history.get('energy_total', [])
    elif metric_key == 'carbon_rate':
        metric_data = history.get('carbon_rate', [])
    else:
        metric_data = history.get('cpu', [])

    peak_value = max(metric_data) if metric_data else 0
    peak_time = '--:--:--'
    if metric_data:
        idx = metric_data.index(peak_value)
        if 0 <= idx < len(labels):
            peak_time = labels[idx]
    dynamic['peak_metric_value'] = peak_value
    dynamic['peak_metric_time'] = peak_time
    return jsonify(dynamic)


@app.route('/debug_gpu')
def debug_gpu():
    ps_usage, ps_samples = parse_gpu_usage_powershell()
    cim_usage, cim_samples = parse_gpu_usage_cim()
    return jsonify({
        'gpu_name': SYSTEM_INFO.get('gpu_name', 'Unknown'),
        'cached_gpu_usage': get_gpu_usage_cached(),
        'last_gpu_scan_time': last_gpu_scan_time,
        'gpu_method': gpu_method,
        'nvidia_value': parse_gpu_usage_nvidia(),
        'powershell_value': ps_usage,
        'powershell_samples': ps_samples,
        'cim_value': cim_usage,
        'cim_samples': cim_samples,
        'typeperf_value': parse_gpu_usage_typeperf(),
        'latest_cached_samples': last_gpu_debug_samples,
    })


@app.route('/open_export_folder', methods=['POST'])
def open_export_folder():
    try:
        _, desktop_recent_dir = get_desktop_power_monitor_dir()
        os.makedirs(desktop_recent_dir, exist_ok=True)
        if os.name == "nt":
            os.startfile(desktop_recent_dir)
        else:
            subprocess.Popen(["xdg-open", desktop_recent_dir])
        return jsonify({"ok": True, "path": desktop_recent_dir})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route('/export')
def export():
    os.makedirs(RECENT_DATA_DIR, exist_ok=True)
    _, desktop_recent_dir = get_desktop_power_monitor_dir()
    os.makedirs(desktop_recent_dir, exist_ok=True)
    ts = time.strftime('%Y-%m-%d_%H-%M-%S')

    summary_rows = build_summary_rows()
    blank = {
        'record_type': '',
        'timestamp': '',
        'cpu_percent': '',
        'gpu_percent': '',
        'ram_percent': '',
        'disk_percent': '',
        'power_w': '',
        'energy_total_wh': '',
        'energy_rate_wh_per_sec': '',
        'carbon_rate_kg_per_hr': '',
        'internet_kbps': '',
    }

    history_rows = []
    sample_len = len(time_hist)
    for i in range(sample_len):
        history_rows.append({
            'record_type': 'history_since_app_start',
            'timestamp': time_hist[i],
            'cpu_percent': cpu_hist[i],
            'gpu_percent': gpu_hist[i],
            'ram_percent': ram_hist[i],
            'disk_percent': disk_hist[i],
            'power_w': power_hist[i],
            'energy_total_wh': energy_total_hist[i],
            'energy_rate_wh_per_sec': energy_rate_hist[i],
            'carbon_rate_kg_per_hr': carbon_rate_hist[i],
            'internet_kbps': internet_hist[i],
        })

    if not history_rows:
        history_rows.append({
            'record_type': 'history',
            'timestamp': ts,
            'cpu_percent': 0,
            'gpu_percent': 0,
            'ram_percent': 0,
            'disk_percent': 0,
            'power_w': 0,
            'energy_total_wh': 0,
            'energy_rate_wh_per_sec': 0,
            'carbon_rate_kg_per_hr': 0,
            'internet_kbps': 0,
        })

    session_info_rows = [{
        'record_type': 'Session Start',
        'timestamp': session_start_text,
        'cpu_percent': '',
        'gpu_percent': '',
        'ram_percent': '',
        'disk_percent': '',
        'power_w': '',
        'energy_total_wh': '',
        'energy_rate_wh_per_sec': '',
        'carbon_rate_kg_per_hr': '',
        'internet_kbps': '',
    }, {
        'record_type': 'Export Time',
        'timestamp': ts.replace('_', ' '),
        'cpu_percent': '',
        'gpu_percent': '',
        'ram_percent': '',
        'disk_percent': '',
        'power_w': '',
        'energy_total_wh': '',
        'energy_rate_wh_per_sec': '',
        'carbon_rate_kg_per_hr': '',
        'internet_kbps': '',
    }, {
        'record_type': 'Samples From App Start',
        'timestamp': sample_len,
        'cpu_percent': '',
        'gpu_percent': '',
        'ram_percent': '',
        'disk_percent': '',
        'power_w': '',
        'energy_total_wh': '',
        'energy_rate_wh_per_sec': '',
        'carbon_rate_kg_per_hr': '',
        'internet_kbps': '',
    }]

    df = pd.DataFrame(session_info_rows + [blank] + summary_rows + [blank] + history_rows)
    path = os.path.join(RECENT_DATA_DIR, f'power_history_{ts}.csv')
    desktop_path = os.path.join(desktop_recent_dir, f'power_history_{ts}.csv')
    df.to_csv(path, index=False)
    df.to_csv(desktop_path, index=False)
    return send_file(path, as_attachment=True)


from waitress import serve

def start_server():
    ensure_export_dirs()
    init_static_info()
    psutil.cpu_percent(interval=None)
    update_gpu_usage_cache()
    sample_once()
    threading.Thread(target=gpu_monitor_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    serve(app, host='127.0.0.1', port=5000, threads=8)

if __name__ == '__main__':
    start_server()