from collections import OrderedDict
import html
import json
import keyword
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlparse

import sublime
import sublime_plugin


SETTINGS_FILE = "LiuConda.sublime-settings"
MAIN_MENU_FILE = os.path.join(os.path.dirname(__file__), "Main.sublime-menu")
JEDI_WORKER_FILE = os.path.join(os.path.dirname(__file__), "LiuCondaJediWorker.py")
WINDOWS_LAUNCHER_FILE = os.path.join(os.path.dirname(__file__), "LiuCondaWindowsLauncher.py")
BACKENDS = ("conda", "micromamba")
LOGGER = logging.getLogger(__name__)
_signature_requests = {}
_completion_cache = OrderedDict()
_completion_waiters = {}
_hover_requests = {}
_hover_documentation_cache = OrderedDict()
_pending_run_views = set()
_source_cache = OrderedDict()
_backend_executable_cache = {}
_env_path_cache = {}
_zuban_status_cache = {}
_process_jobs = {}
_detached_workers = set()
_state_lock = threading.Lock()
_worker_lock = threading.RLock()
_zuban_lock = threading.RLock()
_jedi_worker = None
_zuban_worker = None
HOVER_CACHE_LIMIT = 128
SOURCE_CACHE_LIMIT = 16
COMPLETION_CACHE_LIMIT = 32
WORKER_SHUTDOWN_TIMEOUT = 2
ZUBAN_SHUTDOWN_TIMEOUT = 2
DEFAULT_RUNTIME_DOCUMENTATION_IMPORT_WHITELIST = ["numpy"]


def plugin_loaded():
    _ensure_menu()
    _check_jedi_async()
    _check_zuban_async()


def plugin_unloaded():
    _stop_jedi_worker(wait=True)
    _stop_zuban_worker(wait=True)
    with _state_lock:
        _pending_run_views.clear()
    _stop_detached_workers()
    _clear_process_jobs()


def _settings():
    return sublime.load_settings(SETTINGS_FILE)


def _save_settings():
    sublime.save_settings(SETTINGS_FILE)


def _backend():
    value = _settings().get("backend", "conda")
    return value if value in BACKENDS else "conda"


def _selected_env():
    return _settings().get("selected_env", "")


def _jedi_available():
    return bool(_settings().get("jedi_available", False))


def _zuban_available():
    return bool(_settings().get("zuban_available", False))


def _hover_documentation_enabled():
    return bool(_settings().get("hover_documentation", False))


def _global_path_search_enabled():
    return bool(_settings().get("global_path_search", False))


def _envs():
    value = _settings().get("envs", [])
    return value if isinstance(value, list) else []


def _backend_executable(backend):
    configured = _configured_executable(backend)
    cache_key = _backend_executable_cache_key(backend, configured)
    cached = _cached_backend_executable(cache_key)
    if cached:
        return cached

    if configured:
        resolved = _resolve_executable(configured)
        if resolved:
            _store_backend_executable(cache_key, resolved)
            return resolved
        raise FileNotFoundError(_invalid_configured_executable_message(backend, configured))

    found = _which(backend)
    if found:
        _store_backend_executable(cache_key, found)
        return found

    for candidate in _candidate_executables(backend):
        if _is_executable(candidate):
            _store_backend_executable(cache_key, candidate)
            return candidate

    raise FileNotFoundError(_not_found_message(backend))


def _backend_executable_cache_key(backend, configured):
    return (
        backend,
        configured,
        _global_path_search_enabled(),
        os.environ.get("PATH", ""),
    )


def _cached_backend_executable(cache_key):
    with _state_lock:
        cached = _backend_executable_cache.get(cache_key)
    if cached and _is_executable(cached):
        return cached
    return ""


def _store_backend_executable(cache_key, executable):
    with _state_lock:
        _backend_executable_cache[cache_key] = executable


def _clear_backend_executable_cache():
    with _state_lock:
        _backend_executable_cache.clear()


def _clear_zuban_status_cache():
    with _state_lock:
        _zuban_status_cache.clear()


def _cached_zuban_status(cache_key):
    if cache_key is None:
        return ""
    with _state_lock:
        return _zuban_status_cache.get(cache_key, "")


def _store_zuban_status(cache_key, message):
    if cache_key is None or not message:
        return
    with _state_lock:
        _zuban_status_cache[cache_key] = message


def _resolve_executable(value):
    expanded = os.path.expandvars(os.path.expanduser(value))
    if _is_executable(expanded):
        return expanded

    found = _which(expanded)
    if found:
        return found

    return ""


def _configured_executable(backend):
    settings = _settings()

    paths = settings.get("backend_executables", {})
    if isinstance(paths, dict):
        value = paths.get(backend, "")
        if value:
            return os.path.expandvars(os.path.expanduser(value))

    value = settings.get("{}_executable".format(backend), "")
    if value:
        return os.path.expandvars(os.path.expanduser(value))

    return ""


def _which(command):
    found = shutil.which(command)
    if found:
        return found

    if os.name == "nt":
        for suffix in (".exe", ".bat", ".cmd"):
            found = shutil.which(command + suffix)
            if found:
                return found

    return ""


def _candidate_executables(backend):
    home = os.path.expanduser("~")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    program_files_roots = _program_files_roots()
    platform_roots = _platform_backend_roots(backend, home)

    if backend == "micromamba":
        roots = [
            os.environ.get("MAMBA_ROOT_PREFIX", ""),
            os.environ.get("MAMBA_EXE", ""),
            os.environ.get("MICROMAMBA_EXE", ""),
            os.path.join(home, "micromamba"),
            os.path.join(local_app_data, "micromamba"),
            os.path.join(program_data, "micromamba"),
        ]
        roots.extend(platform_roots)
        for root in program_files_roots:
            roots.extend(
                [
                    os.path.join(root, "micromamba"),
                    os.path.join(root, "mambaforge"),
                    os.path.join(root, "miniforge3"),
                ]
            )

        if _global_path_search_enabled():
            for drive in _windows_drive_roots():
                roots.append(os.path.join(drive, "micromamba"))

        return _expand_backend_roots(
            roots,
            [
                ("condabin", "micromamba.bat"),
                ("condabin", "micromamba.exe"),
                ("condabin", "micromamba"),
                ("Scripts", "micromamba.exe"),
                ("Library", "bin", "micromamba.exe"),
                ("bin", "micromamba"),
                ("sbin", "micromamba"),
                ("micromamba.exe",),
                ("micromamba.bat",),
                ("micromamba",),
            ],
        )

    roots = [
        os.environ.get("CONDA_PREFIX", ""),
        os.environ.get("CONDA_ROOT", ""),
        os.environ.get("CONDA_EXE", ""),
        os.path.join(home, "miniconda3"),
        os.path.join(home, "anaconda3"),
        os.path.join(home, "miniforge3"),
        os.path.join(home, "mambaforge"),
        os.path.join(local_app_data, "miniconda3"),
        os.path.join(local_app_data, "anaconda3"),
        os.path.join(program_data, "miniconda3"),
        os.path.join(program_data, "anaconda3"),
    ]
    roots.extend(platform_roots)
    for root in program_files_roots:
        roots.extend(
            [
                os.path.join(root, "miniconda3"),
                os.path.join(root, "anaconda3"),
                os.path.join(root, "Miniconda3"),
                os.path.join(root, "Anaconda3"),
                os.path.join(root, "miniforge3"),
                os.path.join(root, "mambaforge"),
            ]
        )
    if _global_path_search_enabled():
        for drive in _windows_drive_roots():
            roots.extend(
                [
                    os.path.join(drive, "miniconda3"),
                    os.path.join(drive, "anaconda3"),
                    os.path.join(drive, "Miniconda3"),
                    os.path.join(drive, "Anaconda3"),
                ]
            )

    return _expand_backend_roots(
        roots,
        [
            ("condabin", "conda.bat"),
            ("condabin", "conda.exe"),
            ("condabin", "conda"),
            ("Scripts", "conda.exe"),
            ("Scripts", "conda.bat"),
            ("bin", "conda"),
            ("conda.exe",),
            ("conda.bat",),
            ("conda",),
        ],
    )


def _platform_backend_roots(backend, home):
    roots = []
    if os.name == "nt":
        return roots

    common_roots = [
        os.path.join(home, ".local"),
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".conda"),
        os.path.join(home, "opt"),
        "/opt",
        "/usr/local",
        "/usr/local/bin",
        "/opt/homebrew",
        "/opt/homebrew/bin",
    ]

    if backend == "micromamba":
        roots.extend(
            [
                os.path.join(home, ".micromamba"),
                os.path.join(home, ".local", "share", "mamba"),
                os.path.join(home, ".local", "bin"),
                "/usr/local/bin",
                "/opt/homebrew/bin",
                "/opt/micromamba",
            ]
        )
    else:
        for base in common_roots:
            roots.extend(
                [
                    os.path.join(base, "miniconda3"),
                    os.path.join(base, "anaconda3"),
                    os.path.join(base, "miniforge3"),
                    os.path.join(base, "mambaforge"),
                ]
            )
        roots.extend(
            [
                "/Applications/anaconda3",
                "/Applications/miniconda3",
                "/Applications/miniforge3",
                "/usr/local/Caskroom/miniconda/base",
                "/usr/local/Caskroom/miniforge/base",
                "/opt/homebrew/Caskroom/miniconda/base",
                "/opt/homebrew/Caskroom/miniforge/base",
            ]
        )

    return roots


