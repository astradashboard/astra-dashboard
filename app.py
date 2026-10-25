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


def _connect(acc, password, exe):
    """Démarre la connexion MT5 sur le terminal privé (déjà lancé) et renvoie account_info."""
    login = int(acc["login"])
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
    info = mt5.account_info()
    if info is None or info.login != login:
        if not mt5.login(login, password=password, server=acc["server"], timeout=60000):
            code, msg = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(
                f"Connexion refusée ({code} : {msg}). Vérifie le numéro de compte, "
                "le mot de passe et le nom exact du serveur."
            )
        info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise RuntimeError("Impossible de lire les informations du compte.")
    return info


def _history_orders():
    try:  # ordres d'ouverture : SL et TP des trades
        return list(mt5.history_orders_get(dt.datetime(2000, 1, 1), dt.datetime.now() + dt.timedelta(days=2)) or [])
    except Exception:
        return []


def build_account_data(info, deals, orders):
    """Calcule le P&L par jour, le capital de départ et la liste des trades à partir de l'historique MT5."""
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


class Mt5Session:
    """Une seule copie cachée de MT5, gardée ouverte : on change de compte à l'intérieur
    (quelques secondes) au lieu de relancer le terminal à chaque lecture."""

    IDLE_CLOSE = 90  # secondes sans utilisation avant de fermer MT5

    def __init__(self):
        self.exe = self.term = self.login = None
        self.used = 0.0

    def _close_locked(self):
        try:
            mt5.shutdown()
        except Exception:
            pass
        if self.term is not None:
            try:
                self.term.__exit__(None, None, None)
            except Exception:
                pass
        self.exe = self.term = self.login = None

    def close(self):
        with mt5_lock:
            self._close_locked()

    def ensure(self, acc, password):
        """À appeler avec mt5_lock : renvoie account_info du compte demandé, connecté."""
        import time

        self.used = time.time()
        exe = private_terminal(acc)
        login = int(acc["login"])
        if self.term is not None and self.exe == exe:
            try:
                info = mt5.account_info()
            except Exception:
                info = None
            if info is not None and info.login == login:
                self.login = acc["login"]
                return info
            if info is not None:  # terminal en vie, autre compte : simple changement de compte
                if mt5.login(login, password=password, server=acc["server"], timeout=60000):
                    info = mt5.account_info()
                    if info is not None and info.login == login:
                        self.login = acc["login"]
                        return info
                code, msg = mt5.last_error()
                if code == -6:
                    raise RuntimeError(f"Connexion refusée ({msg}). Vérifie le numéro de compte, "
                                       "le mot de passe et le nom exact du serveur.")
            self._close_locked()  # terminal bloqué ou fermé : on repart proprement
        elif self.term is not None:
            self._close_locked()  # autre broker : autre terminal
        term = HiddenTerminal(exe)
        term.__enter__()
        try:
            info = _connect(acc, password, exe)
        except Exception:
            term.__exit__(None, None, None)
            raise
        self.exe, self.term, self.login = exe, term, acc["login"]
        self.used = time.time()
        return info


SESSION = Mt5Session()


def fetch_mt5(acc, password):
    """Lit le compte via la copie cachée de MT5 et calcule le P&L par jour."""
    import time

    if mt5 is None:
        raise RuntimeError(
            "Le module MetaTrader5 n'est pas disponible. L'application fonctionne "
            "uniquement sur Windows avec le terminal MetaTrader 5 installé."
        )
    _start_mt5_threads()
    with mt5_lock:
        try:
            info = SESSION.ensure(acc, password)
        except OSError as e:
            raise RuntimeError(f"Impossible de préparer le terminal MT5 de l'application : {e}")
        deals = wait_for_full_history(info)
        orders = _history_orders()
        SESSION.used = time.time()
    return build_account_data(info, deals, orders)


# ---------------------------------------------------------------- suivi en direct
LIVE = {"login": None, "snap": None, "status": "off", "error": None, "error_at": 0.0, "seen": 0.0,
        "hist_rev": 0, "hist": {}}
live_lock = threading.Lock()
_threads = {"started": False}


def live_stop():
    with live_lock:
        LIVE["login"] = None
        LIVE["seen"] = 0.0


def _start_mt5_threads():
    with live_lock:
        if _threads["started"]:
            return
        _threads["started"] = True
    threading.Thread(target=_live_loop, daemon=True).start()
    threading.Thread(target=_idle_closer, daemon=True).start()


def _idle_closer():
    import time

    while True:
        time.sleep(10)
        watching = time.time() - LIVE["seen"] < 30
        if SESSION.term is not None and not watching and time.time() - SESSION.used > Mt5Session.IDLE_CLOSE:
            if mt5_lock.acquire(timeout=1):
                try:
                    if time.time() - SESSION.used > Mt5Session.IDLE_CLOSE:
                        SESSION._close_locked()
                finally:
                    mt5_lock.release()


def _server_iso(ts):
    return dt.datetime.utcfromtimestamp(ts).isoformat(timespec="seconds")


