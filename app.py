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


class HiddenTerminal:
    """Lance le terminal privé sans fenêtre et le ferme à la fin."""

    def __init__(self, exe):
        self.exe = exe
        self.proc = None
        self._stop = threading.Event()

    def __enter__(self):
        import subprocess

        si = subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None
        if si is not None:
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
        self.proc = subprocess.Popen([self.exe, "/portable"], startupinfo=si,
                                     cwd=os.path.dirname(self.exe))
        threading.Thread(target=self._keep_hidden, daemon=True).start()
        return self

    def _keep_hidden(self):
        """MT5 ignore parfois la demande : on masque ses fenêtres tant qu'il tourne."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def hide(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if self.proc and pid.value == self.proc.pid and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)
            return True

        callback = proto(hide)
        while not self._stop.is_set():
            user32.EnumWindows(callback, 0)
            self._stop.wait(0.1)

    def __exit__(self, *exc):
        import subprocess

        self._stop.set()
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


@app.get("/api/version")
def api_version():
    return jsonify({"version": _load(VERSION_FILE, {}).get("version", "?")})


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


def check_for_update():
    """Télécharge la dernière version depuis GitHub si elle est plus récente.
    Renvoie True si des fichiers ont été mis à jour (l'application doit redémarrer)."""
    if getattr(sys, "frozen", False):  # version .exe : pas de mise à jour de fichiers
        return False
    import hashlib
    import subprocess
    import urllib.request

    base = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/"

    def get(path):
        req = urllib.request.Request(base + path, headers={"Cache-Control": "no-cache", "User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.read()

    try:
        local = _load(VERSION_FILE, {"version": "0"})
        remote = json.loads(get("version.json").decode("utf-8"))
        if _version_tuple(remote.get("version")) <= _version_tuple(local.get("version")):
            return False

        old_requirements = b""
        req_path = os.path.join(APP_DIR, "requirements.txt")
        if os.path.exists(req_path):
            with open(req_path, "rb") as f:
                old_requirements = f.read()

        # tout télécharger d'abord, puis écrire : pas de version à moitié installée
        downloads = {}
        for rel in remote.get("files", []):
            if rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
                continue  # chemin refusé : on reste dans le dossier de l'application
            downloads[rel] = get(rel)
        for rel, content in downloads.items():
            dest = os.path.join(APP_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".maj"
            with open(tmp, "wb") as f:
                f.write(content)
            os.replace(tmp, dest)
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump(remote, f, ensure_ascii=False, indent=2)

        new_requirements = downloads.get("requirements.txt")
        if new_requirements is not None and hashlib.sha256(new_requirements).digest() != hashlib.sha256(old_requirements).digest():
            subprocess.run(
                [sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "-q", "-r", req_path],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=600,
            )
        return True
    except Exception:
        return False  # hors ligne ou GitHub indisponible : on garde la version actuelle


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


def main():
    # Déjà ouverte : on réaffiche simplement une fenêtre.
    if already_running():
        if not open_app_window(f"http://127.0.0.1:{PREFERRED_PORT}/"):
            webbrowser.open(f"http://127.0.0.1:{PREFERRED_PORT}/")
        return

    # Nouvelle version sur GitHub : on l'installe puis on redémarre avec.
    if "--sans-maj" not in sys.argv and check_for_update():
        import subprocess

        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--sans-maj"], cwd=APP_DIR)
        return

    port = free_port(PREFERRED_PORT)
    url = f"http://127.0.0.1:{port}/"
    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server.start()
    threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()

    # 1) Fenêtre d'application Edge/Chrome : l'app s'arrête quand on la ferme.
    proc = open_app_window(url)
    if proc:
        proc.wait()
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