def _program_files_roots():
    if os.name != "nt":
        return []

    roots = []
    names = ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")
    for name in names:
        value = os.environ.get(name, "")
        if value and value not in roots:
            roots.append(value)

    drives = ["{}:\\".format(letter) for letter in "CDEF"]
    if _global_path_search_enabled():
        drives = _windows_drive_roots()

    for drive in drives:
        for suffix in ("Program Files", "Program Files (x86)"):
            path = os.path.join(drive, suffix)
            if os.path.isdir(path) and path not in roots:
                roots.append(path)

    return roots


def _expand_backend_roots(roots, suffixes):
    candidates = []
    seen = set()

    for root in roots:
        if not root:
            continue

        expanded = os.path.expandvars(os.path.expanduser(root))
        if os.path.isfile(expanded):
            paths = [expanded]
        else:
            paths = [os.path.join(expanded, *suffix) for suffix in suffixes]

        for path in paths:
            key = os.path.normcase(os.path.normpath(path))
            if key not in seen:
                seen.add(key)
                candidates.append(path)

    return candidates


def _windows_drive_roots():
    if os.name != "nt":
        return []

    roots = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "{}:\\".format(letter)
        if os.path.isdir(root):
            roots.append(root)
    return roots


def _is_executable(path):
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _not_found_message(backend):
    setting_name = "backend_executables.{}".format(backend)
    return (
        "LiuConda could not find '{}'. Add it to PATH or set '{}' in "
        "LiuConda.sublime-settings."
    ).format(backend, setting_name)


def _invalid_configured_executable_message(backend, configured):
    return (
        "LiuConda configured '{}' executable does not exist or is not a file:\n{}"
    ).format(backend, configured)


def _python_command(env=None):
    return _environment_command(_settings().get("python", "python"), env)


def _zuban_command(env=None):
    return _environment_command(_zuban_target_command(env), env)


def _zuban_target_command(env=None):
    command = _settings().get("zuban", "zuban")
    if command == "zuban":
        preferred = _env_script_executable("zuban", env)
        if preferred:
            return preferred
    return command


def _zuban_status_cache_key(env=None):
    env_name = env or _selected_env()
    if not env_name:
        return None

    command = _zuban_target_command(env_name)
    normalized = os.path.normcase(os.path.normpath(command)) if command else ""
    mtime = 0.0
    if command and os.path.isfile(command):
        try:
            mtime = os.path.getmtime(command)
        except OSError:
            mtime = 0.0
    return (env_name, normalized, mtime)


def _env_script_executable(name, env=None):
    env_path = _env_path(env)
    if not env_path:
        return ""

    candidates = []
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(env_path, "Scripts", name + ".exe"),
                os.path.join(env_path, "Scripts", name + ".bat"),
                os.path.join(env_path, "Scripts", name),
            ]
        )
    else:
        candidates.append(os.path.join(env_path, "bin", name))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _env_id(item):
    if not isinstance(item, dict):
        return ""
    return item.get("path") or item.get("name") or ""


def _find_env_record(env=None, envs=None):
    env_id = env or _selected_env()
    if not env_id:
        return None

    items = envs if envs is not None else _envs()
    legacy_match = None
    for item in items:
        if not isinstance(item, dict):
            continue

        path = item.get("path") or ""
        if path and _same_path(path, env_id):
            return item

        if legacy_match is None and item.get("name") == env_id:
            legacy_match = item

    return legacy_match


def _env_name_counts(envs):
    counts = {}
    for item in envs:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _env_display_name(item, name_counts=None):
    if not isinstance(item, dict):
        return ""

    name = item.get("name") or ""
    path = item.get("path") or ""
    if path and name_counts and name_counts.get(name, 0) > 1:
        return "{} ({})".format(name, path)
    return name or path


def _env_path(env=None):
    env_id = env or _selected_env()
    if env_id and os.path.isabs(env_id) and os.path.isdir(env_id):
        return env_id

    cached = _cached_env_path(env_id)
    if cached:
        return cached

    path = _configured_env_path(env_id)
    if path:
        _store_env_path(env_id, path)
        return path

    path = _refresh_env_path(env_id)
    if path:
        _store_env_path(env_id, path)
        return path
    return ""


def _configured_env_path(env_id):
    record = _find_env_record(env_id)
    if not record:
        return ""
    return record.get("path", "")


def _cached_env_path(env_id):
    if not env_id:
        return ""
    with _state_lock:
        path = _env_path_cache.get((_backend(), env_id), "")
    if path and os.path.isdir(path):
        return path
    return ""


def _store_env_path(env_id, path):
    if not env_id or not path:
        return
    with _state_lock:
        _env_path_cache[(_backend(), env_id)] = path


def _clear_env_path_cache():
    with _state_lock:
        _env_path_cache.clear()


def _discover_env_path(env_id):
    if not env_id:
        return ""
    try:
        envs = _discover_envs(_backend())
    except Exception:
        return ""
    record = _find_env_record(env_id, envs)
    return record.get("path", "") if record else ""


def _refresh_env_path(env_id):
    path, envs = _discover_env_path_with_records(env_id)
    if envs:
        _store_discovered_env_paths(envs)
        _schedule_env_refresh(envs)
    return path


def _discover_env_path_with_records(env_id):
    if not env_id:
        return "", []
    try:
        envs = _discover_envs(_backend())
    except Exception:
        return "", []

    record = _find_env_record(env_id, envs)
    path = record.get("path", "") if record else ""
    return path, envs


def _store_discovered_env_paths(envs):
    for item in envs:
        if not isinstance(item, dict):
            continue
        env_id = _env_id(item)
        path = item.get("path") or ""
        if env_id and path:
            _store_env_path(env_id, path)


def _schedule_env_refresh(envs):
    if envs == _envs():
        return

    def update():
        if envs != _envs():
            _set_envs(envs)
            _write_menu(envs)

    sublime.set_timeout(update, 0)


def _environment_command(command, env=None):
    env_id = env or _selected_env()
    if not env_id:
        raise FileNotFoundError("LiuConda: no environment is selected.")

    backend = _backend()
    executable = _backend_executable(backend)
    env_path = _env_path(env_id)
    if os.name == "nt" and backend == "micromamba":
        if not env_path:
            raise FileNotFoundError("LiuConda: refresh environments to resolve the selected environment path.")
        python = os.path.join(env_path, "python.exe")
        if not os.path.isfile(python):
            raise FileNotFoundError("LiuConda: the selected environment has no python.exe: {}".format(env_path))
        # micromamba run constructs CMD scripts and corrupts non-ASCII arguments.
        # The launcher activates through PowerShell, then starts native argv/stdio.
        return [python, "-I", "-u", WINDOWS_LAUNCHER_FILE, executable, env_path, "--", command]
    if env_path:
        return [executable, "run", "-p", env_path, command]
    return [executable, "run", "-n", env_id, command]


def _startupinfo():
    if os.name != "nt":
        return None

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _run_process(cmd, input_text=None, timeout=30):
    input_data = input_text.encode("utf-8") if input_text is not None else None
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=_startupinfo(),
    )
    try:
        output, _ = process.communicate(input_data, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            _kill_process_tree(process)
        else:
            _kill_process(process)
        try:
            output, _ = process.communicate(timeout=1)
        except Exception:
            output = error.output or ""
        error.output = _decode_process_output(output)
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, cmd, _decode_process_output(output))
    return _decode_process_output(output)


def _decode_process_output(output):
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace")
    return output


def _kill_process(process):
    if not process or process.poll() is not None:
        return

    try:
        process.kill()
    except Exception:
        return


def _terminate_process_tree(process):
    if not process:
        return

    if process.poll() is None:
        if os.name == "nt":
            _taskkill_process_tree(process.pid)
        else:
            _kill_unix_process_tree(process.pid)

    deadline = time.time() + WORKER_SHUTDOWN_TIMEOUT
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.05)

    if process.poll() is None:
        _kill_process_tree(process)

    _close_process_job(process)


def _kill_unix_process_tree(pid, sig=15):
    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.send_signal(sig)
            except psutil.NoSuchProcess:
                pass
        parent.send_signal(sig)
    except ImportError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    except psutil.NoSuchProcess:
        pass


def _kill_process_tree(process):
    if not process or process.poll() is not None:
        return

    if os.name == "nt":
        _taskkill_process_tree(process.pid)
        return

    _kill_unix_process_tree(process.pid, 9)


def _taskkill_process_tree(pid):
    try:
        subprocess.Popen(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_startupinfo(),
        ).communicate(timeout=5)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _register_process_job(process):
    if os.name != "nt" or not process:
        return

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        ok = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return

        process_handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)
        if not process_handle:
            kernel32.CloseHandle(job)
            return

        try:
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                kernel32.CloseHandle(job)
                return
        finally:
            kernel32.CloseHandle(process_handle)

        with _state_lock:
            _process_jobs[process.pid] = (kernel32, job)
    except Exception:
        LOGGER.exception("Failed to register process job for PID %s", getattr(process, "pid", ""))


