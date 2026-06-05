"""
app.py -- Balfund Renko Trading System GUI
CustomTkinter dark-themed GUI with Token Manager + Strategy Control + Live Dashboard
"""
import sys, os, threading, time, json, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import customtkinter as ctk

# Frozen EXE path fix
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from engine import (
    INSTRUMENTS, DhanTokenManager, DhanAPI, api, resolve_security_ids,
    fetch_historical, get_signal_config, RenkoEngine, TradeManager,
    parse_header_8, parse_ticker, _norm_epoch, now_ist, ENV_FILE,
    IST, REQ_SUB_TICKER, REQ_UNSUB_TICKER, RESP_TICKER, RenkoBrick
)
from dotenv import load_dotenv, set_key
import websocket

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"renko_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S",
                    handlers=[logging.FileHandler(str(log_file),encoding='utf-8'),logging.StreamHandler()])
log = logging.getLogger("RENKO")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Colors
BG       = "#0a0e1a"
CARD_BG  = "#111827"
ACCENT   = "#06b6d4"
GREEN    = "#00e676"
RED      = "#ff1744"
YELLOW   = "#ffd600"
TEXT     = "#e0e0e0"
DIM      = "#6b7280"

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Balfund Renko Trading System v2.6")
        self.geometry("1000x700")
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # State
        self.running = False
        self.stop_event = threading.Event()
        self.ws = None
        self.ws_connected = threading.Event()
        self.engines = {}
        self.trade_managers = {}
        self.signal_secid_to_key = {}
        self.trade_secid_to_key = {}
        self.ws_lock = threading.Lock()
        self.client_id = ""
        self.access_token = ""

        # Load .env
        if ENV_FILE.exists():
            load_dotenv(str(ENV_FILE), override=True)

        # Build UI
        self._build_tabs()
        self._load_credentials()

    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(self, fg_color=CARD_BG, segmented_button_fg_color="#1e293b",
                                       segmented_button_selected_color=ACCENT)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_token = self.tabview.add("Token Manager")
        self.tab_config = self.tabview.add("Strategy Config")
        self.tab_dash = self.tabview.add("Live Dashboard")

        self._build_token_tab()
        self._build_config_tab()
        self._build_dash_tab()

    # ── Token Tab ──
    def _build_token_tab(self):
        f = self.tab_token
        ctk.CTkLabel(f, text="Dhan API Credentials", font=("Segoe UI",18,"bold"), text_color=ACCENT).pack(pady=15)

        self.ent_client = self._labeled_entry(f, "Client ID")
        self.ent_pin = self._labeled_entry(f, "PIN", show="*")
        self.ent_totp = self._labeled_entry(f, "TOTP Secret", show="*")
        self.ent_token = self._labeled_entry(f, "Access Token (auto)")

        bf = ctk.CTkFrame(f, fg_color="transparent")
        bf.pack(pady=15)
        ctk.CTkButton(bf, text="Save Credentials", command=self._save_credentials,
                       fg_color="#1e40af", hover_color="#2563eb", width=180).pack(side="left", padx=5)
        ctk.CTkButton(bf, text="Generate Token", command=self._generate_token,
                       fg_color="#065f46", hover_color="#059669", width=180).pack(side="left", padx=5)
        ctk.CTkButton(bf, text="Verify Token", command=self._verify_token,
                       fg_color="#713f12", hover_color="#a16207", width=180).pack(side="left", padx=5)

        self.lbl_token_status = ctk.CTkLabel(f, text="", font=("Segoe UI",12), text_color=DIM)
        self.lbl_token_status.pack(pady=5)

    def _labeled_entry(self, parent, label, show=""):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=40, pady=3)
        ctk.CTkLabel(f, text=label, width=140, anchor="e", text_color=DIM).pack(side="left", padx=5)
        ent = ctk.CTkEntry(f, width=400, show=show if show else None, fg_color="#1e293b", border_color="#374151")
        ent.pack(side="left", padx=5)
        return ent

    def _load_credentials(self):
        self.ent_client.insert(0, os.getenv("DHAN_CLIENT_ID",""))
        self.ent_pin.insert(0, os.getenv("DHAN_PIN",""))
        self.ent_totp.insert(0, os.getenv("DHAN_TOTP_SECRET",""))
        self.ent_token.insert(0, os.getenv("DHAN_ACCESS_TOKEN",""))

    def _save_credentials(self):
        if not ENV_FILE.exists(): ENV_FILE.write_text("")
        set_key(str(ENV_FILE),"DHAN_CLIENT_ID",self.ent_client.get().strip())
        set_key(str(ENV_FILE),"DHAN_PIN",self.ent_pin.get().strip())
        set_key(str(ENV_FILE),"DHAN_TOTP_SECRET",self.ent_totp.get().strip())
        if self.ent_token.get().strip():
            set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",self.ent_token.get().strip())
        self.lbl_token_status.configure(text="Credentials saved to .env", text_color=GREEN)

    def _generate_token(self):
        self.lbl_token_status.configure(text="Generating token...", text_color=YELLOW)
        self.update()
        cid=self.ent_client.get().strip(); pin=self.ent_pin.get().strip(); totp_s=self.ent_totp.get().strip()
        if not all([cid,pin,totp_s]):
            self.lbl_token_status.configure(text="Fill all fields first", text_color=RED); return
        def _gen():
            mgr=DhanTokenManager(cid,pin,totp_s,self.ent_token.get().strip())
            token=mgr.ensure_token()
            if token:
                self.ent_token.delete(0,"end"); self.ent_token.insert(0,token)
                set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",token)
                self.lbl_token_status.configure(text="Token generated and saved!", text_color=GREEN)
            else:
                self.lbl_token_status.configure(text="Token generation failed", text_color=RED)
        threading.Thread(target=_gen, daemon=True).start()

    def _verify_token(self):
        cid=self.ent_client.get().strip(); token=self.ent_token.get().strip()
        if not token:
            self.lbl_token_status.configure(text="No token to verify", text_color=RED); return
        mgr=DhanTokenManager(cid,"","",token)
        if mgr.verify(token):
            self.lbl_token_status.configure(text="Token is VALID", text_color=GREEN)
        else:
            self.lbl_token_status.configure(text="Token is INVALID or expired", text_color=RED)

    # ── Config Tab ──
    def _build_config_tab(self):
        f = self.tab_config
        ctk.CTkLabel(f, text="Strategy Configuration", font=("Segoe UI",18,"bold"), text_color=ACCENT).pack(pady=10)

        # Instrument selection
        sf = ctk.CTkFrame(f, fg_color=CARD_BG)
        sf.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(sf, text="Instrument:", text_color=DIM, width=120).pack(side="left", padx=10)
        self.cmb_instrument = ctk.CTkComboBox(sf, values=list(INSTRUMENTS.keys()), width=200,
                                               fg_color="#1e293b", command=self._on_instrument_change)
        self.cmb_instrument.pack(side="left", padx=5)
        self.cmb_instrument.set("CRUDEOILM")

        # Config grid
        gf = ctk.CTkFrame(f, fg_color=CARD_BG)
        gf.pack(fill="x", padx=20, pady=10)

        configs = [
            ("Brick Size", "brick_size", "5"),
            ("Reversal Bricks", "reversal", "2"),
            ("ITM Offset", "itm_offset", "100"),
            ("Lot Size", "lot_size", "10"),
            ("Lots", "lots", "1"),
            ("Target Points", "target_points", "10"),
            ("Daily Profit Target", "daily_profit_target", "500"),
        ]
        self.config_entries = {}
        for i, (label, key, default) in enumerate(configs):
            row, col = divmod(i, 3)
            cf = ctk.CTkFrame(gf, fg_color="transparent")
            cf.grid(row=row, column=col, padx=15, pady=5, sticky="w")
            ctk.CTkLabel(cf, text=label, text_color=DIM, font=("Segoe UI",11)).pack(anchor="w")
            ent = ctk.CTkEntry(cf, width=120, fg_color="#1e293b", border_color="#374151")
            ent.insert(0, default)
            ent.pack()
            self.config_entries[key] = ent

        # Mode
        mf = ctk.CTkFrame(f, fg_color=CARD_BG)
        mf.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(mf, text="Trade Mode:", text_color=DIM, width=120).pack(side="left", padx=10)
        self.cmb_mode = ctk.CTkComboBox(mf, values=["paper","live"], width=120, fg_color="#1e293b")
        self.cmb_mode.pack(side="left", padx=5)
        self.cmb_mode.set("paper")

        # Squareoff
        sqf = ctk.CTkFrame(f, fg_color=CARD_BG)
        sqf.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(sqf, text="Squareoff IST:", text_color=DIM, width=120).pack(side="left", padx=10)
        self.ent_sq_hour = ctk.CTkEntry(sqf, width=50, fg_color="#1e293b"); self.ent_sq_hour.insert(0,"23")
        self.ent_sq_hour.pack(side="left", padx=2)
        ctk.CTkLabel(sqf, text=":", text_color=DIM).pack(side="left")
        self.ent_sq_min = ctk.CTkEntry(sqf, width=50, fg_color="#1e293b"); self.ent_sq_min.insert(0,"15")
        self.ent_sq_min.pack(side="left", padx=2)

        # Start/Stop
        bf = ctk.CTkFrame(f, fg_color="transparent")
        bf.pack(pady=15)
        self.btn_start = ctk.CTkButton(bf, text="START", command=self._start_strategy,
                                        fg_color="#065f46", hover_color="#059669", width=200, height=45,
                                        font=("Segoe UI",16,"bold"))
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = ctk.CTkButton(bf, text="STOP", command=self._stop_strategy,
                                       fg_color="#7f1d1d", hover_color="#dc2626", width=200, height=45,
                                       font=("Segoe UI",16,"bold"), state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        self._on_instrument_change("CRUDEOILM")

    def _on_instrument_change(self, choice):
        inst = INSTRUMENTS.get(choice, {})
        mapping = {"brick_size":"brick_size","reversal":"reversal","itm_offset":"itm_offset",
                   "lot_size":"lot_size","lots":"lots","target_points":"target_points",
                   "daily_profit_target":"daily_profit_target"}
        for key, field in mapping.items():
            if key in self.config_entries:
                ent = self.config_entries[key]
                ent.delete(0,"end")
                ent.insert(0, str(inst.get(field, 0)))
        self.cmb_mode.set(inst.get("trade_mode","paper"))

    # ── Dashboard Tab ──
    def _build_dash_tab(self):
        f = self.tab_dash
        self.lbl_status = ctk.CTkLabel(f, text="NOT RUNNING", font=("Consolas",14,"bold"), text_color=RED)
        self.lbl_status.pack(pady=10)

        # Info cards
        cf = ctk.CTkFrame(f, fg_color=CARD_BG)
        cf.pack(fill="x", padx=20, pady=5)
        self.lbl_instrument = ctk.CTkLabel(cf, text="--", font=("Consolas",12), text_color=TEXT)
        self.lbl_instrument.pack(anchor="w", padx=15, pady=2)
        self.lbl_position = ctk.CTkLabel(cf, text="Position: FLAT", font=("Consolas",13,"bold"), text_color=DIM)
        self.lbl_position.pack(anchor="w", padx=15, pady=2)
        self.lbl_pnl = ctk.CTkLabel(cf, text="PnL: 0.00", font=("Consolas",14,"bold"), text_color=TEXT)
        self.lbl_pnl.pack(anchor="w", padx=15, pady=2)
        self.lbl_brick = ctk.CTkLabel(cf, text="Last Brick: --", font=("Consolas",11), text_color=DIM)
        self.lbl_brick.pack(anchor="w", padx=15, pady=2)

        # Trade log
        ctk.CTkLabel(f, text="Trade Log", font=("Segoe UI",13,"bold"), text_color=ACCENT).pack(pady=(10,2))
        self.txt_log = ctk.CTkTextbox(f, height=300, fg_color="#0f172a", text_color=TEXT,
                                       font=("Consolas",10), state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=20, pady=5)

    def _log_to_dash(self, msg):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"{now_ist().strftime('%H:%M:%S')} | {msg}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ── Engine Control ──
    def _apply_config(self):
        key = self.cmb_instrument.get()
        inst = INSTRUMENTS[key]
        for field, ent in self.config_entries.items():
            try:
                val = ent.get().strip()
                if "." in val: inst[field] = float(val)
                else: inst[field] = int(val)
            except: pass
        inst["trade_mode"] = self.cmb_mode.get()
        return key

    def _start_strategy(self):
        if self.running: return
        self.client_id = self.ent_client.get().strip()
        self.access_token = self.ent_token.get().strip()
        if not self.client_id or not self.access_token:
            self._log_to_dash("ERROR: Generate token first!"); return

        active_key = self._apply_config()
        api.set_auth(self.access_token, self.client_id)

        self.running = True
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="RUNNING", text_color=GREEN)
        self.tabview.set("Live Dashboard")

        sq_h = int(self.ent_sq_hour.get() or 23)
        sq_m = int(self.ent_sq_min.get() or 15)

        threading.Thread(target=self._run_engine, args=(active_key, sq_h, sq_m), daemon=True).start()
        # Dashboard refresh loop
        self._refresh_dash()

    def _stop_strategy(self):
        if not self.running: return
        self.stop_event.set()
        for tm in self.trade_managers.values(): tm.squareoff()
        if self.ws:
            try: self.ws.close()
            except: pass
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="STOPPED", text_color=RED)
        self._log_to_dash("Strategy stopped.")

    def _gui_callback(self, event, key, data):
        """Called from engine threads. Schedule UI update on main thread."""
        try:
            if event == "signal":
                d = "BUY" if data["direction"] == 1 else "SELL"
                self.after(0, lambda: self._log_to_dash(f"SIGNAL {key} | {d} | Price={data['brick_close']:.2f}"))
            elif event == "entry":
                ot = data["type"]; s = int(data["strike"]) if data["strike"] else "FUT"
                d = "LONG" if data["direction"] == 1 else "SHORT"
                self.after(0, lambda: self._log_to_dash(f"ENTRY | {d} {ot} {s} @ {data['price']:.2f} | Qty={data['qty']}"))
            elif event == "exit":
                self.after(0, lambda: self._log_to_dash(f"EXIT | {data['reason']} | PnL={data['pnl']:+.2f} | Total={data['total']:+.2f}"))
            elif event == "target_hit":
                self.after(0, lambda: self._log_to_dash(f"TARGET HIT | LTP={data['ltp']:.2f}"))
            elif event == "daily_target":
                self.after(0, lambda: self._log_to_dash(f"DAILY TARGET REACHED | PnL={data['pnl']:+.2f}"))
        except: pass

    def _run_engine(self, active_key, sq_h, sq_m):
        try:
            inst = INSTRUMENTS[active_key]
            self.after(0, lambda: self._log_to_dash(f"Starting {inst['label']}..."))

            # Resolve security IDs
            resolve_security_ids([active_key])
            sig_sid, sig_seg, sig_inst = get_signal_config(active_key)
            if not sig_sid:
                self.after(0, lambda: self._log_to_dash(f"ERROR: No signal security_id for {active_key}"))
                self._stop_strategy(); return

            self.signal_secid_to_key = {sig_sid: active_key}

            # Trade manager
            tm = TradeManager(active_key, self.client_id,
                              ws_sub_cb=self._ws_sub, ws_unsub_cb=self._ws_unsub,
                              gui_cb=self._gui_callback)
            self.trade_managers = {active_key: tm}

            # Renko engine
            engine = RenkoEngine(inst["brick_size"], inst["reversal"], on_brick_callback=tm.on_brick)
            engine.callback_key = active_key
            candles = fetch_historical(sig_sid, sig_seg, sig_inst, 5)
            if candles:
                engine.build_from_candles(candles)
                self.after(0, lambda: self._log_to_dash(f"Seeded {len(engine.bricks)} bricks from {len(candles)} candles"))
            self.engines = {active_key: engine}

            # WS
            ws_url = f"wss://api-feed.dhan.co?version=2&token={self.access_token}&clientId={self.client_id}&authType=2"
            self.after(0, lambda: self._log_to_dash("Connecting WebSocket..."))

            backoff = 0
            while not self.stop_event.is_set():
                try:
                    def _on_open(ws):
                        self.ws_connected.set(); backoff = 0
                        insts = [{"ExchangeSegment": sig_seg, "SecurityId": sig_sid}]
                        ws.send(json.dumps({"RequestCode": REQ_SUB_TICKER, "InstrumentCount": len(insts), "InstrumentList": insts}))
                        self.after(0, lambda: self._log_to_dash(f"WS connected | {sig_seg}:{sig_sid}"))
                    def _on_msg(ws, message):
                        if isinstance(message, str): return
                        hdr = parse_header_8(bytes(message))
                        if not hdr or int(hdr["resp_code"]) != RESP_TICKER: return
                        t = parse_ticker(hdr["payload"])
                        if not t: return
                        sid = str(hdr["security_id"]); ltp = float(t["ltp"])
                        ltt = _norm_epoch(int(t["ltt_epoch"]))
                        ts = datetime.fromtimestamp(ltt, tz=IST)
                        key = self.signal_secid_to_key.get(sid)
                        if key and key in self.engines:
                            self.engines[key].process_price(ltp, ts)
                            if key in self.trade_managers:
                                self.trade_managers[key].update_signal_ltp(ltp)
                                self.trade_managers[key].check_target(ltp)
                        tk = self.trade_secid_to_key.get(sid)
                        if tk and tk in self.trade_managers:
                            self.trade_managers[tk].update_ltp(sid, ltp)
                    def _on_err(ws, error):
                        self.after(0, lambda: self._log_to_dash(f"WS error: {error}"))
                    def _on_close(ws, sc, msg):
                        self.ws_connected.clear()
                    self.ws = websocket.WebSocketApp(ws_url, on_open=_on_open, on_message=_on_msg, on_error=_on_err, on_close=_on_close)
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    self.after(0, lambda: self._log_to_dash(f"WS exception: {e}"))
                finally:
                    if not self.stop_event.is_set():
                        delay = min(2*(2**backoff), 30); backoff += 1
                        time.sleep(delay)

            # Squareoff check thread runs inside _run_engine
        except Exception as e:
            self.after(0, lambda: self._log_to_dash(f"Engine error: {e}"))
            self.after(0, self._stop_strategy)

    def _ws_sub(self, sid, exch, key):
        if sid in self.signal_secid_to_key:
            self.trade_secid_to_key[sid] = key; return
        self.trade_secid_to_key[sid] = key
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode":REQ_SUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":exch,"SecurityId":str(sid)}]}))
            except: pass
    def _ws_unsub(self, sid, exch):
        if sid in self.signal_secid_to_key: return
        self.trade_secid_to_key.pop(sid, None)
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode":REQ_UNSUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":exch,"SecurityId":str(sid)}]}))
            except: pass

    def _refresh_dash(self):
        """Refresh dashboard labels every 500ms."""
        if not self.running: return
        try:
            for key, tm in self.trade_managers.items():
                inst = INSTRUMENTS[key]
                eng = self.engines.get(key)
                bricks = eng.bricks if eng else []

                self.lbl_instrument.configure(text=f"{inst['label']} | Mode={inst['trade_mode'].upper()} | Bricks={len(bricks)} | Size={inst['brick_size']}")

                if bricks:
                    lb = bricks[-1]
                    bc = "GREEN" if lb.is_green else "RED"
                    self.lbl_brick.configure(text=f"Last Brick: {bc} O={lb.open:.2f} C={lb.close:.2f} @ {lb.time.strftime('%H:%M:%S')}",
                                             text_color=GREEN if lb.is_green else RED)

                t = tm.current_trade
                ur = tm.get_unrealized_pnl()
                if tm.daily_target_reached:
                    self.lbl_position.configure(text="DAILY TARGET REACHED -- stopped", text_color=YELLOW)
                elif tm.squaredoff:
                    self.lbl_position.configure(text="SQUARED OFF", text_color=DIM)
                elif tm.waiting_for_reversal:
                    self.lbl_position.configure(text="FLAT (target hit) -- waiting reversal", text_color=YELLOW)
                elif t and t.is_open:
                    d = "LONG" if t.direction == 1 else "SHORT"
                    ot = f"{t.option_type}{int(t.strike)}" if t.strike else t.option_type
                    ltp = f"{t.current_ltp:.2f}" if t.current_ltp > 0 else "..."
                    tgt = f" | Tgt={t.target_price:.2f}" if t.target_price > 0 else ""
                    self.lbl_position.configure(text=f"{d} {ot} @ {t.entry_price:.2f} | LTP={ltp}{tgt} | Unreal={ur:+.2f}",
                                                text_color=GREEN if t.direction == 1 else RED)
                else:
                    self.lbl_position.configure(text="Position: FLAT", text_color=DIM)

                total = tm.total_pnl + ur
                self.lbl_pnl.configure(text=f"Realized: {tm.total_pnl:+.2f}  |  Unrealized: {ur:+.2f}  |  Net: {total:+.2f}  |  Trades: {tm.trade_count}",
                                       text_color=GREEN if total >= 0 else RED)
        except: pass
        self.after(500, self._refresh_dash)

    def _on_close(self):
        if self.running: self._stop_strategy()
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
