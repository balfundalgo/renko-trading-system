"""
engine.py -- Renko Trading Engine v3.0
ATM/ITM/OTM strike selection, position adoption, multi-instrument ready
"""
import os,sys,io,csv,time,json,struct,threading,logging
from datetime import datetime,timedelta,timezone
from dataclasses import dataclass
from typing import Dict,Optional,List
from pathlib import Path
import requests,pyotp,websocket
from dotenv import load_dotenv,set_key

log=logging.getLogger("RENKO")
IST=timezone(timedelta(hours=5,minutes=30))
if getattr(sys,'frozen',False): BASE_DIR=Path(sys.executable).parent
else: BASE_DIR=Path(__file__).resolve().parent
ENV_FILE=BASE_DIR/".env"

def now_ist(): return datetime.now(IST)
def _norm_epoch(ts):
    ts=int(ts);d=ts-int(time.time())
    if 16200<=d<=23400: ts-=19800
    return ts
def _ensure_dict(r):
    if isinstance(r,list): return r[0] if r else {}
    return r if isinstance(r,dict) else {}

BASE_URL="https://api.dhan.co/v2"
AUTH_GENERATE_URL="https://auth.dhan.co/app/generateAccessToken"
AUTH_RENEW_URL="https://api.dhan.co/v2/RenewToken"
AUTH_VERIFY_URL="https://api.dhan.co/v2/profile"
DHAN_INSTRUMENT_API="https://api.dhan.co/v2/instrument"
REQ_SUB_TICKER=15;REQ_UNSUB_TICKER=16;RESP_TICKER=2
EXCH_SEG_MAP={0:"IDX_I",1:"NSE_EQ",2:"NSE_FNO",3:"NSE_CURRENCY",4:"BSE_EQ",5:"MCX_COMM",7:"BSE_CURRENCY",8:"BSE_FNO"}

# strike_mode: "ATM" / "ITM" / "OTM"
# ATM: trade at-the-money (offset ignored)
# ITM: CE below spot, PE above spot (deeper = higher delta)
# OTM: CE above spot, PE below spot (cheaper premium)

INSTRUMENTS = {
    "NIFTY": {
        "security_id":"","segment":"NSE_FNO","instrument":"FUTIDX","exch_for_ws":"NSE_FNO",
        "symbol_root":"NIFTY","inst_filter":"FUTIDX","brick_size":30,"reversal":2,
        "label":"NIFTY SPOT","trade_type":"options","strike_mode":"ITM","itm_offset":100,"strike_gap":50,
        "lot_size":75,"lots":1,"trade_mode":"paper","index_security_id":"13","index_segment":"IDX_I",
        "target_points":0,"daily_profit_target":0,
    },
    "BANKNIFTY": {
        "security_id":"","segment":"NSE_FNO","instrument":"FUTIDX","exch_for_ws":"NSE_FNO",
        "symbol_root":"BANKNIFTY","inst_filter":"FUTIDX","brick_size":50,"reversal":2,
        "label":"BANKNIFTY SPOT","trade_type":"options","strike_mode":"ITM","itm_offset":200,"strike_gap":100,
        "lot_size":30,"lots":1,"trade_mode":"paper","index_security_id":"25","index_segment":"IDX_I",
        "target_points":0,"daily_profit_target":0,
    },
    "SENSEX": {
        "security_id":"","segment":"BSE_FNO","instrument":"FUTIDX","exch_for_ws":"BSE_FNO",
        "symbol_root":"SENSEX","inst_filter":"FUTIDX","brick_size":100,"reversal":2,
        "label":"SENSEX SPOT","trade_type":"options","strike_mode":"ITM","itm_offset":300,"strike_gap":100,
        "lot_size":10,"lots":1,"trade_mode":"paper","index_security_id":"51","index_segment":"IDX_I",
        "target_points":0,"daily_profit_target":0,
    },
    "GOLDPETAL": {
        "security_id":"","segment":"MCX_COMM","instrument":"FUTCOM","exch_for_ws":"MCX_COMM",
        "symbol_root":"GOLDPETAL","inst_filter":"FUTCOM","brick_size":5,"reversal":2,
        "label":"GOLDPETAL (MCX)","trade_type":"futures","strike_mode":"ATM","itm_offset":0,"strike_gap":0,
        "lot_size":100,"lots":1,"trade_mode":"paper","index_security_id":"","index_segment":"",
        "target_points":0,"daily_profit_target":0,
    },
    "SILVERMICRO": {
        "security_id":"","segment":"MCX_COMM","instrument":"FUTCOM","exch_for_ws":"MCX_COMM",
        "symbol_root":"SILVERM","inst_filter":"FUTCOM","brick_size":50,"reversal":2,
        "label":"SILVERMICRO (MCX)","trade_type":"futures","strike_mode":"ATM","itm_offset":0,"strike_gap":0,
        "lot_size":100,"lots":1,"trade_mode":"paper","index_security_id":"","index_segment":"",
        "target_points":0,"daily_profit_target":0,
    },
    "CRUDEOILM": {
        "security_id":"","segment":"MCX_COMM","instrument":"FUTCOM","exch_for_ws":"MCX_COMM",
        "symbol_root":"CRUDEOILM","inst_filter":"FUTCOM","brick_size":5,"reversal":2,
        "label":"CRUDEOIL MINI (MCX)","trade_type":"futures","strike_mode":"ATM","itm_offset":0,"strike_gap":0,
        "lot_size":10,"lots":1,"trade_mode":"live","index_security_id":"","index_segment":"",
        "target_points":10,"daily_profit_target":500,
    },
    "GOLDM": {
        "security_id":"","segment":"MCX_COMM","instrument":"FUTCOM","exch_for_ws":"MCX_COMM",
        "symbol_root":"GOLDM","inst_filter":"FUTCOM","brick_size":50,"reversal":2,
        "label":"GOLD MINI (MCX)","trade_type":"futures","strike_mode":"ATM","itm_offset":0,"strike_gap":0,
        "lot_size":10,"lots":1,"trade_mode":"paper","index_security_id":"","index_segment":"",
        "target_points":0,"daily_profit_target":0,
    },
}