def _close_process_job(process):
    if os.name != "nt" or not process:
        return

    with _state_lock:
        item = _process_jobs.pop(process.pid, None)
    if not item:
        return

    kernel32, job = item
    try:
        kernel32.CloseHandle(job)
    except Exception:
        pass


def _clear_process_jobs():
    if os.name != "nt":
        return

    with _state_lock:
        items = list(_process_jobs.values())
        _process_jobs.clear()
    for kernel32, job in items:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass


def _track_detached_worker(worker):
    with _state_lock:
        _detached_workers.add(worker)


def _untrack_detached_worker(worker):
    with _state_lock:
        _detached_workers.discard(worker)


def _stop_detached_workers():
    with _state_lock:
        workers = list(_detached_workers)
    for worker in workers:
        try:
            worker.stop()
        except Exception:
            LOGGER.exception("Failed to stop detached LiuConda worker")


def _stop_worker_async(worker, label, reason=""):
    if not worker:
        return

    _track_detached_worker(worker)

    def work():
        try:
            if reason:
                LOGGER.debug("Stopping LiuConda %s worker asynchronously: %s", label, reason)
            else:
                LOGGER.debug("Stopping LiuConda %s worker asynchronously", label)
            worker.stop()
        finally:
            _untrack_detached_worker(worker)

    threading.Thread(target=work, daemon=True).start()


def _queue_run_after_save(view):
    if not view:
        return
    with _state_lock:
        _pending_run_views.add(view.id())


def _consume_pending_run(view):
    if not view:
        return False
    with _state_lock:
        if view.id() not in _pending_run_views:
            return False
        _pending_run_views.discard(view.id())
        return True


def _run_python(args, input_data=None, timeout=10, env=None):
    payload = json.dumps(input_data) if input_data is not None else None
    return _run_process(_python_command(env) + args, input_text=payload, timeout=timeout)


def _run_worker(action, payload, timeout=8, env=None):
    if action == "documentation":
        payload = dict(payload)
        payload["runtime_import_whitelist"] = _runtime_documentation_import_whitelist()
    result = _request_jedi_worker(env or _selected_env(), action, payload, timeout)
    if result.get("error"):
        raise RuntimeError(result.get("error"))
    return result


def _runtime_documentation_import_whitelist():
    value = _settings().get(
        "runtime_documentation_import_whitelist",
        DEFAULT_RUNTIME_DOCUMENTATION_IMPORT_WHITELIST,
    )
    if not isinstance(value, list):
        return DEFAULT_RUNTIME_DOCUMENTATION_IMPORT_WHITELIST
    return [str(item) for item in value if str(item).strip()]


def _request_jedi_worker(env, action, payload, timeout):
    worker = _ensure_jedi_worker(env)
    return worker.request(action, payload, timeout)


def _retire_jedi_worker(worker, reason):
    global _jedi_worker

    with _worker_lock:
        if _jedi_worker is worker:
            _jedi_worker = None

    _track_detached_worker(worker)

    def work():
        try:
            LOGGER.warning("Retiring LiuConda Jedi worker: %s", reason)
            worker.stop()
        finally:
            _untrack_detached_worker(worker)

    threading.Thread(target=work, daemon=True).start()


def _ensure_jedi_worker(env):
    global _jedi_worker

    if not env:
        raise FileNotFoundError("LiuConda: no environment is selected.")

    with _worker_lock:
        if _jedi_worker and _jedi_worker.matches(env) and _jedi_worker.is_alive():
            return _jedi_worker

        old_worker = _jedi_worker
        _jedi_worker = None

    if old_worker:
        _stop_worker_async(old_worker, "Jedi", "replaced active worker")

    new_worker = _JediWorker(env)
    try:
        new_worker.start()
    except Exception:
        new_worker.stop()
        raise

    with _worker_lock:
        if _jedi_worker is None:
            _jedi_worker = new_worker
            return new_worker
        current_worker = _jedi_worker

    _stop_worker_async(new_worker, "Jedi", "discarded duplicate worker")
    if current_worker.matches(env) and current_worker.is_alive():
        return current_worker
    return _ensure_jedi_worker(env)


def _stop_jedi_worker(wait=True):
    global _jedi_worker

    with _worker_lock:
        worker = _jedi_worker
        _jedi_worker = None

    if worker:
        if wait:
            worker.stop()
        else:
            _stop_worker_async(worker, "Jedi", "stop requested")


class _JediWorker:
    def __init__(self, env):
        self.env = env
        self.process = None
        self.next_request_id = 1
        self.pending = {}
        self.lock = threading.RLock()
        self.reader = None
        self.stderr_reader = None
        self.unhealthy = False
        self.stopping = False

    def matches(self, env):
        return self.env == env

    def is_alive(self):
        return bool(not self.unhealthy and self.process and self.process.poll() is None)

    def start(self):
        cmd = _python_command(self.env) + [
            JEDI_WORKER_FILE,
            "--server",
            "--parent-pid",
            str(os.getpid()),
        ]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_startupinfo(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _register_process_job(self.process)
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()

    def request(self, action, payload, timeout):
        if not self.is_alive():
            raise RuntimeError("LiuConda Jedi worker is not running.")

        event = threading.Event()
        holder = {}
        with self.lock:
            if self.unhealthy:
                raise RuntimeError("LiuConda Jedi worker is restarting.")
            request_id = self.next_request_id
            self.next_request_id += 1
            self.pending[request_id] = (event, holder)
            message = {
                "id": request_id,
                "action": action,
                "payload": payload,
            }
            try:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
            except Exception:
                self.pending.pop(request_id, None)
                self.unhealthy = True
                _retire_jedi_worker(self, "write failed")
                raise

        if not event.wait(timeout):
            with self.lock:
                self.pending.pop(request_id, None)
                self.unhealthy = True
            _retire_jedi_worker(self, "{} timed out".format(action))
            raise TimeoutError("LiuConda Jedi worker timed out.")

        if holder.get("error"):
            raise RuntimeError(holder.get("error"))

        result = holder.get("result", {})
        if isinstance(result, dict):
            return result
        return {"error": "Invalid LiuConda Jedi worker response."}

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        process = self.process
        if not process:
            return
        self.unhealthy = True

        if process.poll() is None:
            try:
                request_id = self._next_shutdown_id()
                process.stdin.write(json.dumps({"id": request_id, "action": "shutdown"}) + "\n")
                process.stdin.flush()
            except Exception:
                pass

            deadline = time.time() + WORKER_SHUTDOWN_TIMEOUT
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.05)

        if process.poll() is None:
            _terminate_process_tree(process)
        else:
            _close_process_job(process)

        self._fail_pending("LiuConda Jedi worker stopped.")
        self._close_pipes()

    def _next_shutdown_id(self):
        with self.lock:
            request_id = self.next_request_id
            self.next_request_id += 1
            return request_id

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self._handle_response(line)
        except Exception:
            LOGGER.exception("LiuConda Jedi worker stdout reader failed")
        finally:
            self.unhealthy = True
            self._fail_pending("LiuConda Jedi worker exited.")
            _close_process_job(self.process)

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                LOGGER.debug("LiuConda Jedi worker stderr: %s", line.rstrip())
        except Exception:
            LOGGER.exception("LiuConda Jedi worker stderr reader failed")

    def _handle_response(self, line):
        try:
            response = json.loads(line)
        except Exception:
            LOGGER.warning("Invalid LiuConda Jedi worker response: %s", line.rstrip())
            return

        request_id = response.get("id")
        with self.lock:
            pending = self.pending.pop(request_id, None)
        if not pending:
            return

        event, holder = pending
        if response.get("error"):
            holder["error"] = response.get("error")
        else:
            holder["result"] = response.get("result", {})
        event.set()

    def _fail_pending(self, message):
        with self.lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for event, holder in pending:
            holder["error"] = message
            event.set()

    def _close_pipes(self):
        for stream in (
            getattr(self.process, "stdin", None),
            getattr(self.process, "stdout", None),
            getattr(self.process, "stderr", None),
        ):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass


def _run_zuban_worker(action, payload, timeout=10, env=None, root_path=""):
    result = _request_zuban_worker(env or _selected_env(), root_path, action, payload, timeout)
    if result.get("error"):
        raise RuntimeError(result.get("error"))
    return result


def _request_zuban_worker(env, root_path, action, payload, timeout):
    worker = _ensure_zuban_worker(env, root_path)
    return worker.request_action(action, payload, timeout)


def _retire_zuban_worker(worker, reason):
    global _zuban_worker

    with _zuban_lock:
        if _zuban_worker is worker:
            _zuban_worker = None

    _track_detached_worker(worker)

    def work():
        try:
            LOGGER.warning("Retiring LiuConda Zuban worker: %s", reason)
            worker.stop()
        finally:
            _untrack_detached_worker(worker)

    threading.Thread(target=work, daemon=True).start()