def _live_snapshot(info, positions):
    now_srv = None
    out = []
    for p_ in positions:
        tick = None
        try:
            tick = mt5.symbol_info_tick(p_.symbol)
        except Exception:
            pass
        if tick is not None and getattr(tick, "time", 0):
            now_srv = max(now_srv or 0, tick.time)
        out.append({
            "ticket": int(p_.ticket), "symbol": p_.symbol,
            "side": "buy" if p_.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume": p_.volume, "open": p_.price_open, "current": p_.price_current,
            "sl": p_.sl or None, "tp": p_.tp or None, "profit": round(p_.profit + p_.swap, 2),
            "opened": _server_iso(p_.time),
        })
    for o in out:
        open_ts = dt.datetime.fromisoformat(o["opened"]).replace(tzinfo=dt.timezone.utc).timestamp()
        o["duration"] = max(0, int((now_srv or open_ts) - open_ts)) if now_srv else None
    return {
        "balance": info.balance, "equity": info.equity, "profit": round(info.profit, 2),
        "margin": info.margin, "free_margin": info.margin_free, "currency": info.currency,
        "positions": sorted(out, key=lambda x: x["opened"]),
        "at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _live_loop():
    """Toutes les secondes : équité et positions du compte suivi.
    L'historique est relu en arrière-plan, sans bloquer l'affichage des trades en cours."""
    import time

    while True:
        time.sleep(1.0)
        with live_lock:
            login = LIVE["login"]
            watching = login and time.time() - LIVE["seen"] < 30
            in_error = LIVE["status"] == "error" and time.time() - LIVE["error_at"] < 15
        if not watching or in_error:
            continue
        acc = load_accounts().get(login)
        password = get_saved_password(login)
        if not acc or not password:
            with live_lock:
                if LIVE["login"] == login:
                    LIVE["status"] = "need_password"
            continue
        hist = LIVE["hist"].setdefault(login, {"at": 0.0, "tries": 0, "count": None, "pending": True})
        try:
            with mt5_lock:
                info = SESSION.ensure(acc, password)
                positions = list(mt5.positions_get() or [])
                snap = _live_snapshot(info, positions)
                if hist["count"] is not None and len(positions) != hist["count"]:
                    hist["pending"] = True  # un trade s'est ouvert ou fermé
                hist["count"] = len(positions)
                deals = orders = None
                if hist["pending"] or time.time() - hist["at"] > 120:
                    deals = list(mt5.history_deals_get(dt.datetime(2000, 1, 1),
                                                       dt.datetime.now() + dt.timedelta(days=2)) or [])
                    orders = _history_orders()
            with live_lock:
                if LIVE["login"] == login:
                    LIVE["snap"], LIVE["status"], LIVE["error"] = snap, "running", None
            if deals is not None:
                total = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
                complete = deals and abs(total - info.balance) < 0.01
                hist["tries"] += 1
                if complete or hist["tries"] >= 20:  # historique complet (ou stable depuis ~20 s)
                    data = build_account_data(info, deals, orders)
                    cache = load_cache()
                    cache[login] = data
                    save_cache(cache)
                    hist.update(at=time.time(), tries=0, pending=False)
                    with live_lock:
                        LIVE["hist_rev"] += 1
        except Exception as ex:
            with live_lock:
                if LIVE["login"] == login:
                    LIVE["status"], LIVE["error"], LIVE["error_at"] = "error", str(ex), time.time()


# ---------------------------------------------------------------- cotations des indices
QUOTE_CATS = ["Devises", "Indices", "Matières premières", "Obligations", "Actions", "ETF", "Crypto"]
QUOTE_LIST = [  # (clé, nom, drapeau, catégorie, noms possibles du symbole selon le broker)
    ("us30", "US 30", "🇺🇸", "Indices", ["US30.cash", "US30", "US30Cash", "DJ30", "WS30", "DJI30", "USA30"]),
    ("us500", "US 500", "🇺🇸", "Indices", ["US500.cash", "US500", "US500Cash", "SPX500", "SP500", "USA500"]),
    ("us100", "US Tech 100", "🇺🇸", "Indices", ["US100.cash", "US100", "US100Cash", "NAS100", "USTEC", "NDX100", "USTECH"]),
    ("us2000", "Russell 2000", "🇺🇸", "Indices", ["US2000.cash", "US2000", "RUSS2000", "US2000Cash"]),
    ("vix", "S&P 500 VIX", "🇺🇸", "Indices", ["VIX", "VIX.cash", "VIXX", "VOLX"]),
    ("ger40", "DAX", "🇩🇪", "Indices", ["GER40.cash", "GER40", "DE40", "GER30", "DAX40", "DE30"]),
    ("fra40", "CAC 40", "🇫🇷", "Indices", ["FRA40.cash", "FRA40", "FR40", "F40"]),
    ("eu50", "Euro Stoxx 50", "🇪🇺", "Indices", ["EU50.cash", "EU50", "EUSTX50", "STOXX50"]),
    ("uk100", "FTSE 100", "🇬🇧", "Indices", ["UK100.cash", "UK100", "FTSE100"]),
    ("jp225", "Nikkei 225", "🇯🇵", "Indices", ["JP225.cash", "JP225", "JPN225", "NIKKEI225", "JAP225"]),
    ("hk50", "Hang Seng", "🇭🇰", "Indices", ["HK50.cash", "HK50", "HSI50", "HKG50"]),
    ("aus200", "ASX 200", "🇦🇺", "Indices", ["AUS200.cash", "AUS200", "AU200", "ASX200"]),
    ("xauusd", "Or", "🥇", "Matières premières", ["XAUUSD", "GOLD", "XAUUSD.cash"]),
    ("xagusd", "Argent", "🥈", "Matières premières", ["XAGUSD", "SILVER", "XAGUSD.cash"]),
    ("oil", "Pétrole WTI", "🛢", "Matières premières", ["USOIL.cash", "USOIL", "WTI", "XTIUSD", "CL.cash"]),
    ("brent", "Pétrole Brent", "🛢", "Matières premières", ["UKOIL.cash", "UKOIL", "BRENT", "XBRUSD"]),
    ("natgas", "Gaz naturel", "🔥", "Matières premières", ["NATGAS.cash", "NATGAS", "XNGUSD", "NGAS"]),
    ("copper", "Cuivre", "🟤", "Matières premières", ["COPPER", "COPPER.cash", "XCUUSD", "HG.cash"]),
    ("xptusd", "Platine", "⚪", "Matières premières", ["XPTUSD", "PLATINUM"]),
    ("us10y", "US 10 ans", "🇺🇸", "Obligations", ["US10Y", "UST10Y", "USTN10", "TNOTE", "US10YR", "T-NOTE", "UST10Y.f", "USTN10.f"]),
    ("us30y", "US 30 ans", "🇺🇸", "Obligations", ["US30Y", "UST30Y", "USTBOND", "TBOND", "US30YR", "T-BOND", "UST30Y.f"]),
    ("bund", "Bund allemand", "🇩🇪", "Obligations", ["BUND", "EUBUND", "GER10Y", "DE10Y", "FGBL", "BUND.f", "EUBUND.f"]),
    ("gilt", "Gilt britannique", "🇬🇧", "Obligations", ["GILT", "UK10Y", "UKGILT", "GILT.f", "UKGB"]),
    ("jgb", "JGB japonais", "🇯🇵", "Obligations", ["JGB", "JP10Y", "JGB.f"]),
    ("aapl", "Apple", "🇺🇸", "Actions", ["AAPL", "AAPL.US", "#AAPL", "AAPL.NAS", "AAPL.OQ", "Apple"]),
    ("msft", "Microsoft", "🇺🇸", "Actions", ["MSFT", "MSFT.US", "#MSFT", "MSFT.NAS", "MSFT.OQ", "Microsoft"]),
    ("nvda", "Nvidia", "🇺🇸", "Actions", ["NVDA", "NVDA.US", "#NVDA", "NVDA.NAS", "NVDA.OQ", "Nvidia"]),
    ("tsla", "Tesla", "🇺🇸", "Actions", ["TSLA", "TSLA.US", "#TSLA", "TSLA.NAS", "TSLA.OQ", "Tesla"]),
    ("amzn", "Amazon", "🇺🇸", "Actions", ["AMZN", "AMZN.US", "#AMZN", "AMZN.NAS", "AMZN.OQ", "Amazon"]),
    ("meta", "Meta", "🇺🇸", "Actions", ["META", "META.US", "#META", "META.NAS", "META.OQ", "FB", "Meta"]),
    ("googl", "Alphabet", "🇺🇸", "Actions", ["GOOGL", "GOOGL.US", "#GOOGL", "GOOG", "GOOGL.NAS", "GOOGL.OQ", "Alphabet"]),
    ("nflx", "Netflix", "🇺🇸", "Actions", ["NFLX", "NFLX.US", "#NFLX", "NFLX.NAS", "NFLX.OQ", "Netflix"]),
    ("spy", "SPDR S&P 500", "🇺🇸", "ETF", ["SPY", "SPY.US", "#SPY", "SPY.ARCA"]),
    ("qqq", "Invesco QQQ", "🇺🇸", "ETF", ["QQQ", "QQQ.US", "#QQQ", "QQQ.NAS"]),
    ("iwm", "iShares Russell 2000", "🇺🇸", "ETF", ["IWM", "IWM.US", "#IWM", "IWM.ARCA"]),
    ("gld", "SPDR Gold", "🇺🇸", "ETF", ["GLD", "GLD.US", "#GLD", "GLD.ARCA"]),
    ("eurusd", "EUR/USD", "🇪🇺", "Devises", ["EURUSD"]),
    ("gbpusd", "GBP/USD", "🇬🇧", "Devises", ["GBPUSD"]),
    ("usdjpy", "USD/JPY", "🇯🇵", "Devises", ["USDJPY"]),
    ("usdchf", "USD/CHF", "🇨🇭", "Devises", ["USDCHF"]),
    ("audusd", "AUD/USD", "🇦🇺", "Devises", ["AUDUSD"]),
    ("usdcad", "USD/CAD", "🇨🇦", "Devises", ["USDCAD"]),
    ("eurgbp", "EUR/GBP", "🇪🇺", "Devises", ["EURGBP"]),
    ("nzdusd", "NZD/USD", "🇳🇿", "Devises", ["NZDUSD"]),
    ("dxy", "Dollar Index", "💵", "Devises", ["DXY", "DXY.cash", "USDX", "DX.cash"]),
    ("btcusd", "Bitcoin", "₿", "Crypto", ["BTCUSD", "BTCUSD.cash", "BTC/USD"]),
    ("ethusd", "Ethereum", "Ξ", "Crypto", ["ETHUSD", "ETHUSD.cash", "ETH/USD"]),
    ("solusd", "Solana", "◎", "Crypto", ["SOLUSD", "SOLUSD.cash"]),
    ("xrpusd", "XRP", "✕", "Crypto", ["XRPUSD", "XRPUSD.cash"]),
    ("ltcusd", "Litecoin", "Ł", "Crypto", ["LTCUSD", "LTCUSD.cash"]),
]
QUOTE_DEFAULT = ["us30", "us500", "us100", "vix", "ger40", "jp225"]
QUOTE_TAB_DEFAULT = {
    "Devises": ["eurusd", "gbpusd", "usdjpy", "usdchf", "audusd", "usdcad", "dxy"],
    "Indices": ["us30", "us500", "us100", "us2000", "vix", "ger40", "jp225"],
    "Matières premières": ["xauusd", "xagusd", "oil", "brent", "natgas", "copper"],
    "Obligations": ["us10y", "us30y", "bund", "gilt", "jgb"],
    "Actions": ["aapl", "msft", "nvda", "tsla", "amzn", "meta", "googl"],
    "ETF": ["spy", "qqq", "iwm", "gld"],
    "Crypto": ["btcusd", "ethusd", "solusd", "xrpusd", "ltcusd"],
}
QUOTE_TABS_ON = ["Indices", "Matières premières", "Obligations", "Actions"]
_quote_symbols = {"exe": None, "map": {}, "names": {}}
_quote_state = {}


def _resolve_symbols():
    """Trouve, pour chaque indice, le nom exact du symbole chez le broker connecté."""
    if _quote_symbols["exe"] == SESSION.exe and _quote_symbols["map"]:
        return _quote_symbols["map"]
    found = {}
    try:
        names = {sy.name.lower(): sy.name for sy in (mt5.symbols_get() or [])}
    except Exception:
        names = {}
    for key, _n, _f, _cat, cands in QUOTE_LIST:
        for c in cands:
            real = names.get(c.lower())
            if real:
                found[key] = real
                break
    _quote_symbols.update(exe=SESSION.exe, map=found, names=names)
    return found


def _quote_row(key, name, flag, real):
    import time

    try:
        mt5.symbol_select(real, True)
        tick = mt5.symbol_info_tick(real)
        rates = mt5.copy_rates_from_pos(real, mt5.TIMEFRAME_D1, 0, 2)
        info = mt5.symbol_info(real)
    except Exception:
        return None
    if tick is None or rates is None or len(rates) == 0 or not tick.bid:
        return None
    last = float(tick.bid)
    today = rates[-1]
    prev = float(rates[-2]["close"] if len(rates) > 1 else today["open"])
    st = _quote_state.setdefault(key, {"last": None, "at": None})
    if st["last"] != last:
        st["last"], st["at"] = last, time.time()
    return {"key": key, "name": name, "flag": flag, "symbol": real, "digits": getattr(info, "digits", 2) if info else 2,
            "last": last, "prev": prev, "high": max(float(today["high"]), last), "low": min(float(today["low"]), last),
            "chg": last - prev, "pct": (last - prev) / prev * 100 if prev else 0.0, "tt": int(getattr(tick, "time", 0) or 0),
            "at": dt.datetime.fromtimestamp(st["at"]).strftime("%H:%M:%S") if st["at"] else None}


def _read_quotes(keys):
    out, missing = [], []
    sym = _resolve_symbols()
    names = _quote_symbols.get("names") or {}
    meta = {q[0]: q for q in QUOTE_LIST}
    for key in keys:
        if key.startswith("x:"):  # symbole libre tapé par l'utilisateur
            real = names.get(key[2:].lower())
            row = _quote_row(key, real or key[2:], "📈", real) if real else None
        else:
            q = meta.get(key)
            real = sym.get(key) if q else None
            row = _quote_row(key, q[1], q[2], real) if real else None
        if row:
            out.append(row)
        else:
            missing.append(key)
    _mark_open(out)
    return out, missing


_server_offset = {"h": None}


def _mark_open(rows):
    """Marché ouvert = dernier prix reçu il y a moins de 15 min (heure du serveur du broker).
    Le décalage horaire du serveur est déduit du tick le plus récent quand un marché tourne."""
    import time

    tts = [r.get("tt") or 0 for r in rows]
    try:  # les paires de devises cotent 24 h/24 en semaine : bon repère pour l'heure du serveur
        ref = mt5.symbol_info_tick("EURUSD")
        if ref is not None and getattr(ref, "time", 0):
            tts.append(int(ref.time))
    except Exception:
        pass
    latest = max(tts or [0])
    if latest:
        off = (latest - time.time()) / 3600
        if -12.5 <= off <= 14.5 and abs(off - round(off * 2) / 2) < 0.2:
            _server_offset["h"] = round(off * 2) / 2
    off = _server_offset["h"] if _server_offset["h"] is not None else 2
    now_srv = time.time() + off * 3600
    for r in rows:
        tt = r.pop("tt", 0)
        r["open"] = bool(tt) and now_srv - tt < 15 * 60


_demo_quotes = {"rnd": random.Random(11), "v": None}


QUOTE_DEMO_BASE = {"us30": (52449, 53068, 2), "us500": (7671.25, 7704.5, 2), "us100": (29163.25, 29142, 2), "us2000": (2512.4, 2498.7, 2),
                   "vix": (19.35, 19.38, 2), "ger40": (25261.1, 25261.0, 1), "fra40": (8120.4, 8095.2, 1), "eu50": (5710.3, 5698.1, 1),
                   "uk100": (9650.3, 9632.1, 1), "jp225": (62617.5, 62362.5, 1), "hk50": (26120.0, 26305.0, 1), "aus200": (8890.2, 8872.4, 1),
                   "xauusd": (4105.2, 4088.9, 2), "xagusd": (48.62, 48.10, 3), "oil": (61.85, 62.40, 2), "brent": (65.70, 66.12, 2),
                   "natgas": (3.412, 3.385, 3), "eurusd": (1.1652, 1.1640, 5), "gbpusd": (1.3375, 1.3390, 5), "usdjpy": (151.82, 151.40, 3),
                   "usdchf": (0.7962, 0.7975, 5), "audusd": (0.6545, 0.6530, 5), "usdcad": (1.3990, 1.4005, 5), "dxy": (98.62, 98.75, 3),
                   "btcusd": (108250.0, 106900.0, 2), "ethusd": (3890.5, 3842.0, 2), "solusd": (186.4, 181.9, 2),
                   "copper": (5.12, 5.08, 4), "xptusd": (1585.4, 1572.0, 2), "us10y": (110.84, 110.62, 3), "us30y": (114.5, 114.9, 2),
                   "bund": (129.12, 129.40, 2), "gilt": (91.35, 91.20, 2), "jgb": (136.1, 136.25, 2),
                   "aapl": (262.8, 259.6, 2), "msft": (523.6, 520.5, 2), "nvda": (186.3, 182.2, 2), "tsla": (433.7, 448.9, 2),
                   "amzn": (224.2, 221.1, 2), "meta": (738.4, 733.3, 2), "googl": (259.9, 253.1, 2), "nflx": (1094.0, 1115.2, 2),
                   "spy": (677.3, 671.6, 2), "qqq": (617.1, 610.6, 2), "iwm": (249.6, 247.0, 2), "gld": (377.5, 375.9, 2),
                   "eurgbp": (0.8712, 0.8705, 5), "nzdusd": (0.5735, 0.5722, 5), "xrpusd": (2.452, 2.398, 4), "ltcusd": (96.3, 94.1, 2)}


def _demo_open(cat, now=None):
    """Mode démo : horaires d'ouverture approximatifs (heure UTC)."""
    now = now or dt.datetime.utcnow()
    if cat == "Crypto":
        return True
    wd, h = now.weekday(), now.hour + now.minute / 60
    if wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21):
        return False
    if cat in ("Actions", "ETF"):
        return wd < 5 and 13.5 <= h < 20
    return not (21 <= h < 22)


