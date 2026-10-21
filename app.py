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


def fetch_mt5(acc, password):
    """Se connecte au compte via le terminal MT5 local et calcule le P&L par jour."""
    if mt5 is None:
        raise RuntimeError(
            "Le module MetaTrader5 n'est pas disponible. L'application fonctionne "
            "uniquement sur Windows avec le terminal MetaTrader 5 installé."
        )
    login = int(acc["login"])
    init_kwargs = {"timeout": 60000}
    if acc.get("terminal"):
        init_kwargs["path"] = acc["terminal"]

    with mt5_lock:
        if not mt5.initialize(**init_kwargs):
            code, msg = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(
                f"Impossible de démarrer MetaTrader 5 ({code} : {msg}). "
                "Vérifie que MT5 est installé, ou indique le chemin de terminal64.exe."
            )
        try:
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


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/favicon.png")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.png")


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