class DhanTokenManager:
    def __init__(self,client_id,pin,totp_secret,existing_token=""):
        self.client_id=client_id;self.pin=pin;self.totp_secret=totp_secret;self.existing_token=existing_token
    def verify(self,token):
        if not token: return False
        try: return requests.get(AUTH_VERIFY_URL,headers={"access-token":token,"client-id":self.client_id},timeout=10).status_code==200
        except: return False
    def renew(self,token):
        try:
            d=requests.get(AUTH_RENEW_URL,headers={"access-token":token,"dhanClientId":self.client_id,"Content-Type":"application/json"},timeout=15).json()
            if "accessToken" in d: return d["accessToken"]
        except: pass
        return None
    def generate(self,max_retries=3):
        for a in range(max_retries):
            rem=30-(int(time.time())%30)
            if a>0 or rem<10: time.sleep(rem+1)
            totp=pyotp.TOTP(self.totp_secret).now();log.info(f"TOTP attempt {a+1}: {totp}")
            try:
                d=requests.post(AUTH_GENERATE_URL,params={"dhanClientId":self.client_id,"pin":self.pin,"totp":totp},timeout=15).json()
                if "accessToken" in d: return d["accessToken"]
            except Exception as e: log.warning(f"Gen err: {e}");time.sleep(2)
        return None
    def ensure_token(self):
        if self.existing_token:
            if self.verify(self.existing_token): return self.existing_token
            r=self.renew(self.existing_token)
            if r: return r
        return self.generate()

def get_signal_config(key):
    inst=INSTRUMENTS[key]
    if inst["trade_type"]=="options" and inst.get("index_security_id"):
        return inst["index_security_id"],inst.get("index_segment","IDX_I"),"INDEX"
    return inst["security_id"],inst["segment"],inst["instrument"]

class DhanAPI:
    def __init__(self): self.headers={}
    def set_auth(self,token,client_id):
        self.headers={"Content-Type":"application/json","Accept":"application/json","access-token":token,"client-id":client_id}
    def post(self,ep,payload,retries=2):
        for a in range(retries+1):
            try:
                r=requests.post(f"{BASE_URL}{ep}",headers=self.headers,json=payload,timeout=15)
                if r.status_code==200: return r.json()
                log.warning(f"API {ep}->{r.status_code}: {r.text[:200]}")
            except Exception as e: log.error(f"API {ep}: {e}")
            if a<retries: time.sleep(1)
        return None
    def get(self,ep,retries=2):
        for a in range(retries+1):
            try:
                r=requests.get(f"{BASE_URL}{ep}",headers=self.headers,timeout=15)
                if r.status_code==200: return r.json()
            except: pass
            if a<retries: time.sleep(1)
        return None