def _demo_quote_list(keys):
    import time

    rnd = _demo_quotes["rnd"]
    if _demo_quotes["v"] is None:
        _demo_quotes["v"] = {k: {"last": v[0], "high": v[0] * 1.004, "low": v[0] * 0.995, "at": time.time()} for k, v in QUOTE_DEMO_BASE.items()}
    meta = {q[0]: q for q in QUOTE_LIST}
    out, missing = [], []
    for key in keys:
        if key not in QUOTE_DEMO_BASE:
            missing.append(key)
            continue
        _k, name, flag, _cat, _c = meta[key]
        last0, prev, dg = QUOTE_DEMO_BASE[key]
        q = _demo_quotes["v"][key]
        if rnd.random() < 0.6 and _demo_open(_cat):
            q["last"] = round(q["last"] * (1 + rnd.gauss(0, 0.00025)), dg)
            q["at"] = time.time()
            q["high"], q["low"] = max(q["high"], q["last"]), min(q["low"], q["last"])
        out.append({"key": key, "name": name, "flag": flag, "symbol": key.upper(), "digits": dg, "last": q["last"], "prev": prev,
                    "open": _demo_open(_cat),
                    "high": q["high"], "low": q["low"], "chg": q["last"] - prev, "pct": (q["last"] - prev) / prev * 100,
                    "at": dt.datetime.fromtimestamp(q["at"]).strftime("%H:%M:%S")})
    return out, missing


def _quote_keys():
    raw = [k.strip() for k in str(request.args.get("keys", "")).split(",") if k.strip()]
    known = {q[0] for q in QUOTE_LIST}
    keys = [k for k in raw if k in known or (k.startswith("x:") and 2 < len(k) <= 40)][:30]
    return keys if raw else list(QUOTE_DEFAULT)


@app.get("/api/quotes/catalog")
def api_quotes_catalog():
    """Liste des marchés proposés, et ceux que le broker connecté propose vraiment."""
    avail = set(_quote_symbols["map"]) if _quote_symbols["map"] else None
    return jsonify({"default": QUOTE_DEFAULT, "cats": QUOTE_CATS, "tab_default": QUOTE_TAB_DEFAULT, "tabs_on": QUOTE_TABS_ON, "items": [
        {"key": k, "name": n, "flag": f, "cat": c, "available": None if avail is None else k in avail} for k, n, f, c, _x in QUOTE_LIST]})


QUOTE_RANGES = {  # période → (unité de temps MT5, nombre de barres)
    "1D": ("M5", 288), "1W": ("M30", 336), "1M": ("H4", 186), "6M": ("D1", 130),
    "1Y": ("D1", 260), "5Y": ("W1", 262), "MAX": ("MN1", 600)}


def _demo_history(key, rng):
    import math
    import time

    if key not in QUOTE_DEMO_BASE:
        return None
    tf, n = QUOTE_RANGES[rng]
    step = {"M5": 300, "M30": 1800, "H4": 14400, "D1": 86400, "W1": 604800, "MN1": 2629800}[tf]
    last = (_demo_quotes["v"] or {}).get(key, {}).get("last") or QUOTE_DEMO_BASE[key][0]
    prev = QUOTE_DEMO_BASE[key][1]
    rnd = random.Random(hash((key, rng)) & 0xffffffff)
    vol = {"M5": .00028, "M30": .0012, "H4": .004, "D1": .011, "W1": .024, "MN1": .05}[tf]
    pts, v = [], last
    now = int(time.time()) // step * step
    for i in range(n):
        pts.append([now - i * step, round(v, 6)])
        v = v / (1 + rnd.gauss(0.0004 if tf in ("W1", "MN1") else 0, vol))
    pts.reverse()
    if rng == "1D":  # la courbe du jour part de la clôture de la veille
        w0, wn, m = pts[0][1], pts[-1][1], len(pts) - 1
        pts = [[t, round(prev + (p - w0) - (wn - w0) * i / m + (last - prev) * i / m, 6)] for i, (t, p) in enumerate(pts)]
        pts[-1][1] = last
    return pts


@app.get("/api/quotes/history")
def api_quotes_history():
    """Historique d'un marché pour le petit graphique (barres MT5 du broker)."""
    key = str(request.args.get("key", ""))[:40]
    rng = str(request.args.get("range", "1D")).upper()
    if rng not in QUOTE_RANGES:
        rng = "1D"
    accounts, cache = load_accounts(), load_cache()
    if mt5 is None or not accounts or all(cache.get(l, {}).get("demo") for l in accounts):
        pts = _demo_history(key, rng)
        return jsonify({"status": "demo", "points": pts or [], "prev": QUOTE_DEMO_BASE.get(key, (0, 0))[1]})
    if not mt5_lock.acquire(timeout=3):
        return jsonify({"status": "busy"})
    try:
        if SESSION.term is None:
            return jsonify({"status": "starting"})
        sym = _resolve_symbols()
        real = (_quote_symbols.get("names") or {}).get(key[2:].lower()) if key.startswith("x:") else sym.get(key)
        if not real:
            return jsonify({"status": "missing", "points": []})
        tf, n = QUOTE_RANGES[rng]
        mt5.symbol_select(real, True)
        rates = mt5.copy_rates_from_pos(real, getattr(mt5, "TIMEFRAME_" + tf), 0, n)
        d1 = mt5.copy_rates_from_pos(real, mt5.TIMEFRAME_D1, 0, 2)
        off = (_server_offset["h"] if _server_offset["h"] is not None else 2) * 3600
        pts = [[int(r["time"] - off), float(r["close"])] for r in (rates if rates is not None else [])]
        if rng == "1D" and d1 is not None and len(d1):  # séance du jour uniquement
            start = int(d1[-1]["time"] - off)
            day = [p for p in pts if p[0] >= start]
            pts = day if len(day) >= 6 else pts
        prev = float(d1[-2]["close"]) if d1 is not None and len(d1) > 1 else None
        return jsonify({"status": "running", "points": pts, "prev": prev})
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex), "points": []})
    finally:
        mt5_lock.release()


@app.get("/api/quotes")
def api_quotes():
    """Cours des grands indices, lus dans la copie cachée de MT5 (mêmes prix que le broker)."""
    accounts, cache = load_accounts(), load_cache()
    if mt5 is None or not accounts or all(cache.get(l, {}).get("demo") for l in accounts):
        q, miss = _demo_quote_list(_quote_keys())
        return jsonify({"status": "demo", "quotes": q, "missing": miss})
    _start_mt5_threads()
    # un compte avec mot de passe mémorisé suffit : les cours sont les mêmes pour tous les comptes du broker
    pref = str(request.args.get("login", ""))
    order = ([pref] if pref in accounts else []) + [l for l in accounts if l != pref]
    acc = next((accounts[l] for l in order if not cache.get(l, {}).get("demo") and get_saved_password(l)), None)
    if acc is None:
        return jsonify({"status": "need_password",
                        "error": "Mémorise le mot de passe d'un compte pour afficher les cours en direct."})
    if not mt5_lock.acquire(timeout=3):
        return jsonify({"status": "busy"})  # MT5 occupé (actualisation en cours) : réessayer plus tard
    try:
        if SESSION.term is None:
            SESSION.ensure(acc, get_saved_password(acc["login"]))
        import time
        SESSION.used = time.time()
        quotes, miss = _read_quotes(_quote_keys())
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex)})
    finally:
        mt5_lock.release()
    return jsonify({"status": "running", "quotes": quotes, "missing": miss})


# démo : positions fictives qui bougent à chaque seconde
_demo_live = {"positions": None, "rnd": random.Random(7)}


def _demo_snapshot(c):
    import time

    rnd = _demo_live["rnd"]
    if _demo_live["positions"] is None:
        now = time.time()
        _demo_live["positions"] = [
            {"ticket": 175000001, "symbol": "US100.cash", "side": "buy", "volume": 5.0, "open": 20850.5,
             "current": 20850.5, "sl": 20810.5, "tp": 20930.5, "t": now - 760},
            {"ticket": 175000002, "symbol": "US100.cash", "side": "sell", "volume": 3.0, "open": 20878.0,
             "current": 20878.0, "sl": 20918.0, "tp": None, "t": now - 190},
        ]
    out, floating = [], 0.0
    for p_ in _demo_live["positions"]:
        p_["current"] = round(p_["current"] + rnd.gauss(0, 1.6), 2)
        move = p_["current"] - p_["open"] if p_["side"] == "buy" else p_["open"] - p_["current"]
        profit = round(move * p_["volume"], 2)
        floating += profit
        out.append({**{k: v for k, v in p_.items() if k != "t"}, "profit": profit,
                    "opened": dt.datetime.utcfromtimestamp(p_["t"]).isoformat(timespec="seconds"),
                    "duration": int(time.time() - p_["t"])})
    bal = c.get("balance") or 0.0
    return {"balance": bal, "equity": round(bal + floating, 2), "profit": round(floating, 2), "margin": 1200.0,
            "free_margin": round(bal + floating - 1200, 2), "currency": c.get("currency") or "USD",
            "positions": out, "at": dt.datetime.now().isoformat(timespec="seconds")}


@app.get("/api/live")
def api_live():
    import time

    if not has_perk("live"):
        return _perk_refused("live")
    login = str(request.args.get("login", ""))
    accounts, cache = load_accounts(), load_cache()
    if login not in accounts:
        return jsonify({"status": "error", "error": "Compte introuvable."}), 404
    if cache.get(login, {}).get("demo"):
        LIVE["seen"] = time.time()
        return jsonify({"status": "running", "snap": _demo_snapshot(cache[login]), "rev": 0})
    if mt5 is None:
        return jsonify({"status": "unavailable", "error": "MetaTrader 5 n'est pas disponible sur cet ordinateur."})
    if not get_saved_password(login):
        return jsonify({"status": "need_password"})
    _start_mt5_threads()
    with live_lock:
        if LIVE["login"] != login:  # nouveau compte suivi
            LIVE.update(login=login, snap=None, status="starting", error=None)
        LIVE["seen"] = time.time()
        return jsonify({"status": LIVE["status"], "snap": LIVE["snap"], "rev": LIVE["hist_rev"], "error": LIVE["error"]})


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


