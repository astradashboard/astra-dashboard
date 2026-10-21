"""
ASTRA CAPITAL - Dashboard de comptes de trading (MT5)

Application locale : un petit serveur tourne sur l'ordinateur (127.0.0.1 uniquement)
et l'interface s'ouvre dans une fenêtre. Les identifiants ne quittent jamais le PC :
la connexion passe par le terminal MetaTrader 5 installé localement.
"""
import datetime as dt
import json
import os
import random
import socket
import sys
import threading
import webbrowser
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory

try:
    import MetaTrader5 as mt5
except ImportError:  # Mac / Linux, ou module non installé
    mt5 = None

try:
    import keyring  # Gestionnaire d'identification Windows
except ImportError:
    keyring = None

APP_NAME = "ASTRA Dashboard"
KEYRING_SERVICE = "ASTRA Dashboard MT5"
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ASTRA Dashboard")
os.makedirs(DATA_DIR, exist_ok=True)
ACCOUNTS_FILE = os.path.join(DATA_DIR, "comptes.json")
CACHE_FILE = os.path.join(DATA_DIR, "donnees.json")

mt5_lock = threading.Lock()
session_passwords = {}  # mots de passe gardés en mémoire le temps de la session

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------- stockage local
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_accounts():
    return _load(ACCOUNTS_FILE, {})


def save_accounts(accounts):
    _save(ACCOUNTS_FILE, accounts)


def load_cache():
    return _load(CACHE_FILE, {})


def save_cache(cache):
    _save(CACHE_FILE, cache)


def get_saved_password(login):
    if login in session_passwords:
        return session_passwords[login]
    if keyring:
        try:
            return keyring.get_password(KEYRING_SERVICE, login)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- MetaTrader 5
CAPITAL_TYPES = set()
if mt5:
    CAPITAL_TYPES = {
        getattr(mt5, name)
        for name in ("DEAL_TYPE_BALANCE", "DEAL_TYPE_CREDIT", "DEAL_TYPE_BONUS")
        if hasattr(mt5, name)
    }


# ---- terminal MT5 privé et invisible -------------------------------------------
# L'application n'utilise jamais le MT5 de la personne : elle en garde une copie
# "portable" dans son propre dossier, la lance cachée, lit les données et la ferme.
PRIVATE_TERMINALS_DIR = os.path.join(DATA_DIR, "terminaux")


def find_installed_terminals():
    found = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if not root or not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            exe = os.path.join(root, name, "terminal64.exe")
            if os.path.isfile(exe):
                found.append(exe)
    return found


def pick_source_terminal(acc):
    if acc.get("terminal") and os.path.isfile(acc["terminal"]):
        return acc["terminal"]
    terminals = find_installed_terminals()
    if not terminals:
        raise RuntimeError(
            "Aucun MetaTrader 5 trouvé sur cet ordinateur. Installe le MT5 de ta prop firm "
            "(pour FTMO : depuis ton espace client), ou indique le chemin de terminal64.exe "
            "dans les options avancées."
        )
    # le terminal dont le dossier ressemble au serveur (ex. "FTMO" pour FTMO-Server)
    brand = acc.get("server", "").split("-")[0].lower()
    for exe in terminals:
        if brand and brand in os.path.basename(os.path.dirname(exe)).lower():
            return exe
    return terminals[0]


def _copy_servers_list(src_dir, dest_dir):
    """Copie la liste des serveurs du broker depuis le dossier de données du MT5 installé."""
    import glob
    import shutil

    roaming = os.environ.get("APPDATA")
    if not roaming:
        return
    for origin in glob.glob(os.path.join(roaming, "MetaQuotes", "Terminal", "*", "origin.txt")):
        try:
            with open(origin, "r", encoding="utf-16") as f:
                path = f.read().strip()
        except (OSError, UnicodeError):
            try:
                with open(origin, "r", encoding="utf-8", errors="ignore") as f:
                    path = f.read().strip()
            except OSError:
                continue
        if os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(src_dir)):
            servers = os.path.join(os.path.dirname(origin), "Config", "servers.dat")
            if os.path.isfile(servers):
                os.makedirs(os.path.join(dest_dir, "Config"), exist_ok=True)
                shutil.copy2(servers, os.path.join(dest_dir, "Config", "servers.dat"))
            return