api=DhanAPI()

def resolve_security_ids(keys):
    today=datetime.now(IST).date()
    fkeys=[k for k in keys if INSTRUMENTS.get(k,{}).get("trade_type")=="futures"]
    if not fkeys: return
    needed={INSTRUMENTS[k]["segment"] for k in fkeys}
    seg_rows={}
    for seg in needed:
        rows=[]
        try:
            r=requests.get(f"{DHAN_INSTRUMENT_API}/{seg}",headers=api.headers,timeout=60)
            if r.status_code==200 and len(r.text)>100: rows=list(csv.DictReader(io.StringIO(r.text)))
        except: pass
        seg_rows[seg]=rows
    for key in fkeys:
        inst=INSTRUMENTS.get(key)
        if not inst: continue
        rows=seg_rows.get(inst["segment"],[])
        if not rows: continue
        s=rows[0]
        def _f(cs):
            for c in cs:
                if c in s: return c
            for c in s:
                for cd in cs:
                    if cd.upper().replace("_","") in c.upper().replace("_",""): return c
            return ""
        cs=_f(["SEM_TRADING_SYMBOL","SM_SYMBOL_NAME","SYMBOL_NAME"])
        ci=_f(["SEM_INSTRUMENT_NAME","INSTRUMENT"])
        cid=_f(["SEM_SMST_SECURITY_ID","SECURITY_ID"])
        ce=_f(["SEM_EXPIRY_DATE","SM_EXPIRY_DATE"])
        cc=_f(["SEM_CUSTOM_SYMBOL","DISPLAY_NAME"])
        if not all([cs,ci,cid,ce]): continue
        cands=[]
        for row in rows:
            sym=row.get(cs,"").strip();ins=row.get(ci,"").strip();sid=row.get(cid,"").strip();exp=row.get(ce,"").strip()
            cust=row.get(cc,"").strip() if cc else ""
            if ins!=inst["inst_filter"]: continue
            if not (sym.startswith(inst["symbol_root"]) or cust.startswith(inst["symbol_root"])): continue
            ed=None
            for fmt in ("%Y-%m-%d","%Y-%m-%d %H:%M:%S","%d-%m-%Y","%d/%m/%Y","%d-%b-%Y"):
                try: ed=datetime.strptime(exp.split(" ")[0],fmt).date(); break
                except: continue
            if not ed or ed<today: continue
            cands.append({"security_id":sid,"trading_symbol":sym or cust,"expiry_date":ed})
        if not cands: continue
        cands.sort(key=lambda x:x["expiry_date"]);ch=cands[0]
        inst["security_id"]=str(ch["security_id"])
        log.info(f"  {key}: {ch['trading_symbol']} | SecID={ch['security_id']}")

def fetch_historical(sid,seg,inst_type,days=5):
    to_d=now_ist().strftime("%Y-%m-%d");fr_d=(now_ist()-timedelta(days=days)).strftime("%Y-%m-%d")
    resp=api.post("/charts/intraday",{"securityId":str(sid),"exchangeSegment":seg,"instrument":inst_type,"interval":"1","fromDate":fr_d,"toDate":to_d})
    if not resp or not resp.get("open"): return []
    candles=[];ts_list=resp.get("timestamp") or resp.get("start_Time") or []
    for i in range(len(resp["open"])):
        t=_norm_epoch(int(ts_list[i])) if i<len(ts_list) else 0
        candles.append({"timestamp":t,"open":float(resp["open"][i]),"high":float(resp["high"][i]),"low":float(resp["low"][i]),"close":float(resp["close"][i])})
    return candles

def get_nearest_expiry(idx_name):
    sid={"NIFTY":"13","BANKNIFTY":"25","SENSEX":"51"}.get(idx_name,"")
    if not sid: return None
    resp=api.post("/optionchain/expirylist",{"UnderlyingScrip":int(sid),"UnderlyingSeg":"IDX_I"})
    if not resp or resp.get("status")!="success": return None
    today=now_ist().date();valid=[]
    for e in resp.get("data",[]):
        try:
            d=datetime.strptime(e,"%Y-%m-%d").date()
            if d>=today: valid.append((d,e))
        except: pass
    valid.sort(); return valid[0][1] if valid else None