def shutdown_app():
    """Ferme le MT5 caché du suivi en direct, puis quitte."""
    try:
        live_stop()
        if SESSION.term is not None and mt5_lock.acquire(timeout=3):
            SESSION._close_locked()
        if os.name == "nt":
            _kill_private_terminals()
    except Exception:
        pass
    os._exit(0)


@app.post("/api/quit")
def api_quit():
    threading.Timer(0.3, shutdown_app).start()
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
            shutdown_app()
        if now - _life["last_ping"] > 600:
            shutdown_app()


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
    if isinstance(body.get("kpis_v"), int):
        prefs["kpis_v"] = body["kpis_v"]
    if isinstance(body.get("news"), list):
        prefs["news"] = [str(x)[:20] for x in body["news"] if str(x) in NEWS_FEEDS][:12]
        prefs["newsV"] = 3
    if isinstance(body.get("newsThemes"), list):
        th = [x for x in body["newsThemes"] if x in ("us", "eu", "asia", "au", "crypto")]
        if th:
            prefs["newsThemes"] = th
    if isinstance(body.get("eco"), dict):
        eco = body["eco"]
        imp = str(eco.get("imp", "0,1"))
        countries = [str(c)[:2].upper() for c in (eco.get("countries") or []) if isinstance(c, str)][:20]
        rng = str(eco.get("range", "today"))
        if imp in ("-1,0,1", "0,1", "1") and countries:
            prefs["eco"] = {"imp": imp, "countries": countries,
                            "range": rng if rng in ("yesterday", "today", "tomorrow", "week", "nextweek") else "today"}
            tz = str(eco.get("tz", ""))[:40]
            if tz and all(c.isalnum() or c in "/_+-" for c in tz):
                prefs["eco"]["tz"] = tz
    for sk in ("split", "split2"):
        if isinstance(body.get(sk), (int, float)):
            prefs[sk] = min(0.8, max(0.35, float(body[sk])))
    if isinstance(body.get("card"), dict):
        cd = body["card"]
        prefs["card"] = {"mode": cd.get("mode") if cd.get("mode") in ("both", "pct", "r") else "both",
                         "format": cd.get("format") if cd.get("format") in ("post", "story", "square") else "post",
                         "handle": str(cd.get("handle", ""))[:30], "hideAcc": bool(cd.get("hideAcc")),
                         "qr": cd.get("qr") is not False,
                         "qrSrc": "link" if cd.get("qrSrc") == "link" else "details",
                         "links": {str(k)[:30]: str(v)[:300] for k, v in list((cd.get("links") or {}).items())[:60]
                                   if isinstance(v, str) and v.startswith(("http://", "https://"))}}
    if isinstance(body.get("quotes"), list):
        prefs["quotes"] = [str(x)[:40] for x in body["quotes"] if isinstance(x, str)][:30]
    if isinstance(body.get("mk"), dict):  # onglets du bloc Marchés
        mk = body["mk"]
        tabs = [c for c in mk.get("tabs", []) if c in QUOTE_CATS][:4]
        lists = {c: [str(x)[:40] for x in v if isinstance(x, str)][:30]
                 for c, v in (mk.get("lists") or {}).items() if c in QUOTE_CATS and isinstance(v, list)}
        selk = {c: str(v)[:40] for c, v in (mk.get("sel") or {}).items() if c in QUOTE_CATS and isinstance(v, str)}
        prefs["mk"] = {"tabs": tabs or QUOTE_TABS_ON, "lists": lists, "sel": selk,
                       "tab": mk.get("tab") if mk.get("tab") in QUOTE_CATS else (tabs or QUOTE_TABS_ON)[0],
                       "range": str(mk.get("range", "1D")).upper() if str(mk.get("range", "1D")).upper() in QUOTE_RANGES else "1D"}
    if isinstance(body.get("mask"), bool):
        prefs["mask"] = body["mask"]
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
    threading.Timer(0.6, shutdown_app).start()
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


# ---- traduction en français des intitulés du calendrier (le flux TradingView est en anglais)
ECO_PHRASES = [  # (anglais, français) — du plus long au plus court au moment de l'emploi
    ("non farm payrolls", "Emplois non agricoles (NFP)"), ("nonfarm payrolls", "Emplois non agricoles (NFP)"),
    ("non farm payrolls private", "Emplois non agricoles privés"), ("nonfarm payrolls private", "Emplois non agricoles privés"),
    ("manufacturing payrolls", "Emplois manufacturiers"), ("government payrolls", "Emplois publics"),
    ("adp employment change", "Créations d'emplois ADP"), ("employment change", "Variation de l'emploi"),
    ("unemployment rate", "Taux de chômage"), ("unemployment change", "Variation du chômage"),
    ("unemployed persons", "Nombre de chômeurs"), ("claimant count change", "Demandeurs d'allocations chômage"),
    ("initial jobless claims", "Inscriptions hebdomadaires au chômage"), ("continuing jobless claims", "Demandes continues d'allocations chômage"),
    ("jobless claims 4-week average", "Inscriptions au chômage (moyenne 4 semaines)"),
    ("jolts job openings", "Offres d'emploi JOLTS"), ("job openings", "Offres d'emploi"), ("challenger job cuts", "Suppressions d'emplois Challenger"),
    ("average hourly earnings", "Salaire horaire moyen"), ("average weekly hours", "Durée hebdomadaire moyenne du travail"),
    ("average earnings incl. bonus", "Salaires moyens (primes incluses)"), ("average earnings excl. bonus", "Salaires moyens (hors primes)"),
    ("participation rate", "Taux de participation"), ("labor cost index", "Indice du coût du travail"), ("unit labour costs", "Coûts unitaires de la main-d'œuvre"),
    ("unit labor costs", "Coûts unitaires de la main-d'œuvre"), ("nonfarm productivity", "Productivité non agricole"),
    ("core inflation rate", "Inflation sous-jacente"), ("inflation rate", "Taux d'inflation"),
    ("core pce price index", "Indice des prix PCE sous-jacent"), ("pce price index", "Indice des prix PCE"),
    ("core consumer prices", "Prix à la consommation sous-jacents"), ("consumer price index", "Indice des prix à la consommation"),
    ("harmonised inflation rate", "Inflation harmonisée"), ("harmonized inflation rate", "Inflation harmonisée"),
    ("core cpi", "CPI sous-jacent"), ("cpi", "Indice des prix (CPI)"), ("core ppi", "PPI sous-jacent"), ("ppi", "Prix à la production (PPI)"),
    ("producer price index", "Indice des prix à la production"), ("producer prices", "Prix à la production"),
    ("import prices", "Prix à l'importation"), ("export prices", "Prix à l'exportation"),
    ("gdp growth rate annualized", "Croissance du PIB (annualisée)"), ("gdp growth rate", "Croissance du PIB"), ("gdp price index", "Indice des prix du PIB"),
    ("gdp", "PIB"), ("core pce prices", "Prix PCE sous-jacents"),
    ("retail sales ex autos", "Ventes au détail hors automobiles"), ("retail sales ex gas/autos", "Ventes au détail hors carburant et automobiles"),
    ("retail sales control group", "Ventes au détail (groupe de contrôle)"), ("retail sales", "Ventes au détail"),
    ("personal income", "Revenus des ménages"), ("personal spending", "Dépenses des ménages"), ("household spending", "Dépenses des ménages"),
    ("ism manufacturing pmi", "ISM manufacturier"), ("ism services pmi", "ISM des services"), ("ism non-manufacturing pmi", "ISM des services"),
    ("ism manufacturing prices", "ISM manufacturier : prix"), ("ism services prices", "ISM des services : prix"),
    ("s&p global manufacturing pmi", "PMI manufacturier S&P Global"), ("s&p global services pmi", "PMI des services S&P Global"),
    ("s&p global composite pmi", "PMI composite S&P Global"), ("hcob manufacturing pmi", "PMI manufacturier HCOB"),
    ("hcob services pmi", "PMI des services HCOB"), ("hcob composite pmi", "PMI composite HCOB"),
    ("caixin manufacturing pmi", "PMI manufacturier Caixin"), ("caixin services pmi", "PMI des services Caixin"),
    ("nbs manufacturing pmi", "PMI manufacturier NBS"), ("nbs non manufacturing pmi", "PMI non manufacturier NBS"),
    ("manufacturing pmi", "PMI manufacturier"), ("services pmi", "PMI des services"), ("composite pmi", "PMI composite"),
    ("construction pmi", "PMI de la construction"), ("chicago pmi", "PMI de Chicago"),
    ("philadelphia fed manufacturing index", "Indice manufacturier de la Fed de Philadelphie"),
    ("ny empire state manufacturing index", "Indice manufacturier Empire State (New York)"),
    ("richmond fed manufacturing index", "Indice manufacturier de la Fed de Richmond"),
    ("kansas fed manufacturing index", "Indice manufacturier de la Fed de Kansas City"),
    ("dallas fed manufacturing index", "Indice manufacturier de la Fed de Dallas"),
    ("industrial production", "Production industrielle"), ("manufacturing production", "Production manufacturière"),
    ("capacity utilization", "Taux d'utilisation des capacités"), ("factory orders", "Commandes industrielles"),
    ("durable goods orders ex transportation", "Commandes de biens durables hors transport"), ("durable goods orders ex defense", "Commandes de biens durables hors défense"),
    ("durable goods orders", "Commandes de biens durables"), ("core durable goods orders", "Commandes de biens durables hors transport"),
    ("wholesale inventories", "Stocks des grossistes"), ("business inventories", "Stocks des entreprises"), ("retail inventories ex autos", "Stocks des détaillants hors automobiles"),
    ("building permits", "Permis de construire"), ("housing starts", "Mises en chantier"), ("existing home sales", "Ventes de logements existants"),
    ("new home sales", "Ventes de logements neufs"), ("pending home sales", "Promesses de ventes de logements"),
    ("s&p/case-shiller home price", "Prix des logements S&P/Case-Shiller"), ("house price index", "Indice des prix des logements"),
    ("nahb housing market index", "Indice du marché immobilier NAHB"), ("mba mortgage applications", "Demandes de prêts immobiliers MBA"),
    ("mba 30-year mortgage rate", "Taux hypothécaire à 30 ans MBA"), ("construction spending", "Dépenses de construction"),
    ("michigan consumer sentiment", "Moral des consommateurs (Michigan)"), ("michigan consumer expectations", "Attentes des consommateurs (Michigan)"),
    ("michigan current conditions", "Conditions actuelles (Michigan)"), ("michigan inflation expectations", "Anticipations d'inflation (Michigan)"),
    ("michigan 5 year inflation expectations", "Anticipations d'inflation à 5 ans (Michigan)"),
    ("cb consumer confidence", "Confiance des consommateurs (Conference Board)"), ("consumer confidence", "Confiance des consommateurs"),
    ("consumer inflation expectations", "Anticipations d'inflation des consommateurs"),
    ("business confidence", "Climat des affaires"), ("business climate", "Climat des affaires"), ("economic sentiment", "Sentiment économique"),
    ("ifo business climate", "Climat des affaires Ifo"), ("ifo expectations", "Attentes Ifo"), ("ifo current conditions", "Situation actuelle Ifo"),
    ("zew economic sentiment index", "Sentiment économique ZEW"), ("zew current conditions", "Situation actuelle ZEW"),
    ("gfk consumer confidence", "Confiance des consommateurs GfK"), ("sentix investor confidence", "Confiance des investisseurs Sentix"),
    ("tankan large manufacturers index", "Indice Tankan des grands industriels"), ("leading index", "Indicateur avancé"),
    ("cb leading index", "Indicateur avancé (Conference Board)"), ("nfib business optimism index", "Optimisme des PME (NFIB)"),
    ("balance of trade", "Balance commerciale"), ("trade balance", "Balance commerciale"), ("goods trade balance", "Balance commerciale des biens"),
    ("current account", "Balance courante"), ("exports", "Exportations"), ("imports", "Importations"),
    ("government budget", "Budget de l'État"), ("monthly budget statement", "Solde budgétaire mensuel"), ("budget balance", "Solde budgétaire"),
    ("foreign exchange reserves", "Réserves de change"), ("money supply m2", "Masse monétaire M2"), ("money supply m3", "Masse monétaire M3"),
    ("consumer credit", "Crédit à la consommation"), ("loan growth", "Croissance des crédits"), ("new yuan loans", "Nouveaux prêts en yuans"),
    ("fed interest rate decision", "Décision de la Fed sur les taux"), ("ecb interest rate decision", "Décision de la BCE sur les taux"),
    ("boe interest rate decision", "Décision de la BoE sur les taux"), ("boj interest rate decision", "Décision de la BoJ sur les taux"),
    ("snb interest rate decision", "Décision de la BNS sur les taux"), ("boc interest rate decision", "Décision de la BoC sur les taux"),
    ("rba interest rate decision", "Décision de la RBA sur les taux"), ("rbnz interest rate decision", "Décision de la RBNZ sur les taux"),
    ("pboc loan prime rate", "Taux préférentiel de la PBoC"), ("loan prime rate 1y", "Taux préférentiel à 1 an"), ("loan prime rate 5y", "Taux préférentiel à 5 ans"),
    ("interest rate decision", "Décision sur les taux"), ("deposit facility rate", "Taux de la facilité de dépôt"),
    ("main refinancing rate", "Taux de refinancement"), ("marginal lending rate", "Taux de prêt marginal"),
    ("fomc minutes", "Compte rendu du FOMC"), ("fomc economic projections", "Projections économiques du FOMC"),
    ("fed press conference", "Conférence de presse de la Fed"), ("ecb press conference", "Conférence de presse de la BCE"),
    ("ecb monetary policy meeting accounts", "Compte rendu de la réunion de la BCE"), ("boe minutes", "Compte rendu de la BoE"),
    ("monetary policy statement", "Déclaration de politique monétaire"), ("monetary policy report", "Rapport de politique monétaire"),
    ("fed beige book", "Livre beige de la Fed"), ("beige book", "Livre beige de la Fed"),
    ("crude oil inventories", "Stocks de pétrole brut"), ("eia crude oil stocks change", "Variation des stocks de pétrole brut (EIA)"),
    ("api crude oil stock change", "Variation des stocks de pétrole brut (API)"), ("eia gasoline stocks change", "Variation des stocks d'essence (EIA)"),
    ("eia distillate stocks change", "Variation des stocks de distillats (EIA)"), ("eia natural gas stocks change", "Variation des stocks de gaz naturel (EIA)"),
    ("eia cushing crude oil stocks change", "Variation des stocks de pétrole à Cushing (EIA)"), ("eia refinery crude runs change", "Variation du raffinage de brut (EIA)"),
    ("baker hughes oil rig count", "Nombre de puits de pétrole (Baker Hughes)"), ("baker hughes total rig count", "Nombre total de puits (Baker Hughes)"),
    ("total vehicle sales", "Ventes de véhicules"), ("new car registrations", "Immatriculations de voitures neuves"),
    ("cftc s&p 500 speculative net positions", "Positions nettes spéculatives CFTC sur le S&P 500"),
    ("cftc gold speculative net positions", "Positions nettes spéculatives CFTC sur l'or"),
    ("cftc crude oil speculative net positions", "Positions nettes spéculatives CFTC sur le pétrole"),
    ("cftc eur speculative net positions", "Positions nettes spéculatives CFTC sur l'euro"),
    ("treasury bill auction", "Adjudication de bons du Trésor"), ("bank holiday", "Jour férié"), ("stress test results", "Résultats des tests de résistance"),
    ("wage growth", "Croissance des salaires"), ("real earnings", "Salaires réels"), ("current account balance", "Solde de la balance courante"),
]
ECO_PHRASES.sort(key=lambda p: -len(p[0]))
_ECO_EXACT = {a: b for a, b in ECO_PHRASES}
ECO_QUALS = [(r"mom", "m/m"), (r"yoy", "a/a"), (r"qoq", "t/t"), (r"final", "définitif"), (r"flash", "flash"),
             (r"prel(?:im(?:inary)?)?", "préliminaire"), (r"adv(?:ance)?", "préliminaire"), (r"2nd est(?:imate)?", "2e estimation"),
             (r"3rd est(?:imate)?", "3e estimation"), (r"s\.a\.?", "cvs"), (r"n\.s\.a\.?", "non cvs"), (r"annualized", "annualisé")]