def private_terminal(acc):
    """Renvoie le terminal64.exe de la copie privée (la crée la première fois)."""
    import shutil

    src_exe = pick_source_terminal(acc)
    src_dir = os.path.dirname(src_exe)
    dest_dir = os.path.join(PRIVATE_TERMINALS_DIR, os.path.basename(src_dir) or "MT5")
    dest_exe = os.path.join(dest_dir, "terminal64.exe")
    if not os.path.isfile(dest_exe):
        shutil.copytree(
            src_dir, dest_dir, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("logs", "Logs", "Tester", "Bases", "uninstall.exe"),
        )
    if not os.path.isfile(os.path.join(dest_dir, "Config", "servers.dat")):
        _copy_servers_list(src_dir, dest_dir)
    return dest_exe


def _private_terminal_pids():
    """PID de tous les processus lancés depuis la copie privée de MT5 (Windows uniquement)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32
    arr = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
        return set()
    root = os.path.normcase(os.path.abspath(PRIVATE_TERMINALS_DIR))
    pids = set()
    buf = ctypes.create_unicode_buffer(1024)
    for pid in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.normcase(buf.value).startswith(root):
                    pids.add(pid)
        finally:
            kernel32.CloseHandle(h)
    return pids


def _kill_private_terminals():
    import subprocess

    for pid in _private_terminal_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class HiddenTerminal:
    """Garde invisible toute fenêtre de la copie privée de MT5, puis la ferme à la fin.
    C'est la bibliothèque MetaTrader5 qui démarre le terminal : on ne fait que le cacher."""

    def __init__(self, exe):
        self.exe = exe
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if os.name == "nt":
            _kill_private_terminals()  # une copie restée ouverte après un plantage
            self._thread = threading.Thread(target=self._keep_hidden, daemon=True)
            self._thread.start()
        return self

    def _keep_hidden(self):
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        pids = set()

        def hide(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
            return True

        callback = proto(hide)
        tick = 0
        while not self._stop.is_set():
            if tick % 5 == 0:  # liste des processus rafraîchie toutes les 100 ms
                pids = _private_terminal_pids()
            if pids:
                user32.EnumWindows(callback, 0)
            tick += 1
            self._stop.wait(0.02)

    def __exit__(self, *exc):
        if os.name == "nt":
            _kill_private_terminals()
        self._stop.set()
        return False


def fetch_mt5(acc, password):
    """Se connecte au compte via un terminal MT5 privé et caché, et calcule le P&L par jour."""
    if mt5 is None:
        raise RuntimeError(
            "Le module MetaTrader5 n'est pas disponible. L'application fonctionne "
            "uniquement sur Windows avec le terminal MetaTrader 5 installé."
        )
    login = int(acc["login"])

    with mt5_lock:
        try:
            exe = private_terminal(acc)
        except OSError as e:
            raise RuntimeError(f"Impossible de préparer le terminal MT5 de l'application : {e}")

        with HiddenTerminal(exe):
            ok = mt5.initialize(path=exe, portable=True, login=login, password=password,
                                server=acc["server"], timeout=90000)
            if not ok:
                code, msg = mt5.last_error()
                mt5.shutdown()
                if code == -6:  # autorisation refusée
                    raise RuntimeError(
                        f"Connexion refusée ({msg}). Vérifie le numéro de compte, "
                        "le mot de passe et le nom exact du serveur."
                    )
                raise RuntimeError(
                    f"Impossible de se connecter via MetaTrader 5 ({code} : {msg}). "
                    "Vérifie le nom exact du serveur et réessaie."
                )
            try:
                info = mt5.account_info()
                if info is None or info.login != login:
                    if not mt5.login(login, password=password, server=acc["server"], timeout=60000):
                        code, msg = mt5.last_error()
                        raise RuntimeError(
                            f"Connexion refusée ({code} : {msg}). Vérifie le numéro de compte, "
                            "le mot de passe et le nom exact du serveur."
                        )
                    info = mt5.account_info()
                if info is None:
                    raise RuntimeError("Impossible de lire les informations du compte.")
                deals = mt5.history_deals_get(
                    dt.datetime(2000, 1, 1), dt.datetime.now() + dt.timedelta(days=2)
                ) or []
            finally:
                mt5.shutdown()

    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    start_balance = 0.0
    exits = {getattr(mt5, n) for n in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT", "DEAL_ENTRY_OUT_BY") if hasattr(mt5, n)}
    trade_types = {mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL}

    for d in deals:
        day = dt.datetime.utcfromtimestamp(d.time).date().isoformat()  # date du serveur
        net = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        if d.type in CAPITAL_TYPES:
            start_balance += d.profit  # dépôts / retraits / capital du challenge
            continue
        daily[day]["pnl"] += net
        if d.type in trade_types and d.entry in exits:
            daily[day]["trades"] += 1
            if d.profit + d.swap > 0:
                daily[day]["wins"] += 1
            elif d.profit + d.swap < 0:
                daily[day]["losses"] += 1

    return {
        "currency": info.currency,
        "balance": info.balance,
        "equity": info.equity,
        "start_balance": start_balance,
        "company": info.company,
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "daily": {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in sorted(daily.items())},
        "demo": False,
    }


def make_demo_data(size=100000.0):
    """Données fictives pour découvrir l'application sans compte."""
    rnd = random.Random(42)
    daily = {}
    day = dt.date.today() - dt.timedelta(days=120)
    balance = size
    while day <= dt.date.today():
        if day.weekday() < 5 and rnd.random() < 0.8:
            trades = rnd.randint(1, 3)
            pnl = round(rnd.gauss(220, 650), 2)
            wins = rnd.randint((trades + 1) // 2, trades) if pnl > 0 else rnd.randint(0, trades // 2)
            daily[day.isoformat()] = {"pnl": pnl, "trades": trades, "wins": wins, "losses": trades - wins}
            balance += pnl
        day += dt.timedelta(days=1)
    return {
        "currency": "USD",
        "balance": round(balance, 2),
        "equity": round(balance + rnd.uniform(-300, 300), 2),
        "start_balance": size,
        "company": "Démo",
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "daily": daily,
        "demo": True,
    }


# ---------------------------------------------------------------- API
def account_summary(login, acc, cache):
    c = cache.get(login, {})
    return {
        "login": login,
        "name": acc.get("name") or login,
        "server": acc.get("server", ""),
        "currency": c.get("currency"),
        "balance": c.get("balance"),
        "equity": c.get("equity"),
        "start_balance": c.get("start_balance"),
        "fetched_at": c.get("fetched_at"),
        "demo": c.get("demo", False),
        "has_password": bool(get_saved_password(login)) or c.get("demo", False),
    }


@app.after_request
def no_cache(resp):
    # la fenêtre doit toujours afficher la version installée, jamais une copie en cache
    resp.headers["Cache-Control"] = "no-store"
    return resp


_life = {"last_ping": 0.0, "closing_since": None}


@app.post("/api/ping")
def api_ping():
    import time

    _life["last_ping"] = time.time()
    _life["closing_since"] = None
    return jsonify({"ok": True})


@app.post("/api/bye")
def api_bye():
    import time

    _life["closing_since"] = time.time()
    return ("", 204)


@app.post("/api/quit")
def api_quit():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


def lifetime_watchdog():
    """Fenêtre fermée (ou plus aucun signe de vie pendant 10 min) : l'application s'arrête."""
    import time

    _life["last_ping"] = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        closing = _life["closing_since"]
        if closing and now - closing > 5:
            os._exit(0)
        if now - _life["last_ping"] > 600:
            os._exit(0)


PREFS_FILE = os.path.join(DATA_DIR, "preferences.json")


@app.get("/api/prefs")
def api_prefs_get():
    return jsonify(_load(PREFS_FILE, {}))


@app.post("/api/prefs")
def api_prefs_set():
    body = request.get_json(force=True) or {}
    prefs = _load(PREFS_FILE, {})
    if isinstance(body.get("kpis"), list):
        prefs["kpis"] = [str(x)[:40] for x in body["kpis"]][:50]
    _save(PREFS_FILE, prefs)
    return jsonify(prefs)


@app.get("/api/version")
def api_version():
    return jsonify({"version": _load(VERSION_FILE, {}).get("version", "?"),
                    "update": _load(UPDATE_STATUS_FILE, {})})


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/favicon.png")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.png")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


@app.get("/api/state")
def api_state():
    accounts, cache = load_accounts(), load_cache()
    return jsonify({
        "mt5_available": mt5 is not None,
        "keyring_available": keyring is not None,
        "accounts": [account_summary(k, v, cache) for k, v in accounts.items()],
    })


@app.post("/api/connect")
def api_connect():
    body = request.get_json(force=True) or {}
    login = str(body.get("login", "")).strip()
    password = body.get("password") or ""
    server = str(body.get("server", "")).strip()
    if not login.isdigit() or not password or not server:
        return jsonify({"error": "Numéro de compte, mot de passe et serveur sont obligatoires."}), 400

    acc = {
        "login": login,
        "server": server,
        "name": str(body.get("name", "")).strip() or f"Compte {login}",
        "terminal": str(body.get("terminal", "")).strip(),
    }
    try:
        data = fetch_mt5(acc, password)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    accounts, cache = load_accounts(), load_cache()
    accounts[login] = acc
    cache[login] = data
    save_accounts(accounts)
    save_cache(cache)
    session_passwords[login] = password
    if body.get("remember") and keyring:
        try:
            keyring.set_password(KEYRING_SERVICE, login, password)
        except Exception:
            pass
    return jsonify(account_summary(login, acc, cache))


@app.post("/api/refresh")
def api_refresh():
    body = request.get_json(force=True) or {}
    target = str(body.get("login", "all"))
    accounts, cache = load_accounts(), load_cache()
    logins = list(accounts) if target == "all" else [target]
    errors, need_password = [], []
    for login in logins:
        acc = accounts.get(login)
        if not acc:
            continue
        if cache.get(login, {}).get("demo"):
            continue
        pw = body.get("password") if target != "all" and body.get("password") else get_saved_password(login)
        if not pw:
            need_password.append(login)
            continue
        try:
            cache[login] = fetch_mt5(acc, pw)
            session_passwords[login] = pw
        except RuntimeError as e:
            errors.append(f"{acc.get('name', login)} : {e}")
    save_cache(cache)
    return jsonify({"errors": errors, "need_password": need_password})


@app.post("/api/remove")
def api_remove():
    login = str((request.get_json(force=True) or {}).get("login", ""))
    accounts, cache = load_accounts(), load_cache()
    accounts.pop(login, None)
    cache.pop(login, None)
    session_passwords.pop(login, None)
    if keyring:
        try:
            keyring.delete_password(KEYRING_SERVICE, login)
        except Exception:
            pass
    save_accounts(accounts)
    save_cache(cache)
    return jsonify({"ok": True})


@app.post("/api/demo")
def api_demo():
    accounts, cache = load_accounts(), load_cache()
    login = "0"
    accounts[login] = {"login": login, "server": "Démo", "name": "Compte démo 100k", "terminal": ""}
    cache[login] = make_demo_data()
    save_accounts(accounts)
    save_cache(cache)
    return jsonify(account_summary(login, accounts[login], cache))


@app.get("/api/data")
def api_data():
    target = request.args.get("login", "all")
    accounts, cache = load_accounts(), load_cache()
    logins = [l for l in (accounts if target == "all" else [target]) if l in cache]
    if not logins:
        return jsonify({"error": "Aucune donnée pour ce compte."}), 404

    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    totals = {"balance": 0.0, "equity": 0.0, "start_balance": 0.0}
    currencies = set()
    for login in logins:
        c = cache[login]
        currencies.add(c.get("currency") or "USD")
        for k in totals:
            totals[k] += c.get(k) or 0.0
        for day, v in c.get("daily", {}).items():
            for f in ("pnl", "trades", "wins", "losses"):
                daily[day][f] += v.get(f, 0)

    return jsonify({
        **{k: round(v, 2) for k, v in totals.items()},
        "currency": sorted(currencies)[0],
        "mixed_currencies": len(currencies) > 1,
        "accounts": len(logins),
        "daily": [{"date": d, **{**v, "pnl": round(v["pnl"], 2)}} for d, v in sorted(daily.items())],
    })


# ---------------------------------------------------------------- démarrage
def free_port(preferred=8765):
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


PREFERRED_PORT = 8765

# ---------------------------------------------------------------- mise à jour automatique
UPDATE_REPO = "collignonguillian-design/astra-dashboard"
UPDATE_BRANCH = "main"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(APP_DIR, "version.json")


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


UPDATE_STATUS_FILE = os.path.join(DATA_DIR, "mise-a-jour.json")
# fichiers sans lesquels la nouvelle version ne fonctionne pas
CRITICAL_FILES = ("app.py", "static/index.html")


def _record_update(status):
    status["at"] = dt.datetime.now().isoformat(timespec="seconds")
    try:
        _save(UPDATE_STATUS_FILE, status)
    except OSError:
        pass


def _replace_file(dest, content):
    """Remplace un fichier, en réessayant s'il est momentanément verrouillé par Windows."""
    import time

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".maj"
    with open(tmp, "wb") as f:
        f.write(content)
    for attempt in range(5):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 4:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.4)


def check_for_update():
    """Télécharge la dernière version depuis GitHub si elle est plus récente.
    Renvoie True si la nouvelle version est installée (l'application doit redémarrer)."""
    if getattr(sys, "frozen", False):  # version .exe : pas de mise à jour de fichiers
        return False
    import hashlib
    import subprocess
    import time
    import urllib.request

    base = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/"

    def get(path):
        from urllib.parse import quote

        url = base + quote(path) + f"?t={int(time.time())}"
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()

    local_version = _load(VERSION_FILE, {"version": "0"}).get("version", "0")
    try:
        remote = json.loads(get("version.json").decode("utf-8"))
    except Exception as e:
        _record_update({"ok": False, "step": "vérification", "error": f"{type(e).__name__} : {e}",
                        "installed": local_version})
        return False
    if _version_tuple(remote.get("version")) <= _version_tuple(local_version):
        _record_update({"ok": True, "installed": local_version, "latest": remote.get("version")})
        return False

    # 1) tout télécharger avant de toucher au moindre fichier
    downloads = {}
    try:
        for rel in remote.get("files", []):
            if rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
                continue  # chemin refusé : on reste dans le dossier de l'application
            downloads[rel] = get(rel)
    except Exception as e:
        _record_update({"ok": False, "step": "téléchargement", "error": f"{type(e).__name__} : {e}",
                        "installed": local_version, "latest": remote.get("version")})
        return False

    req_path = os.path.join(APP_DIR, "requirements.txt")
    try:
        with open(req_path, "rb") as f:
            old_requirements = f.read()
    except OSError:
        old_requirements = b""

    # 2) installer : les fichiers verrouillés non essentiels (icône...) sont ignorés
    order = [r for r in downloads if r not in CRITICAL_FILES] + [r for r in CRITICAL_FILES if r in downloads]
    skipped, failed_critical = [], []
    for rel in order:
        try:
            _replace_file(os.path.join(APP_DIR, *rel.split("/")), downloads[rel])
        except Exception as e:
            (failed_critical if rel in CRITICAL_FILES else skipped).append(f"{rel} ({type(e).__name__})")
    if failed_critical:
        _record_update({"ok": False, "step": "installation", "error": "Fichiers impossibles à remplacer : "
                        + ", ".join(failed_critical + skipped), "installed": local_version,
                        "latest": remote.get("version")})
        return False
    try:
        _replace_file(VERSION_FILE, json.dumps(remote, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception:
        pass

    new_requirements = downloads.get("requirements.txt")
    if new_requirements is not None and hashlib.sha256(new_requirements).digest() != hashlib.sha256(old_requirements).digest():
        try:
            subprocess.run(
                [sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "-q", "-r", req_path],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=600,
            )
        except Exception:
            pass
    _record_update({"ok": True, "installed": remote.get("version"), "latest": remote.get("version"),
                    "skipped": skipped})
    return True


def find_app_browser():
    """Edge (présent sur tous les Windows 10/11) ou Chrome, pour une fenêtre d'application."""
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates += [
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    return next((p for p in candidates if os.path.isfile(p)), None)


def open_app_window(url):
    """Ouvre une fenêtre sans barre de navigateur. Renvoie le processus, ou None."""
    import subprocess

    browser = find_app_browser()
    if not browser:
        return None
    profile = os.path.join(DATA_DIR, "fenetre")
    return subprocess.Popen([
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1360,880",
        "--no-first-run",
        "--no-default-browser-check",
    ])


def ensure_desktop_shortcut():
    """Crée ou met à jour le raccourci du bureau. L'icône est copiée sous un nouveau nom
    à chaque changement de logo : Windows ne peut plus afficher l'ancienne image en mémoire."""
    if getattr(sys, "frozen", False) or os.name != "nt":
        return
    import hashlib
    import shutil
    import subprocess

    try:
        ico = os.path.join(APP_DIR, "icone.ico")
        if not os.path.isfile(ico):
            return
        with open(ico, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:10]
        icons_dir = os.path.join(DATA_DIR, "icones")
        os.makedirs(icons_dir, exist_ok=True)
        marker = os.path.join(icons_dir, "raccourci.txt")
        try:
            with open(marker, "r", encoding="utf-8") as f:
                if f.read().strip() == f"{digest}|{APP_DIR}":
                    return  # raccourci déjà à jour
        except OSError:
            pass

        icon_copy = os.path.join(icons_dir, f"astra-{digest}.ico")
        shutil.copy2(ico, icon_copy)
        pythonw = sys.executable
        candidate = os.path.join(os.path.dirname(pythonw), "pythonw.exe")
        if os.path.isfile(candidate):
            pythonw = candidate

        script = (
            "$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ASTRA Dashboard.lnk';"
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk);"
            "$s.TargetPath = $env:ASTRA_TARGET; $s.Arguments = 'app.py';"
            "$s.WorkingDirectory = $env:ASTRA_DIR; $s.IconLocation = $env:ASTRA_ICON + ',0';"
            "$s.Description = 'ASTRA CAPITAL - Dashboard des comptes'; $s.Save()"
        )
        env = {**os.environ, "ASTRA_TARGET": pythonw, "ASTRA_DIR": APP_DIR, "ASTRA_ICON": icon_copy}
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env, capture_output=True, timeout=30, creationflags=no_window,
        )
        if done.returncode != 0:
            return
        # demander à Windows de redessiner les icônes
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, timeout=15, creationflags=no_window)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"{digest}|{APP_DIR}")
        for old in os.listdir(icons_dir):
            if old.startswith("astra-") and old.endswith(".ico") and old != os.path.basename(icon_copy):
                try:
                    os.remove(os.path.join(icons_dir, old))
                except OSError:
                    pass
    except Exception:
        pass  # le raccourci n'est qu'un confort : l'application démarre quand même


