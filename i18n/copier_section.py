# ---------------------------------------------------------------- copieur de trades
# Copie les positions d'un compte (source) vers d'autres comptes (destinations), comme un copieur en ligne,
# mais en local. Chaque compte du copieur a son propre MT5 caché (une copie privée par compte) piloté par un
# petit processus « agent » (python app.py --agent-copieur). L'application compare en continu les positions
# du compte source et des comptes destination et envoie aux agents les ordres à passer.
COPIER_FILE = os.path.join(DATA_DIR, "copieur.json")
COPIER_STATE_FILE = os.path.join(DATA_DIR, "copieur-etat.json")
COPIER_LOG_FILE = os.path.join(DATA_DIR, "copieur-journal.log")
COPIER_TERMINALS_DIR = os.path.join(DATA_DIR, "copieur-terminaux")
COPIER_KEYRING = "ASTRA Dashboard Copieur"
COPIER_MAGIC = 770077            # numéro « magique » des positions ouvertes par le copieur
COPIER_RISKS = ("balance", "lot", "fixed")
PERK_NAMES["copier"] = "Le copieur de trades"


copier_cfg_lock = threading.RLock()


def load_copier():
    c = _load(COPIER_FILE, {})
    c.setdefault("accounts", [])
    c.setdefault("copiers", [])
    return c


def save_copier(c):
    _save(COPIER_FILE, c)


def copier_password(login):
    if ("copieur:" + login) in session_passwords:
        return session_passwords["copieur:" + login]
    if keyring:
        try:
            pw = keyring.get_password(COPIER_KEYRING, login)
            if pw:
                return pw
        except Exception:
            pass
    return None


def _set_copier_password(login, pw):
    session_passwords["copieur:" + login] = pw
    if keyring:
        try:
            keyring.set_password(COPIER_KEYRING, login, pw)
        except Exception:
            pass


def _del_copier_password(login):
    session_passwords.pop("copieur:" + login, None)
    if keyring:
        try:
            keyring.delete_password(COPIER_KEYRING, login)
        except Exception:
            pass


# ---- côté agent (processus séparé, un par compte) -------------------------------
def _pids_under(root):
    """PID des processus lancés depuis un dossier (Windows)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()
    if os.name != "nt":
        return set()
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32
    arr = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
        return set()
    root = os.path.normcase(os.path.abspath(root)) + os.sep
    pids, buf = set(), ctypes.create_unicode_buffer(1024)
    for pid in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        h = kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and os.path.normcase(buf.value).startswith(root):
                pids.add(pid)
        finally:
            kernel32.CloseHandle(h)
    return pids


def _kill_under(root):
    import subprocess

    for pid in _pids_under(root):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _hide_windows_under(root, stop):
    """Cache en continu les fenêtres du MT5 de l'agent."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    pids = set()

    def hide(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 0)
        return True

    cb = proto(hide)
    tick = 0
    while not stop.is_set():
        if tick % 10 == 0:
            pids = _pids_under(root)
        if pids:
            user32.EnumWindows(cb, 0)
        tick += 1
        stop.wait(0.05)


def _enable_algo_trading(term_dir):
    """Active le bouton « Algo Trading » de la copie privée : sans lui, MT5 refuse les ordres envoyés par l'app."""
    cfg = os.path.join(term_dir, "Config")
    os.makedirs(cfg, exist_ok=True)
    path = os.path.join(cfg, "common.ini")
    text = ""
    for enc in ("utf-16", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            break
        except (OSError, UnicodeError):
            continue
    want = {"Enabled": "1", "AllowLiveTrading": "1", "Account": "0", "Profile": "0"}
    lines, out, in_exp, done = text.splitlines(), [], False, set()
    for ln in lines:
        s = ln.strip()
        if s.startswith("["):
            if in_exp:
                out += [f"{k}={v}" for k, v in want.items() if k not in done]
            in_exp = s.lower() == "[experts]"
            out.append(ln)
            continue
        if in_exp and "=" in s and s.split("=", 1)[0] in want:
            k = s.split("=", 1)[0]
            out.append(f"{k}={want[k]}")
            done.add(k)
            continue
        out.append(ln)
    if in_exp:
        out += [f"{k}={v}" for k, v in want.items() if k not in done]
    if not any(l.strip().lower() == "[experts]" for l in lines):
        out += ["[Experts]"] + [f"{k}={v}" for k, v in want.items()]
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write("\r\n".join(out) + "\r\n")
    except OSError:
        pass


def copier_terminal(acc):
    """Copie privée de MT5 propre à un compte du copieur (un terminal = un compte connecté en permanence)."""
    import shutil

    src_exe = pick_source_terminal(acc)
    src_dir = os.path.dirname(src_exe)
    dest_dir = os.path.join(COPIER_TERMINALS_DIR, str(acc["login"]))
    dest_exe = os.path.join(dest_dir, "terminal64.exe")
    if not os.path.isfile(dest_exe):
        shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("logs", "Logs", "Tester", "Bases", "uninstall.exe", "MQL5"))
    if not os.path.isfile(os.path.join(dest_dir, "Config", "servers.dat")):
        _copy_servers_list(src_dir, dest_dir)
    _enable_algo_trading(dest_dir)
    return dest_exe