def _ensure_zuban_worker(env, root_path=""):
    global _zuban_worker

    if not env:
        raise FileNotFoundError("LiuConda: no environment is selected.")

    root_path = os.path.abspath(root_path) if root_path else ""
    with _zuban_lock:
        if _zuban_worker and _zuban_worker.matches(env, root_path) and _zuban_worker.is_alive():
            return _zuban_worker

        old_worker = _zuban_worker
        _zuban_worker = None

    if old_worker:
        _stop_worker_async(old_worker, "Zuban", "replaced active worker")

    new_worker = _ZubanWorker(env, root_path)
    try:
        new_worker.start()
    except Exception:
        new_worker.stop()
        raise

    with _zuban_lock:
        if _zuban_worker is None:
            _zuban_worker = new_worker
            return new_worker
        current_worker = _zuban_worker

    _stop_worker_async(new_worker, "Zuban", "discarded duplicate worker")
    if current_worker.matches(env, root_path) and current_worker.is_alive():
        return current_worker
    return _ensure_zuban_worker(env, root_path)


def _stop_zuban_worker(wait=True):
    global _zuban_worker

    with _zuban_lock:
        worker = _zuban_worker
        _zuban_worker = None

    if worker:
        if wait:
            worker.stop()
        else:
            _stop_worker_async(worker, "Zuban", "stop requested")


class _ZubanWorker:
    def __init__(self, env, root_path=""):
        self.env = env
        self.root_path = os.path.abspath(root_path) if root_path else ""
        self.process = None
        self.next_request_id = 1
        self.pending = {}
        self.diagnostic_inflight = set()
        self.diagnostic_waiters = {}
        self.open_documents = {}
        self.document_change_counts = {}
        self.latest_diagnostics = {}
        self.server_capabilities = {}
        self.server_info = {}
        self.last_error = ""
        self.lock = threading.RLock()
        self.reader = None
        self.stderr_reader = None
        self.unhealthy = False
        self.stopping = False

    def matches(self, env, root_path=""):
        root_path = os.path.abspath(root_path) if root_path else ""
        return self.env == env and _same_path(self.root_path, root_path)

    def is_alive(self):
        return bool(not self.unhealthy and self.process and self.process.poll() is None)

    def start(self):
        cmd = _zuban_command(self.env) + ["server"]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_startupinfo(),
        )
        _register_process_job(self.process)
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        self._initialize()

    def request_action(self, action, payload, timeout):
        if action == "diagnostics":
            return {"diagnostics": self.diagnostics(payload, timeout)}
        return {"error": "Unknown Zuban action: {}".format(action)}

    def server_info_message(self):
        name = self.server_info.get("name") or "Zuban"
        version = self.server_info.get("version") or ""
        return "{} {}".format(name, version).strip()

    def wait_until_settled(self, seconds):
        deadline = time.time() + max(0, seconds)
        while time.time() < deadline:
            if self.last_error:
                raise RuntimeError(self.last_error)
            if self.process and self.process.poll() is not None:
                raise RuntimeError(self.last_error or "LiuConda Zuban worker exited.")
            time.sleep(0.05)

    def diagnostics(self, payload, timeout):
        if not self.is_alive():
            raise RuntimeError("LiuConda Zuban worker is not running.")

        path = payload.get("path") or ""
        source = payload.get("source") or ""
        version = int(payload.get("version") or 1)
        if not path:
            raise RuntimeError("Zuban diagnostics need a saved Python file.")

        uri = _path_to_uri(path)
        event = threading.Event()
        holder = {}
        uses_pull_diagnostics = bool(self.server_capabilities.get("diagnosticProvider"))
        with self.lock:
            if uri in self.diagnostic_inflight:
                raise RuntimeError("Zuban diagnostics are already running for this file.")
            self.diagnostic_inflight.add(uri)
            if not uses_pull_diagnostics:
                self.diagnostic_waiters[uri] = (event, holder)

        try:
            self._sync_document_for_diagnostics(uri, source, version)

            if uses_pull_diagnostics:
                result = self._request(
                    "textDocument/diagnostic",
                    {"textDocument": {"uri": uri}},
                    timeout,
                )
                return [
                    _zuban_diagnostic_item(uri, item)
                    for item in (result or {}).get("items", [])
                ]

            if not event.wait(timeout):
                raise TimeoutError("Timed out waiting for Zuban diagnostics.")

            if holder.get("error"):
                raise RuntimeError(holder.get("error"))
            return holder.get("diagnostics") or []
        except Exception:
            with self.lock:
                self.document_change_counts.pop(uri, None)
            raise
        finally:
            with self.lock:
                self.diagnostic_waiters.pop(uri, None)
                self.diagnostic_inflight.discard(uri)

    def _sync_document_for_diagnostics(self, uri, source, version):
        with self.lock:
            previous_version = self.document_change_counts.get(uri)
            already_open = uri in self.open_documents

        if previous_version == version:
            return

        if already_open:
            self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": version,
                    "text": source,
                }
            },
        )

        with self.lock:
            self.open_documents[uri] = version
            self.document_change_counts[uri] = version

    def stop(self):
        process = self.process
        if not process:
            return
        if self.stopping:
            return
        self.stopping = True

        if process.poll() is None:
            try:
                self._request("shutdown", {}, ZUBAN_SHUTDOWN_TIMEOUT)
                self._notify("exit", {})
            except Exception:
                pass

            deadline = time.time() + ZUBAN_SHUTDOWN_TIMEOUT
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.05)

        self.unhealthy = True

        if process.poll() is None:
            _terminate_process_tree(process)
        else:
            _close_process_job(process)

        self._fail_pending("LiuConda Zuban worker stopped.")
        self._fail_diagnostics("LiuConda Zuban worker stopped.")
        self._close_pipes()

    def _initialize(self):
        root_uri = _path_to_uri(self.root_path) if self.root_path else None
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "LiuConda", "version": "0.1"},
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {"workspaceFolders": True},
            },
        }
        if root_uri:
            params["rootUri"] = root_uri
            params["workspaceFolders"] = [{"uri": root_uri, "name": os.path.basename(self.root_path)}]
        result = self._request("initialize", params, max(_zuban_timeout(), 10)) or {}
        self.server_capabilities = result.get("capabilities") or {}
        self.server_info = result.get("serverInfo") or {}
        self._notify("initialized", {})

    def _request(self, method, params, timeout):
        if not self.is_alive():
            raise RuntimeError("LiuConda Zuban worker is not running.")

        event = threading.Event()
        holder = {}
        with self.lock:
            if self.unhealthy:
                raise RuntimeError("LiuConda Zuban worker is restarting.")
            request_id = self.next_request_id
            self.next_request_id += 1
            self.pending[request_id] = (event, holder)
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )

        if not event.wait(timeout):
            with self.lock:
                self.pending.pop(request_id, None)
                self.unhealthy = True
            if not self.stopping:
                _retire_zuban_worker(self, "{} timed out".format(method))
            raise TimeoutError("LiuConda Zuban worker timed out.")

        if holder.get("error"):
            raise RuntimeError(holder.get("error"))
        return holder.get("result")

    def _notify(self, method, params):
        if not self.is_alive():
            raise RuntimeError("LiuConda Zuban worker is not running.")

        with self.lock:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                }
            )

    def _write_message(self, message):
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = "Content-Length: {}\r\n\r\n".format(len(body)).encode("ascii")
        try:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()
        except Exception:
            self.unhealthy = True
            if not self.stopping:
                _retire_zuban_worker(self, "write failed")
            raise

    def _read_stdout(self):
        try:
            while self.process and self.process.poll() is None:
                message = self._read_message()
                if message is None:
                    break
                self._handle_message(message)
        except Exception:
            LOGGER.exception("LiuConda Zuban worker stdout reader failed")
        finally:
            self.unhealthy = True
            message = self.last_error or "LiuConda Zuban worker exited."
            self._fail_pending(message)
            self._fail_diagnostics(message)
            _close_process_job(self.process)

    def _read_message(self):
        headers = {}
        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            if b":" not in line:
                continue
            name, value = line.decode("ascii", "replace").split(":", 1)
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length") or 0)
        if length <= 0:
            return None

        body = self.process.stdout.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                LOGGER.debug("LiuConda Zuban stderr: %s", line.decode("utf-8", "replace").rstrip())
        except Exception:
            LOGGER.exception("LiuConda Zuban stderr reader failed")

    def _handle_message(self, message):
        if "id" in message:
            self._handle_response(message)
            return

        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            self._handle_diagnostics(message.get("params") or {})
        elif method == "window/showMessage":
            self._handle_show_message(message.get("params") or {})

    def _handle_response(self, response):
        request_id = response.get("id")
        with self.lock:
            pending = self.pending.pop(request_id, None)
        if not pending:
            return

        event, holder = pending
        if response.get("error"):
            holder["error"] = _lsp_error_message(response.get("error"))
        else:
            holder["result"] = response.get("result")
        event.set()

    def _handle_diagnostics(self, params):
        uri = params.get("uri") or ""
        diagnostics = [_zuban_diagnostic_item(uri, item) for item in params.get("diagnostics") or []]
        with self.lock:
            self.latest_diagnostics[uri] = diagnostics
            waiter = self.diagnostic_waiters.get(uri)
            if waiter:
                event, holder = waiter
                holder["diagnostics"] = diagnostics
                event.set()

    def _handle_show_message(self, params):
        message = params.get("message") or ""
        if not message:
            return
        LOGGER.warning("LiuConda Zuban message: %s", message)
        if int(params.get("type") or 0) == 1:
            self.last_error = message
            self._fail_pending(message)
            self._fail_diagnostics(message)

    def _fail_pending(self, message):
        with self.lock:
            pending = list(self.pending.values())
            self.pending.clear()
        for event, holder in pending:
            holder["error"] = message
            event.set()

    def _fail_diagnostics(self, message):
        with self.lock:
            waiters = list(self.diagnostic_waiters.values())
            self.diagnostic_waiters.clear()
        for event, holder in waiters:
            holder["error"] = message
            event.set()

    def _close_pipes(self):
        for stream in (
            getattr(self.process, "stdin", None),
            getattr(self.process, "stdout", None),
            getattr(self.process, "stderr", None),
        ):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass


