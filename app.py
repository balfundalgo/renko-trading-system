"""
app.py -- Balfund Renko Trading System v3.0
Multi-instrument dynamic start/stop + ATM/ITM/OTM + Embedded Chart + Position Adoption
"""
import sys,os,threading,time,json,logging,re
from datetime import datetime,timedelta,timezone
from pathlib import Path
import customtkinter as ctk
if getattr(sys,'frozen',False): BASE_DIR=Path(sys.executable).parent
else: BASE_DIR=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE_DIR))

from engine import (INSTRUMENTS,DhanTokenManager,DhanAPI,api,resolve_security_ids,
    fetch_historical,get_signal_config,RenkoEngine,TradeManager,get_broker_positions,
    parse_header_8,parse_ticker,_norm_epoch,now_ist,ENV_FILE,IST,
    REQ_SUB_TICKER,REQ_UNSUB_TICKER,RESP_TICKER)
from dotenv import load_dotenv,set_key
import websocket
import matplotlib;matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.patches as mp

LOG_DIR=BASE_DIR/"logs";LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,format="%(asctime)s|%(levelname)-7s|%(message)s",datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(str(LOG_DIR/f"renko_{datetime.now().strftime('%Y%m%d')}.log"),encoding='utf-8'),logging.StreamHandler()])
log=logging.getLogger("RENKO")
ctk.set_appearance_mode("dark");ctk.set_default_color_theme("blue")

