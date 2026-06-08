"""
app.py -- Balfund Renko Trading System v2.7
CustomTkinter GUI + Embedded Renko Chart + ITM/OTM selector
"""
import sys, os, threading, time, json, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import customtkinter as ctk

if getattr(sys, 'frozen', False): BASE_DIR = Path(sys.executable).parent
else: BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from engine import (
    INSTRUMENTS, DhanTokenManager, DhanAPI, api, resolve_security_ids,
    fetch_historical, get_signal_config, RenkoEngine, TradeManager,
    parse_header_8, parse_ticker, _norm_epoch, now_ist, ENV_FILE,
    IST, REQ_SUB_TICKER, REQ_UNSUB_TICKER, RESP_TICKER
)
from dotenv import load_dotenv, set_key
import websocket

# Matplotlib embedded (import after ctk to avoid backend conflicts)
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.patches as mpatches

LOG_DIR = BASE_DIR / "logs"; LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.FileHandler(str(LOG_DIR/f"renko_{datetime.now().strftime('%Y%m%d')}.log"),encoding='utf-8'),logging.StreamHandler()])
log = logging.getLogger("RENKO")
ctk.set_appearance_mode("dark"); ctk.set_default_color_theme("blue")

BG="#0a0e1a"; CARD="#111827"; ACC="#06b6d4"; GRN="#00e676"; RED="#ff1744"; YEL="#ffd600"; TXT="#e0e0e0"; DIM="#6b7280"
MAX_CHART_BRICKS = 120

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Balfund Renko Trading System v2.7"); self.geometry("1100x780"); self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.running=False; self.stop_event=threading.Event(); self.ws=None; self.ws_connected=threading.Event()
        self.engines={}; self.trade_managers={}; self.signal_secid_to_key={}; self.trade_secid_to_key={}
        self.ws_lock=threading.Lock(); self.client_id=""; self.access_token=""
        self._chart_brick_count=0  # track for efficient redraw
        if ENV_FILE.exists(): load_dotenv(str(ENV_FILE), override=True)
        self._build_tabs(); self._load_creds()

    def _build_tabs(self):
        self.tabview=ctk.CTkTabview(self,fg_color=CARD,segmented_button_fg_color="#1e293b",segmented_button_selected_color=ACC)
        self.tabview.pack(fill="both",expand=True,padx=8,pady=8)
        self.tab_token=self.tabview.add("Token"); self.tab_config=self.tabview.add("Config")
        self.tab_chart=self.tabview.add("Renko Chart"); self.tab_dash=self.tabview.add("Dashboard")
        self._build_token(); self._build_config(); self._build_chart(); self._build_dash()

    # === TOKEN TAB ===
    def _build_token(self):
        f=self.tab_token
        ctk.CTkLabel(f,text="Dhan API Credentials",font=("Segoe UI",18,"bold"),text_color=ACC).pack(pady=12)
        self.ent_client=self._entry(f,"Client ID"); self.ent_pin=self._entry(f,"PIN","*")
        self.ent_totp=self._entry(f,"TOTP Secret","*"); self.ent_token=self._entry(f,"Access Token")
        bf=ctk.CTkFrame(f,fg_color="transparent"); bf.pack(pady=12)
        ctk.CTkButton(bf,text="Save",command=self._save_creds,fg_color="#1e40af",width=150).pack(side="left",padx=4)
        ctk.CTkButton(bf,text="Generate Token",command=self._gen_token,fg_color="#065f46",width=150).pack(side="left",padx=4)
        ctk.CTkButton(bf,text="Verify",command=self._verify_token,fg_color="#713f12",width=150).pack(side="left",padx=4)
        self.lbl_tok=ctk.CTkLabel(f,text="",font=("Segoe UI",11),text_color=DIM); self.lbl_tok.pack(pady=4)

    def _entry(self,p,label,show=""):
        f=ctk.CTkFrame(p,fg_color="transparent"); f.pack(fill="x",padx=40,pady=2)
        ctk.CTkLabel(f,text=label,width=130,anchor="e",text_color=DIM).pack(side="left",padx=4)
        e=ctk.CTkEntry(f,width=380,show=show or None,fg_color="#1e293b",border_color="#374151"); e.pack(side="left",padx=4)
        return e

    def _load_creds(self):
        self.ent_client.insert(0,os.getenv("DHAN_CLIENT_ID","")); self.ent_pin.insert(0,os.getenv("DHAN_PIN",""))
        self.ent_totp.insert(0,os.getenv("DHAN_TOTP_SECRET","")); self.ent_token.insert(0,os.getenv("DHAN_ACCESS_TOKEN",""))

    def _save_creds(self):
        if not ENV_FILE.exists(): ENV_FILE.write_text("")
        for k,e in [("DHAN_CLIENT_ID",self.ent_client),("DHAN_PIN",self.ent_pin),("DHAN_TOTP_SECRET",self.ent_totp)]:
            set_key(str(ENV_FILE),k,e.get().strip())
        if self.ent_token.get().strip(): set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",self.ent_token.get().strip())
        self.lbl_tok.configure(text="Saved!",text_color=GRN)

    def _gen_token(self):
        self.lbl_tok.configure(text="Generating...",text_color=YEL); self.update()
        cid=self.ent_client.get().strip();pin=self.ent_pin.get().strip();ts=self.ent_totp.get().strip()
        if not all([cid,pin,ts]): self.lbl_tok.configure(text="Fill all fields",text_color=RED); return
        def _g():
            t=DhanTokenManager(cid,pin,ts,self.ent_token.get().strip()).ensure_token()
            if t:
                self.ent_token.delete(0,"end"); self.ent_token.insert(0,t)
                set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",t)
                self.lbl_tok.configure(text="Token generated!",text_color=GRN)
            else: self.lbl_tok.configure(text="Failed",text_color=RED)
        threading.Thread(target=_g,daemon=True).start()

    def _verify_token(self):
        t=self.ent_token.get().strip()
        if not t: self.lbl_tok.configure(text="No token",text_color=RED); return
        ok=DhanTokenManager(self.ent_client.get().strip(),"","",t).verify(t)
        self.lbl_tok.configure(text="VALID" if ok else "INVALID",text_color=GRN if ok else RED)

    # === CONFIG TAB ===
    def _build_config(self):
        f=self.tab_config
        ctk.CTkLabel(f,text="Strategy Configuration",font=("Segoe UI",18,"bold"),text_color=ACC).pack(pady=8)
        sf=ctk.CTkFrame(f,fg_color=CARD); sf.pack(fill="x",padx=15,pady=4)
        ctk.CTkLabel(sf,text="Instrument:",text_color=DIM,width=100).pack(side="left",padx=8)
        self.cmb_inst=ctk.CTkComboBox(sf,values=list(INSTRUMENTS.keys()),width=180,fg_color="#1e293b",command=self._on_inst)
        self.cmb_inst.pack(side="left",padx=4); self.cmb_inst.set("CRUDEOILM")

        gf=ctk.CTkFrame(f,fg_color=CARD); gf.pack(fill="x",padx=15,pady=8)
        cfgs=[("Brick Size","brick_size","5"),("Reversal","reversal","2"),("Offset","itm_offset","100"),
              ("Lot Size","lot_size","10"),("Lots","lots","1"),("Target Pts","target_points","10"),("Day Target","daily_profit_target","500")]
        self.cfg_ents={}
        for i,(label,key,default) in enumerate(cfgs):
            r,c=divmod(i,4)
            cf=ctk.CTkFrame(gf,fg_color="transparent"); cf.grid(row=r,column=c,padx=10,pady=4,sticky="w")
            ctk.CTkLabel(cf,text=label,text_color=DIM,font=("Segoe UI",10)).pack(anchor="w")
            e=ctk.CTkEntry(cf,width=100,fg_color="#1e293b",border_color="#374151"); e.insert(0,default); e.pack()
            self.cfg_ents[key]=e

        # Mode + Strike Mode row
        mf=ctk.CTkFrame(f,fg_color=CARD); mf.pack(fill="x",padx=15,pady=4)
        ctk.CTkLabel(mf,text="Trade Mode:",text_color=DIM,width=100).pack(side="left",padx=8)
        self.cmb_mode=ctk.CTkComboBox(mf,values=["paper","live"],width=100,fg_color="#1e293b"); self.cmb_mode.pack(side="left",padx=4); self.cmb_mode.set("paper")
        ctk.CTkLabel(mf,text="Strike:",text_color=DIM,width=60).pack(side="left",padx=(20,4))
        self.cmb_strike=ctk.CTkComboBox(mf,values=["ITM","OTM"],width=80,fg_color="#1e293b"); self.cmb_strike.pack(side="left",padx=4); self.cmb_strike.set("ITM")

        sqf=ctk.CTkFrame(f,fg_color=CARD); sqf.pack(fill="x",padx=15,pady=4)
        ctk.CTkLabel(sqf,text="Squareoff IST:",text_color=DIM,width=100).pack(side="left",padx=8)
        self.ent_sqh=ctk.CTkEntry(sqf,width=45,fg_color="#1e293b"); self.ent_sqh.insert(0,"23"); self.ent_sqh.pack(side="left",padx=2)
        ctk.CTkLabel(sqf,text=":",text_color=DIM).pack(side="left")
        self.ent_sqm=ctk.CTkEntry(sqf,width=45,fg_color="#1e293b"); self.ent_sqm.insert(0,"15"); self.ent_sqm.pack(side="left",padx=2)

        bf=ctk.CTkFrame(f,fg_color="transparent"); bf.pack(pady=12)
        self.btn_start=ctk.CTkButton(bf,text="START",command=self._start,fg_color="#065f46",hover_color="#059669",width=180,height=42,font=("Segoe UI",15,"bold"))
        self.btn_start.pack(side="left",padx=8)
        self.btn_stop=ctk.CTkButton(bf,text="STOP",command=self._stop,fg_color="#7f1d1d",hover_color="#dc2626",width=180,height=42,font=("Segoe UI",15,"bold"),state="disabled")
        self.btn_stop.pack(side="left",padx=8)
        self._on_inst("CRUDEOILM")

    def _on_inst(self,choice):
        inst=INSTRUMENTS.get(choice,{})
        for k,e in self.cfg_ents.items():
            e.delete(0,"end"); e.insert(0,str(inst.get(k,0)))
        self.cmb_mode.set(inst.get("trade_mode","paper"))
        self.cmb_strike.set(inst.get("strike_mode","ITM"))

    # === RENKO CHART TAB (embedded matplotlib) ===
    def _build_chart(self):
        f=self.tab_chart
        self.fig,self.ax=plt.subplots(1,1,figsize=(12,5))
        self.fig.patch.set_facecolor("#1a1a2e"); self.ax.set_facecolor("#16213e")
        self.canvas=FigureCanvasTkAgg(self.fig,master=f)
        self.canvas.get_tk_widget().pack(fill="both",expand=True,padx=5,pady=5)

    def _update_chart(self):
        """Called every 500ms. Only redraws if brick count changed."""
        if not self.running: return
        try:
            for key,eng in self.engines.items():
                bc=len(eng.bricks)
                if bc==self._chart_brick_count and bc>0:
                    self.after(500,self._update_chart); return
                self._chart_brick_count=bc
                bricks=eng.get_last_n(MAX_CHART_BRICKS)
                if not bricks: self.after(500,self._update_chart); return
                inst=INSTRUMENTS[key]; bs=inst["brick_size"]
                ax=self.ax; ax.clear(); ax.set_facecolor("#16213e")
                for i,b in enumerate(bricks):
                    c="#00e676" if b.is_green else "#ff1744"
                    ax.add_patch(mpatches.FancyBboxPatch((i-0.4,b.low),0.8,b.high-b.low,boxstyle="round,pad=0.02",linewidth=0.6,edgecolor=c,facecolor=c,alpha=0.85))
                ax.set_xlim(-1,len(bricks)+1)
                ax.set_ylim(min(b.low for b in bricks)-bs*2,max(b.high for b in bricks)+bs*2)
                iv=max(1,len(bricks)//12)
                tk=list(range(0,len(bricks),iv))
                ax.set_xticks(tk); ax.set_xticklabels([bricks[i].time.strftime("%d-%b\n%H:%M") for i in tk],fontsize=7,color="#888")
                ax.tick_params(colors="#888",labelsize=8)
                for s in ["top","right"]: ax.spines[s].set_visible(False)
                for s in ["bottom","left"]: ax.spines[s].set_color("#333")
                ax.grid(True,alpha=0.12,color="#555")
                gc=sum(1 for b in bricks if b.is_green);rc=len(bricks)-gc
                ax.set_title(f"{inst['label']} | {bricks[-1].close:.2f} @ {bricks[-1].time.strftime('%H:%M:%S')} | UP:{gc} DN:{rc} | Size={bs}",fontsize=11,fontweight="bold",color="white",pad=8)
                # Trade status on chart
                tm=self.trade_managers.get(key)
                if tm:
                    t=tm.current_trade
                    if t and t.is_open:
                        d="LONG" if t.direction==1 else "SHORT"
                        tgt=f" Tgt={t.target_price:.2f}" if t.target_price>0 else ""
                        ax.set_xlabel(f"[TRADE] {d} {t.option_type}{int(t.strike) if t.strike else ''} @ {t.entry_price:.2f}{tgt} | PnL={tm.total_pnl:+.2f}",fontsize=9,color="#FFD700",labelpad=6)
                    elif tm.daily_target_reached:
                        ax.set_xlabel(f"[DAILY TARGET REACHED] PnL={tm.total_pnl:+.2f}",fontsize=9,color=GRN,labelpad=6)
                self.canvas.draw_idle()
        except Exception as e: log.error(f"Chart err: {e}")
        self.after(500,self._update_chart)

    # === DASHBOARD TAB ===
    def _build_dash(self):
        f=self.tab_dash
        self.lbl_status=ctk.CTkLabel(f,text="NOT RUNNING",font=("Consolas",14,"bold"),text_color=RED); self.lbl_status.pack(pady=8)
        cf=ctk.CTkFrame(f,fg_color=CARD); cf.pack(fill="x",padx=15,pady=4)
        self.lbl_inst=ctk.CTkLabel(cf,text="--",font=("Consolas",11),text_color=TXT); self.lbl_inst.pack(anchor="w",padx=12,pady=1)
        self.lbl_pos=ctk.CTkLabel(cf,text="FLAT",font=("Consolas",13,"bold"),text_color=DIM); self.lbl_pos.pack(anchor="w",padx=12,pady=1)
        self.lbl_pnl=ctk.CTkLabel(cf,text="PnL: 0.00",font=("Consolas",13,"bold"),text_color=TXT); self.lbl_pnl.pack(anchor="w",padx=12,pady=1)
        self.lbl_brick=ctk.CTkLabel(cf,text="--",font=("Consolas",10),text_color=DIM); self.lbl_brick.pack(anchor="w",padx=12,pady=1)
        ctk.CTkLabel(f,text="Trade Log",font=("Segoe UI",12,"bold"),text_color=ACC).pack(pady=(8,2))
        self.txt_log=ctk.CTkTextbox(f,height=280,fg_color="#0f172a",text_color=TXT,font=("Consolas",10),state="disabled")
        self.txt_log.pack(fill="both",expand=True,padx=15,pady=4)

    def _dash_log(self,msg):
        self.txt_log.configure(state="normal"); self.txt_log.insert("end",f"{now_ist().strftime('%H:%M:%S')} | {msg}\n")
        self.txt_log.see("end"); self.txt_log.configure(state="disabled")

    def _refresh_dash(self):
        if not self.running: return
        try:
            for key,tm in self.trade_managers.items():
                inst=INSTRUMENTS[key]; eng=self.engines.get(key); bricks=eng.bricks if eng else []
                sm=inst.get("strike_mode","ITM")
                self.lbl_inst.configure(text=f"{inst['label']} | {inst['trade_mode'].upper()} | {sm} | Bricks={len(bricks)} | Size={inst['brick_size']}")
                if bricks:
                    lb=bricks[-1]; bc="GREEN" if lb.is_green else "RED"
                    self.lbl_brick.configure(text=f"Last: {bc} O={lb.open:.2f} C={lb.close:.2f} @ {lb.time.strftime('%H:%M:%S')}",text_color=GRN if lb.is_green else RED)
                t=tm.current_trade; ur=tm.get_unrealized_pnl()
                if tm.daily_target_reached: self.lbl_pos.configure(text="DAILY TARGET REACHED",text_color=YEL)
                elif tm.squaredoff: self.lbl_pos.configure(text="SQUARED OFF",text_color=DIM)
                elif tm.waiting_for_reversal: self.lbl_pos.configure(text="FLAT (target hit) -- waiting reversal",text_color=YEL)
                elif t and t.is_open:
                    d="LONG" if t.direction==1 else "SHORT"
                    ot=f"{t.option_type}{int(t.strike)}" if t.strike else t.option_type
                    ltp=f"{t.current_ltp:.2f}" if t.current_ltp>0 else "..."
                    tgt=f" | Tgt={t.target_price:.2f}" if t.target_price>0 else ""
                    self.lbl_pos.configure(text=f"{d} {ot} @ {t.entry_price:.2f} | LTP={ltp}{tgt} | Unreal={ur:+.2f}",text_color=GRN if t.direction==1 else RED)
                else: self.lbl_pos.configure(text="FLAT",text_color=DIM)
                total=tm.total_pnl+ur
                self.lbl_pnl.configure(text=f"Real={tm.total_pnl:+.2f} | Unreal={ur:+.2f} | Net={total:+.2f} | Trades={tm.trade_count}",text_color=GRN if total>=0 else RED)
        except: pass
        self.after(500,self._refresh_dash)

    # === ENGINE ===
    def _apply_cfg(self):
        key=self.cmb_inst.get(); inst=INSTRUMENTS[key]
        for k,e in self.cfg_ents.items():
            try:
                v=e.get().strip()
                inst[k]=float(v) if "." in v else int(v)
            except: pass
        inst["trade_mode"]=self.cmb_mode.get()
        inst["strike_mode"]=self.cmb_strike.get()
        return key

    def _start(self):
        if self.running: return
        self.client_id=self.ent_client.get().strip(); self.access_token=self.ent_token.get().strip()
        if not self.client_id or not self.access_token: self._dash_log("ERROR: Generate token first!"); return
        key=self._apply_cfg(); api.set_auth(self.access_token,self.client_id)
        self.running=True; self.stop_event.clear(); self._chart_brick_count=0
        self.btn_start.configure(state="disabled"); self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="RUNNING",text_color=GRN)
        self.tabview.set("Renko Chart")
        threading.Thread(target=self._run_engine,args=(key,),daemon=True).start()
        self._refresh_dash(); self._update_chart()

    def _stop(self):
        if not self.running: return
        self.stop_event.set()
        for tm in self.trade_managers.values(): tm.squareoff()
        if self.ws:
            try: self.ws.close()
            except: pass
        self.running=False; self.btn_start.configure(state="normal"); self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="STOPPED",text_color=RED); self._dash_log("Stopped.")

    def _gui_cb(self,event,key,data):
        try:
            if event=="signal":
                d="BUY" if data["direction"]==1 else "SELL"
                self.after(0,lambda:self._dash_log(f"SIGNAL | {d} | {data['brick_close']:.2f}"))
            elif event=="entry":
                d="LONG" if data["direction"]==1 else "SHORT"; s=int(data["strike"]) if data["strike"] else "FUT"
                sm=data.get("mode","ITM")
                self.after(0,lambda:self._dash_log(f"ENTRY | {d} {data['type']}{s} ({sm}) @ {data['price']:.2f} | Qty={data['qty']}"))
            elif event=="exit":
                self.after(0,lambda:self._dash_log(f"EXIT | {data['reason']} | PnL={data['pnl']:+.2f} | Total={data['total']:+.2f}"))
            elif event=="target_hit":
                self.after(0,lambda:self._dash_log(f"TARGET HIT | LTP={data['ltp']:.2f}"))
            elif event=="daily_target":
                self.after(0,lambda:self._dash_log(f"DAILY TARGET REACHED | PnL={data['pnl']:+.2f}"))
        except: pass

    def _run_engine(self,active_key):
        try:
            inst=INSTRUMENTS[active_key]
            self.after(0,lambda:self._dash_log(f"Starting {inst['label']} ({inst.get('strike_mode','ITM')})..."))
            resolve_security_ids([active_key])
            sig_sid,sig_seg,sig_inst=get_signal_config(active_key)
            if not sig_sid: self.after(0,lambda:self._dash_log("ERROR: No signal ID")); self.after(0,self._stop); return
            self.signal_secid_to_key={sig_sid:active_key}; self.trade_secid_to_key={}
            tm=TradeManager(active_key,self.client_id,ws_sub_cb=self._ws_sub,ws_unsub_cb=self._ws_unsub,gui_cb=self._gui_cb)
            self.trade_managers={active_key:tm}
            engine=RenkoEngine(inst["brick_size"],inst["reversal"],on_brick_callback=tm.on_brick)
            engine.callback_key=active_key
            candles=fetch_historical(sig_sid,sig_seg,sig_inst,5)
            if candles:
                engine.build_from_candles(candles)
                self.after(0,lambda:self._dash_log(f"Seeded {len(engine.bricks)} bricks | Signal={sig_seg}:{sig_sid}"))
            self.engines={active_key:engine}

            ws_url=f"wss://api-feed.dhan.co?version=2&token={self.access_token}&clientId={self.client_id}&authType=2"
            self.after(0,lambda:self._dash_log("Connecting WS..."))
            backoff=0
            while not self.stop_event.is_set():
                try:
                    def _on_open(ws):
                        nonlocal backoff; self.ws_connected.set(); backoff=0
                        insts=[{"ExchangeSegment":sig_seg,"SecurityId":sig_sid}]
                        ws.send(json.dumps({"RequestCode":REQ_SUB_TICKER,"InstrumentCount":len(insts),"InstrumentList":insts}))
                        self.after(0,lambda:self._dash_log(f"WS connected | {sig_seg}:{sig_sid}"))
                    def _on_msg(ws,message):
                        if isinstance(message,str): return
                        hdr=parse_header_8(bytes(message))
                        if not hdr or int(hdr["resp_code"])!=RESP_TICKER: return
                        t=parse_ticker(hdr["payload"])
                        if not t: return
                        sid=str(hdr["security_id"]);ltp=float(t["ltp"]);ltt=_norm_epoch(int(t["ltt_epoch"]))
                        ts=datetime.fromtimestamp(ltt,tz=IST)
                        key=self.signal_secid_to_key.get(sid)
                        if key and key in self.engines:
                            self.engines[key].process_price(ltp,ts)
                            if key in self.trade_managers:
                                self.trade_managers[key].update_signal_ltp(ltp)
                                self.trade_managers[key].check_target(ltp)
                        tk=self.trade_secid_to_key.get(sid)
                        if tk and tk in self.trade_managers: self.trade_managers[tk].update_ltp(sid,ltp)
                    def _on_err(ws,error): self.after(0,lambda:self._dash_log(f"WS error: {error}"))
                    def _on_close(ws,sc,msg): self.ws_connected.clear()
                    self.ws=websocket.WebSocketApp(ws_url,on_open=_on_open,on_message=_on_msg,on_error=_on_err,on_close=_on_close)
                    self.ws.run_forever(ping_interval=20,ping_timeout=10)
                except Exception as e: self.after(0,lambda:self._dash_log(f"WS exception: {e}"))
                finally:
                    if not self.stop_event.is_set():
                        delay=min(2*(2**backoff),30);backoff+=1; time.sleep(delay)
        except Exception as e:
            self.after(0,lambda:self._dash_log(f"Engine error: {e}")); self.after(0,self._stop)

    def _ws_sub(self,sid,exch,key):
        if sid in self.signal_secid_to_key: self.trade_secid_to_key[sid]=key; return
        self.trade_secid_to_key[sid]=key
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock: self.ws.send(json.dumps({"RequestCode":REQ_SUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":exch,"SecurityId":str(sid)}]}))
            except: pass
    def _ws_unsub(self,sid,exch):
        if sid in self.signal_secid_to_key: return
        self.trade_secid_to_key.pop(sid,None)
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock: self.ws.send(json.dumps({"RequestCode":REQ_UNSUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":exch,"SecurityId":str(sid)}]}))
            except: pass

    def _on_close(self):
        if self.running: self._stop()
        self.destroy()

if __name__=="__main__": App().mainloop()