def _path_to_uri(path):
    path = os.path.abspath(path).replace("\\", "/")
    if os.name == "nt" and not path.startswith("/"):
        path = "/" + path
    return "file://" + quote(path, safe="/:")


def _uri_to_path(uri):
    parsed = urlparse(uri)
    path = unquote(parsed.path or "")
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep)


def _lsp_error_message(error):
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(error)


def _zuban_diagnostic_item(uri, item):
    range_info = item.get("range") or {}
    start = range_info.get("start") or {}
    end = range_info.get("end") or {}
    return {
        "path": _uri_to_path(uri),
        "line": int(start.get("line") or 0) + 1,
        "column": int(start.get("character") or 0) + 1,
        "end_line": int(end.get("line") or start.get("line") or 0) + 1,
        "end_column": int(end.get("character") or start.get("character") or 0) + 1,
        "severity": int(item.get("severity") or 1),
        "code": str(item.get("code") or ""),
        "source": str(item.get("source") or "zuban"),
        "message": str(item.get("message") or ""),
    }


def _set_jedi_available(available, message="", env=None, persist=True, stop_worker=True):
    checked_env = env or _selected_env()
    if checked_env != _selected_env():
        LOGGER.debug("Ignoring stale Jedi check for environment %s", checked_env)
        return

    if not persist:
        if message:
            sublime.status_message("LiuConda: {}".format(message))
        return

    settings = _settings()
    changed = (
        bool(settings.get("jedi_available", False)) != bool(available)
        or str(settings.get("jedi_checked_env", "")) != checked_env
        or str(settings.get("jedi_message", "")) != message
    )
    if changed:
        settings.set("jedi_available", bool(available))
        settings.set("jedi_checked_env", checked_env)
        settings.set("jedi_message", message)
        _save_settings()
        _write_menu(_envs())
    if available:
        _prewarm_jedi_worker(checked_env)
    elif stop_worker:
        _stop_jedi_worker(wait=False)


def _prewarm_jedi_worker(env):
    def work():
        try:
            _ensure_jedi_worker(env)
        except Exception:
            LOGGER.exception("Failed to prewarm LiuConda Jedi worker for %s", env)

    threading.Thread(target=work, daemon=True).start()


def _check_jedi_async():
    env = _selected_env()
    if not env:
        _set_jedi_available(False, "No environment selected")
        return

    _set_jedi_available(False, "Checking Jedi", env=env, persist=False, stop_worker=False)

    def work():
        try:
            output = _run_python(
                ["-c", "import jedi, sys; sys.stdout.write(jedi.__version__)"],
                timeout=8,
                env=env,
            )
            available = True
            message = "Jedi {}".format(output.strip())
        except Exception as error:
            available = False
            message = str(error)

        sublime.set_timeout(lambda: _set_jedi_available(available, message, env=env), 0)

    threading.Thread(target=work, daemon=True).start()


def _set_zuban_available(available, message="", env=None, persist=True, stop_worker=True):
    checked_env = env or _selected_env()
    if checked_env != _selected_env():
        LOGGER.debug("Ignoring stale Zuban check for environment %s", checked_env)
        return

    if not persist:
        if message:
            sublime.status_message("LiuConda: {}".format(message))
        return

    settings = _settings()
    changed = (
        bool(settings.get("zuban_available", False)) != bool(available)
        or str(settings.get("zuban_checked_env", "")) != checked_env
        or str(settings.get("zuban_message", "")) != message
    )
    if changed:
        settings.set("zuban_available", bool(available))
        settings.set("zuban_checked_env", checked_env)
        settings.set("zuban_message", message)
        _save_settings()
        _write_menu(_envs())
    if not available and stop_worker:
        _stop_zuban_worker(wait=False)


def _check_zuban_async():
    env = _selected_env()
    if not env:
        _set_zuban_available(False, "No environment selected")
        return

    custom_command = _settings().get("zuban", "zuban") != "zuban"
    cache_key = _zuban_status_cache_key(env)
    if custom_command:
        cached_message = _cached_zuban_status(cache_key)
        if cached_message:
            _set_zuban_available(True, cached_message, env=env, persist=True, stop_worker=False)
            return

    _set_zuban_available(False, "Checking Zuban", env=env, persist=False, stop_worker=False)

    def work():
        worker = None
        try:
            if custom_command:
                worker = _ZubanWorker(env, "")
                worker.start()
                worker.wait_until_settled(1.0)
                message = worker.server_info_message()
            else:
                output = _run_python(
                    ["-c", "import zuban, sys; sys.stdout.write(getattr(zuban, '__version__', 'zuban'))"],
                    timeout=8,
                    env=env,
                )
                version = output.strip()
                message = "Zuban {}".format(version).strip()
            available = True
            if custom_command:
                _store_zuban_status(cache_key, message)
        except Exception as error:
            available = False
            message = str(error)
        finally:
            if worker:
                worker.stop()

        sublime.set_timeout(lambda: _set_zuban_available(available, message, env=env), 0)

    threading.Thread(target=work, daemon=True).start()


def _set_envs(envs):
    settings = _settings()
    settings.set("envs", envs)

    selected = settings.get("selected_env", "")
    selected_record = _find_env_record(selected, envs)
    if selected_record:
        settings.set("selected_env", _env_id(selected_record))
    elif envs:
        settings.set("selected_env", _env_id(envs[0]))
    else:
        settings.set("selected_env", "")

    _save_settings()


def _run_json(cmd):
    output = _run_process(cmd, timeout=30)
    if not output or not output.strip():
        return {}
    return json.loads(output)


def _discover_envs(backend):
    executable = _backend_executable(backend)
    envs = []
    seen_paths = set()
    paths = []
    try:
        info = _run_json([executable, "info", "--json"])
    except Exception:
        info = {}

    root_prefix = info.get("root_prefix") or info.get("base environment") or ""

    errors = []
    for cmd in (
        [executable, "env", "list", "--json"],
        [executable, "info", "--envs", "--json"],
    ):
        try:
            data = _run_json(cmd)
        except Exception as error:
            errors.append(error)
            continue
        paths = data.get("envs", [])
        if isinstance(paths, list):
            break
        paths = []
    else:
        if errors:
            raise errors[-1]

    for path in paths:
        path_key = os.path.normcase(os.path.normpath(path)) if path else ""
        if not path_key or path_key in seen_paths or _is_conda_build_env_path(path):
            continue
        seen_paths.add(path_key)
        name = _env_name(path, root_prefix)
        if not name:
            continue
        envs.append({"name": name, "path": path})

    return envs


def _env_name(path, root_prefix):
    if not path:
        return ""

    if root_prefix and _same_path(path, root_prefix):
        return "base"

    name = os.path.basename(os.path.normpath(path))
    return name or path


def _is_conda_build_env_path(path):
    parts = [
        part.lower()
        for part in os.path.normpath(path).split(os.sep)
        if part
    ]
    return "conda-bld" in parts


def _same_path(left, right):
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _ensure_menu():
    try:
        _write_menu(_envs())
    except Exception as error:
        LOGGER.exception("Failed to write LiuConda menu: %s", error)