ECO_BANKS = {"fed": "Fed", "fomc": "FOMC", "ecb": "BCE", "boe": "BoE", "boj": "BoJ", "snb": "BNS", "boc": "BoC", "rba": "RBA", "rbnz": "RBNZ", "pboc": "PBoC"}
ECO_MONTHS = {"jan": "janv.", "feb": "févr.", "mar": "mars", "apr": "avr.", "may": "mai", "jun": "juin", "jul": "juil.",
              "aug": "août", "sep": "sept.", "oct": "oct.", "nov": "nov.", "dec": "déc."}


def eco_fr(title):
    """Traduit un intitulé d'annonce économique (anglais → français). Ce qui n'est pas connu reste tel quel."""
    import re

    t = " ".join(str(title or "").split())
    if not t:
        return t
    low = t.lower()
    if low in _ECO_EXACT:
        return _ECO_EXACT[low]
    # discours : « Fed Chair Powell Speech », « ECB President Lagarde Speech », « Fed Waller Speech »
    m = re.match(r"^(.*?)\s+speech$", t, re.I)
    if m:
        who = m.group(1).split()
        bank = next((ECO_BANKS[w.lower()] for w in who if w.lower() in ECO_BANKS), None)
        name = [w for w in who if w.lower() not in ECO_BANKS and w.lower() not in ("chair", "president", "governor", "member", "vice", "board")]
        return "Discours de " + (" ".join(name) or "la banque centrale") + (f" ({bank})" if bank else "")
    m = re.match(r"^(\d+)[- ](year|yr)\s+.*auction$", t, re.I)
    if m:
        return f"Adjudication d'obligations à {m.group(1)} ans"
    m = re.match(r"^(\d+)[- ](week|month)\s+bill\s+auction$", t, re.I)
    if m:
        return f"Adjudication de bons à {m.group(1)} " + ("semaines" if m.group(2).lower() == "week" else "mois")
    # qualificatifs en fin d'intitulé (MoM, YoY, Prel, Final…)
    quals = []
    while True:
        for rx, fr in ECO_QUALS:
            mm = re.search(r"\s+" + rx + r"$", t, re.I)
            if mm:
                quals.insert(0, fr)
                t = t[:mm.start()]
                break
        else:
            break
    low = t.lower()
    core = _ECO_EXACT.get(low)
    if core is None:
        for en, fr in ECO_PHRASES:  # expression connue au début ou dans l'intitulé
            i = low.find(en)
            if i >= 0 and (i == 0 or not low[i - 1].isalnum()) and (i + len(en) == len(low) or not low[i + len(en)].isalnum()):
                rest = (t[:i] + t[i + len(en):]).strip(" -")
                core = fr + (f" ({rest})" if rest else "")
                break
    core = core or t
    if quals and core.endswith(")"):
        return core[:-1] + ", " + ", ".join(quals) + ")"
    return core + (f" ({', '.join(quals)})" if quals else "")


def eco_period_fr(p):
    import re

    if not p:
        return p
    s = str(p)
    s = re.sub(r"\bQ([1-4])\b", r"T\1", s)
    s = re.sub(r"\bH([12])\b", r"S\1", s)
    return re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", lambda m: ECO_MONTHS[m.group(1).lower()], s)


ECO_SCALE = {"K": " k", "M": " M", "B": " Md", "T": " Bn"}


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
            "id": it.get("id"), "date": it.get("date"), "title": eco_fr(it.get("title") or it.get("indicator")),
            "title_en": it.get("title") or it.get("indicator"),
            "country": it.get("country"), "currency": it.get("currency"), "period": eco_period_fr(it.get("period")),
            "importance": imp if imp in (-1, 0, 1) else -1,
            "actual": _num(it.get("actual")), "forecast": _num(it.get("forecast")), "previous": _num(it.get("previous")),
            "unit": it.get("unit") or "", "scale": ECO_SCALE.get(it.get("scale") or "", it.get("scale") or ""),
        })
    data = {"events": events}
    _eco_cache[key] = {"at": time.time(), "data": data}
    return jsonify(data)


def _gnews(query):
    """Flux RSS de Google Actualités pour une recherche, en français."""
    from urllib.parse import urlencode
    return "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "fr", "gl": "FR", "ceid": "FR:fr"})


US_QUERY = ('"économie américaine" OR "Réserve fédérale" OR Fed OR Powell OR "Wall Street" OR "inflation aux États-Unis" '
            'OR "emploi américain" OR "dollar américain" OR "Trésor américain" OR Nasdaq when:3d')
CRYPTO_QUERY = 'bitcoin OR crypto OR cryptomonnaie OR ethereum OR stablecoin when:3d'
EU_QUERY = ('"zone euro" OR BCE OR Lagarde OR "économie européenne" OR "Union européenne" économie OR Eurostat '
            'OR "inflation en zone euro" OR "croissance européenne" OR "Commission européenne" économie when:3d')
EU_MARKETS_QUERY = ('"Bourses européennes" OR "Euro Stoxx" OR DAX OR "marchés européens" OR "économie allemande" '
                    'OR "euro dollar" OR "EUR/USD" OR "Bundesbank" when:3d')

def _bing(query):
    """Flux RSS de Bing Actualités (avec une photo pour la plupart des articles), en français."""
    from urllib.parse import urlencode
    return "https://www.bing.com/news/search?" + urlencode({"q": query, "format": "rss", "setlang": "fr", "cc": "FR", "mkt": "fr-FR"})


B_US = '"économie américaine" OR "Réserve fédérale" OR Powell OR "Wall Street" OR Nasdaq OR "dollar américain" OR "inflation américaine"'
B_EU = '"zone euro" OR BCE OR Lagarde OR "économie européenne" OR "Bourses européennes" OR DAX OR "Euro Stoxx"'
B_CRYPTO = 'bitcoin OR cryptomonnaie OR ethereum OR crypto'
B_ASIA = '"économie chinoise" OR "Banque du Japon" OR Nikkei OR "Hang Seng" OR yuan OR yen OR "économie japonaise" OR "Bourses asiatiques"'
B_AU = '"économie australienne" OR "Banque de réserve d\'Australie" OR "dollar australien" OR ASX OR RBA OR "Australie économie"'
ASIA_QUERY = ('"économie chinoise" OR "Banque populaire de Chine" OR "Banque du Japon" OR Nikkei OR "Hang Seng" OR "Bourses asiatiques" '
              'OR "économie japonaise" OR yuan OR yen OR "Corée du Sud" économie OR Inde économie when:3d')