def _sym_key(s):
    import re
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    for suf in ("cash", "pro", "raw", "ecn", "std", "micro", "mini"):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[: -len(suf)]
    return s


class Mt5Backend:
    """Accès MT5 réel d'un agent (une copie privée du terminal, un compte)."""

    def __init__(self):
        self.syms = {}
        self.resolved = {}

    def connect(self, p):
        acc = {"login": p["login"], "server": p["server"], "terminal": p.get("terminal") or ""}
        exe = copier_terminal(acc)
        self.root = os.path.dirname(exe)
        _kill_under(self.root)
        self.stop = threading.Event()
        threading.Thread(target=_hide_windows_under, args=(self.root, self.stop), daemon=True).start()
        info = _connect(acc, p["password"], exe)
        term = mt5.terminal_info()
        return {"login": str(info.login), "currency": info.currency, "balance": info.balance, "equity": info.equity,
                "company": info.company, "trade_allowed": bool(info.trade_allowed) and bool(getattr(info, "trade_expert", True)),
                "algo_allowed": bool(getattr(term, "trade_allowed", True)),
                "hedging": getattr(info, "margin_mode", 2) == getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)}

    def close(self):
        try:
            mt5.shutdown()
        except Exception:
            pass
        try:
            self.stop.set()
            _kill_under(self.root)
        except Exception:
            pass

    def _meta(self, sym):
        m = self.syms.get(sym)
        if m is None:
            si = mt5.symbol_info(sym)
            if si is None:
                return {"step": 0.01, "point": 0.00001, "digits": 5, "min": 0.01, "max": 100.0}
            m = {"step": si.volume_step or 0.01, "point": si.point or 0.00001, "digits": si.digits,
                 "min": si.volume_min or 0.01, "max": si.volume_max or 100.0, "fill": si.filling_mode}
            self.syms[sym] = m
        return m

    def snapshot(self):
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("Connexion au compte perdue.")
        pos = []
        for p_ in mt5.positions_get() or []:
            m = self._meta(p_.symbol)
            pos.append({"ticket": int(p_.ticket), "id": int(getattr(p_, "identifier", p_.ticket)), "symbol": p_.symbol,
                        "side": "buy" if p_.type == mt5.POSITION_TYPE_BUY else "sell", "volume": p_.volume,
                        "sl": p_.sl or 0.0, "tp": p_.tp or 0.0, "magic": int(p_.magic), "comment": p_.comment or "",
                        "price": p_.price_open, "time": int(getattr(p_, "time_msc", p_.time * 1000)),
                        "profit": round(p_.profit + p_.swap, 2), "step": m["step"], "point": m["point"]})
        return {"balance": info.balance, "equity": info.equity, "currency": info.currency}, pos

    def resolve(self, sym):
        """Symbole équivalent chez ce broker (ex. US100.cash ↔ NAS100)."""
        if sym in self.resolved:
            return self.resolved[sym]
        found = None
        if mt5.symbol_info(sym) is not None:
            found = sym
        else:
            names = [s.name for s in (mt5.symbols_get() or [])]
            k = _sym_key(sym)
            cands = [n for n in names if _sym_key(n) == k]
            if not cands:
                for _, _, _, _, aliases in QUOTE_LIST:
                    keys = {_sym_key(a) for a in aliases}
                    if k in keys:
                        cands = [n for n in names if _sym_key(n) in keys]
                        if cands:
                            break
            found = cands[0] if cands else None
        if found:
            mt5.symbol_select(found, True)
        self.resolved[sym] = found
        return found

    def _fillings(self, sym):
        fm = self._meta(sym).get("fill") or 0
        order = []
        if fm & 1:
            order.append(mt5.ORDER_FILLING_FOK)
        if fm & 2:
            order.append(mt5.ORDER_FILLING_IOC)
        order.append(mt5.ORDER_FILLING_RETURN)
        for f in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC):
            if f not in order:
                order.append(f)
        return order

    def _send(self, req):
        last = None
        for fill in self._fillings(req["symbol"]):
            req["type_filling"] = fill
            r = mt5.order_send(req)
            if r is None:
                code, msg = mt5.last_error()
                last = (False, None, f"{msg} ({code})", code)
                continue
            if r.retcode in (10009, 10008, 10010):  # exécuté / placé / exécuté en partie
                return True, int(r.order or r.deal or 0), "ok", r.retcode
            last = (False, None, f"{r.comment} ({r.retcode})", r.retcode)
            if r.retcode != 10030:  # 10030 : mode de remplissage refusé → on essaie le suivant
                break
        return last

    def _norm_vol(self, sym, v):
        m = self._meta(sym)
        step = m["step"]
        v = round(round(v / step) * step, 8)
        return min(max(v, m["min"]), m["max"])

    def open(self, c):
        sym = self.resolve(c["symbol"])
        if not sym:
            return False, None, f"Symbole {c['symbol']} introuvable sur ce compte.", 0
        m = self._meta(sym)
        vol = self._norm_vol(sym, c["volume"])
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return False, None, f"Pas de prix pour {sym} (marché fermé ?).", 0
        buy = c["side"] == "buy"
        rd = lambda x: round(x, m["digits"]) if x else 0.0
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": vol,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": tick.ask if buy else tick.bid,
               "sl": rd(c.get("sl")), "tp": rd(c.get("tp")), "deviation": 30, "magic": COPIER_MAGIC,
               "comment": c.get("comment", "")[:31], "type_time": mt5.ORDER_TIME_GTC}
        res = self._send(dict(req))
        if not res[0] and res[3] == 10016 and (req["sl"] or req["tp"]):  # stops refusés : on ouvre sans, ils seront posés ensuite
            req["sl"] = req["tp"] = 0.0
            res = self._send(dict(req))
        return res[0], res[1], res[2] if not res[0] else f"{sym} {vol}", res[3]

    def close_pos(self, c):
        p_ = next(iter(mt5.positions_get(ticket=int(c["ticket"])) or []), None)
        if p_ is None:
            return True, None, "déjà fermée", 0
        vol = self._norm_vol(p_.symbol, min(c.get("volume") or p_.volume, p_.volume))
        if c.get("volume") is None or vol >= p_.volume:
            vol = p_.volume
        tick = mt5.symbol_info_tick(p_.symbol)
        buy = p_.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p_.symbol, "volume": vol, "position": p_.ticket,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
               "price": (tick.bid if buy else tick.ask) if tick else 0.0, "deviation": 30, "magic": COPIER_MAGIC,
               "type_time": mt5.ORDER_TIME_GTC}
        return self._send(req)

    def modify(self, c):
        p_ = next(iter(mt5.positions_get(ticket=int(c["ticket"])) or []), None)
        if p_ is None:
            return False, None, "position introuvable", 0
        d = self._meta(p_.symbol)["digits"]
        req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": p_.symbol, "position": p_.ticket,
               "sl": round(c.get("sl") or 0.0, d), "tp": round(c.get("tp") or 0.0, d), "magic": COPIER_MAGIC}
        r = mt5.order_send(req)
        if r is None:
            code, msg = mt5.last_error()
            return False, None, f"{msg} ({code})", code
        return r.retcode == 10009 or r.retcode == 10025, None, f"{r.comment} ({r.retcode})", r.retcode