def _write_menu(envs):
    children = [
        {
            "caption": "Conda Version",
            "children": [
                {
                    "caption": "conda",
                    "command": "liu_conda_set_backend",
                    "args": {"backend": "conda"},
                    "checkbox": True,
                },
                {
                    "caption": "micromamba",
                    "command": "liu_conda_set_backend",
                    "args": {"backend": "micromamba"},
                    "checkbox": True,
                },
            ],
        },
        {"caption": "Refresh Env", "command": "liu_conda_refresh_env"},
        {
            "caption": "Environments",
            "children": _env_menu_items(envs),
        },
        {"caption": "Run", "command": "liu_conda_run"},
        {"caption": "-"},
        {"caption": "Go to Definition", "command": "liu_conda_goto_definition"},
        {"caption": "Show Documentation", "command": "liu_conda_show_documentation"},
        {
            "caption": "Hover Documentation",
            "command": "liu_conda_toggle_hover_documentation",
            "checkbox": True,
        },
        {"caption": "Type Diagnostics", "command": "liu_conda_type_diagnostics"},
        {"caption": "-"},
        {"caption": _jedi_caption(), "command": "liu_conda_jedi_status"},
        {"caption": _zuban_caption(), "command": "liu_conda_zuban_status"},
        {"caption": "-"},
        {"caption": "Set Backend Path...", "command": "liu_conda_set_backend_path"},
        {
            "caption": "Global Path Search",
            "command": "liu_conda_toggle_global_path_search",
            "checkbox": True,
        },
    ]

    menu = [
        {"caption": "Preferences", "id": "preferences"},
        {
            "caption": "LiuPlugins",
            "id": "liu_plugins",
            "children": [
                {
                    "caption": "LiuConda",
                    "id": "liu_conda",
                    "children": children,
                }
            ],
        },
    ]
    menu_dir = os.path.dirname(MAIN_MENU_FILE)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=menu_dir,
            prefix="Main.",
            suffix=".sublime-menu.tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(menu, handle, indent=4)
            handle.write("\n")
        os.replace(temp_name, MAIN_MENU_FILE)
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                LOGGER.warning("Failed to remove temporary menu file %s", temp_name)


def _env_menu_items(envs):
    if not envs:
        return [
            {
                "caption": "No environments found",
                "command": "liu_conda_refresh_env",
            }
        ]

    name_counts = _env_name_counts(envs)
    items = []
    for env in envs:
        env_id = _env_id(env)
        if not env_id:
            continue
        items.append(
            {
                "caption": _env_display_name(env, name_counts),
                "command": "liu_conda_select_env",
                "args": {"env": env_id},
                "checkbox": True,
            }
        )
    return items


def _jedi_caption():
    return "Jedi On" if _jedi_available() else "Jedi Off"


def _zuban_caption():
    return "Zuban On" if _zuban_available() else "Zuban Off"


def _is_python_view(view):
    if not view:
        return False

    point = view.sel()[0].b if view.sel() else 0
    if view.score_selector(point, "source.python") > 0:
        return True

    file_name = view.file_name() or ""
    return file_name.lower().endswith(".py")


def _jedi_ready(view=None, show_error=False):
    if view and not _is_python_view(view):
        return False

    if _jedi_available() and _selected_env():
        return True

    if show_error:
        sublime.error_message(
            "LiuConda: Jedi is Off for the selected environment. Install jedi in that environment first."
        )
    return False


def _zuban_ready(view=None, show_error=False):
    if view and not _is_python_view(view):
        return False

    if _zuban_available() and _selected_env():
        return True

    if show_error:
        message = _settings().get("zuban_message") or (
            "LiuConda: Zuban is Off for the selected environment. Install zuban in that environment first."
        )
        sublime.error_message(message)
    return False


def _view_payload(view, point):
    row, col = view.rowcol(point)
    return {
        "source": _view_source(view),
        "path": view.file_name() or "",
        "line": row + 1,
        "column": col,
        "change_count": view.change_count(),
    }


def _view_payload_if_current(view, point, token):
    if not _request_is_current(view, token):
        return None
    return _view_payload(view, point)


def _view_source(view):
    cache_key = (view.buffer_id(), view.change_count())
    with _state_lock:
        cached = _source_cache.get(cache_key)
    if cached is not None:
        with _state_lock:
            _source_cache.move_to_end(cache_key)
        return cached

    source = view.substr(sublime.Region(0, view.size()))
    with _state_lock:
        if cache_key in _source_cache:
            _source_cache.pop(cache_key)
        _source_cache[cache_key] = source
        while len(_source_cache) > SOURCE_CACHE_LIMIT:
            _source_cache.popitem(last=False)
    return source


def _worker_timeout():
    value = _settings().get("jedi_timeout", 5)
    try:
        return max(1, float(value))
    except Exception:
        return 5


def _command_worker_timeout():
    value = _settings().get("jedi_command_timeout", 20)
    try:
        return max(_worker_timeout(), float(value))
    except Exception:
        return max(_worker_timeout(), 20)


def _zuban_timeout():
    value = _settings().get("zuban_timeout", 10)
    try:
        return max(1, float(value))
    except Exception:
        return 10


def _popup_css():
    return """
        <style>
            body { font-family: system; margin: 0; }
            h3 { margin: 0 0 0.45rem 0; }
            p { margin: 0.35rem 0; }
            code { background-color: color(var(--background) blend(var(--foreground) 90%)); padding: 0.1rem 0.25rem; }
            .muted { color: color(var(--foreground) alpha(0.65)); }
            .signature { font-family: monospace; }
            .active { font-weight: bold; color: var(--bluish); }
        </style>
    """


def _show_html_popup(view, body, point=-1):
    view.show_popup(
        _popup_css() + body,
        flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        location=point,
        max_width=900,
        max_height=700,
    )


def _plain_to_html(text):
    return html.escape(text or "").replace("\n", "<br>")


def _documentation_popup_body(item):
    title = html.escape(item.get("name") or "Documentation")
    type_name = html.escape(item.get("type") or "")
    doc = _plain_to_html(item.get("docstring") or "")
    path = html.escape(item.get("path") or "")
    body = "<h3>{}</h3>".format(title)
    if type_name:
        body += '<p class="muted">{}</p>'.format(type_name)
    if path:
        body += '<p class="muted">{}</p>'.format(path)
    if doc:
        body += "<p>{}</p>".format(doc)
    return body


def _window_root(window, file_name):
    folders = window.folders() if window else []
    file_path = os.path.abspath(file_name) if file_name else ""
    for folder in folders:
        folder_path = os.path.abspath(folder)
        try:
            common = os.path.commonpath([folder_path, file_path]) if file_path else ""
        except ValueError:
            common = ""
        if common and _same_path(common, folder_path):
            return folder_path
    return os.path.dirname(file_path) if file_path else ""


def _show_zuban_diagnostics(window, file_name, diagnostics, error=""):
    panel = window.create_output_panel("liu_conda_zuban")
    panel.settings().set("result_file_regex", r"^(.+):([0-9]+):([0-9]+):")
    panel.set_read_only(False)
    panel.run_command("select_all")
    panel.run_command("right_delete")

    if error:
        text = "Zuban Type Diagnostics failed\n\n{}\n".format(error)
    elif diagnostics:
        lines = ["Zuban Type Diagnostics: {} issue(s)".format(len(diagnostics)), ""]
        for item in diagnostics:
            lines.append(_format_zuban_diagnostic(item))
        text = "\n".join(lines) + "\n"
    else:
        text = "Zuban Type Diagnostics: no issues found in {}\n".format(file_name)

    panel.run_command("append", {"characters": text, "force": True, "scroll_to_end": False})
    panel.set_read_only(True)
    window.run_command("show_panel", {"panel": "output.liu_conda_zuban"})


def _format_zuban_diagnostic(item):
    severity = {
        1: "error",
        2: "warning",
        3: "info",
        4: "hint",
    }.get(item.get("severity"), "diagnostic")
    code = item.get("code") or ""
    code_text = " [{}]".format(code) if code else ""
    message = (item.get("message") or "").replace("\r\n", "\n").replace("\r", "\n")
    message = "\n    ".join(message.splitlines())
    return "{}:{}:{}: {}{}: {}".format(
        item.get("path") or "",
        item.get("line") or 1,
        item.get("column") or 1,
        severity,
        code_text,
        message,
    )


def _hover_point(view, point):
    word = view.word(point)
    if word.empty():
        return None

    name = view.substr(word)
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return None
    if not all(char.isalnum() or char == "_" for char in name):
        return None

    selectors = "comment, string, constant.numeric"
    if view.score_selector(point, selectors) > 0:
        return None

    return word.b


def _hover_cache_key(view, point):
    return (_selected_env(), view.buffer_id(), view.change_count(), point)


def _cached_hover_documentation(cache_key):
    with _state_lock:
        if cache_key not in _hover_documentation_cache:
            return False, ""
        cached = _hover_documentation_cache.pop(cache_key)
        _hover_documentation_cache[cache_key] = cached
        return True, cached


def _store_hover_documentation(cache_key, body):
    with _state_lock:
        if cache_key in _hover_documentation_cache:
            _hover_documentation_cache.pop(cache_key)
        _hover_documentation_cache[cache_key] = body
        while len(_hover_documentation_cache) > HOVER_CACHE_LIMIT:
            _hover_documentation_cache.popitem(last=False)


def _clear_hover_request(view, token=None):
    with _state_lock:
        current = _hover_requests.get(view.id())
        if token is None or current == token:
            _hover_requests.pop(view.id(), None)


def _clear_signature_request(view, token=None):
    with _state_lock:
        current = _signature_requests.get(view.id())
        if token is None or current == token:
            _signature_requests.pop(view.id(), None)