AU_QUERY = ('"économie australienne" OR "Banque de réserve d\'Australie" OR RBA OR "dollar australien" OR ASX OR "Australie" économie '
            'OR "Nouvelle-Zélande" économie when:7d')

NEWS_FEEDS = {  # id : (nom affiché, adresse du flux, thème)
    "us": ("Économie US", _gnews(US_QUERY), "us"),
    "bus": ("Économie US", _bing(B_US), "us"),
    "beu": ("Économie Europe", _bing(B_EU), "eu"),
    "bcrypto": ("Crypto", _bing(B_CRYPTO), "crypto"),
    "basia": ("Économie Asie", _bing(B_ASIA), "asia"),
    "asia": ("Économie Asie", _gnews(ASIA_QUERY), "asia"),
    "bau": ("Économie Australie", _bing(B_AU), "au"),
    "au": ("Économie Australie", _gnews(AU_QUERY), "au"),
    "auen": ("Australie (anglais)", "https://news.google.com/rss/search?q=Australia+economy+OR+RBA+OR+ASX+OR+%22Australian+dollar%22+when:3d&hl=en-AU&gl=AU&ceid=AU:en", "au"),
    "eu": ("Économie Europe", _gnews(EU_QUERY), "eu"),
    "eumk": ("Marchés européens", _gnews(EU_MARKETS_QUERY), "eu"),
    "lemonde": ("Le Monde", "https://www.lemonde.fr/economie/rss_full.xml", "eu"),
    "lesechos": ("Les Echos", "https://services.lesechos.fr/rss/les-echos-economie.xml", "eu"),
    "figaro": ("Le Figaro", "https://www.lefigaro.fr/rss/figaro_economie.xml", "eu"),
    "bfm": ("BFM Business", "https://www.bfmtv.com/rss/economie/", "eu"),
    "investing": ("Investing.com", "https://fr.investing.com/rss/news.rss", "eu"),
    "cnbc": ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "us"),
    "gcrypto": ("Crypto", _gnews(CRYPTO_QUERY), "crypto"),
    "cryptoast": ("Cryptoast", "https://cryptoast.fr/feed/", "crypto"),
    "jdc": ("Journal du Coin", "https://journalducoin.com/feed/", "crypto"),
    "ctfr": ("Cointelegraph FR", "https://fr.cointelegraph.com/rss", "crypto"),
    "cointribune": ("Cointribune", "https://www.cointribune.com/feed/", "crypto"),
    "coindesk": ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
}
_news_cache = {}


def _strip_html(text):
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _parse_feed(source_id, raw, name=None, tag=None):
    """Lit un flux RSS ou Atom et renvoie des articles simples (titre, lien, date, résumé, image)."""
    import email.utils
    import xml.etree.ElementTree as ET

    if b"<!ENTITY" in raw[:4096]:
        return []  # entités XML refusées
    root = ET.fromstring(raw)
    if name is None:
        name, _url, tag = NEWS_FEEDS[source_id]
    items = []

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        it = {"source": name, "title": "", "link": "", "date": None, "summary": "", "image": None, "tag": tag}
        origin = None
        for ch in node:
            t = local(ch.tag)
            if t == "title":
                it["title"] = _strip_html("".join(ch.itertext()))
            elif t == "link":
                href = ch.get("href") or (ch.text or "").strip()
                if href and (not it["link"] or ch.get("rel") in (None, "alternate")):
                    it["link"] = href
            elif t in ("pubDate", "published", "updated", "date") and not it["date"]:
                val = (ch.text or "").strip()
                try:
                    d = email.utils.parsedate_to_datetime(val)
                except (TypeError, ValueError):
                    try:
                        d = dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
                    except ValueError:
                        d = None
                if d is not None:
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=dt.timezone.utc)
                    it["date"] = d.astimezone(dt.timezone.utc).isoformat()  # même fuseau pour trier
            elif t in ("description", "summary", "content") and not it["summary"]:
                it["summary"] = _strip_html("".join(ch.itertext()))[:300]
            elif t in ("content", "thumbnail") and ch.get("url") and not it["image"]:
                it["image"] = ch.get("url")
            elif t == "enclosure" and (ch.get("type") or "").startswith("image") and not it["image"]:
                it["image"] = ch.get("url")
            elif t in ("source", "Source") and (ch.text or "").strip():
                origin = ch.text.strip()  # journal d'origine (Google / Bing Actualités)
            elif t == "Image" and (ch.text or "").strip() and not it["image"]:
                img = ch.text.strip().replace("http://www.bing.com/", "https://www.bing.com/")
                it["image"] = img + ("&" if "?" in img else "?") + "w=720&h=405&c=7"  # photo Bing en bonne taille
            elif t == "group":
                for g in ch:
                    if local(g.tag) in ("content", "thumbnail") and g.get("url") and not it["image"]:
                        it["image"] = g.get("url")
        if origin:
            it["source"] = origin
            if it["title"].endswith(" - " + origin):
                it["title"] = it["title"][: -len(origin) - 3].strip()
            if it["summary"].startswith(it["title"][:40]):
                it["summary"] = ""  # le résumé de Google ne fait que répéter le titre
        if "bing.com/news/apiclick" in it["link"]:  # lien Bing : on garde l'adresse directe de l'article
            from urllib.parse import urlparse, parse_qs
            real = parse_qs(urlparse(it["link"]).query).get("url", [""])[0]
            if real.startswith(("http://", "https://")):
                it["link"] = real
        if it["title"] and it["link"].startswith(("http://", "https://")):
            if it["image"] and not str(it["image"]).startswith(("http://", "https://")):
                it["image"] = None
            items.append(it)
    return items