BG="#0a0e1a";CARD="#111827";ACC="#06b6d4";GRN="#00e676";RED="#ff1744";YEL="#ffd600";TXT="#e0e0e0";DIM="#6b7280"
FONT_L=("Segoe UI",15,"bold");FONT_M=("Segoe UI",13);FONT_S=("Segoe UI",12);FONT_XS=("Consolas",11)
FONT_TITLE=("Segoe UI",20,"bold");FONT_HEAD=("Segoe UI",16,"bold")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Balfund Renko Trading System v3.0");self.geometry("1200x850");self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW",self._on_close)
        # Shared state
        self.ws=None;self.ws_connected=threading.Event();self.ws_lock=threading.Lock()
        self.ws_thread=None;self.ws_stop=threading.Event()
        self.signal_secid_to_key={};self.trade_secid_to_key={}
        self.engines={};self.trade_managers={};self.running_keys=set()
        self.client_id="";self.access_token=""
        self._chart_counts={}
        self.sq_hour=23;self.sq_min=15;self._sq_done=False
        if ENV_FILE.exists(): load_dotenv(str(ENV_FILE),override=True)
        self._build_ui();self._load_creds()
        self._start_timers()

    def _build_ui(self):
        self.tv=ctk.CTkTabview(self,fg_color=CARD,segmented_button_fg_color="#1e293b",segmented_button_selected_color=ACC)
        self.tv.pack(fill="both",expand=True,padx=8,pady=8)
        self.t_tok=self.tv.add("Token");self.t_ctrl=self.tv.add("Instruments")
        self.t_chart=self.tv.add("Renko Chart");self.t_dash=self.tv.add("Dashboard")
        self._build_token();self._build_control();self._build_chart();self._build_dash()

    # ==================== TOKEN TAB ====================
    def _build_token(self):
        f=self.t_tok
        ctk.CTkLabel(f,text="Dhan API Credentials",font=FONT_TITLE,text_color=ACC).pack(pady=15)
        self.e_cid=self._inp(f,"Client ID");self.e_pin=self._inp(f,"PIN","*")
        self.e_totp=self._inp(f,"TOTP Secret","*");self.e_tok=self._inp(f,"Access Token")
        bf=ctk.CTkFrame(f,fg_color="transparent");bf.pack(pady=15)
        ctk.CTkButton(bf,text="Save",command=self._save_creds,fg_color="#1e40af",width=160,font=FONT_M).pack(side="left",padx=5)
        ctk.CTkButton(bf,text="Generate Token",command=self._gen_tok,fg_color="#065f46",width=180,font=FONT_M).pack(side="left",padx=5)
        ctk.CTkButton(bf,text="Verify",command=self._verify_tok,fg_color="#713f12",width=140,font=FONT_M).pack(side="left",padx=5)
        self.lb_tok=ctk.CTkLabel(f,text="",font=FONT_S,text_color=DIM);self.lb_tok.pack(pady=5)

    def _inp(self,p,label,show=""):
        f=ctk.CTkFrame(p,fg_color="transparent");f.pack(fill="x",padx=50,pady=3)
        ctk.CTkLabel(f,text=label,width=140,anchor="e",text_color=DIM,font=FONT_S).pack(side="left",padx=5)
        e=ctk.CTkEntry(f,width=420,show=show or None,fg_color="#1e293b",border_color="#374151",font=FONT_S);e.pack(side="left",padx=5)
        return e

    def _load_creds(self):
        for e,k in [(self.e_cid,"DHAN_CLIENT_ID"),(self.e_pin,"DHAN_PIN"),(self.e_totp,"DHAN_TOTP_SECRET"),(self.e_tok,"DHAN_ACCESS_TOKEN")]:
            e.insert(0,os.getenv(k,""))

    def _save_creds(self):
        if not ENV_FILE.exists(): ENV_FILE.write_text("")
        for k,e in [("DHAN_CLIENT_ID",self.e_cid),("DHAN_PIN",self.e_pin),("DHAN_TOTP_SECRET",self.e_totp)]:
            set_key(str(ENV_FILE),k,e.get().strip())
        if self.e_tok.get().strip(): set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",self.e_tok.get().strip())
        self.lb_tok.configure(text="Saved!",text_color=GRN)

    def _gen_tok(self):
        self.lb_tok.configure(text="Generating...",text_color=YEL);self.update()
        cid=self.e_cid.get().strip();pin=self.e_pin.get().strip();ts=self.e_totp.get().strip()
        if not all([cid,pin,ts]): self.lb_tok.configure(text="Fill all fields",text_color=RED);return
        def _g():
            t=DhanTokenManager(cid,pin,ts,self.e_tok.get().strip()).ensure_token()
            if t:
                self.e_tok.delete(0,"end");self.e_tok.insert(0,t)
                set_key(str(ENV_FILE),"DHAN_ACCESS_TOKEN",t)
                self.lb_tok.configure(text="Token OK!",text_color=GRN)
            else: self.lb_tok.configure(text="Failed",text_color=RED)
        threading.Thread(target=_g,daemon=True).start()

    def _verify_tok(self):
        t=self.e_tok.get().strip()
        if not t: self.lb_tok.configure(text="No token",text_color=RED);return
        ok=DhanTokenManager(self.e_cid.get().strip(),"","",t).verify(t)
        self.lb_tok.configure(text="VALID" if ok else "INVALID",text_color=GRN if ok else RED)

    # ==================== INSTRUMENTS TAB ====================
    def _build_control(self):
        f=self.t_ctrl
        ctk.CTkLabel(f,text="Instrument Control Panel",font=FONT_TITLE,text_color=ACC).pack(pady=8)
        # Scrollable frame for instrument cards
        self.scroll=ctk.CTkScrollableFrame(f,fg_color=BG,height=600)
        self.scroll.pack(fill="both",expand=True,padx=10,pady=5)
        self.inst_widgets={}
        for key in INSTRUMENTS:
            self._build_inst_card(key)

    def _build_inst_card(self,key):
        inst=INSTRUMENTS[key]
        card=ctk.CTkFrame(self.scroll,fg_color=CARD,corner_radius=10,border_width=1,border_color="#1e293b")
        card.pack(fill="x",padx=5,pady=4)

        # Row 1: Name + Status + Start/Stop
        r1=ctk.CTkFrame(card,fg_color="transparent");r1.pack(fill="x",padx=10,pady=(8,2))
        ctk.CTkLabel(r1,text=inst["label"],font=FONT_HEAD,text_color=TXT,width=220,anchor="w").pack(side="left")
        lbl_st=ctk.CTkLabel(r1,text="STOPPED",font=FONT_S,text_color=DIM,width=200)
        lbl_st.pack(side="left",padx=10)
        btn_stop=ctk.CTkButton(r1,text="STOP",fg_color="#7f1d1d",hover_color="#dc2626",width=80,font=FONT_S,
                                command=lambda k=key:self._stop_inst(k),state="disabled")
        btn_stop.pack(side="right",padx=3)
        btn_start=ctk.CTkButton(r1,text="START",fg_color="#065f46",hover_color="#059669",width=80,font=FONT_S,
                                 command=lambda k=key:self._start_inst(k))
        btn_start.pack(side="right",padx=3)

        # Row 2: Config fields
        r2=ctk.CTkFrame(card,fg_color="transparent");r2.pack(fill="x",padx=10,pady=(2,2))
        ents={}
        fields=[("Brick",inst["brick_size"]),("Rev",inst["reversal"]),("Lots",inst["lots"]),
                ("LotSz",inst["lot_size"]),("TgtPts",inst["target_points"]),("DayTgt",inst["daily_profit_target"])]
        for label,val in fields:
            cf=ctk.CTkFrame(r2,fg_color="transparent");cf.pack(side="left",padx=6)
            ctk.CTkLabel(cf,text=label,text_color=DIM,font=("Segoe UI",10)).pack()
            e=ctk.CTkEntry(cf,width=60,fg_color="#1e293b",border_color="#374151",font=FONT_XS)
            e.insert(0,str(val));e.pack()
            ents[label]=e

        # Row 3: Strike selection + Mode (only for options)
        r3=ctk.CTkFrame(card,fg_color="transparent");r3.pack(fill="x",padx=10,pady=(2,8))
        if inst["trade_type"]=="options":
            ctk.CTkLabel(r3,text="Strike:",text_color=DIM,font=FONT_S).pack(side="left",padx=(0,4))
            cmb_sm=ctk.CTkComboBox(r3,values=["ATM","ITM","OTM"],width=80,font=FONT_XS,fg_color="#1e293b")
            cmb_sm.set(inst.get("strike_mode","ITM"));cmb_sm.pack(side="left",padx=3)
            ctk.CTkLabel(r3,text="Offset:",text_color=DIM,font=FONT_S).pack(side="left",padx=(10,4))
            e_off=ctk.CTkEntry(r3,width=60,fg_color="#1e293b",font=FONT_XS)
            e_off.insert(0,str(inst["itm_offset"]));e_off.pack(side="left",padx=3)
            ents["strike_mode"]=cmb_sm;ents["offset"]=e_off
        else:
            ctk.CTkLabel(r3,text="Futures (direct trade)",text_color=DIM,font=FONT_S).pack(side="left")

        ctk.CTkLabel(r3,text="Mode:",text_color=DIM,font=FONT_S).pack(side="left",padx=(20,4))
        cmb_md=ctk.CTkComboBox(r3,values=["paper","live"],width=90,font=FONT_XS,fg_color="#1e293b")
        cmb_md.set(inst.get("trade_mode","paper"));cmb_md.pack(side="left",padx=3)
        ents["mode"]=cmb_md

        self.inst_widgets[key]={"card":card,"status":lbl_st,"btn_start":btn_start,"btn_stop":btn_stop,"ents":ents}

    def _read_inst_config(self,key):
        """Read GUI fields into INSTRUMENTS dict."""
        w=self.inst_widgets[key]["ents"];inst=INSTRUMENTS[key]
        fmap={"Brick":"brick_size","Rev":"reversal","Lots":"lots","LotSz":"lot_size","TgtPts":"target_points","DayTgt":"daily_profit_target"}
        for label,field in fmap.items():
            if label in w:
                try: inst[field]=int(w[label].get().strip())
                except: pass
        if "strike_mode" in w: inst["strike_mode"]=w["strike_mode"].get()
        if "offset" in w:
            try: inst["itm_offset"]=int(w["offset"].get().strip())
            except: pass
        if "mode" in w: inst["trade_mode"]=w["mode"].get()

    # ==================== RENKO CHART ====================
    def _build_chart(self):
        self.fig=plt.Figure(figsize=(13,6),facecolor="#1a1a2e")
        self.canvas=FigureCanvasTkAgg(self.fig,master=self.t_chart)
        self.canvas.get_tk_widget().pack(fill="both",expand=True,padx=4,pady=4)

    def _redraw_chart(self):
        """Redraw chart only when brick counts change. Called every 500ms."""
        try:
            active=list(self.running_keys)
            if not active: self.after(500,self._redraw_chart);return
            changed=False
            for k in active:
                eng=self.engines.get(k)
                if eng:
                    bc=len(eng.bricks)
                    if self._chart_counts.get(k,0)!=bc: changed=True;self._chart_counts[k]=bc
            if not changed: self.after(500,self._redraw_chart);return
            self.fig.clear()
            n=len(active)
            for idx,key in enumerate(active):
                eng=self.engines.get(key)
                if not eng: continue
                bricks=eng.get_last_n(120)
                if not bricks: continue
                inst=INSTRUMENTS[key];bs=inst["brick_size"]
                ax=self.fig.add_subplot(n,1,idx+1)
                ax.set_facecolor("#16213e")
                for i,b in enumerate(bricks):
                    c="#00e676" if b.is_green else "#ff1744"
                    ax.add_patch(mp.FancyBboxPatch((i-0.4,b.low),0.8,b.high-b.low,boxstyle="round,pad=0.02",lw=0.6,ec=c,fc=c,alpha=0.85))
                ax.set_xlim(-1,len(bricks)+1)
                ax.set_ylim(min(b.low for b in bricks)-bs*2,max(b.high for b in bricks)+bs*2)
                iv=max(1,len(bricks)//12);tk=list(range(0,len(bricks),iv))
                ax.set_xticks(tk);ax.set_xticklabels([bricks[i].time.strftime("%d-%b\n%H:%M") for i in tk],fontsize=7,color="#888")
                ax.tick_params(colors="#888",labelsize=8)
                for s in ["top","right"]: ax.spines[s].set_visible(False)
                for s in ["bottom","left"]: ax.spines[s].set_color("#333")
                ax.grid(True,alpha=0.12,color="#555")
                gc=sum(1 for b in bricks if b.is_green);rc=len(bricks)-gc
                ax.set_title(f"{inst['label']} | {bricks[-1].close:.2f} @ {bricks[-1].time.strftime('%H:%M:%S')} | UP:{gc} DN:{rc} | Size={bs}",fontsize=11,fontweight="bold",color="white",pad=6)
                tm=self.trade_managers.get(key)
                if tm and tm.current_trade and tm.current_trade.is_open:
                    t=tm.current_trade;d="LONG" if t.direction==1 else "SHORT"
                    ax.set_xlabel(f"[{d}] {t.option_type}{int(t.strike) if t.strike else ''} @ {t.entry_price:.2f} | PnL={tm.total_pnl:+.2f}",fontsize=9,color="#FFD700",labelpad=5)
            self.fig.tight_layout(pad=2.0)
            self.canvas.draw_idle()
        except Exception as e: log.error(f"Chart: {e}")
        self.after(500,self._redraw_chart)

    # ==================== DASHBOARD ====================
    def _build_dash(self):
        f=self.t_dash
        self.lbl_summary=ctk.CTkLabel(f,text="No instruments running",font=FONT_HEAD,text_color=DIM)
        self.lbl_summary.pack(pady=8)
        self.dash_frame=ctk.CTkScrollableFrame(f,fg_color=BG,height=200)
        self.dash_frame.pack(fill="x",padx=10,pady=5)
        self.dash_labels={}
        ctk.CTkLabel(f,text="Trade Log",font=FONT_HEAD,text_color=ACC).pack(pady=(8,2))
        self.txt_log=ctk.CTkTextbox(f,height=300,fg_color="#0f172a",text_color=TXT,font=FONT_XS,state="disabled")
        self.txt_log.pack(fill="both",expand=True,padx=10,pady=4)

    def _dlog(self,msg):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end",f"{now_ist().strftime('%H:%M:%S')}|{msg}\n")
        self.txt_log.see("end");self.txt_log.configure(state="disabled")

    def _refresh_dash(self):
        """Update dashboard labels every 500ms."""
        try:
            total_real=total_unreal=0
            for key in list(self.running_keys):
                tm=self.trade_managers.get(key);eng=self.engines.get(key);inst=INSTRUMENTS.get(key)
                if not all([tm,eng,inst]): continue
                ur=tm.get_unrealized_pnl();total_real+=tm.total_pnl;total_unreal+=ur
                # Create label if not exists
                if key not in self.dash_labels:
                    lf=ctk.CTkFrame(self.dash_frame,fg_color=CARD,corner_radius=8)
                    lf.pack(fill="x",padx=5,pady=3)
                    lb=ctk.CTkLabel(lf,text="",font=FONT_XS,text_color=TXT,anchor="w",justify="left")
                    lb.pack(fill="x",padx=10,pady=5)
                    self.dash_labels[key]=lb
                # Build status text
                bricks=eng.bricks;lb_txt=f"{inst['label']} | Bricks={len(bricks)}"
                if bricks:
                    b=bricks[-1];bc="GREEN" if b.is_green else "RED"
                    lb_txt+=f" | Last={bc} {b.close:.2f}"
                t=tm.current_trade
                if tm.daily_target_reached: lb_txt+=f"\n  >> DAILY TARGET REACHED | PnL={tm.total_pnl:+.2f}"
                elif tm.squaredoff: lb_txt+=f"\n  >> SQUARED OFF | PnL={tm.total_pnl:+.2f}"
                elif tm.waiting_for_reversal: lb_txt+=f"\n  >> FLAT (waiting reversal) | PnL={tm.total_pnl:+.2f}"
                elif t and t.is_open:
                    d="LONG" if t.direction==1 else "SHORT"
                    ot=f"{t.option_type}{int(t.strike)}" if t.strike else t.option_type
                    ltp=f"{t.current_ltp:.2f}" if t.current_ltp>0 else "..."
                    tgt=f" Tgt={t.target_price:.2f}" if t.target_price>0 else ""
                    lb_txt+=f"\n  >> {d} {ot} @ {t.entry_price:.2f} | LTP={ltp}{tgt} | Unreal={ur:+.2f}"
                else: lb_txt+=f"\n  >> FLAT | PnL={tm.total_pnl:+.2f} | Trades={tm.trade_count}"
                self.dash_labels[key].configure(text=lb_txt)

                # Update instrument card status
                w=self.inst_widgets.get(key,{})
                if "status" in w:
                    if tm.daily_target_reached: w["status"].configure(text="DAILY TARGET",text_color=YEL)
                    elif t and t.is_open:
                        d="LONG" if t.direction==1 else "SHORT"
                        w["status"].configure(text=f"{d} | PnL={tm.total_pnl:+.2f}",text_color=GRN if t.direction==1 else RED)
                    elif tm.waiting_for_reversal: w["status"].configure(text="WAITING REV",text_color=YEL)
                    else: w["status"].configure(text=f"RUNNING | PnL={tm.total_pnl:+.2f}",text_color=GRN)

            net=total_real+total_unreal
            if self.running_keys:
                self.lbl_summary.configure(text=f"Real={total_real:+.2f} | Unreal={total_unreal:+.2f} | Net={net:+.2f} | Running: {len(self.running_keys)}",
                                           text_color=GRN if net>=0 else RED)
        except: pass
        self.after(500,self._refresh_dash)

    # ==================== ENGINE CONTROL ====================
    def _ensure_ws(self):
        """Start shared WS if not running."""
        if self.ws_thread and self.ws_thread.is_alive(): return
        self.client_id=self.e_cid.get().strip();self.access_token=self.e_tok.get().strip()
        if not self.client_id or not self.access_token:
            self._dlog("ERROR: Generate token first!");return False
        api.set_auth(self.access_token,self.client_id)
        self.ws_stop.clear();self.ws_connected.clear()
        self.ws_thread=threading.Thread(target=self._ws_loop,daemon=True)
        self.ws_thread.start()
        return True

    def _ws_loop(self):
        url=f"wss://api-feed.dhan.co?version=2&token={self.access_token}&clientId={self.client_id}&authType=2"
        backoff=0
        while not self.ws_stop.is_set():
            try:
                def _open(ws):
                    nonlocal backoff;self.ws_connected.set();backoff=0
                    # Resubscribe all active signal instruments
                    insts=[]
                    for k in list(self.running_keys):
                        sid,seg,_=get_signal_config(k)
                        if sid: insts.append({"ExchangeSegment":seg,"SecurityId":sid})
                    if insts:
                        ws.send(json.dumps({"RequestCode":REQ_SUB_TICKER,"InstrumentCount":len(insts),"InstrumentList":insts}))
                    self.after(0,lambda:self._dlog(f"WS connected | {len(insts)} signals"))
                def _msg(ws,message):
                    if isinstance(message,str): return
                    hdr=parse_header_8(bytes(message))
                    if not hdr or int(hdr["resp_code"])!=RESP_TICKER: return
                    t=parse_ticker(hdr["payload"])
                    if not t: return
                    sid=str(hdr["security_id"]);ltp=float(t["ltp"])
                    ltt=_norm_epoch(int(t["ltt_epoch"]));ts=datetime.fromtimestamp(ltt,tz=IST)
                    key=self.signal_secid_to_key.get(sid)
                    if key and key in self.engines:
                        nb=self.engines[key].process_price(ltp,ts)
                        if nb:
                            for b in nb: log.info(f"BRICK {key}|{'^' if b.is_green else 'v'} O={b.open:.2f} C={b.close:.2f}|#{len(self.engines[key].bricks)}")
                        if key in self.trade_managers:
                            self.trade_managers[key].update_signal_ltp(ltp)
                            self.trade_managers[key].check_target(ltp)
                    tk=self.trade_secid_to_key.get(sid)
                    if tk and tk in self.trade_managers: self.trade_managers[tk].update_ltp(sid,ltp)
                def _err(ws,error): self.after(0,lambda:self._dlog(f"WS err: {error}"))
                def _close(ws,sc,msg): self.ws_connected.clear()
                self.ws=websocket.WebSocketApp(url,on_open=_open,on_message=_msg,on_error=_err,on_close=_close)
                self.ws.run_forever(ping_interval=20,ping_timeout=10)
            except: pass
            finally:
                if not self.ws_stop.is_set():
                    delay=min(2*(2**backoff),30);backoff+=1;time.sleep(delay)

    def _ws_subscribe(self,sid,seg):
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode":REQ_SUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":seg,"SecurityId":str(sid)}]}))
            except: pass

    def _ws_sub_cb(self,sid,exch,key):
        if sid in self.signal_secid_to_key: self.trade_secid_to_key[sid]=key;return
        self.trade_secid_to_key[sid]=key
        self._ws_subscribe(sid,exch)

    def _ws_unsub_cb(self,sid,exch):
        if sid in self.signal_secid_to_key: return
        self.trade_secid_to_key.pop(sid,None)
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode":REQ_UNSUB_TICKER,"InstrumentCount":1,"InstrumentList":[{"ExchangeSegment":exch,"SecurityId":str(sid)}]}))
            except: pass

    def _start_inst(self,key):
        if key in self.running_keys: return
        if not self._ensure_ws(): return
        self._read_inst_config(key)
        threading.Thread(target=self._init_instrument,args=(key,),daemon=True).start()

    def _init_instrument(self,key):
        """Initialize one instrument (runs in background thread)."""
        try:
            inst=INSTRUMENTS[key]
            self.after(0,lambda:self._dlog(f"Starting {inst['label']}..."))
            # Resolve security IDs for futures
            resolve_security_ids([key])
            sig_sid,sig_seg,sig_inst=get_signal_config(key)
            if not sig_sid:
                self.after(0,lambda:self._dlog(f"ERROR: No signal ID for {key}"));return
            self.signal_secid_to_key[sig_sid]=key

            # Trade manager
            self.client_id=self.e_cid.get().strip()
            tm=TradeManager(key,self.client_id,ws_sub_cb=self._ws_sub_cb,ws_unsub_cb=self._ws_unsub_cb,gui_cb=self._gui_cb)
            self.trade_managers[key]=tm

            # Renko engine
            eng=RenkoEngine(inst["brick_size"],inst["reversal"],on_brick_callback=tm.on_brick)
            eng.callback_key=key
            candles=fetch_historical(sig_sid,sig_seg,sig_inst,5)
            if candles:
                eng.build_from_candles(candles)
                self.after(0,lambda:self._dlog(f"{inst['label']}: {len(eng.bricks)} bricks | {sig_seg}:{sig_sid}"))
            self.engines[key]=eng

            # Subscribe to WS
            self._ws_subscribe(sig_sid,sig_seg)

            # Position adoption
            self._adopt_positions(key,tm)

            self.running_keys.add(key)
            self.after(0,lambda:self._update_inst_ui(key,True))
            self.after(0,lambda:self._dlog(f"{inst['label']} STARTED | Mode={inst['trade_mode'].upper()} | Strike={inst.get('strike_mode','ATM')}"))
        except Exception as e:
            self.after(0,lambda:self._dlog(f"ERROR starting {key}: {e}"))

    def _adopt_positions(self,key,tm):
        """Check broker positions and adopt matching ones."""
        inst=INSTRUMENTS[key]
        if inst["trade_mode"]!="live": return
        try:
            positions=get_broker_positions()
            if not positions: return
            for p in positions:
                sid=str(p.get("securityId",""));nq=int(p.get("netQty",0))
                sym=p.get("tradingSymbol","");seg=p.get("exchangeSegment","")
                if nq==0: continue
                # Match futures by security_id
                if inst["trade_type"]=="futures" and sid==inst["security_id"]:
                    direction=1 if nq>0 else -1
                    avg=float(p.get("buyAvg",0)) if nq>0 else float(p.get("sellAvg",0))
                    if avg<=0: avg=float(p.get("dayBuyAvg",0)) if nq>0 else float(p.get("daySellAvg",0))
                    tm.adopt_position(sid,direction,nq,avg)
                    self.after(0,lambda:self._dlog(f"ADOPTED {sym} | {'LONG' if direction==1 else 'SHORT'} | Qty={abs(nq)} | Avg={avg:.2f}"))
                    return
                # Match options by symbol root
                if inst["trade_type"]=="options" and inst["symbol_root"] in sym:
                    direction=1 if nq>0 else -1
                    avg=float(p.get("buyAvg",0)) if nq>0 else float(p.get("sellAvg",0))
                    # Extract strike and type from symbol
                    ot="CE" if "CE" in sym.upper() else "PE" if "PE" in sym.upper() else "?"
                    strike=0.0
                    nums=re.findall(r'\d+',sym)
                    if len(nums)>=2:
                        try: strike=float(nums[-1])
                        except: pass
                    tm.adopt_position(sid,direction,nq,avg,option_type=ot,strike=strike)
                    self.after(0,lambda:self._dlog(f"ADOPTED {sym} | {'LONG' if direction==1 else 'SHORT'} {ot}{int(strike)} | Qty={abs(nq)} | Avg={avg:.2f}"))
                    return
        except Exception as e:
            log.error(f"Adopt err: {e}")

    def _stop_inst(self,key):
        if key not in self.running_keys: return
        tm=self.trade_managers.get(key)
        if tm: tm.squareoff()
        self.running_keys.discard(key)
        # Remove from signal routing
        sig_sid,_,_=get_signal_config(key)
        self.signal_secid_to_key.pop(sig_sid,None)
        self.engines.pop(key,None);self.trade_managers.pop(key,None)
        self._chart_counts.pop(key,None)
        # Remove dash label
        if key in self.dash_labels:
            self.dash_labels[key].master.destroy();del self.dash_labels[key]
        self._update_inst_ui(key,False)
        self._dlog(f"{INSTRUMENTS[key]['label']} STOPPED")

    def _update_inst_ui(self,key,running):
        w=self.inst_widgets.get(key,{})
        if running:
            w.get("btn_start",ctk.CTkButton(self)).configure(state="disabled")
            w.get("btn_stop",ctk.CTkButton(self)).configure(state="normal")
            w.get("status",ctk.CTkLabel(self)).configure(text="RUNNING",text_color=GRN)
        else:
            w.get("btn_start",ctk.CTkButton(self)).configure(state="normal")
            w.get("btn_stop",ctk.CTkButton(self)).configure(state="disabled")
            w.get("status",ctk.CTkLabel(self)).configure(text="STOPPED",text_color=DIM)

    def _gui_cb(self,event,key,data):
        try:
            inst=INSTRUMENTS.get(key,{});name=inst.get("label",key)
            if event=="signal":
                d="BUY" if data["direction"]==1 else "SELL"
                self.after(0,lambda:self._dlog(f"{name}|SIGNAL {d}|{data['brick_close']:.2f}"))
            elif event=="entry":
                d="LONG" if data["direction"]==1 else "SHORT";s=int(data["strike"]) if data["strike"] else "FUT"
                self.after(0,lambda:self._dlog(f"{name}|ENTRY {d} {data['type']}{s} ({data.get('mode','')}) @ {data['price']:.2f}|Qty={data['qty']}"))
            elif event=="exit":
                self.after(0,lambda:self._dlog(f"{name}|EXIT {data['reason']}|PnL={data['pnl']:+.2f}|Total={data['total']:+.2f}"))
            elif event=="target_hit":
                self.after(0,lambda:self._dlog(f"{name}|TARGET HIT|LTP={data['ltp']:.2f}"))
            elif event=="daily_target":
                self.after(0,lambda:self._dlog(f"{name}|DAILY TARGET|PnL={data['pnl']:+.2f}"))
            elif event=="squareoff":
                self.after(0,lambda:self._dlog(f"{name}|SQUAREOFF|PnL={data['total']:+.2f}"))
        except: pass

    # ==================== TIMERS ====================
    def _start_timers(self):
        self._refresh_dash();self._redraw_chart();self._check_squareoff()

    def _check_squareoff(self):
        if not self._sq_done and self.running_keys:
            n=now_ist()
            if n.hour>self.sq_hour or (n.hour==self.sq_hour and n.minute>=self.sq_min):
                log.info(f"AUTO-SQUAREOFF|IST {n.strftime('%H:%M:%S')}")
                self._dlog("AUTO-SQUAREOFF triggered")
                for key in list(self.running_keys):
                    tm=self.trade_managers.get(key)
                    if tm: tm.squareoff()
                self._sq_done=True
        self.after(5000,self._check_squareoff)

    def _on_close(self):
        for key in list(self.running_keys):
            tm=self.trade_managers.get(key)
            if tm: tm.squareoff()
        self.ws_stop.set()
        if self.ws:
            try: self.ws.close()
            except: pass
        self.destroy()

if __name__=="__main__": App().mainloop()