def _completion_items_from_result(result):
    completions = []
    for item in result.get("completions", []):
        trigger = item.get("trigger") or item.get("name")
        contents = item.get("completion") or item.get("name")
        if trigger and contents:
            completions.append((trigger, contents))
    return completions


def _cached_completion(cache_key):
    with _state_lock:
        if cache_key not in _completion_cache:
            return False, []
        cached = _completion_cache.pop(cache_key)
        _completion_cache[cache_key] = cached
        return True, cached


def _store_completion(cache_key, completions):
    with _state_lock:
        if cache_key in _completion_cache:
            _completion_cache.pop(cache_key)
        _completion_cache[cache_key] = completions
        while len(_completion_cache) > COMPLETION_CACHE_LIMIT:
            _completion_cache.popitem(last=False)


def _should_query_signature(view, point, previous):
    if view.score_selector(point, "comment, string") > 0:
        return False

    if previous == "(":
        return _has_callable_before_open_paren(view, point - 1)

    if previous == ",":
        open_point = _find_call_open_paren(view, point - 1)
        return open_point is not None and _has_callable_before_open_paren(view, open_point)

    return False


def _find_call_open_paren(view, point):
    balance = 0
    current = point - 1
    while current >= 0:
        char = view.substr(sublime.Region(current, current + 1))
        if char == ")":
            balance += 1
        elif char == "(":
            if balance == 0:
                return current
            balance -= 1
        elif char == "\n" and balance == 0:
            previous = _previous_non_whitespace_char(view, current - 1)
            if previous and previous not in "(,[{\\":
                return None
        current -= 1
    return None


def _previous_non_whitespace_char(view, point):
    current = point
    while current >= 0:
        char = view.substr(sublime.Region(current, current + 1))
        if not char.isspace():
            return char
        current -= 1
    return ""


def _has_callable_before_open_paren(view, open_point):
    current = open_point - 1
    while current >= 0 and view.substr(sublime.Region(current, current + 1)).isspace():
        current -= 1

    if current < 0:
        return False

    char = view.substr(sublime.Region(current, current + 1))
    if char in ")]}":
        return True

    if not (char.isalnum() or char == "_"):
        return False

    word = view.word(current)
    name = view.substr(word)
    if keyword.iskeyword(name):
        return False

    return bool(name)


def _new_completion_list():
    if not hasattr(sublime, "CompletionList"):
        return None

    try:
        return sublime.CompletionList()
    except TypeError:
        try:
            return sublime.CompletionList([], sublime.INHIBIT_WORD_COMPLETIONS)
        except TypeError:
            LOGGER.exception("Unsupported Sublime CompletionList constructor")
            return None


def _request_is_current(view, token):
    env, view_id, change_count, point = token
    if not view or not view.is_valid():
        return False
    return (
        env == _selected_env()
        and view.id() == view_id
        and view.change_count() == change_count
        and 0 <= point <= view.size()
    )


class LiuCondaSetBackendCommand(sublime_plugin.WindowCommand):
    def run(self, backend="conda"):
        if backend not in BACKENDS:
            sublime.error_message("Unsupported LiuConda backend: {}".format(backend))
            return

        settings = _settings()
        settings.set("backend", backend)
        settings.set("selected_env", "")
        _save_settings()
        _clear_backend_executable_cache()
        _stop_jedi_worker(wait=False)
        _stop_zuban_worker(wait=False)
        _set_jedi_available(False, "No environment selected")
        _set_zuban_available(False, "No environment selected")

        sublime.status_message("LiuConda: using {}".format(backend))
        self.window.run_command("liu_conda_refresh_env")

    def is_checked(self, backend="conda"):
        return _backend() == backend


class LiuCondaRefreshEnvCommand(sublime_plugin.WindowCommand):
    def run(self):
        backend = _backend()
        sublime.status_message("LiuConda: refreshing {} environments...".format(backend))

        def work():
            try:
                envs = _discover_envs(backend)
            except FileNotFoundError as error:
                message = str(error)
                sublime.set_timeout(
                    lambda: sublime.error_message(message),
                    0,
                )
                return
            except subprocess.CalledProcessError as error:
                message = error.output.strip() if error.output else str(error)
                sublime.set_timeout(
                    lambda: sublime.error_message("LiuConda refresh failed:\n{}".format(message)),
                    0,
                )
                return
            except Exception as error:
                message = str(error)
                sublime.set_timeout(
                    lambda: sublime.error_message("LiuConda refresh failed:\n{}".format(message)),
                    0,
                )
                return

            def done():
                previous_env = _selected_env()
                _set_envs(envs)
                if previous_env != _selected_env():
                    _stop_jedi_worker(wait=False)
                    _stop_zuban_worker(wait=False)
                _write_menu(envs)
                _check_jedi_async()
                _check_zuban_async()
                sublime.status_message("LiuConda: found {} environment(s)".format(len(envs)))

            sublime.set_timeout(done, 0)

        threading.Thread(target=work, daemon=True).start()


class LiuCondaSelectEnvCommand(sublime_plugin.WindowCommand):
    def run(self, env=""):
        if not env:
            return
        _stop_jedi_worker(wait=False)
        _stop_zuban_worker(wait=False)
        settings = _settings()
        settings.set("selected_env", env)
        _save_settings()
        label = env
        record = _find_env_record(env)
        if record:
            label = _env_display_name(record, _env_name_counts(_envs()))
        sublime.status_message("LiuConda: selected environment '{}'".format(label))
        _check_jedi_async()
        _check_zuban_async()

    def is_checked(self, env=""):
        return bool(env) and _selected_env() == env


class LiuCondaRunCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return

        if view.is_dirty():
            _queue_run_after_save(view)
            view.run_command("save")
            return

        self._run_view(view)

    def _run_view(self, view):
        file_name = view.file_name()
        if not file_name:
            sublime.error_message("LiuConda: save the current Python file before running it.")
            return

        env = _selected_env()
        if not env:
            sublime.error_message(
                "LiuConda: choose an environment from LiuPlugins > LiuConda > Environments first."
            )
            return

        try:
            cmd = _python_command(env) + [file_name]
        except FileNotFoundError as error:
            sublime.error_message(str(error))
            return

        self.window.run_command(
            "exec",
            {
                "cmd": cmd,
                "working_dir": os.path.dirname(file_name),
                "file_regex": r"^  File \"(...*?)\", line ([0-9]*)",
            },
        )

    def is_enabled(self):
        view = self.window.active_view()
        return bool(view and view.file_name() and _selected_env())


class LiuCondaJediStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        message = _settings().get("jedi_message", "")
        status = _jedi_caption()
        sublime.message_dialog("LiuConda: {}\n{}".format(status, message))


class LiuCondaZubanStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        message = _settings().get("zuban_message", "")
        status = _zuban_caption()
        sublime.message_dialog("LiuConda: {}\n{}".format(status, message))


class LiuCondaTypeDiagnosticsCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view or not _is_python_view(view):
            return

        file_name = view.file_name()
        if not file_name:
            sublime.error_message("LiuConda: save the current Python file before running Zuban diagnostics.")
            return

        if not _selected_env():
            sublime.error_message(
                "LiuConda: choose an environment from LiuPlugins > LiuConda > Environments first."
            )
            return

        env = _selected_env()
        root_path = _window_root(self.window, file_name)
        payload = {
            "source": _view_source(view),
            "path": file_name,
            "version": view.change_count(),
        }
        sublime.status_message("LiuConda: running Zuban type diagnostics...")

        def work():
            try:
                result = _run_zuban_worker(
                    "diagnostics",
                    payload,
                    timeout=_zuban_timeout(),
                    env=env,
                    root_path=root_path,
                )
                diagnostics = result.get("diagnostics", [])
                error = ""
            except Exception as exc:
                diagnostics = []
                error = str(exc)

            def done():
                _show_zuban_diagnostics(self.window, file_name, diagnostics, error=error)
                if error:
                    sublime.status_message("LiuConda: Zuban diagnostics failed")
                else:
                    sublime.status_message(
                        "LiuConda: Zuban found {} issue(s)".format(len(diagnostics))
                    )

            sublime.set_timeout(done, 0)

        threading.Thread(target=work, daemon=True).start()

    def is_enabled(self):
        view = self.window.active_view()
        return bool(view and view.file_name() and _is_python_view(view) and _selected_env())


class LiuCondaSetBackendPathCommand(sublime_plugin.WindowCommand):
    def run(self):
        backend = _backend()
        current = _configured_executable(backend)
        caption = "Path to {} executable".format(backend)
        self.window.show_input_panel(caption, current, self._set_path, None, None)

    def _set_path(self, value):
        settings = _settings()
        paths = settings.get("backend_executables", {})
        if not isinstance(paths, dict):
            paths = {}
        paths[_backend()] = value.strip()
        settings.set("backend_executables", paths)
        _save_settings()
        _clear_backend_executable_cache()
        _stop_jedi_worker(wait=False)
        _stop_zuban_worker(wait=False)
        _write_menu(_envs())
        self.window.run_command("liu_conda_refresh_env")