def already_running():
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PREFERRED_PORT}/api/state", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def stop_running_instance():
    """Arrête une instance déjà ouverte (y compris les anciennes versions sans /api/quit)."""
    import subprocess
    import time
    import urllib.request

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PREFERRED_PORT}/api/quit", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass
    time.sleep(1)
    if not already_running():
        return
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{PREFERRED_PORT}") and parts[3].upper() in ("LISTENING", "ECOUTE", "ÉCOUTE"):
                subprocess.run(["taskkill", "/PID", parts[4], "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    time.sleep(1)


def restart_updated():
    import subprocess

    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--sans-maj"], cwd=APP_DIR)


def main():
    check = "--sans-maj" not in sys.argv

    if already_running():
        # Une version ouverte tourne déjà : si une mise à jour existe, on la remplace.
        if check and check_for_update():
            stop_running_instance()
            restart_updated()
            return
        if not open_app_window(f"http://127.0.0.1:{PREFERRED_PORT}/"):
            webbrowser.open(f"http://127.0.0.1:{PREFERRED_PORT}/")
        return

    # Nouvelle version sur GitHub : on l'installe puis on redémarre avec.
    if check and check_for_update():
        restart_updated()
        return

    port = free_port(PREFERRED_PORT)
    url = f"http://127.0.0.1:{port}/"
    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server.start()
    threading.Thread(target=lifetime_watchdog, daemon=True).start()
    threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()

    # 1) Fenêtre d'application Edge/Chrome. L'app s'arrête quand la fenêtre se ferme
    #    (signalé par la page), même si Edge reste actif en arrière-plan.
    if open_app_window(url):
        server.join()
        return

    # 2) Fenêtre pywebview si elle est installée.
    try:
        import webview

        webview.create_window(APP_NAME, url, width=1360, height=880, min_size=(900, 640))
        webview.start()
        return
    except Exception:
        pass

    # 3) Dernier recours : le navigateur habituel.
    webbrowser.open(url)
    server.join()


if __name__ == "__main__":
    main()