def resolve_option(inst_key,direction):
    """Resolve option strike based on strike_mode: ATM, ITM, or OTM."""
    inst=INSTRUMENTS[inst_key];idx_name=inst["symbol_root"]
    idx_sid=inst.get("index_security_id","")
    if not idx_sid: return None
    expiry=get_nearest_expiry(idx_name)
    if not expiry: return None
    resp=api.post("/optionchain",{"UnderlyingScrip":int(idx_sid),"UnderlyingSeg":"IDX_I","Expiry":expiry})
    if not resp or resp.get("status")!="success": return None
    spot=float(resp["data"]["last_price"]);oc=resp["data"]["oc"]
    gap=inst["strike_gap"];atm=round(spot/gap)*gap
    offset=inst["itm_offset"];mode=inst.get("strike_mode","ITM").upper()

    if mode=="ATM":
        if direction==1: tgt=atm;ot="CE"
        else: tgt=atm;ot="PE"
    elif mode=="ITM":
        if direction==1: tgt=atm-offset;ot="CE"
        else: tgt=atm+offset;ot="PE"
    else: # OTM
        if direction==1: tgt=atm+offset;ot="CE"
        else: tgt=atm-offset;ot="PE"

    key=None
    for k in oc:
        try:
            if abs(float(k)-tgt)<0.01: key=k; break
        except: pass
    if not key: log.error(f"  {idx_name}: Strike {tgt}{ot} not in OC"); return None
    ok="ce" if ot=="CE" else "pe"
    if key not in oc or ok not in oc[key]: return None
    od=oc[key][ok]
    log.info(f"  {idx_name} {int(tgt)}{ot} ({mode}) | LTP={od.get('last_price',0)} | Spot={spot:.0f}")
    return {"security_id":str(od["security_id"]),"strike":tgt,"option_type":ot,"last_price":float(od.get("last_price",0)),"expiry":expiry}

def place_order(client_id,security_id,exchange_segment,qty,buy_sell,max_retries=3):
    payload={"dhanClientId":client_id,"transactionType":buy_sell,"exchangeSegment":exchange_segment,"productType":"INTRADAY","orderType":"MARKET","validity":"DAY","securityId":str(security_id),"quantity":int(qty),"price":0,"triggerPrice":0,"disclosedQuantity":0,"afterMarketOrder":False}
    order_id=None
    for a in range(max_retries):
        log.info(f"ORDER|{buy_sell} {qty}|SecID={security_id}|Attempt {a+1}")
        resp=api.post("/orders",payload,retries=0)
        if resp:
            resp=_ensure_dict(resp)
            if resp.get("orderId"): order_id=str(resp["orderId"]);break
            log.error(f"  Failed: {resp.get('errorMessage') or resp}")
        if a<max_retries-1: time.sleep(1)
    if not order_id: return None,0.0
    fill=0.0
    for p in range(10):
        time.sleep(0.5)
        try:
            tr=api.get(f"/trades/{order_id}",retries=0)
            if tr:
                tl=tr if isinstance(tr,list) else [tr];tq=tv=0
                for t in tl:
                    if not isinstance(t,dict): continue
                    tq+=int(t.get("tradedQuantity",0));tv+=int(t.get("tradedQuantity",0))*float(t.get("tradedPrice",0))
                if tq>0: fill=tv/tq;break
        except: pass
        try:
            oi=_ensure_dict(api.get(f"/orders/{order_id}",retries=0) or {})
            if oi.get("orderStatus")=="REJECTED": return None,0.0
            if oi.get("orderStatus")=="TRADED" and fill<=0:
                fp=float(oi.get("price",0))
                if fp>0: fill=fp;break
        except: pass
    return order_id,fill

def get_broker_positions():
    resp=api.get("/positions")
    if not resp or not isinstance(resp,list): return []
    return [p for p in resp if isinstance(p,dict) and int(p.get("netQty",0))!=0]

def parse_header_8(msg):
    if len(msg)<8: return None
    return {"resp_code":msg[0],"security_id":str(struct.unpack_from("<I",msg,4)[0]),"payload":msg[8:]}
def parse_ticker(payload):
    if len(payload)<8: return None
    return {"ltp":float(struct.unpack_from("<f",payload,0)[0]),"ltt_epoch":int(struct.unpack_from("<I",payload,4)[0])}

