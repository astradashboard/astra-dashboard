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


def wait_for_full_history(info, max_wait=45):
    """MT5 télécharge l'historique petit à petit après la connexion.
    On attend qu'il soit complet : la somme de toutes les opérations doit redonner
    le solde du compte (ou, à défaut, le nombre d'opérations doit cesser d'augmenter)."""
    import time

    start, end = dt.datetime(2000, 1, 1), dt.datetime.now() + dt.timedelta(days=2)
    deadline = time.time() + max_wait
    last_count, stable_since, deals = -1, time.time(), []
    while True:
        deals = list(mt5.history_deals_get(start, end) or [])
        total = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        if deals and abs(total - info.balance) < 0.01:
            return deals  # historique complet : il correspond au solde
        now = time.time()
        if len(deals) != last_count:
            last_count, stable_since = len(deals), now
        elif deals and now - stable_since > 8:
            return deals  # plus rien n'arrive depuis 8 s
        if now > deadline:
            return deals
        time.sleep(0.5)


def _iso(ts):
    return dt.datetime.utcfromtimestamp(ts).isoformat(timespec="seconds")  # heure du serveur, comme MT5


def closed_trades(deals, orders=None):
    """Une ligne par position clôturée, avec tout ce qu'affiche le journal :
    sens, volume, prix moyens d'entrée et de sortie, SL/TP, frais, résultat brut et net, heures."""
    entries = {mt5.DEAL_ENTRY_IN}
    exits = {getattr(mt5, n) for n in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_INOUT", "DEAL_ENTRY_OUT_BY") if hasattr(mt5, n)}
    trade_types = {mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL}
    reason_sl = getattr(mt5, "DEAL_REASON_SL", None)
    reason_tp = getattr(mt5, "DEAL_REASON_TP", None)
    orders = {o.ticket: o for o in (orders or [])}

    positions = defaultdict(lambda: {"first": None, "in_vol": 0.0, "in_px": 0.0, "out_vol": 0.0, "out_px": 0.0,
                                     "open": None, "close": None, "g": 0.0, "c": 0.0, "sw": 0.0, "fee": 0.0,
                                     "exit_reason": None, "exit_price": None})
    for d in deals:
        if d.type not in trade_types or not d.position_id:
            continue
        p = positions[d.position_id]
        p["g"] += d.profit
        p["c"] += d.commission
        p["sw"] += d.swap
        p["fee"] += getattr(d, "fee", 0.0)
        if d.entry in entries:
            if p["first"] is None:
                p["first"] = d
            p["in_vol"] += d.volume
            p["in_px"] += d.price * d.volume
            p["open"] = d.time if p["open"] is None else min(p["open"], d.time)
        elif d.entry in exits:
            p["out_vol"] += d.volume
            p["out_px"] += d.price * d.volume
            if p["close"] is None or d.time >= p["close"]:
                p["close"] = d.time
                p["exit_reason"] = getattr(d, "reason", None)
                p["exit_price"] = d.price

    trades = []
    for pid, p in positions.items():
        if p["close"] is None or (p["first"] is not None and p["out_vol"] + 1e-9 < p["in_vol"]):
            continue  # position encore ouverte
        first = p["first"]
        net = p["g"] + p["c"] + p["sw"] + p["fee"]
        t = {"d": dt.datetime.utcfromtimestamp(p["close"]).date().isoformat(), "n": round(net, 2),
             "id": int(pid), "ct": _iso(p["close"]), "g": round(p["g"], 2), "c": round(p["c"], 2),
             "sw": round(p["sw"], 2), "fee": round(p["fee"], 2)}
        if first is not None:
            sl = tp = 0.0
            order = orders.get(first.order)
            if order is not None:
                sl, tp = getattr(order, "sl", 0.0) or 0.0, getattr(order, "tp", 0.0) or 0.0
            if not sl and p["exit_reason"] == reason_sl:
                sl = p["exit_price"]  # stop touché : son prix est le SL final
            if not tp and p["exit_reason"] == reason_tp:
                tp = p["exit_price"]
            t.update({
                "s": first.symbol,
                "side": "buy" if first.type == mt5.DEAL_TYPE_BUY else "sell",
                "v": round(p["in_vol"], 2),
                "ot": _iso(p["open"]),
                "op": round(p["in_px"] / p["in_vol"] if p["in_vol"] else first.price, 5),
                "cp": round(p["out_px"] / p["out_vol"], 5) if p["out_vol"] else None,
                "sl": sl or None,
                "tp": tp or None,
            })
        trades.append(t)
    trades.sort(key=lambda t: t["ct"])
    return trades


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
                deals = wait_for_full_history(info)
                try:  # ordres d'ouverture : SL et TP des trades
                    orders = list(mt5.history_orders_get(dt.datetime(2000, 1, 1),
                                                         dt.datetime.now() + dt.timedelta(days=2)) or [])
                except Exception:
                    orders = []
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

    try:
        trades = closed_trades(deals, orders)
    except Exception:
        trades = None  # bonus : ne doit jamais bloquer la lecture du compte

    return {
        "trades": trades,
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
    demo_trades = []
    day = dt.date.today() - dt.timedelta(days=120)
    balance = size
    while day <= dt.date.today():
        if day.weekday() < 5 and rnd.random() < 0.8:
            trades = rnd.randint(1, 3)
            pnl = round(rnd.gauss(220, 650), 2)
            wins = rnd.randint((trades + 1) // 2, trades) if pnl > 0 else rnd.randint(0, trades // 2)
            daily[day.isoformat()] = {"pnl": pnl, "trades": trades, "wins": wins, "losses": trades - wins}
            # trades fictifs dont la somme redonne le résultat du jour (risque type : 500 $)
            parts = [rnd.uniform(500, 1500) for _ in range(wins)] + [-rnd.uniform(250, 500) for _ in range(trades - wins)]
            if not parts:
                parts = [pnl]
            adj = (pnl - sum(parts)) / len(parts)
            for x in parts:
                net = round(x + adj, 2)
                side = rnd.choice(("buy", "sell"))
                vol = round(rnd.uniform(2, 12), 2)
                op = round(rnd.uniform(19500, 21500), 2)
                move = round(net / (vol * 1.0) / 1.0, 2) if vol else 0  # 1 lot US100 ~ 1 $/point ici
                cp = round(op + move if side == "buy" else op - move, 2)
                risk_pts = round(rnd.uniform(25, 60), 2)
                sl = round(op - risk_pts if side == "buy" else op + risk_pts, 2)
                opened = dt.datetime.combine(day, dt.time(15, 30)) + dt.timedelta(minutes=rnd.randint(0, 240))
                closed = opened + dt.timedelta(seconds=rnd.randint(90, 5400))
                demo_trades.append({
                    "d": day.isoformat(), "n": net, "id": 170000000 + len(demo_trades), "s": "US100.cash",
                    "side": side, "v": vol, "ot": opened.isoformat(timespec="seconds"),
                    "ct": closed.isoformat(timespec="seconds"), "op": op, "cp": cp, "sl": sl, "tp": None,
                    "g": net, "c": 0.0, "sw": 0.0, "fee": 0.0})
            balance += pnl
        day += dt.timedelta(days=1)
    return {
        "currency": "USD",
        "balance": round(balance, 2),
        "equity": round(balance + rnd.uniform(-300, 300), 2),
        "start_balance": size,
        "trades": demo_trades,
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
        "risk": acc.get("risk"),
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
    if isinstance(body.get("eco"), dict):
        eco = body["eco"]
        imp = str(eco.get("imp", "0,1"))
        countries = [str(c)[:2].upper() for c in (eco.get("countries") or []) if isinstance(c, str)][:20]
        rng = str(eco.get("range", "today"))
        if imp in ("-1,0,1", "0,1", "1") and countries:
            prefs["eco"] = {"imp": imp, "countries": countries,
                            "range": rng if rng in ("yesterday", "today", "tomorrow", "week", "nextweek") else "today"}
    if body.get("tvCount") in (1, 2, 3, 4):
        prefs["tvCount"] = body["tvCount"]
    if isinstance(body.get("tv"), list):
        prefs["tv"] = [{"symbol": str(x.get("symbol", ""))[:40], "interval": str(x.get("interval", "15"))[:4]}
                       for x in body["tv"][:4] if isinstance(x, dict)]
    if isinstance(body.get("layout"), list):
        prefs["layout"] = [str(x)[:20] for x in body["layout"]][:10]
    if "heroCur" in body:
        cur = str(body.get("heroCur") or "")[:3].upper()
        prefs["heroCur"] = cur if cur in FX_CURRENCIES else ""
    _save(PREFS_FILE, prefs)
    return jsonify(prefs)


SERVER = {"port": None}
_remote_cache = {"at": 0.0, "version": None}


def latest_ref():
    """Identifiant du dernier commit publié, via l'API GitHub (réponse immédiate, sans cache).
    En cas d'échec (hors ligne, limite de l'API), on retombe sur le nom de la branche."""
    import urllib.request

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/commits/{UPDATE_BRANCH}",
            headers={"Accept": "application/vnd.github.sha", "User-Agent": APP_NAME, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=8) as r:
            sha = r.read().decode("ascii", "ignore").strip()
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
    except Exception:
        pass
    return UPDATE_BRANCH


def remote_version():
    """Dernière version publiée sur GitHub (gardée 60 s en mémoire)."""
    import time
    import urllib.request

    if time.time() - _remote_cache["at"] < 45 and _remote_cache["version"]:
        return _remote_cache["version"]
    url = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{latest_ref()}/version.json?t={int(time.time())}"
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=8) as r:
        v = json.loads(r.read().decode("utf-8")).get("version")
    _remote_cache.update(at=time.time(), version=v)
    return v


@app.get("/api/update/check")
def api_update_check():
    current = _load(VERSION_FILE, {}).get("version", "0")
    try:
        latest = remote_version()
    except Exception:
        return jsonify({"current": current, "latest": None, "available": False})
    return jsonify({"current": current, "latest": latest,
                    "available": _version_tuple(latest) > _version_tuple(current)})


@app.post("/api/update/apply")
def api_update_apply():
    import subprocess

    if not check_for_update():
        status = _load(UPDATE_STATUS_FILE, {})
        if status.get("ok") is False:
            return jsonify({"error": f"Mise à jour impossible ({status.get('step')}) : {status.get('error')}"}), 500
        return jsonify({"error": "Aucune nouvelle version à installer."}), 400
    # relancer la nouvelle version sur le même port : la fenêtre ouverte se recharge simplement
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--sans-maj", f"--relance={SERVER['port']}"], cwd=APP_DIR)
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return jsonify({"ok": True, "version": _load(VERSION_FILE, {}).get("version")})


FX_CURRENCIES = ("EUR", "USD", "CHF", "JPY", "AUD")
FX_FILE = os.path.join(DATA_DIR, "taux-de-change.json")


def _fetch_rates(base):
    """Taux du jour depuis un service gratuit (BCE via Frankfurter, sinon open.er-api.com)."""
    import urllib.request

    others = ",".join(c for c in FX_CURRENCIES if c != base)
    sources = [
        f"https://api.frankfurter.dev/v1/latest?base={base}&symbols={others}",
        f"https://api.frankfurter.app/latest?from={base}&to={others}",
        f"https://open.er-api.com/v6/latest/{base}",
    ]
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            rates = data.get("rates") or {}
            rates = {c: float(rates[c]) for c in FX_CURRENCIES if c in rates}
            if rates:
                rates[base] = 1.0
                date = data.get("date") or data.get("time_last_update_utc") or dt.date.today().isoformat()
                return {"base": base, "rates": rates, "date": str(date)[:16]}
        except Exception:
            continue
    return None


@app.get("/api/fx")
def api_fx():
    import time

    base = str(request.args.get("from", "USD")).upper()[:3]
    if base not in FX_CURRENCIES:
        return jsonify({"error": f"Devise {base} non prise en charge."}), 400
    cache = _load(FX_FILE, {})
    entry = cache.get(base)
    if entry and time.time() - entry.get("fetched", 0) < 3 * 3600:
        return jsonify(entry)
    fresh = _fetch_rates(base)
    if fresh:
        fresh["fetched"] = time.time()
        cache[base] = fresh
        try:
            _save(FX_FILE, cache)
        except OSError:
            pass
        return jsonify(fresh)
    if entry:  # hors ligne : derniers taux connus
        return jsonify({**entry, "stale": True})
    return jsonify({"error": "Taux de change indisponibles (pas de connexion internet ?)."}), 503


ECO_ALLOWED = {"US", "EU", "DE", "FR", "GB", "IT", "ES", "CH", "JP", "CN", "CA", "AU", "NZ"}
_eco_cache = {}


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return str(v)


@app.get("/api/eco")
def api_eco():
    """Annonces du calendrier économique (flux public utilisé par le calendrier de TradingView)."""
    import time
    import urllib.request

    frm, to = str(request.args.get("from", "")), str(request.args.get("to", ""))
    try:
        d0, d1 = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
    except ValueError:
        return jsonify({"error": "Dates invalides."}), 400
    if d1 < d0 or (d1 - d0).days > 31:
        return jsonify({"error": "Période invalide."}), 400
    countries = sorted({c.strip().upper() for c in str(request.args.get("countries", "US")).split(",")} & ECO_ALLOWED) or ["US"]
    key = (frm, to, ",".join(countries))
    hit = _eco_cache.get(key)
    if hit and time.time() - hit["at"] < 120:
        return jsonify(hit["data"])
    # marge d'un jour de chaque côté : l'app filtre ensuite sur les jours de Bruxelles
    start = (d0 - dt.timedelta(days=1)).isoformat() + "T00:00:00.000Z"
    end = (d1 + dt.timedelta(days=1)).isoformat() + "T23:59:59.000Z"
    url = (f"https://economic-calendar.tradingview.com/events?from={start}&to={end}"
           f"&countries={','.join(countries)}")
    req = urllib.request.Request(url, headers={
        "Origin": "https://fr.tradingview.com", "Referer": "https://fr.tradingview.com/",
        "Accept": "application/json", "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        if hit:
            return jsonify({**hit["data"], "stale": True})
        return jsonify({"error": f"service indisponible ({type(ex).__name__})"}), 502
    items = raw.get("result") if isinstance(raw, dict) else raw
    events = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("date"):
            continue
        imp = it.get("importance")
        events.append({
            "id": it.get("id"), "date": it.get("date"), "title": it.get("title") or it.get("indicator"),
            "country": it.get("country"), "currency": it.get("currency"), "period": it.get("period"),
            "importance": imp if imp in (-1, 0, 1) else -1,
            "actual": _num(it.get("actual")), "forecast": _num(it.get("forecast")), "previous": _num(it.get("previous")),
            "unit": it.get("unit") or "", "scale": it.get("scale") or "",
        })
    data = {"events": events}
    _eco_cache[key] = {"at": time.time(), "data": data}
    return jsonify(data)


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


@app.post("/api/risk")
def api_risk():
    body = request.get_json(force=True) or {}
    login = str(body.get("login", ""))
    accounts = load_accounts()
    if login not in accounts:
        return jsonify({"error": "Compte introuvable."}), 404
    mode = body.get("mode")
    if mode is None:
        accounts[login].pop("risk", None)
    else:
        try:
            value = float(str(body.get("value", "")).replace(",", "."))
        except ValueError:
            return jsonify({"error": "Valeur de risque invalide."}), 400
        if mode not in ("amount", "percent") or value <= 0 or (mode == "percent" and value > 100):
            return jsonify({"error": "Valeur de risque invalide."}), 400
        accounts[login]["risk"] = {"mode": mode, "value": value}
    save_accounts(accounts)
    return jsonify({"ok": True, "risk": accounts[login].get("risk")})


@app.post("/api/rename")
def api_rename():
    body = request.get_json(force=True) or {}
    login = str(body.get("login", ""))
    name = str(body.get("name", "")).strip()[:60]
    if not name:
        return jsonify({"error": "Le nom ne peut pas être vide."}), 400
    accounts = load_accounts()
    if login not in accounts:
        return jsonify({"error": "Compte introuvable."}), 404
    accounts[login]["name"] = name
    save_accounts(accounts)
    return jsonify({"ok": True, "name": name})


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


# ---------------------------------------------------------------- rapport mensuel PDF
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
CUR_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CHF": "CHF", "AUD": "A$", "CAD": "C$"}


def _fmt_money(v, cur, sign=False):
    dec = 0 if cur == "JPY" else 2
    txt = f"{abs(v):,.{dec}f}".replace(",", " ").replace(".", ",")
    if float(txt.replace(" ", "").replace(",", ".")) == 0:
        v = 0
    s_ = "+" if sign and v > 0 else ("−" if v < 0 else "")
    return f"{s_}{txt} {CUR_SYMBOL.get(cur, cur)}"


def _fmt_num(v, dec=2):
    return f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")


def _fmt_pct(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + _fmt_num(abs(v)) + " %"


def _fmt_r(v):
    if abs(v) < 0.005:
        v = 0
    return ("+" if v > 0 else "−" if v < 0 else "") + _fmt_num(abs(v)) + " R"


def _cls(v):
    return "pos" if v > 0 else "neg" if v < 0 else ""


def month_report(login, month):
    """Chiffres d'un mois pour un compte (même logique que le tableau de bord)."""
    accounts, cache = load_accounts(), load_cache()
    if login not in accounts or login not in cache:
        raise KeyError("Compte introuvable.")
    acc, c = accounts[login], cache[login]
    cur = c.get("currency") or "USD"
    daily = c.get("daily", {})
    start_cap = (c.get("start_balance") or 0.0) + sum(v["pnl"] for d, v in daily.items() if d < month + "-01")
    days = [(d, v) for d, v in sorted(daily.items()) if d.startswith(month)]
    traded = [(d, v) for d, v in days if v.get("trades") or v.get("pnl")]
    pnl = sum(v["pnl"] for _, v in days)
    trades_n = sum(v.get("trades", 0) for _, v in days)
    wins = sum(v.get("wins", 0) for _, v in days)
    losses = sum(v.get("losses", 0) for _, v in days)
    gain = sum(v["pnl"] for _, v in traded if v["pnl"] > 0)
    loss = -sum(v["pnl"] for _, v in traded if v["pnl"] < 0)
    bal, peak, max_dd = start_cap, start_cap, 0.0
    curve = [("", start_cap)]
    for d, v in days:
        bal += v["pnl"]
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)
        curve.append((d, bal))
    risk_cfg = acc.get("risk")
    risk = None
    if risk_cfg:
        risk = risk_cfg["value"] if risk_cfg["mode"] == "amount" else risk_cfg["value"] / 100 * (c.get("start_balance") or 0)
    trades = [t for t in (c.get("trades") or []) if t.get("d", "").startswith(month)]
    trades.sort(key=lambda t: t.get("ct") or t["d"])
    if trades:  # compteurs alignés sur la liste des positions du rapport
        trades_n = len(trades)
        wins = sum(1 for t in trades if t["n"] > 0)
        losses = sum(1 for t in trades if t["n"] < 0)
    best = max(traded, key=lambda x: x[1]["pnl"]) if traded else None
    worst = min(traded, key=lambda x: x[1]["pnl"]) if traded else None
    return {
        "acc": acc, "cur": cur, "month": month, "start_cap": start_cap, "end_cap": start_cap + pnl, "pnl": pnl,
        "pct": pnl / start_cap * 100 if start_cap else 0.0, "r": pnl / risk if risk else None, "risk": risk,
        "trades_n": trades_n, "wins": wins, "losses": losses, "win_days": sum(1 for _, v in traded if v["pnl"] > 0),
        "traded_days": len(traded), "pf": (gain / loss) if loss else None, "gain": gain, "loss": loss,
        "max_dd": max_dd, "max_dd_pct": max_dd / peak * 100 if peak else 0.0, "best": best, "worst": worst,
        "curve": curve, "days": dict(days), "trades": trades,
    }


def report_html(rep, auto_print=False, preview=False):
    from html import escape as e

    cur, month = rep["cur"], rep["month"]
    y, m = int(month[:4]), int(month[5:7])
    title = f"{MOIS[m - 1].capitalize()} {y}"
    acc = rep["acc"]

    # courbe du capital
    W, H, L, B = 720, 170, 70, 22
    vals = [v for _, v in rep["curve"]]
    lo, hi = min(vals), max(vals)
    pad_ = (hi - lo) * 0.1 or max(50.0, abs(hi) * 0.002)
    lo, hi = lo - pad_, hi + pad_
    n = max(1, len(vals) - 1)
    X = lambda i: L + (W - L - 10) * i / n
    Y = lambda v: 8 + (H - 8 - B) * (1 - (v - lo) / (hi - lo))
    grid = "".join(
        f'<line x1="{L}" x2="{W-10}" y1="{Y(lo+(hi-lo)*k/4):.1f}" y2="{Y(lo+(hi-lo)*k/4):.1f}" stroke="#e5e1da"/>'
        f'<text x="{L-6}" y="{Y(lo+(hi-lo)*k/4)+4:.1f}" text-anchor="end" font-size="9" fill="#8a8f99">{e(_fmt_money(lo+(hi-lo)*k/4, cur).split(",")[0])}</text>'
        for k in range(5))
    path = " ".join(("M" if i == 0 else "L") + f"{X(i):.1f} {Y(v):.1f}" for i, v in enumerate(vals))
    area = path + f" L{X(len(vals)-1):.1f} {H-B} L{X(0):.1f} {H-B} Z"
    labels = ""
    step = max(1, len(vals) // 7)
    for i in range(1, len(vals), step):
        d = rep["curve"][i][0]
        labels += f'<text x="{X(i):.1f}" y="{H-6}" text-anchor="middle" font-size="9" fill="#8a8f99">{int(d[8:])}</text>'
    base_y = Y(rep["start_cap"])
    chart = (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">'
             f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c48a45" stop-opacity=".30"/>'
             f'<stop offset="1" stop-color="#c48a45" stop-opacity="0"/></linearGradient></defs>{grid}'
             f'<line x1="{L}" x2="{W-10}" y1="{base_y:.1f}" y2="{base_y:.1f}" stroke="#8a8f99" stroke-dasharray="4 4"/>'
             f'<path d="{area}" fill="url(#g)"/><path d="{path}" fill="none" stroke="#c48a45" stroke-width="2.2" stroke-linejoin="round"/>'
             f'{labels}</svg>') if len(vals) > 1 else '<p class="muted">Aucun trade ce mois-ci.</p>'

    # synthèse
    def card(k, v, sub="", c_=""):
        return f'<div class="kpi"><div class="k">{k}</div><div class="v {c_}">{e(v)}</div><div class="s">{e(sub)}</div></div>'

    wr = f"{round(rep['wins'] / rep['trades_n'] * 100)} %" if rep["trades_n"] else "—"
    kpis = "".join([
        card("Résultat du mois", _fmt_money(rep["pnl"], cur, True), "", _cls(rep["pnl"])),
        card("Résultat en %", _fmt_pct(rep["pct"]), "du capital en début de mois", _cls(rep["pnl"])),
        card("Résultat en R", _fmt_r(rep["r"]) if rep["r"] is not None else "—",
             f"1 R = {_fmt_money(rep['risk'], cur)}" if rep["risk"] else "Risque par trade non défini",
             _cls(rep["r"] or 0)),
        card("Trades gagnants", str(rep["wins"]), "", "pos" if rep["wins"] else ""),
        card("Trades perdants", str(rep["losses"]), "", "neg" if rep["losses"] else ""),
        card("Trades au total", str(rep["trades_n"]), ""),
        card("Taux de réussite", wr, f"{rep['wins']} gagnants sur {rep['trades_n']} trades"),
        card("Capital début → fin", _fmt_money(rep["start_cap"], cur) + "  →  " + _fmt_money(rep["end_cap"], cur), ""),
    ])

    # trades
    rows = []
    for t in rep["trades"]:
        ct = t.get("ct") or t["d"]
        when = f"{ct[8:10]}/{ct[5:7]}" + (f" {ct[11:16]}" if len(ct) > 10 else "")
        side = {"buy": '<span class="badge buy">Long</span>', "sell": '<span class="badge sell">Short</span>'}.get(t.get("side"), "—")
        r_txt = _fmt_r(t["n"] / rep["risk"]) if rep["risk"] else "—"
        px = lambda v: _fmt_num(v, 2) if isinstance(v, (int, float)) else "—"
        rows.append(f"<tr><td>{e(when)}</td><td><b>{e(t.get('s') or '—')}</b></td><td>{side}</td>"
                    f"<td class='r'>{px(t.get('v'))}</td><td class='r'>{px(t.get('op'))}</td><td class='r'>{px(t.get('cp'))}</td>"
                    f"<td class='r {_cls(t['n'])}'><b>{e(_fmt_money(t['n'], cur, True))}</b></td>"
                    f"<td class='r {_cls(t['n']) if rep['risk'] else ''}'>{e(r_txt)}</td></tr>")
    table = ("<table><thead><tr><th>Clôture</th><th>Symbole</th><th>Sens</th><th class='r'>Lots</th><th class='r'>Entrée</th>"
             "<th class='r'>Sortie</th><th class='r'>PnL</th><th class='r'>R</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
             ) if rows else '<p class="muted">Aucun trade détaillé pour ce mois. Actualise le compte dans l\'app pour charger les trades.</p>'

    now = dt.datetime.now().strftime("%d/%m/%Y à %H:%M")
    acc_line = e(acc.get("name") or acc["login"]) + (f" · {e(acc['login'])} · {e(acc.get('server', ''))}" if acc.get("login") != "0" else " · Démo")
    script = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300));</script>" if auto_print else ""
    preview_css = ("<style>html{background:#3a3f4b}body{width:210mm;max-width:calc(100% - 24px);margin:14px auto;"
                   "padding:12mm;background:#fff;border-radius:4px;box-shadow:0 6px 24px rgba(0,0,0,.35)}</style>") if preview else ""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>ASTRA CAPITAL - {e(acc.get('name') or '')} - {title}</title>
<style>
@page {{ size: A4; margin: 12mm 12mm 14mm; }}
* {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; background: #fff; }}
.rep {{ padding: 0 3px; font: 11px/1.45 "Segoe UI", system-ui, sans-serif; color: #1c2230; background: #fff; }}
.head {{ background: #0f1420; color: #e8ebf2; border-radius: 12px; padding: 18px 20px; text-align: center; }}
.head h1 {{ margin: 0; font-size: 22px; font-weight: 650; }}
.head h1 span {{ color: #c48a45; }}
.head p {{ margin: 3px 0 0; color: #9aa3b5; font-size: 11px; }}
h2 {{ font-size: 13px; margin: 18px 0 8px; color: #0f1420; border-left: 3px solid #c48a45; padding-left: 8px; }}
.kpis {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }}
.kpis .kpi:nth-child(-n+6) {{ grid-column: span 2; }}
.kpis .kpi:nth-child(n+7) {{ grid-column: span 3; }}
.kpi .v {{ font-size: 18px !important; }}

.kpi {{ border: 1px solid #e5e1da; border-radius: 9px; padding: 9px 11px; break-inside: avoid; }}
.kpi .k {{ color: #6b7280; font-size: 10px; }}
.kpi .v {{ font-size: 16px; font-weight: 700; margin-top: 2px; }}
.kpi .s {{ color: #6b7280; font-size: 9.5px; margin-top: 1px; }}
.pos {{ color: #15803d; }} .neg {{ color: #dc2626; }} .muted {{ color: #6b7280; }}
.chart {{ border: 1px solid #e5e1da; border-radius: 9px; padding: 8px 10px 2px; }}
.cal {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; break-inside: avoid; }}
.dow {{ text-align: center; color: #6b7280; font-size: 9px; text-transform: uppercase; letter-spacing: .06em; }}
.day {{ border-radius: 6px; background: #f5f3ef; min-height: 44px; padding: 4px 5px; display: flex; flex-direction: column; justify-content: space-between; }}
.day.empty {{ background: transparent; }}
.day.win {{ background: #e7f6ec; }} .day.loss {{ background: #fdeaea; }}
.day .n {{ color: #6b7280; font-size: 9px; }}
.day .p {{ font-weight: 700; font-size: 10px; text-align: right; }}
.day .t {{ color: #6b7280; font-size: 8.5px; text-align: right; }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th {{ text-align: left; color: #6b7280; font-weight: 600; font-size: 9.5px; text-transform: uppercase; letter-spacing: .05em; padding: 6px; border-bottom: 1.5px solid #c48a45; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #eee9e1; }}
tr {{ break-inside: avoid; }}
.r {{ text-align: right; }}
.badge {{ padding: 1px 7px; border-radius: 5px; font-size: 9.5px; font-weight: 600; }}
.badge.buy {{ background: #e7f6ec; color: #15803d; }} .badge.sell {{ background: #fdeaea; color: #dc2626; }}
.foot {{ margin-top: 16px; color: #9aa3b5; font-size: 9px; text-align: center; }}
</style></head><body><div class="rep">
<div class="head"><h1>Rapport mensuel · <span>{title}</span></h1><p>{acc_line}</p><p>Généré le {now}</p></div>
<h2>Synthèse du mois</h2><div class="kpis">{kpis}</div>
<h2>Évolution du capital</h2><div class="chart">{chart}</div>
<div class="page2"><h2>Trades du mois ({len(rep['trades'])})</h2>{table}</div>
<div class="foot">ASTRA CAPITAL · Données lues depuis MetaTrader 5 · Montants en {e(cur)}{' · R calculé avec 1 R = ' + e(_fmt_money(rep['risk'], cur)) if rep['risk'] else ''}</div>
</div>{script}{preview_css}</body></html>"""


def _valid_month(month):
    return isinstance(month, str) and len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit() and 1 <= int(month[5:]) <= 12


@app.get("/rapport")
def report_page():
    login, month = str(request.args.get("login", "")), str(request.args.get("month", ""))
    if not _valid_month(month):
        return "Mois invalide", 400
    try:
        rep = month_report(login, month)
    except KeyError as ex:
        return str(ex), 404
    return report_html(rep, auto_print=request.args.get("print") == "1", preview=request.args.get("apercu") == "1")


def find_pdf_browser():
    """Navigateur capable de produire un PDF (Edge est présent sur tous les Windows)."""
    exe = find_app_browser()
    if exe:
        return exe
    for p in ("/opt/pw-browsers/chromium", "/usr/bin/chromium", "/usr/bin/google-chrome"):
        if os.path.isfile(p):
            return p
    return None


@app.get("/api/report.pdf")
def api_report_pdf():
    import shutil
    import subprocess
    import tempfile
    from urllib.parse import quote

    login, month = str(request.args.get("login", "")), str(request.args.get("month", ""))
    if not _valid_month(month):
        return jsonify({"error": "Mois invalide."}), 400
    accounts = load_accounts()
    if login not in accounts:
        return jsonify({"error": "Compte introuvable."}), 404
    exe = find_pdf_browser()
    if not exe:
        return jsonify({"error": "Aucun navigateur trouvé pour créer le PDF.", "fallback": True}), 501
    tmp = tempfile.mkdtemp(prefix="astra-pdf-")
    out = os.path.join(tmp, "rapport.pdf")
    url = f"http://127.0.0.1:{SERVER['port'] or PREFERRED_PORT}/rapport?login={quote(login)}&month={month}"
    pdf, detail = None, ""
    for attempt in range(2):
        # profil neuf à chaque fois : un Edge resté ouvert en arrière-plan ne peut pas bloquer la création
        profile = os.path.join(tmp, f"profil-{attempt}")
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--disable-background-mode", "--disable-features=msEdgeStartupBoost",
               f"--user-data-dir={profile}", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               "--virtual-time-budget=4000", f"--print-to-pdf={out}", url]
        if os.name != "nt":
            cmd.insert(1, "--no-sandbox")
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=90,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.path.isfile(out):
                with open(out, "rb") as f:
                    data = f.read()
                if data.startswith(b"%PDF"):
                    pdf = data
                    break
            err = (done.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            detail = f"code {done.returncode}" + (f" · {err[-1][:160]}" if err else "")
        except subprocess.TimeoutExpired:
            detail = "délai dépassé"
        except Exception as ex:
            detail = f"{type(ex).__name__} : {ex}"[:200]
    shutil.rmtree(tmp, ignore_errors=True)
    if pdf is None:
        return jsonify({"error": "La création du PDF a échoué" + (f" ({detail})" if detail else "") + ".",
                        "fallback": True, "browser": os.path.basename(exe)}), 500
    y, m = int(month[:4]), int(month[5:])
    safe = "".join(ch for ch in (accounts[login].get("name") or login) if ch not in '\\/:*?"<>|').strip() or login
    filename = f"ASTRA CAPITAL - {safe} - {MOIS[m - 1].capitalize()} {y}.pdf"
    resp = app.response_class(pdf, mimetype="application/pdf")
    mode = "inline" if request.args.get("inline") == "1" else "attachment"
    resp.headers["Content-Disposition"] = f"{mode}; filename*=UTF-8''{quote(filename)}"
    return resp


@app.get("/api/data")
def api_data():
    target = request.args.get("login", "all")
    accounts, cache = load_accounts(), load_cache()
    logins = [l for l in (accounts if target == "all" else [target]) if l in cache]
    if not logins:
        return jsonify({"error": "Aucune donnée pour ce compte."}), 404

    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    totals = {"balance": 0.0, "equity": 0.0, "start_balance": 0.0}
    trades, per_account = [], {}
    currencies = set()
    for login in logins:
        c = cache[login]
        currencies.add(c.get("currency") or "USD")
        per_account[login] = {
            "name": accounts[login].get("name", login),
            "start_balance": c.get("start_balance") or 0.0,
            "risk": accounts[login].get("risk"),
            "has_trades": c.get("trades") is not None,
        }
        for t in c.get("trades") or []:
            trades.append({**t, "a": login})
        for k in totals:
            totals[k] += c.get(k) or 0.0
        for day, v in c.get("daily", {}).items():
            for f in ("pnl", "trades", "wins", "losses"):
                daily[day][f] += v.get(f, 0)

    return jsonify({
        **{k: round(v, 2) for k, v in totals.items()},
        "currency": sorted(currencies)[0],
        "mixed_currencies": len(currencies) > 1,
        "trades": sorted(trades, key=lambda t: t["d"]),
        "per_account": per_account,
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

    base = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{latest_ref()}/"

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


def serve_only(port):
    """Relance après une mise à jour depuis l'app : même port, pas de nouvelle fenêtre."""
    import time

    deadline = time.time() + 20
    while time.time() < deadline:  # attendre que l'ancienne version libère le port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s_:
            try:
                s_.bind(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.3)
    SERVER["port"] = port
    threading.Thread(target=lifetime_watchdog, daemon=True).start()
    threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def main():
    check = "--sans-maj" not in sys.argv
    relance = next((a for a in sys.argv if a.startswith("--relance=")), None)
    if relance:
        serve_only(int(relance.split("=", 1)[1]))
        return

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
    SERVER["port"] = port
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