class LiuCondaToggleGlobalPathSearchCommand(sublime_plugin.WindowCommand):
    def run(self):
        settings = _settings()
        enabled = not _global_path_search_enabled()
        settings.set("global_path_search", enabled)
        _save_settings()
        _clear_backend_executable_cache()
        sublime.status_message("LiuConda: global path search {}".format("on" if enabled else "off"))
        _write_menu(_envs())

    def is_checked(self):
        return _global_path_search_enabled()


class LiuCondaGotoDefinitionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if not _jedi_ready(self.view, show_error=True):
            return

        point = self.view.sel()[0].b
        timeout = _command_worker_timeout()
        request_env = _selected_env()
        token = (request_env, self.view.id(), self.view.change_count(), point)
        window = self.view.window()
        sublime.status_message("LiuConda: finding definition...")

        def work():
            try:
                payload = _view_payload_if_current(self.view, point, token)
                if payload is None:
                    return
                result = _run_worker("goto", payload, timeout=timeout, env=request_env)
                error = ""
            except Exception as exc:
                result = {}
                error = str(exc)

            def done():
                if not _request_is_current(self.view, token):
                    return
                if error:
                    sublime.error_message("LiuConda goto definition failed:\n{}".format(error))
                    return

                definitions = result.get("definitions", [])
                if not definitions:
                    sublime.status_message("LiuConda: no definition found")
                    return

                definition = definitions[0]
                path = definition.get("path")
                line = definition.get("line")
                column = definition.get("column") or 0
                if not path or not line:
                    sublime.status_message("LiuConda: definition has no file location")
                    return

                encoded = "{}:{}:{}".format(path, line, column + 1)
                window.open_file(encoded, sublime.ENCODED_POSITION)

            sublime.set_timeout(done, 0)

        threading.Thread(target=work, daemon=True).start()

    def is_enabled(self):
        return _jedi_ready(self.view)


class LiuCondaShowDocumentationCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if not _jedi_ready(self.view, show_error=True):
            return

        point = self.view.sel()[0].b
        timeout = _command_worker_timeout()
        request_env = _selected_env()
        token = (request_env, self.view.id(), self.view.change_count(), point)
        sublime.status_message("LiuConda: loading documentation...")

        def work():
            try:
                payload = _view_payload_if_current(self.view, point, token)
                if payload is None:
                    return
                result = _run_worker("documentation", payload, timeout=timeout, env=request_env)
                error = ""
            except Exception as exc:
                result = {}
                error = str(exc)

            def done():
                if not _request_is_current(self.view, token):
                    return
                if error:
                    sublime.error_message("LiuConda documentation failed:\n{}".format(error))
                    return

                docs = result.get("items", [])
                if not docs:
                    sublime.status_message("LiuConda: no documentation found")
                    return

                _show_html_popup(self.view, _documentation_popup_body(docs[0]), point)

            sublime.set_timeout(done, 0)

        threading.Thread(target=work, daemon=True).start()

    def is_enabled(self):
        return _jedi_ready(self.view)


class LiuCondaToggleHoverDocumentationCommand(sublime_plugin.WindowCommand):
    def run(self):
        settings = _settings()
        enabled = not _hover_documentation_enabled()
        settings.set("hover_documentation", enabled)
        _save_settings()
        sublime.status_message("LiuConda: hover documentation {}".format("on" if enabled else "off"))
        _write_menu(_envs())

    def is_checked(self):
        return _hover_documentation_enabled()


class LiuCondaJediEventListener(sublime_plugin.EventListener):
    def on_post_save_async(self, view):
        if not _consume_pending_run(view):
            return

        window = view.window()
        if not window:
            return

        sublime.set_timeout(lambda: LiuCondaRunCommand(window)._run_view(view), 0)

    def on_hover(self, view, point, hover_zone):
        if hover_zone != sublime.HOVER_TEXT:
            _clear_hover_request(view)
            return
        if not _hover_documentation_enabled() or not _jedi_ready(view):
            _clear_hover_request(view)
            return

        symbol_point = _hover_point(view, point)
        if symbol_point is None:
            _clear_hover_request(view)
            return

        cache_key = _hover_cache_key(view, symbol_point)
        has_cached_body, cached_body = _cached_hover_documentation(cache_key)
        if has_cached_body:
            _clear_hover_request(view)
            if cached_body:
                _show_html_popup(view, cached_body, symbol_point)
            return

        request_env = _selected_env()
        token = (request_env, view.id(), view.change_count(), symbol_point)
        with _state_lock:
            _hover_requests[view.id()] = token

        timeout = _worker_timeout()

        def query():
            with _state_lock:
                current_token = _hover_requests.get(view.id())
            if current_token != token:
                return

            try:
                payload = _view_payload_if_current(view, symbol_point, token)
                if payload is None:
                    _clear_hover_request(view, token)
                    return
                result = _run_worker("documentation", payload, timeout=timeout, env=request_env)
                docs = result.get("items", [])
                body = _documentation_popup_body(docs[0]) if docs else ""
            except Exception:
                LOGGER.exception("LiuConda hover documentation failed")
                body = ""

            _store_hover_documentation(cache_key, body)
            if not body:
                _clear_hover_request(view, token)
                return

            def show():
                try:
                    with _state_lock:
                        current = _hover_requests.get(view.id())
                    if current == token and _request_is_current(view, token):
                        _show_html_popup(view, body, symbol_point)
                finally:
                    _clear_hover_request(view, token)

            sublime.set_timeout(show, 0)

        sublime.set_timeout_async(query, 350)

    def on_query_completions(self, view, prefix, locations):
        if not locations or not _jedi_ready(view):
            return None

        point = locations[0]
        cache_key = (_selected_env(), view.buffer_id(), view.change_count(), point, prefix)
        has_cached, cached = _cached_completion(cache_key)
        if has_cached:
            return (cached, sublime.INHIBIT_WORD_COMPLETIONS)

        completion_list = _new_completion_list()
        if completion_list is None:
            return None

        request_env = _selected_env()
        token = (request_env, view.id(), view.change_count(), point)
        with _state_lock:
            waiters = _completion_waiters.get(cache_key)
            if waiters is not None:
                waiters.append(completion_list)
                return completion_list
            _completion_waiters[cache_key] = [completion_list]

        timeout = _worker_timeout()

        def query():
            try:
                payload = _view_payload_if_current(view, point, token)
                if payload is None:
                    completions = []
                else:
                    result = _run_worker("complete", payload, timeout=timeout, env=request_env)
                    completions = _completion_items_from_result(result)
            except Exception:
                LOGGER.exception("LiuConda completion failed")
                completions = []

            with _state_lock:
                waiters = _completion_waiters.pop(cache_key, [])

            def publish():
                if _request_is_current(view, token):
                    published = completions
                    _store_completion(cache_key, published)
                else:
                    published = []

                for waiter in waiters:
                    waiter.set_completions(published, sublime.INHIBIT_WORD_COMPLETIONS)

            sublime.set_timeout(publish, 0)

        threading.Thread(target=query, daemon=True).start()
        return completion_list

    def on_modified_async(self, view):
        self._schedule_signature(view)

    def on_selection_modified_async(self, view):
        self._schedule_signature(view)

    def _schedule_signature(self, view):
        if not _jedi_ready(view):
            return

        selections = list(view.sel())
        if len(selections) != 1:
            return

        point = selections[0].b
        if point == 0:
            return

        previous = view.substr(sublime.Region(max(0, point - 1), point))
        if not _should_query_signature(view, point, previous):
            return

        request_env = _selected_env()
        token = (request_env, view.id(), view.change_count(), point)
        with _state_lock:
            _signature_requests[view.id()] = token

        def query():
            with _state_lock:
                current_token = _signature_requests.get(view.id())
            if current_token != token:
                _clear_signature_request(view, token)
                return
            self._show_signature(view, point, request_env, token)

        sublime.set_timeout_async(query, 250)

    def _show_signature(self, view, point, request_env, token):
        try:
            payload = _view_payload_if_current(view, point, token)
            if payload is None:
                _clear_signature_request(view, token)
                return
            result = _run_worker(
                "signature",
                payload,
                timeout=_worker_timeout(),
                env=request_env,
            )
        except Exception as error:
            LOGGER.exception("LiuConda signature failed: %s", error)
            _clear_signature_request(view, token)
            return

        signatures = result.get("signatures", [])
        if not signatures:
            _clear_signature_request(view, token)
            return

        signature = signatures[0]
        name = html.escape(signature.get("name") or "")
        params = signature.get("params") or []
        index = signature.get("index")

        rendered_params = []
        for idx, param in enumerate(params):
            text = html.escape(param)
            if index == idx:
                rendered_params.append('<span class="active">{}</span>'.format(text))
            else:
                rendered_params.append(text)

        body = '<p class="signature">{}({})</p>'.format(name, ", ".join(rendered_params))
        doc = signature.get("docstring") or ""
        if doc:
            body += '<p class="muted">{}</p>'.format(_plain_to_html(doc.splitlines()[0]))

        def show():
            try:
                if _request_is_current(view, token):
                    _show_html_popup(view, body, point)
            finally:
                _clear_signature_request(view, token)

        sublime.set_timeout(show, 0)