@dataclass
class RenkoBrick:
    time:datetime;open:float;close:float
    @property
    def high(self): return max(self.open,self.close)
    @property
    def low(self): return min(self.open,self.close)
    @property
    def is_green(self): return self.close>self.open
    @property
    def is_red(self): return self.close<self.open

class RenkoEngine:
    def __init__(self,brick_size,reversal_bricks=2,on_brick_callback=None):
        self.brick_size=brick_size;self.reversal_bricks=reversal_bricks
        self.last_brick_close=0.0;self.last_direction=0
        self.bricks:List[RenkoBrick]=[];self.lock=threading.Lock()
        self.on_brick_callback=on_brick_callback;self.callback_key=""
    def seed(self,price): self.last_brick_close=price;self.last_direction=0
    def process_price(self,price,timestamp):
        new=[]
        with self.lock:
            while True:
                d=price-self.last_brick_close;bs=self.brick_size;rb=self.reversal_bricks
                if self.last_direction==0:
                    if d>=bs: o,c=self.last_brick_close,self.last_brick_close+bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close,self.last_direction=c,1;continue
                    elif d<=-bs: o,c=self.last_brick_close,self.last_brick_close-bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close,self.last_direction=c,-1;continue
                    else: break
                if self.last_direction==1 and d>=bs: o,c=self.last_brick_close,self.last_brick_close+bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close=c;continue
                if self.last_direction==-1 and d<=-bs: o,c=self.last_brick_close,self.last_brick_close-bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close=c;continue
                if self.last_direction==1 and d<=-(bs*rb): o=self.last_brick_close-bs;c=self.last_brick_close-2*bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close,self.last_direction=c,-1;continue
                if self.last_direction==-1 and d>=(bs*rb): o=self.last_brick_close+bs;c=self.last_brick_close+2*bs;new.append(RenkoBrick(time=timestamp,open=o,close=c));self.last_brick_close,self.last_direction=c,1;continue
                break
            self.bricks.extend(new)
        if new and self.on_brick_callback: self.on_brick_callback(new[-1],self.callback_key)
        return new
    def build_from_candles(self,candles):
        if not candles: return 0
        cb=self.on_brick_callback;self.on_brick_callback=None
        self.seed(float(candles[0]["close"]))
        for c in candles:
            ts=datetime.fromtimestamp(c["timestamp"],tz=IST) if c["timestamp"] else now_ist()
            self.process_price(float(c["close"]),ts)
        self.on_brick_callback=cb; return len(self.bricks)
    def get_last_n(self,n):
        with self.lock: return list(self.bricks[-n:]) if self.bricks else []

@dataclass
class Trade:
    instrument_key:str;direction:int;option_type:str;security_id:str
    strike:float;entry_price:float;entry_time:datetime;qty:int
    target_price:float=0.0;expiry:str="";exit_price:float=0.0
    exit_time:Optional[datetime]=None;pnl:float=0.0;is_open:bool=True
    current_ltp:float=0.0;exit_reason:str=""