class FakeBackend:
    """Courtier simulé (tests sans MetaTrader 5) : positions gardées dans un fichier JSON par compte."""

    def connect(self, p):
        self.login = str(p["login"])
        self.path = os.path.join(DATA_DIR, "faux-courtier", self.login + ".json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if p.get("password") == "mauvais":
            raise RuntimeError("Connexion refusée (Authorization failed). Vérifie le numéro de compte, le mot de passe et le nom exact du serveur.")
        d = self._load()
        self._save(d)
        return {"login": self.login, "currency": "USD", "balance": d["balance"], "equity": d["balance"], "company": "Faux courtier",
                "trade_allowed": p.get("password") != "investisseur", "algo_allowed": True, "hedging": True}

    def _load(self):
        d = _load(self.path, None) or {"balance": 100000.0, "next": 1000, "pos": []}
        return d

    def _save(self, d):
        _save(self.path, d)

    def close(self):
        pass

    def snapshot(self):
        d = self._load()
        return {"balance": d["balance"], "equity": d["balance"], "currency": "USD"}, \
            [{**p_, "id": p_["ticket"], "step": 0.01, "point": 0.01, "profit": 0.0} for p_ in d["pos"]]

    def open(self, c):
        d = self._load()
        d["next"] += 1
        vol = max(0.01, round(round(c["volume"] / 0.01) * 0.01, 2))
        d["pos"].append({"ticket": d["next"], "symbol": c["symbol"], "side": c["side"], "volume": vol, "sl": c.get("sl") or 0.0,
                         "tp": c.get("tp") or 0.0, "magic": COPIER_MAGIC, "comment": c.get("comment", "")[:31], "price": 1.0,
                         "time": int(dt.datetime.now().timestamp() * 1000)})
        self._save(d)
        return True, d["next"], f"{c['symbol']} {vol}", 10009

    def close_pos(self, c):
        d = self._load()
        for p_ in d["pos"]:
            if p_["ticket"] == int(c["ticket"]):
                v = c.get("volume")
                if v is None or v >= p_["volume"] - 1e-9:
                    d["pos"].remove(p_)
                else:
                    p_["volume"] = round(p_["volume"] - round(v, 2), 2)
                self._save(d)
                return True, None, "ok", 10009
        return True, None, "déjà fermée", 0

    def modify(self, c):
        d = self._load()
        for p_ in d["pos"]:
            if p_["ticket"] == int(c["ticket"]):
                p_["sl"], p_["tp"] = c.get("sl") or 0.0, c.get("tp") or 0.0
                self._save(d)
                return True, None, "ok", 10009
        return False, None, "position introuvable", 0


def copier_agent_main():
    """Processus agent : se connecte à un compte, envoie ses positions toutes les 100 ms et exécute les ordres reçus."""
    import queue
    import time

    if sys.stdout is None:  # pythonw : on récupère les tuyaux ouverts par l'application
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
    if sys.stdin is None:
        sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
    out_lock = threading.Lock()

    def emit(obj):
        with out_lock:
            try:
                sys.stdout.write(json.dumps(obj) + "\n")
                sys.stdout.flush()
            except Exception:
                os._exit(0)

    cmds = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                cmds.put(json.loads(line))
            except ValueError:
                continue
        cmds.put({"cmd": "stop"})  # l'application s'est fermée

    threading.Thread(target=reader, daemon=True).start()
    first = cmds.get()
    if first.get("cmd") != "init":
        return
    be = FakeBackend() if os.environ.get("ASTRA_COPIER_FAKE") == "1" else Mt5Backend()
    if not isinstance(be, FakeBackend) and mt5 is None:
        emit({"ev": "error", "fatal": True, "msg": "MetaTrader 5 n'est pas disponible sur cet ordinateur (Windows requis)."})
        return
    try:
        info = be.connect(first)
    except Exception as ex:
        emit({"ev": "error", "fatal": True, "msg": str(ex)})
        try:
            be.close()
        except Exception:
            pass
        return
    emit({"ev": "ready", "info": info})
    last_sig, last_emit, fails = None, 0.0, 0
    try:
        while True:
            try:
                while True:
                    c = cmds.get_nowait()
                    if c.get("cmd") == "stop":
                        return
                    try:
                        fn = {"open": be.open, "close": be.close_pos, "modify": be.modify}[c["cmd"]]
                        ok, ticket, msg, code = fn(c)
                    except Exception as ex:
                        ok, ticket, msg, code = False, None, str(ex), 0
                    emit({"ev": "res", "rid": c.get("rid"), "ok": bool(ok), "ticket": ticket, "msg": msg, "code": code})
                    last_sig = None  # renvoie l'état tout de suite
            except queue.Empty:
                pass
            try:
                acc, pos = be.snapshot()
                fails = 0
            except Exception as ex:
                fails += 1
                if fails >= 30:
                    emit({"ev": "error", "fatal": True, "msg": f"Connexion perdue : {ex}"})
                    return
                time.sleep(0.2)
                continue
            sig = json.dumps([pos, round(acc["balance"], 2)], sort_keys=True)
            now = time.time()
            if sig != last_sig or now - last_emit > 1.0:
                emit({"ev": "snap", "acc": acc, "pos": pos})
                last_sig, last_emit = sig, now
            time.sleep(0.1)
    finally:
        be.close()


# ---- côté application : agents et moteur de copie ------------------------------------
class CopierAgent:
    def __init__(self, acc):
        self.acc = acc
        self.login = str(acc["login"])
        self.proc = None
        self.status, self.error, self.info = "starting", None, {}
        self.snap, self.snap_at = None, 0.0
        self.started = 0.0
        self.died = 0.0
        self.lock = threading.Lock()
        self.results = {}
        self.rid = 0

    def start(self, password):
        import subprocess
        import time

        self.started = time.time()
        self.status, self.error = "starting", None
        args = [sys.executable, os.path.abspath(__file__), "--agent-copieur"]
        errlog = open(os.path.join(DATA_DIR, "copieur-agents.log"), "a", encoding="utf-8")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                     text=True, encoding="utf-8", bufsize=1, cwd=APP_DIR,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"cmd": "init", "login": self.login, "server": self.acc.get("server", ""),
                   "terminal": self.acc.get("terminal", ""), "password": password})

    def send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def request(self, obj, cb):
        with self.lock:
            self.rid += 1
            obj["rid"] = self.rid
            self.results[self.rid] = cb
        if not self.send(obj):
            with self.lock:
                self.results.pop(obj["rid"], None)
            cb({"ok": False, "msg": "agent arrêté"})

    def _read(self):
        import time

        proc = self.proc
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            ev = m.get("ev")
            if ev == "snap":
                self.snap, self.snap_at = m, time.time()
                if self.status != "ok":
                    self.status, self.error = "ok", None
            elif ev == "ready":
                self.info = m.get("info") or {}
                self.status = "ok"
            elif ev == "error":
                self.status, self.error, self.died = "error", m.get("msg"), time.time()
            elif ev == "res":
                with self.lock:
                    cb = self.results.pop(m.get("rid"), None)
                if cb:
                    try:
                        cb(m)
                    except Exception:
                        pass
        if self.proc is proc and self.status != "stopped":
            if self.status != "error":
                self.status, self.error, self.died = "error", "L'agent s'est arrêté.", time.time()
        with self.lock:
            pending, self.results = list(self.results.values()), {}
        for cb in pending:
            cb({"ok": False, "msg": "agent arrêté"})

    def stop(self):
        self.status = "stopped"
        try:
            self.send({"cmd": "stop"})
            self.proc.stdin.close()
        except Exception:
            pass

    def fresh(self, max_age=3.0):
        import time
        return self.status == "ok" and self.snap is not None and time.time() - self.snap_at < max_age

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class CopierEngine:
    def __init__(self):
        self.agents = {}
        self.lock = threading.RLock()
        self.inflight = {}           # (copieur, ticket source, action) → heure
        self.fails = defaultdict(int)
        self.armed = set()           # copieurs dont les trades déjà ouverts ont été notés au démarrage
        self.state = _load(COPIER_STATE_FILE, {})
        self.state.setdefault("map", {})
        self.log = []
        self.started = False
        self._last_ensure = 0.0

    # journal visible dans l'app
    def note(self, level, msg, cid=None):
        e = {"at": dt.datetime.now().isoformat(timespec="seconds"), "level": level, "msg": msg, "copier": cid}
        with self.lock:
            self.log.append(e)
            self.log = self.log[-300:]
        try:
            with open(COPIER_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{e['at']} [{level}] {msg}\n")
        except OSError:
            pass

    def save_state(self):
        try:
            _save(COPIER_STATE_FILE, self.state)
        except OSError:
            pass

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import time

        while True:
            try:
                self.tick()
            except Exception as ex:
                self.note("err", f"Erreur interne du copieur : {ex}")
                time.sleep(1)
            time.sleep(0.1)

    def stop_all(self):
        with self.lock:
            for a in self.agents.values():
                a.stop()
            self.agents = {}

    def _ensure_agents(self, cfg):
        import time

        now = time.time()
        if now - self._last_ensure < 1.0:
            return
        self._last_ensure = now
        enabled = licence_active() and has_perk("copier")
        wanted = {str(a["login"]): a for a in cfg["accounts"]} if enabled else {}
        with self.lock:
            for login in list(self.agents):
                a = self.agents[login]
                w = wanted.get(login)
                if w is None or a.acc.get("_rev") != w.get("_rev"):
                    a.stop()
                    del self.agents[login]
            for login, acc in wanted.items():
                a = self.agents.get(login)
                if a is not None:
                    if a.alive() or a.status in ("ok", "starting") and a.proc is not None:
                        continue
                    wait = 300 if a.error and "refusée" in a.error else 20
                    if a.proc is None:
                        wait = 5  # mot de passe manquant : on revérifie de temps en temps
                    if now - (a.died or a.started) < wait:
                        continue
                pw = copier_password(login)
                na = CopierAgent(acc)
                self.agents[login] = na
                if not pw:
                    na.status, na.error, na.died = "error", "Mot de passe manquant : modifie le compte et entre son mot de passe.", now
                    continue
                try:
                    na.start(pw)
                except Exception as ex:
                    na.status, na.error, na.died = "error", f"Impossible de lancer l'agent : {ex}", now

    def tick(self):
        cfg = load_copier()
        self._ensure_agents(cfg)
        accs = {str(a["login"]): a for a in cfg["accounts"]}
        upd = {}
        for c in cfg["copiers"]:
            if not c.get("active"):
                self.armed.discard(c["id"])
                continue
            if c["from"] not in accs or c["to"] not in accs:
                continue
            if self._reconcile(c):
                upd[c["id"]] = c.get("ignore") or []
        if upd:
            with copier_cfg_lock:  # on ne réécrit que la liste des trades ignorés, sans écraser une modification faite entre-temps
                cfg2 = load_copier()
                for c in cfg2["copiers"]:
                    if c["id"] in upd and c.get("active"):
                        c["ignore"] = upd[c["id"]]
                save_copier(cfg2)

    def _vol(self, c, p, M, S):
        mult = float(c.get("mult") or 1.0)
        if c.get("risk") == "fixed":
            return mult
        if c.get("risk") == "lot":
            return p["volume"] * mult
        mb, sb = M.snap["acc"]["balance"] or 0, S.snap["acc"]["balance"] or 0
        if mb <= 0:
            return p["volume"] * mult
        return p["volume"] * sb / mb * mult

    def _busy(self, key, timeout=15.0):
        import time
        t = self.inflight.get(key)
        return t is not None and time.time() - t < timeout

    def _reconcile(self, c):
        """Compare les positions source / destination et envoie ce qu'il faut faire. Renvoie True si la config a changé."""
        import time

        cid = c["id"]
        with self.lock:
            M, S = self.agents.get(c["from"]), self.agents.get(c["to"])
        if not M or not S or not M.fresh() or not S.fresh():
            return False
        if S.info and not S.info.get("trade_allowed", True):
            return False
        changed = False
        mp = {str(p["ticket"]): p for p in M.snap["pos"] if p.get("magic") != COPIER_MAGIC}
        links = self.state["map"].setdefault(cid, {})
        if cid not in self.armed:
            # au démarrage (ou à la reprise) : les trades déjà ouverts ne sont copiés que si l'option est cochée
            ign = set(c.get("ignore") or [])
            if not c.get("existing"):
                ign |= {t for t in mp if t not in links}
            c["ignore"] = sorted(ign & set(mp))
            self.armed.add(cid)
            changed = True
        ignore = set(c.get("ignore") or [])
        spos = S.snap["pos"]
        by_ticket = {}
        for p in spos:
            by_ticket[str(p["ticket"])] = p
            by_ticket.setdefault(str(p.get("id")), p)
        tag = "A" + cid[:4] + "-"
        for p in spos:  # positions reconnues par leur commentaire (si l'état local a été perdu)
            cm = p.get("comment") or ""
            if p.get("magic") == COPIER_MAGIC and cm.startswith(tag):
                mt = cm[len(tag):]
                if mt and mt not in links:
                    links[mt] = {"s": str(p["ticket"]), "mv0": mp[mt]["volume"] if mt in mp else p["volume"], "sv0": p["volume"], "at": time.time()}
                    self.save_state()
        now = time.time()
        for mt, p in mp.items():
            if mt in ignore:
                continue
            link = links.get(mt)
            if link is None:
                key = (cid, mt, "open")
                if self._busy(key) or self.fails[key] >= 3:
                    continue
                vol = self._vol(c, p, M, S)
                self.inflight[key] = now
                req = {"cmd": "open", "symbol": p["symbol"], "side": p["side"], "volume": vol,
                       "sl": p["sl"] if c.get("sltp", True) else 0.0, "tp": p["tp"] if c.get("sltp", True) else 0.0,
                       "comment": tag + mt}
                S.request(req, lambda r, key=key, mt=mt, p=p, vol=vol: self._opened(c, key, mt, p, vol, r))
                continue
            if link.get("gone"):
                continue
            sp = by_ticket.get(link["s"])
            if sp is None:
                if now - link.get("at", 0) > 6:
                    link["gone"] = True  # fermée à la main ou par son SL/TP sur le compte destination
                    self.save_state()
                continue
            # fermeture partielle sur la source → même proportion sur la destination
            if link.get("mv0") and p["volume"] < link["mv0"] - 1e-9:
                target = link["sv0"] * p["volume"] / link["mv0"]
                diff = sp["volume"] - target
                key = (cid, mt, "partial")
                if diff >= (sp.get("step") or 0.01) * 0.999 and not self._busy(key):
                    self.inflight[key] = now
                    S.request({"cmd": "close", "ticket": sp["ticket"], "volume": diff},
                              lambda r, key=key, mt=mt: self._done(c, key, r, f"Fermeture partielle copiée (#{mt})"))
            if c.get("sltp", True):
                eps = (sp.get("point") or 0.00001) / 2
                if abs((sp["sl"] or 0) - (p["sl"] or 0)) > eps or abs((sp["tp"] or 0) - (p["tp"] or 0)) > eps:
                    key = (cid, mt, "modify")
                    if not self._busy(key, 5) and self.fails[key] < 3:
                        self.inflight[key] = now
                        S.request({"cmd": "modify", "ticket": sp["ticket"], "sl": p["sl"], "tp": p["tp"]},
                                  lambda r, key=key, mt=mt: self._done(c, key, r, None))
        # positions fermées sur la source → fermées sur la destination
        for mt, link in list(links.items()):
            if mt in mp:
                continue
            sp = None if link.get("gone") else by_ticket.get(link["s"])
            if sp is None:
                if link.get("gone") or now - link.get("at", 0) > 6:
                    del links[mt]
                    self.save_state()
                continue
            key = (cid, mt, "close")
            if self._busy(key) or self.fails[key] >= 5:
                continue
            self.inflight[key] = now
            S.request({"cmd": "close", "ticket": sp["ticket"]},
                      lambda r, key=key, mt=mt, sym=sp["symbol"]: self._done(c, key, r, f"Fermeture copiée : {sym} (#{mt})"))
        if ignore - set(mp):
            c["ignore"] = sorted(ignore & set(mp))
            changed = True
        return changed

    def _name(self, login):
        a = next((x for x in load_copier()["accounts"] if str(x["login"]) == str(login)), {})
        return a.get("name") or str(login)

    def _opened(self, c, key, mt, p, vol, r):
        import time

        self.inflight.pop(key, None)
        if r.get("ok"):
            self.fails.pop(key, None)
            self.state["map"].setdefault(c["id"], {})[mt] = {"s": str(r.get("ticket")), "mv0": p["volume"], "sv0": round(vol, 2), "at": time.time()}
            self.save_state()
            side = "Achat" if p["side"] == "buy" else "Vente"
            self.note("ok", f"{self._name(c['from'])} → {self._name(c['to'])} : {side} {r.get('msg')} copié (#{mt})", c["id"])
        else:
            self.fails[key] += 1
            self.note("err", f"{self._name(c['from'])} → {self._name(c['to'])} : copie de {p['symbol']} refusée — {r.get('msg')}"
                      + (" (abandon après 3 essais)" if self.fails[key] >= 3 else ""), c["id"])

    def _done(self, c, key, r, okmsg):
        self.inflight.pop(key, None)
        if r.get("ok"):
            self.fails.pop(key, None)
            if okmsg:
                self.note("ok", f"{self._name(c['from'])} → {self._name(c['to'])} : {okmsg}", c["id"])
        else:
            self.fails[key] += 1
            self.note("err", f"{self._name(c['from'])} → {self._name(c['to'])} : {r.get('msg')}", c["id"])

    def forget(self, cid):
        self.state["map"].pop(cid, None)
        self.armed.discard(cid)
        self.save_state()

    def public(self):
        cfg = load_copier()
        with self.lock:
            agents = dict(self.agents)
            log = list(self.log[-120:])
        accounts = []
        for a in cfg["accounts"]:
            ag = agents.get(str(a["login"]))
            st = {"status": "off"}
            if ag is not None:
                st = {"status": "ok" if ag.fresh(5) else ("error" if ag.status == "error" else "starting"), "error": ag.error}
                if ag.snap:
                    st.update(balance=ag.snap["acc"]["balance"], equity=ag.snap["acc"]["equity"], currency=ag.snap["acc"]["currency"],
                              positions=len(ag.snap["pos"]))
                if ag.info:
                    st.update(trade_allowed=ag.info.get("trade_allowed", True), algo_allowed=ag.info.get("algo_allowed", True),
                              company=ag.info.get("company"))
            accounts.append({"login": str(a["login"]), "name": a.get("name") or str(a["login"]), "server": a.get("server", ""),
                             "terminal": a.get("terminal", ""), "has_password": bool(copier_password(str(a["login"]))), **st})
        copiers = [{k: c.get(k) for k in ("id", "from", "to", "risk", "mult", "existing", "sltp", "active")}
                   | {"copies": sum(1 for l in self.state["map"].get(c["id"], {}).values() if not l.get("gone"))}
                   for c in cfg["copiers"]]
        return {"accounts": accounts, "copiers": copiers, "log": log[::-1], "allowed": has_perk("copier"),
                "mt5": mt5 is not None or os.environ.get("ASTRA_COPIER_FAKE") == "1"}


COPIER = CopierEngine()


def _cfg_locked(fn):
    import functools

    @functools.wraps(fn)
    def wrap(*a, **k):
        with copier_cfg_lock:
            return fn(*a, **k)
    return wrap


def _copier_guard():
    if not licence_active():
        return jsonify({"error": "Abonnement requis."}), 403
    if not has_perk("copier"):
        return _perk_refused("copier")
    COPIER.start()
    return None


@app.get("/api/copier")
def api_copier():
    if licence_active() and has_perk("copier"):
        COPIER.start()
    return jsonify(COPIER.public())


@app.post("/api/copier/account")
@_cfg_locked
def api_copier_account():
    g = _copier_guard()
    if g:
        return g
    b = request.get_json(force=True) or {}
    login = str(b.get("login", "")).strip()
    server = str(b.get("server", "")).strip()
    name = str(b.get("name", "")).strip()[:60]
    pw = str(b.get("password", ""))
    old = str(b.get("old") or "")
    if not login.isdigit():
        return jsonify({"error": "Le numéro de compte doit contenir uniquement des chiffres."}), 400
    if not server:
        return jsonify({"error": "Indique le serveur (exactement comme dans MT5, ex. FTMO-Server)."}), 400
    cfg = load_copier()
    accs = cfg["accounts"]
    if any(str(a["login"]) == login for a in accs) and login != old:
        return jsonify({"error": "Ce compte est déjà dans le copieur."}), 409
    if not pw and not copier_password(login) and not (old and copier_password(old)):
        pw = get_saved_password(login) or ""
        if not pw:
            return jsonify({"error": "Entre le mot de passe du compte."}), 400
    if len(accs) >= 50 and not old:
        return jsonify({"error": "50 comptes maximum dans le copieur."}), 400
    entry = next((a for a in accs if str(a["login"]) == old), None) if old else None
    if entry is None:
        entry = {}
        accs.append(entry)
    if old and old != login:
        opw = copier_password(old)
        _del_copier_password(old)
        if not pw and opw:
            pw = opw
        for c in cfg["copiers"]:
            if c["from"] == old:
                c["from"] = login
            if c["to"] == old:
                c["to"] = login
    entry.update(login=login, server=server, name=name or login, terminal=str(b.get("terminal", "")).strip()[:300],
                 _rev=random.randint(1, 10 ** 9))
    if pw:
        _set_copier_password(login, pw)
    save_copier(cfg)
    return jsonify(COPIER.public())


@app.post("/api/copier/account/delete")
@_cfg_locked
def api_copier_account_delete():
    g = _copier_guard()
    if g:
        return g
    login = str((request.get_json(force=True) or {}).get("login", ""))
    cfg = load_copier()
    cfg["accounts"] = [a for a in cfg["accounts"] if str(a["login"]) != login]
    gone = [c["id"] for c in cfg["copiers"] if login in (c["from"], c["to"])]
    cfg["copiers"] = [c for c in cfg["copiers"] if c["id"] not in gone]
    save_copier(cfg)
    for cid in gone:
        COPIER.forget(cid)
    _del_copier_password(login)
    return jsonify(COPIER.public())


@app.post("/api/copier/copier")
@_cfg_locked
def api_copier_copier():
    g = _copier_guard()
    if g:
        return g
    b = request.get_json(force=True) or {}
    cfg = load_copier()
    logins = {str(a["login"]) for a in cfg["accounts"]}
    src, dst = str(b.get("from", "")), str(b.get("to", ""))
    if src not in logins or dst not in logins:
        return jsonify({"error": "Choisis le compte source et le compte destination."}), 400
    if src == dst:
        return jsonify({"error": "Le compte source et le compte destination doivent être différents."}), 400
    risk = b.get("risk") if b.get("risk") in COPIER_RISKS else "balance"
    try:
        mult = float(str(b.get("mult", 1)).replace(",", "."))
    except ValueError:
        mult = 0
    if not (0 < mult <= 100):
        return jsonify({"error": "Le multiplicateur doit être compris entre 0,01 et 100."}), 400
    cid = str(b.get("id") or "")
    c = next((x for x in cfg["copiers"] if x["id"] == cid), None) if cid else None
    if any(x["from"] == src and x["to"] == dst and x is not c for x in cfg["copiers"]):
        return jsonify({"error": "Un copieur existe déjà entre ces deux comptes."}), 409
    if c is None:
        c = {"id": "%04x%04x" % (random.randint(0, 0xFFFF), random.randint(0, 0xFFFF)), "active": True, "ignore": [],
             "created": dt.datetime.now().isoformat(timespec="seconds")}
        cfg["copiers"].append(c)
        COPIER.note("info", f"Copieur créé : {COPIER._name(src)} → {COPIER._name(dst)}", c["id"])
    elif c["from"] != src or c["to"] != dst:
        COPIER.forget(c["id"])
        c["ignore"] = []
    c.update({"from": src, "to": dst, "risk": risk, "mult": round(mult, 4), "existing": bool(b.get("existing")),
              "sltp": bool(b.get("sltp", True))})
    COPIER.armed.discard(c["id"])
    save_copier(cfg)
    return jsonify(COPIER.public())


@app.post("/api/copier/copier/toggle")
@_cfg_locked
def api_copier_toggle():
    g = _copier_guard()
    if g:
        return g
    b = request.get_json(force=True) or {}
    cfg = load_copier()
    c = next((x for x in cfg["copiers"] if x["id"] == str(b.get("id"))), None)
    if c is None:
        return jsonify({"error": "Copieur introuvable."}), 404
    c["active"] = bool(b.get("active"))
    if c["active"]:
        c["ignore"] = []
        COPIER.armed.discard(c["id"])
    save_copier(cfg)
    COPIER.note("info", f"Copieur {COPIER._name(c['from'])} → {COPIER._name(c['to'])} {'activé' if c['active'] else 'mis en pause'}", c["id"])
    return jsonify(COPIER.public())


@app.post("/api/copier/copier/delete")
@_cfg_locked
def api_copier_delete():
    g = _copier_guard()
    if g:
        return g
    cid = str((request.get_json(force=True) or {}).get("id", ""))
    cfg = load_copier()
    cfg["copiers"] = [c for c in cfg["copiers"] if c["id"] != cid]
    save_copier(cfg)
    COPIER.forget(cid)
    return jsonify(COPIER.public())