def _fetch_feed(source_id):
    import time
    import urllib.request

    hit = _news_cache.get(source_id)
    if hit and time.time() - hit["at"] < 600:
        return hit["items"]
    req = urllib.request.Request(NEWS_FEEDS[source_id][1], headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read(3_000_000)
    items = _parse_feed(source_id, raw)
    _news_cache[source_id] = {"at": time.time(), "items": items}
    return items


_img_cache = {}


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_gn_cache = {}


def _is_private_host(host):
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return host in ("localhost",) or host.endswith(".local")


def _google_news_real_url(link):
    """Retrouve l'adresse réelle d'un article à partir d'un lien Google Actualités."""
    import base64
    import re
    import urllib.request
    from urllib.parse import quote, urlparse

    path = urlparse(link).path.split("/")
    if "articles" not in path:
        return None
    gid = path[path.index("articles") + 1] if path.index("articles") + 1 < len(path) else ""
    if not gid:
        return None
    if gid in _gn_cache:
        return _gn_cache[gid]
    real = None
    # 1) ancien format : l'adresse est directement dans l'identifiant
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", raw)
        if m and not raw.startswith(b"\x08\x13\x22\x00") and b"AU_yqL" not in raw[:12]:
            real = m.group(0).decode("ascii", "ignore")
    except Exception:
        pass
    # 2) nouveau format : on demande l'adresse à Google (signature et horodatage lus dans la page)
    if not real:
        try:
            sg = ts = None
            for page_url in (f"https://news.google.com/articles/{gid}", f"https://news.google.com/rss/articles/{gid}"):
                req = urllib.request.Request(page_url, headers={"User-Agent": _UA, "Accept-Language": "fr-FR,fr;q=0.9"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    page = r.read(800_000).decode("utf-8", "ignore")
                m1 = re.search(r'data-n-a-sg="([^"]+)"', page)
                m2 = re.search(r'data-n-a-ts="([^"]+)"', page)
                if m1 and m2:
                    sg, ts = m1.group(1), m2.group(1)
                    break
            if sg and ts:
                inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                         f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts},"{sg}"]')
                data = ("f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))).encode()
                req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=data, headers={
                    "User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    txt = r.read(400_000).decode("utf-8", "ignore")
                part = txt.split("\n\n", 1)[1] if "\n\n" in txt else txt
                arr = json.loads(part)
                for row in arr:
                    if isinstance(row, list) and len(row) > 2 and row[1] == "Fbv4je" and row[2]:
                        cand = json.loads(row[2])[1]
                        if isinstance(cand, str) and cand.startswith(("http://", "https://")):
                            real = cand
                        break
        except Exception:
            real = None
    if real and _is_private_host(urlparse(real).hostname or ""):
        real = None
    if len(_gn_cache) > 3000:
        _gn_cache.clear()
    _gn_cache[gid] = real
    return real


def _og_image(url):
    """Image de partage (og:image / twitter:image) d'une page d'article."""
    import html as _html
    import re
    import urllib.request
    from urllib.parse import urljoin

    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html", "Accept-Language": "fr-FR,fr;q=0.9"})
    with urllib.request.urlopen(req, timeout=8) as r:
        if "html" not in (r.headers.get("Content-Type") or "html"):
            return None
        page = r.read(700_000).decode("utf-8", "ignore")
        final = r.geturl()
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)["\'][^>]*content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)["\']',
                r'<link[^>]+rel=["\']image_src["\'][^>]*href=["\']([^"\']+)'):
        m = re.search(pat, page, re.I)
        if m:
            cand = urljoin(final, _html.unescape(m.group(1).strip()))
            if cand.startswith(("http://", "https://")):
                return cand
    return None


def _bing_image_for(title):
    """Photo d'un article sur le même sujet, trouvée sur Bing Actualités à partir du titre."""
    import re
    import urllib.request

    t = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title or "").strip()  # « Titre - Journal » → « Titre »
    words = re.findall(r"[\w'’%$€.-]+", t)
    for n in (12, 6):
        q = " ".join(words[:n])
        if len(q) < 8:
            continue
        try:
            req = urllib.request.Request(_bing(q), headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                items = _parse_feed(None, r.read(2_000_000), name="Bing", tag="search")
        except Exception:
            continue
        for it in items:
            if it.get("image"):
                return it["image"]
    return None


@app.get("/api/news/image")
def api_news_image():
    """Trouve une photo pour un article : image de l'article, sinon photo Bing du même sujet. Résultat gardé en mémoire."""
    from urllib.parse import urlparse

    url = str(request.args.get("u", ""))[:1500]
    title = str(request.args.get("t", ""))[:300]
    if not url.startswith(("http://", "https://")) or _is_private_host(urlparse(url).hostname or ""):
        return jsonify({"image": None})  # pas de requête vers le réseau local
    if request.args.get("alt"):  # la photo trouvée ne s'affiche pas : autre photo, via Bing
        key = url + "#alt"
        if key not in _img_cache:
            _img_cache[key] = {"image": _bing_image_for(title) if title else None, "link": None}
        return jsonify(_img_cache[key])
    if url in _img_cache:
        return jsonify(_img_cache[url])
    real, image = url, None
    if (urlparse(url).hostname or "").endswith("news.google.com"):
        real = _google_news_real_url(url)
    if real:
        try:
            image = _og_image(real)
        except Exception:
            image = None
    if not image and title:
        image = _bing_image_for(title)
    res = {"image": image, "link": real if real and real != url else None}
    if len(_img_cache) > 2000:
        _img_cache.clear()
    _img_cache[url] = res
    return jsonify(res)


_search_cache = {}


@app.get("/api/news/search")
def api_news_search():
    import time
    import urllib.request

    q = " ".join(str(request.args.get("q", "")).split())[:80]
    if len(q) < 2:
        return jsonify({"items": []})
    hit = _search_cache.get(q.lower())
    if hit and time.time() - hit["at"] < 300:
        return jsonify({"items": hit["items"]})
    items, errors = [], []
    for url in (_bing(q), _gnews(q + " when:14d")):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                items += _parse_feed(None, r.read(3_000_000), name="Actualités", tag="search")
        except Exception as ex:
            errors.append(type(ex).__name__)
    if not items and errors:
        return jsonify({"error": f"recherche indisponible ({errors[0]})"}), 502
    seen, uniq = set(), []
    for it in items:
        k = it["title"].lower()[:70]
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    items = uniq
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    _search_cache[q.lower()] = {"at": time.time(), "items": items[:60]}
    return jsonify({"items": items[:60]})


@app.get("/api/news")
def api_news():
    from concurrent.futures import ThreadPoolExecutor

    ids = [x for x in str(request.args.get("sources", "")).split(",") if x in NEWS_FEEDS][:30] or list(NEWS_FEEDS)[:5]
    if request.args.get("fresh") == "1":
        for i in ids:
            hit = _news_cache.get(i)
            if hit and __import__("time").time() - hit["at"] > 60:
                hit["at"] = 0  # « Actualiser » : relire le flux (au plus une fois par minute)
    items, failed = [], []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {i: pool.submit(_fetch_feed, i) for i in ids}
        for i, f in futures.items():
            try:
                items.extend(f.result(timeout=15))
            except Exception:
                hit = _news_cache.get(i)
                if hit:
                    items.extend(hit["items"])
                failed.append(i)
    import re as _re
    import unicodedata as _ud

    def _key(t):
        t = _ud.normalize("NFD", t.lower())
        t = "".join(ch for ch in t if _ud.category(ch) != "Mn")
        return " ".join(_re.findall(r"[a-z0-9]+", t)[:8])  # mêmes 8 premiers mots = même info

    seen, out = {}, []
    for it in sorted(items, key=lambda x: x["date"] or "", reverse=True):
        key = _key(it["title"])
        if key in seen:
            seen[key]["n"] = seen[key].get("n", 1) + 1
            if not seen[key].get("image") and it.get("image"):
                seen[key]["image"] = it["image"]
            continue
        it = {**it, "n": 1}
        seen[key] = it
        out.append(it)
    return jsonify({"items": out[:250], "failed": failed})


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


# ---------------------------------------------------------------- licence (abonnement Lemon Squeezy)
LICENCE_FILE = os.path.join(DATA_DIR, "licence.json")
OWNER_KEYS = {"20c8f1e472984770c44f9a0ea7ae360e8d577a460fb38a3e64426e804668d303"}  # empreintes SHA-256 des clés propriétaire
LS_API = "https://api.lemonsqueezy.com/v1/licenses/"
LIC_RECHECK = 6 * 3600      # on revérifie l'abonnement toutes les 6 h
LIC_GRACE = 3 * 86400       # sans internet, l'accès reste ouvert 3 jours après la dernière vérification réussie
_lic_lock = threading.Lock()


def load_offers():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "offres.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"plans": []}


def _owner_key(key):
    import hashlib
    return hashlib.sha256(key.strip().encode()).hexdigest() in OWNER_KEYS


def _creem_call(action, fields):
    """Licences Creem, via le relais (Cloudflare) qui garde la clé API secrète hors de l'app.
    Les réponses sont remises au même format que Lemon Squeezy pour le reste du code."""
    import urllib.error
    import urllib.request

    api = (load_offers().get("licence_api") or "").rstrip("/")
    if not api:
        raise ConnectionError("service de licence pas encore configuré")
    body = {"key": fields.get("license_key", "")}
    if action == "activate":
        body["instance_name"] = fields.get("instance_name", "ASTRA Dashboard")
    else:
        body["instance_id"] = fields.get("instance_id", "")
    req = urllib.request.Request(f"{api}/{action}", data=json.dumps(body).encode(), method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "ASTRA-Dashboard"})
    code, data = 200, {}
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            data = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            data = {}
        if code >= 500:
            raise ConnectionError(f"Creem HTTP {code}")
    inst = data.get("instance")
    if isinstance(inst, list):
        inst = inst[-1] if inst else None
    lk = {"status": data.get("status"), "expires_at": data.get("expires_at"),
          "activation_limit": data.get("activation_limit"), "activation_usage": data.get("activation")}
    meta = {"variant_id": data.get("product_id")} if data.get("product_id") else {}
    msg = data.get("error") or data.get("message")
    if isinstance(msg, list):
        msg = " ".join(map(str, msg))
    if code in (401, 403) and not data.get("status"):
        # le relais n'a pas une clé API Creem valide pour ce mode (test / réel)
        if action == "activate":
            return {"activated": False, "error": "relais mal configuré (clé API Creem refusée)", "license_key": lk}
        raise ConnectionError("relais mal configuré")
    if action == "activate":
        if code == 200 and data.get("status") == "active":
            return {"activated": True, "license_key": lk, "instance": inst or {}, "meta": meta}
        if code == 404:
            msg = "license not found"
        elif msg and "limit" in str(msg).lower():
            msg = "activation limit reached"
        return {"activated": False, "error": msg or f"HTTP {code}", "license_key": lk}
    if action == "validate":
        if code == 200:
            return {"valid": data.get("status") == "active", "license_key": lk, "meta": meta}
        status = "expired" if code == 410 else "disabled" if code == 403 else "invalid"
        return {"valid": False, "license_key": {"status": status}, "error": msg}
    return {"deactivated": code == 200}


def _ls_call(action, **fields):
    """Appel à l'API des licences (Lemon Squeezy, ou Creem via son relais)."""
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    if load_offers().get("provider") == "creem":
        return _creem_call(action, fields)

    req = urllib.request.Request(LS_API + action, data=urlencode(fields).encode(), headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ASTRA-Dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # Lemon Squeezy répond 400/404 avec un message d'erreur en JSON
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise ConnectionError(f"Lemon Squeezy HTTP {e.code}")


def _plan_from_meta(meta):
    offers = load_offers()
    plans = [p for p in offers.get("plans") or [] if not p.get("trial")]  # l'essai n'est jamais une clé payante
    vid = meta.get("variant_id")
    for p in plans:
        ids = [p.get("variant_id"), (p.get("annual") or {}).get("variant_id")]  # variante mensuelle ou annuelle
        if any(i and str(i) == str(vid) for i in ids):
            return p
    name = (str(meta.get("variant_name", "")) + " " + str(meta.get("product_name", ""))).lower()
    for p in sorted(plans, key=lambda x: -int(x.get("accounts", 1))):  # repli : d'après le nom de l'offre
        if p.get("name", "").lower() in name or (int(p.get("accounts", 1)) >= 2 and ("pro" in name or "50" in name)):
            return p
    return min(plans, key=lambda x: int(x.get("accounts", 1))) if plans else {"id": "essentiel", "name": "Essentiel", "accounts": 1}


def _store_ok(meta):
    want = load_offers().get("store_id")
    return not want or str(meta.get("store_id")) == str(want)


TRIAL_DAYS = 7


def _trial_marks():
    """Dates de début d'essai déjà enregistrées sur ce PC (fichier caché + registre Windows)."""
    found = []
    try:
        lic = _load(LICENCE_FILE, {})
        if lic.get("trial_start"):
            found.append(float(lic["trial_start"]))
    except Exception:
        pass
    mark = os.path.join(os.environ.get("LOCALAPPDATA") or DATA_DIR, "ASTRA", ".essai")
    try:
        with open(mark, encoding="utf-8") as f:
            found.append(float(f.read().strip()))
    except Exception:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\ASTRA Dashboard") as k:
            found.append(float(winreg.QueryValueEx(k, "Essai")[0]))
    except Exception:
        pass
    return found


def _trial_mark(ts):
    mark = os.path.join(os.environ.get("LOCALAPPDATA") or DATA_DIR, "ASTRA", ".essai")
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        with open(mark, "w", encoding="utf-8") as f:
            f.write(str(ts))
    except Exception:
        pass
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\ASTRA Dashboard") as k:
            winreg.SetValueEx(k, "Essai", 0, winreg.REG_SZ, str(ts))
    except Exception:
        pass


def _lic_public(lic):
    if not lic or not lic.get("key"):
        return {"active": False, "status": "none", "trial_used": bool(_trial_marks())}
    if lic.get("trial"):
        import time
        end = float(lic.get("trial_end", 0))
        left = max(0, end - time.time())
        return {"active": bool(lic.get("active")), "status": lic.get("status"), "owner": False, "trial": True, "trial_used": True,
                "plan": lic.get("plan"), "plan_name": lic.get("plan_name"), "max_accounts": lic.get("max_accounts"),
                "renews_at": dt.datetime.fromtimestamp(end).isoformat(timespec="seconds"), "days_left": int(-(-left // 86400)),
                "key_hint": "Essai gratuit", "error": lic.get("error")}
    k = lic["key"]
    hint = k[:4] + "…" + k[-4:] if len(k) > 10 else "…"
    return {"active": bool(lic.get("active")), "status": lic.get("status"), "owner": bool(lic.get("owner")),
            "plan": lic.get("plan"), "plan_name": lic.get("plan_name"), "max_accounts": lic.get("max_accounts"),
            "email": lic.get("email"), "renews_at": lic.get("expires_at"), "key_hint": hint,
            "checked_at": lic.get("checked_at"), "offline": bool(lic.get("offline")), "error": lic.get("error"),
            "trial_used": bool(_trial_marks())}


def licence(force=False):
    """État de la licence de ce PC (revérifiée en ligne de temps en temps)."""
    import time

    with _lic_lock:
        lic = _load(LICENCE_FILE, {})
        if not lic.get("key"):
            return {}
        if lic.get("owner"):
            lic["active"] = _owner_key(lic["key"])
            return lic
        now = time.time()
        if lic.get("trial"):  # essai gratuit : actif jusqu'à la date de fin, sans vérification en ligne
            lic["active"] = now < float(lic.get("trial_end", 0))
            lic["status"] = "trial" if lic["active"] else "expired"
            lic["error"] = None if lic["active"] else "Ton essai gratuit de 7 jours est terminé : choisis une offre pour continuer."
            return lic
        if not force and lic.get("active") and now - lic.get("last_ok", 0) < LIC_RECHECK:
            return lic
        if not force and now - lic.get("last_try", 0) < 60:
            return lic  # pas plus d'une vérification par minute
        lic["last_try"] = now
        try:
            r = _ls_call("validate", license_key=lic["key"], instance_id=lic.get("instance_id", ""))
        except Exception:
            lic["offline"] = True
            lic["active"] = now - lic.get("last_ok", 0) < LIC_GRACE
            lic["error"] = None if lic["active"] else "Impossible de vérifier ton abonnement (pas d'internet depuis plus de 3 jours)."
            _save(LICENCE_FILE, lic)
            return lic
        lk, meta = r.get("license_key") or {}, r.get("meta") or {}
        status = lk.get("status") or ("active" if r.get("valid") else "invalid")
        ok = bool(r.get("valid")) and status == "active" and _store_ok(meta)
        lic.update(offline=False, status=status, checked_at=dt.datetime.now().isoformat(timespec="seconds"),
                   expires_at=lk.get("expires_at"), email=meta.get("customer_email") or lic.get("email"))
        if meta:
            plan = _plan_from_meta(meta)
            lic.update(plan=plan.get("id"), plan_name=plan.get("name"), max_accounts=int(plan.get("accounts", 1)))
        lic["active"] = ok
        lic["error"] = None if ok else {"expired": "Ton abonnement a expiré.", "disabled": "Cette clé a été désactivée.",
                                        "inactive": "Cette clé n'est plus active sur ce PC."}.get(status, r.get("error") or "Clé invalide.")
        if ok:
            lic["last_ok"] = now
        _save(LICENCE_FILE, lic)
        return lic


def licence_active():
    try:
        return bool(licence().get("active"))
    except Exception:
        return False


PERK_NAMES = {"live": "Le suivi en direct des trades", "pdf": "Le rapport PDF mensuel", "cards": "Les cartes de résultats"}


def has_perk(name):
    """Fonction réservée à certaines offres (suivi en direct, rapport PDF)."""
    lic = licence()
    if lic.get("owner"):
        return True
    plan = next((p for p in load_offers().get("plans", []) if p.get("id") == lic.get("plan")), None)
    if plan is None or "perks" not in plan:
        return True
    return name in plan["perks"]


def _perk_refused(name):
    best = next((p for p in load_offers().get("plans", []) if name in (p.get("perks") or [])), {})
    return jsonify({"error": f"{PERK_NAMES.get(name, 'Cette fonction')} est inclus dans l'offre {best.get('name', 'supérieure')}. "
                             "Change d'offre dans « Tarification ».", "perk": name}), 403


def max_accounts():
    lic = licence()
    if lic.get("owner"):
        return None  # illimité
    return int(lic.get("max_accounts") or 1)


_OPEN_PATHS = ("/api/licence", "/api/offres", "/api/open-url", "/api/version", "/api/update", "/api/ping", "/api/bye",
               "/api/quit", "/api/prefs", "/static/", "/favicon.png")


@app.before_request
def _paywall():
    p = request.path
    if p == "/" or p.startswith(_OPEN_PATHS):
        return None
    if not licence_active():
        return jsonify({"error": "Abonnement requis : choisis une offre dans « Tarification ».", "licence": False}), 402
    return None


@app.get("/api/offres")
def api_offres():
    o = load_offers()
    def clean(p):
        q = {k: v for k, v in p.items() if k != "variant_id"}
        if isinstance(q.get("annual"), dict):
            q["annual"] = {k: v for k, v in q["annual"].items() if k != "variant_id"}
        return q
    return jsonify({"plans": [clean(p) for p in o.get("plans", [])], "annual_discount": o.get("annual_discount"),
                    "features": o.get("features") or [], "portal_url": o.get("portal_url"),
                    "social": o.get("social") or {}})


@app.get("/api/licence")
def api_licence():
    lic = licence(force=bool(request.args.get("verifier")))
    out = _lic_public(lic)
    accounts, cache = load_accounts(), load_cache()
    out["real_accounts"] = sum(1 for l in accounts if not cache.get(l, {}).get("demo"))
    out["perks"] = {k: (has_perk(k) if out.get("active") else False) for k in PERK_NAMES}
    return jsonify(out)


@app.post("/api/licence")
def api_licence_activate():
    import socket as _s
    import time

    key = str((request.get_json(force=True) or {}).get("key", "")).strip()
    if len(key) < 8 or len(key) > 120:
        return jsonify({"error": "Colle la clé de licence reçue par e-mail après ton achat."}), 400
    if _owner_key(key):
        lic = {"key": key, "owner": True, "active": True, "status": "active", "plan": "owner", "plan_name": "Propriétaire",
               "max_accounts": None, "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
        _save(LICENCE_FILE, lic)
        return jsonify(_lic_public(lic))
    try:
        r = _ls_call("activate", license_key=key, instance_name=f"ASTRA Dashboard – {_s.gethostname()}"[:100])
    except Exception:
        return jsonify({"error": "Impossible de vérifier la clé pour le moment (connexion internet ou service de licence). Réessaie dans quelques minutes."}), 502
    meta, lk = r.get("meta") or {}, r.get("license_key") or {}
    if not r.get("activated"):
        err = str(r.get("error") or "Clé invalide.")
        if "relais mal configuré" in err:
            err = ("Le service de vérification des clés est mal configuré (clé API Creem refusée). "
                   "Vérifie la clé API dans le Worker Cloudflare : clé de test avec CREEM_TEST = 1, ou clé réelle sans CREEM_TEST.")
        elif "limit" in err.lower():
            err = (f"Cette clé est déjà utilisée sur {lk.get('activation_usage', 'plusieurs')} PC "
                   f"(maximum {lk.get('activation_limit', '?')}). Retire-la d'un autre PC d'abord.")
        elif "not found" in err.lower() or "invalid" in err.lower():
            err = "Cette clé n'existe pas. Vérifie qu'elle est copiée en entier."
        return jsonify({"error": err}), 400
    if not _store_ok(meta):
        return jsonify({"error": "Cette clé ne vient pas de la boutique ASTRA Capital."}), 400
    plan = _plan_from_meta(meta)
    lic = {"key": key, "instance_id": (r.get("instance") or {}).get("id"), "owner": False, "active": lk.get("status", "active") == "active",
           "status": lk.get("status", "active"), "plan": plan.get("id"), "plan_name": plan.get("name"),
           "max_accounts": int(plan.get("accounts", 1)), "email": meta.get("customer_email"), "expires_at": lk.get("expires_at"),
           "checked_at": dt.datetime.now().isoformat(timespec="seconds"), "last_ok": time.time()}
    old = _load(LICENCE_FILE, {})
    if old.get("trial_start"):
        lic["trial_start"] = old["trial_start"]
    _save(LICENCE_FILE, lic)
    return jsonify(_lic_public(lic))


@app.post("/api/licence/trial")
def api_licence_trial():
    import time

    cur = licence()
    if cur.get("active") and not cur.get("trial"):
        return jsonify({"error": "Tu as déjà un abonnement actif."}), 400
    if _trial_marks():
        return jsonify({"error": "L'essai gratuit a déjà été utilisé sur ce PC. Choisis une offre pour continuer."}), 400
    plan = next((p for p in load_offers().get("plans", []) if p.get("trial")), {"id": "essai", "name": "Essai gratuit", "accounts": 1})
    now = time.time()
    days = int(plan.get("trial_days", TRIAL_DAYS))
    lic = {"key": "ESSAI", "trial": True, "trial_start": now, "trial_end": now + days * 86400, "active": True, "status": "trial",
           "plan": plan.get("id"), "plan_name": plan.get("name"), "max_accounts": int(plan.get("accounts", 1)), "owner": False}
    _save(LICENCE_FILE, lic)
    _trial_mark(now)
    return jsonify(_lic_public(lic))


@app.post("/api/licence/remove")
def api_licence_remove():
    lic = _load(LICENCE_FILE, {})
    if lic.get("key") and not lic.get("owner") and lic.get("instance_id"):
        try:
            _ls_call("deactivate", license_key=lic["key"], instance_id=lic["instance_id"])  # libère la place pour un autre PC
        except Exception:
            pass
    _save(LICENCE_FILE, {"trial_start": lic["trial_start"]} if lic.get("trial_start") else {})  # l'essai reste « utilisé »
    return jsonify({"ok": True})


@app.post("/api/open-url")
def api_open_url():
    """Ouvre la page de paiement dans le navigateur habituel (cartes et comptes déjà enregistrés)."""
    from urllib.parse import urlparse

    url = str((request.get_json(force=True) or {}).get("url", ""))
    o = load_offers()
    allowed = ({p.get("checkout_url") for p in o.get("plans", [])} | {(p.get("annual") or {}).get("checkout_url") for p in o.get("plans", [])}
               | {o.get("portal_url")} | set((o.get("social") or {}).values())) - {None, ""}
    host = urlparse(url).hostname or ""
    if url not in allowed and not (url.startswith("https://") and any(host == d or host.endswith("." + d) for d in ("lemonsqueezy.com", "creem.io"))):
        return jsonify({"error": "Adresse non autorisée."}), 400
    webbrowser.open(url)
    return jsonify({"ok": True})


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

    accounts0, cache0 = load_accounts(), load_cache()
    limit = max_accounts()
    real = [l for l in accounts0 if not cache0.get(l, {}).get("demo")]
    if limit is not None and login not in accounts0 and len(real) >= limit:
        bigger = max(load_offers().get("plans") or [{}], key=lambda x: int(x.get("accounts", 1)))
        more = (f"Passe à l'offre {bigger.get('name', 'supérieure')} dans « Tarification » pour en connecter jusqu'à "
                f"{bigger.get('accounts')}, ou retire d'abord un compte." if int(bigger.get("accounts", 1)) > limit
                else "Retire d'abord un compte pour en connecter un autre.")
        return jsonify({"error": f"Ton offre permet de connecter {limit} compte{'s' if limit > 1 else ''}. " + more,
                        "limit": limit}), 403

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


MASK = "••••••"


def _mask_name(name):
    import re
    return re.sub(r"\d{5,}", MASK, name or "")


def report_html(rep, auto_print=False, preview=False, masked=False):
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
    name_ = acc.get("name") or acc["login"]
    login_ = acc["login"]
    if masked:
        name_, login_ = _mask_name(name_), MASK
    acc_line = e(name_) + (f" · {e(login_)} · {e(acc.get('server', ''))}" if acc.get("login") != "0" else " · Démo")
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
    if not has_perk("pdf"):
        return _perk_refused("pdf")
    login, month = str(request.args.get("login", "")), str(request.args.get("month", ""))
    if not _valid_month(month):
        return "Mois invalide", 400
    try:
        rep = month_report(login, month)
    except KeyError as ex:
        return str(ex), 404
    return report_html(rep, auto_print=request.args.get("print") == "1", preview=request.args.get("apercu") == "1",
                       masked=request.args.get("masque") == "1")


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
    if not has_perk("pdf"):
        return _perk_refused("pdf")
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
    masked = request.args.get("masque") == "1"
    url = (f"http://127.0.0.1:{SERVER['port'] or PREFERRED_PORT}/rapport?login={quote(login)}&month={month}"
           + ("&masque=1" if masked else ""))
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
    nm = accounts[login].get("name") or login
    if masked:
        nm = _mask_name(nm).replace(MASK, "xxxxxx")
    safe = "".join(ch for ch in nm if ch not in '\\/:*?"<>|').strip() or "compte"
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
UPDATE_REPO = "astradashboard/astra-dashboard"
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