class TradeManager:
    def __init__(self,inst_key,client_id,ws_sub_cb=None,ws_unsub_cb=None,gui_cb=None):
        self.inst_key=inst_key;self.inst=INSTRUMENTS[inst_key];self.client_id=client_id
        self.order_lock=threading.Lock();self.current_trade:Optional[Trade]=None
        self.trade_history:List[Trade]=[];self.total_pnl=0.0;self.trade_count=0
        self.last_brick_color:Optional[str]=None;self._ws_sub=ws_sub_cb;self._ws_unsub=ws_unsub_cb
        self.gui_cb=gui_cb;self.squaredoff=False;self.waiting_for_reversal=False
        self.waiting_direction=0;self._order_in_progress=False;self.daily_target_reached=False
        self.enable_trading=True

    def _notify(self,event,data=None):
        if self.gui_cb:
            try: self.gui_cb(event,self.inst_key,data)
            except: pass

    def update_ltp(self,sid,ltp):
        t=self.current_trade
        if t and t.is_open and t.security_id==sid: t.current_ltp=ltp
    def update_signal_ltp(self,ltp):
        t=self.current_trade
        if t and t.is_open and self.inst["trade_type"]=="futures": t.current_ltp=ltp

    def adopt_position(self,security_id,direction,qty,entry_price,option_type="FUT",strike=0.0,expiry=""):
        """Adopt an existing broker position as if we entered it."""
        tp=self.inst.get("target_points",0)
        tgt_p=(entry_price+tp) if direction==1 and tp>0 else (entry_price-tp) if direction==-1 and tp>0 else 0.0
        trade=Trade(instrument_key=self.inst_key,direction=direction,option_type=option_type,
                    security_id=str(security_id),strike=strike,entry_price=entry_price,
                    entry_time=now_ist(),qty=abs(qty),expiry=expiry,target_price=tgt_p)
        self.current_trade=trade;self.trade_count+=1
        self.last_brick_color="green" if direction==1 else "red"
        if self._ws_sub:
            exch="NSE_FNO" if self.inst["trade_type"]=="options" else self.inst["exch_for_ws"]
            self._ws_sub(str(security_id),exch,self.inst_key)
        log.info(f"  ADOPTED | {'LONG' if direction==1 else 'SHORT'} {option_type} | SecID={security_id} | Entry={entry_price:.2f} | Qty={abs(qty)}")
        self._notify("entry",{"direction":direction,"type":option_type,"strike":strike,"price":entry_price,"target":tgt_p,"qty":abs(qty),"mode":"ADOPTED"})

    def check_target(self,ltp):
        if not self.enable_trading or self.squaredoff or self.daily_target_reached or self._order_in_progress: return
        t=self.current_trade
        if not t or not t.is_open or t.target_price<=0: return
        hit=(t.direction==1 and ltp>=t.target_price) or (t.direction==-1 and ltp<=t.target_price)
        if hit:
            self._order_in_progress=True
            self._notify("target_hit",{"ltp":ltp})
            threading.Thread(target=self._bg_target_exit,args=(ltp,),daemon=True).start()

    def on_brick(self,brick,key):
        if not self.enable_trading or key!=self.inst_key or self.squaredoff or self.daily_target_reached: return
        color="green" if brick.is_green else "red"
        if color==self.last_brick_color: return
        self.last_brick_color=color
        if self._order_in_progress: return
        self._order_in_progress=True
        new_dir=1 if color=="green" else -1
        self._notify("signal",{"direction":new_dir,"brick_close":brick.close})
        threading.Thread(target=self._bg_brick_signal,args=(new_dir,brick),daemon=True).start()

    def _check_daily_target(self):
        dpt=self.inst.get("daily_profit_target",0)
        if dpt<=0 or self.total_pnl<dpt: return
        self.daily_target_reached=True
        self._notify("daily_target",{"pnl":self.total_pnl})
        t=self.current_trade
        if t and t.is_open:
            ep,_=self._place_exit(t,t.current_ltp if t.current_ltp>0 else t.entry_price)
            pp=(ep-t.entry_price) if t.direction==1 else (t.entry_price-ep)
            t.exit_price=ep;t.exit_time=now_ist();t.pnl=pp*t.qty;t.is_open=False;t.exit_reason="DAILY_TGT"
            self.total_pnl+=t.pnl;self.trade_history.append(t)

    def _bg_target_exit(self,ltp):
        try:
            with self.order_lock:
                t=self.current_trade
                if not t or not t.is_open: return
                d=t.direction;ep,_=self._place_exit(t,ltp)
                pp=(ep-t.entry_price) if d==1 else (t.entry_price-ep)
                t.exit_price=ep;t.exit_time=now_ist();t.pnl=pp*t.qty;t.is_open=False;t.exit_reason="TARGET"
                self.total_pnl+=t.pnl;self.trade_history.append(t)
                self.waiting_for_reversal=True;self.waiting_direction=d
                self._notify("exit",{"reason":"TARGET","pnl":t.pnl,"total":self.total_pnl})
                self._check_daily_target()
        except Exception as e: log.error(f"Target exit err: {e}")
        finally: self._order_in_progress=False

    def _bg_brick_signal(self,new_dir,brick):
        try:
            with self.order_lock:
                has_open=self.current_trade and self.current_trade.is_open
                is_w=self.waiting_for_reversal;wd=self.waiting_direction
                if has_open:
                    self._do_exit(brick,"REVERSAL");self._check_daily_target()
                    if self.daily_target_reached: return
                    self.waiting_for_reversal=False;self.waiting_direction=0
                    self._do_enter(new_dir,brick)
                elif is_w:
                    if new_dir!=wd:
                        self.waiting_for_reversal=False;self.waiting_direction=0
                        self._do_enter(new_dir,brick)
                else: self._do_enter(new_dir,brick)
        except Exception as e: log.error(f"Brick err: {e}")
        finally: self._order_in_progress=False

    def _do_enter(self,direction,brick):
        inst=self.inst;mode=inst["trade_mode"];qty=inst["lot_size"]*inst["lots"]
        if inst["trade_type"]=="options":
            opt=resolve_option(self.inst_key,direction)
            if not opt: return
            sec_id=opt["security_id"];strike=opt["strike"];ot=opt["option_type"];ep=opt["last_price"];exp=opt["expiry"]
            if mode=="live":
                oid,fp=place_order(self.client_id,sec_id,"NSE_FNO",qty,"BUY")
                if not oid: return
                if fp>0: ep=fp
        elif inst["trade_type"]=="futures":
            sec_id=inst["security_id"];strike=0;ot="FUT";ep=brick.close;exp=""
            if mode=="live":
                oid,fp=place_order(self.client_id,sec_id,inst["exch_for_ws"],qty,"BUY" if direction==1 else "SELL")
                if not oid: return
                if fp>0: ep=fp
        else: return
        tp=inst.get("target_points",0)
        tgt_p=(brick.close+tp) if direction==1 and tp>0 else (brick.close-tp) if direction==-1 and tp>0 else 0.0
        trade=Trade(instrument_key=self.inst_key,direction=direction,option_type=ot,security_id=sec_id,strike=strike,entry_price=ep,entry_time=brick.time,qty=qty,expiry=exp,target_price=tgt_p)
        self.current_trade=trade;self.trade_count+=1
        if self._ws_sub:
            exch="NSE_FNO" if inst["trade_type"]=="options" else inst["exch_for_ws"]
            self._ws_sub(sec_id,exch,self.inst_key)
        sm=inst.get("strike_mode","ITM")
        self._notify("entry",{"direction":direction,"type":ot,"strike":strike,"price":ep,"target":tgt_p,"qty":qty,"mode":sm})

    def _do_exit(self,brick,reason):
        t=self.current_trade
        if not t or not t.is_open: return
        ep,_=self._place_exit(t,brick.close)
        pp=(ep-t.entry_price) if t.direction==1 else (t.entry_price-ep)
        t.exit_price=ep;t.exit_time=now_ist();t.pnl=pp*t.qty;t.is_open=False;t.exit_reason=reason
        self.total_pnl+=t.pnl;self.trade_history.append(t)
        if self._ws_unsub:
            exch="NSE_FNO" if self.inst["trade_type"]=="options" else self.inst["exch_for_ws"]
            self._ws_unsub(t.security_id,exch)
        self._notify("exit",{"reason":reason,"pnl":t.pnl,"total":self.total_pnl})

    def _place_exit(self,t,fallback):
        inst=self.inst;mode=inst["trade_mode"]
        ep=t.current_ltp if t.current_ltp>0 else fallback
        if mode=="live":
            if inst["trade_type"]=="options":
                _,fp=place_order(self.client_id,t.security_id,"NSE_FNO",t.qty,"SELL")
            else:
                _,fp=place_order(self.client_id,t.security_id,inst["exch_for_ws"],t.qty,"SELL" if t.direction==1 else "BUY")
            if fp>0: ep=fp
        return ep,""

    def squareoff(self):
        with self.order_lock:
            t=self.current_trade
            if not t or not t.is_open: self.squaredoff=True;return
            ep,_=self._place_exit(t,t.entry_price)
            pp=(ep-t.entry_price) if t.direction==1 else (t.entry_price-ep)
            t.exit_price=ep;t.exit_time=now_ist();t.exit_reason="SQUAREOFF";t.pnl=pp*t.qty;t.is_open=False
            self.total_pnl+=t.pnl;self.trade_history.append(t);self.squaredoff=True
            self._notify("squareoff",{"pnl":t.pnl,"total":self.total_pnl})

    def get_unrealized_pnl(self):
        t=self.current_trade
        if not t or not t.is_open or t.current_ltp<=0: return 0.0
        if self.inst["trade_type"]=="options": return (t.current_ltp-t.entry_price)*t.qty
        return ((t.current_ltp-t.entry_price) if t.direction==1 else (t.entry_price-t.current_ltp))*t.qty
