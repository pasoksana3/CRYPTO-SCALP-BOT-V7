import os, time, math, logging
import requests
import ccxt

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | V7.1 | %(levelname)s | %(message)s")

SYMBOLS=[x.strip() for x in os.getenv(
    "SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT"
).split(",") if x.strip()]
POLL_SECONDS=int(os.getenv("POLL_SECONDS","60"))
LEVERAGE=int(os.getenv("LEVERAGE","30"))
TP1_PCT=float(os.getenv("TP1_PCT","0.010"))
TP2_PCT=float(os.getenv("TP2_PCT","0.011"))
MIN_RR=float(os.getenv("MIN_RR","1.5"))
COOLDOWN_SECONDS=int(os.getenv("COOLDOWN_SECONDS","5400"))
ZONE_LOOKBACK=int(os.getenv("ZONE_LOOKBACK","80"))
MAX_ZONE_WIDTH_PCT=float(os.getenv("MAX_ZONE_WIDTH_PCT","0.012"))
MAX_ZONE_DISTANCE_PCT=float(os.getenv("MAX_ZONE_DISTANCE_PCT","0.06"))
TELEGRAM_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","")

exchange=ccxt.mexc({
    "enableRateLimit":True,
    "timeout":15000,
    "options":{"defaultType":"swap"}
})

def closes(r): return [float(x[4]) for x in r]
def highs(r): return [float(x[2]) for x in r]
def lows(r): return [float(x[3]) for x in r]
def opens(r): return [float(x[1]) for x in r]

def ema(v,p):
    if not v:return 0.0
    k=2/(p+1); e=v[0]
    for x in v[1:]: e=x*k+e*(1-k)
    return e

def atr(r,p=14):
    if len(r)<p+2:return 0.0
    tr=[]
    for i in range(1,len(r)):
        h,l,pc=float(r[i][2]),float(r[i][3]),float(r[i-1][4])
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(tr[-p:])/p

def fetch(s,tf,limit=120):
    return exchange.fetch_ohlcv(s,timeframe=tf,limit=limit)

def build_10m(d5):
    if len(d5)<4:return []
    closed=d5[:-1]
    out=[]
    for i in range(0,len(closed)-1,2):
        a=closed[i:i+2]
        if len(a)<2:continue
        out.append([a[0][0],a[0][1],max(x[2] for x in a),
                    min(x[3] for x in a),a[-1][4],sum(x[5] for x in a)])
    return out

def bias(d):
    c=closes(d)
    if len(c)<55:return "NEUTRAL"
    c=c[:-1]
    last=c[-1]; e20,e50=ema(c,20),ema(c,50)
    if last>e20>e50:return "LONG"
    if last<e20<e50:return "SHORT"
    return "NEUTRAL"

def structure(d):
    if len(d)<15:return "RANGE"
    c,h,l=closes(d),highs(d),lows(d); last=c[-2]
    if last>max(h[-10:-2]):return "BULL_BOS"
    if last<min(l[-10:-2]):return "BEAR_BOS"
    if last>max(h[-5:-2]):return "BULL_CHOCH"
    if last<min(l[-5:-2]):return "BEAR_CHOCH"
    return "RANGE"

def htf_zones(d,side):
    h,l,o,c=highs(d),lows(d),opens(d),closes(d)
    end=len(d)-2; start=max(2,end-ZONE_LOOKBACK); zones=[]
    for i in range(end-1,start-1,-1):
        if side=="LONG" and l[i]>h[i-2]:
            zones.append(("HTF IMBALANCE",h[i-2],l[i],i))
        elif side=="SHORT" and h[i]<l[i-2]:
            zones.append(("HTF IMBALANCE",h[i],l[i-2],i))
    for i in range(end-3,max(3,end-ZONE_LOOKBACK),-1):
        body=abs(c[i]-o[i]); rng=h[i]-l[i]
        if rng<=0 or body/rng<0.35:continue
        if side=="LONG" and c[i]<o[i] and c[i+1]>h[i] and c[i+2]>=c[i+1]:
            zones.append(("HTF ORDER BLOCK",l[i],h[i],i))
        elif side=="SHORT" and c[i]>o[i] and c[i+1]<l[i] and c[i+2]<=c[i+1]:
            zones.append(("HTF ORDER BLOCK",l[i],h[i],i))
    return zones

def choose_zone(d,side):
    zones=htf_zones(d,side)
    if not zones:return None
    price=closes(d)[-2]; candidates=[]
    for name,zl,zh,idx in zones:
        if zl>zh:zl,zh=zh,zl
        dist=0 if zl<=price<=zh else min(abs(price-zl),abs(price-zh))/max(price,1e-12)
        candidates.append((dist,-idx,(name,zl,zh)))
    near=[x for x in candidates if x[0]<=MAX_ZONE_DISTANCE_PCT]
    pool=near or candidates
    pool.sort()
    return pool[0][2]

def sweep_around_zone(d,zone,side):
    if not zone or len(d)<12:return False,None
    _,zl,zh=zone; h,l,c=highs(d),lows(d),closes(d)
    start=max(2,len(d)-36); end=len(d)-2
    for i in range(end,start-1,-1):
        if side=="LONG" and l[i]<zl and c[i]>zl:return True,"SSL"
        if side=="SHORT" and h[i]>zh and c[i]<zh:return True,"BSL"
    return False,None

def choch_after(d,side):
    if len(d)<15:return False
    c,h,l=closes(d),highs(d),lows(d); end=len(d)-2
    for i in range(max(3,end-12),end+1):
        if side=="LONG" and c[i]>max(h[max(0,i-5):i]):return True
        if side=="SHORT" and c[i]<min(l[max(0,i-5):i]):return True
    return False

def fvg(d,side):
    if len(d)<5:return False,None
    h,l=highs(d),lows(d); end=len(d)-2
    for i in range(max(2,end-8),end+1):
        if side=="LONG" and l[i]>h[i-2]:return True,(h[i-2],l[i])
        if side=="SHORT" and h[i]<l[i-2]:return True,(h[i],l[i-2])
    return False,None

def poi_reaction(d,zone,side):
    if not zone or len(d)<3:return False
    _,zl,zh=zone; o,c,h,l=opens(d),closes(d),highs(d),lows(d); i=len(d)-2
    if side=="LONG":return l[i]<=zh and c[i]>o[i] and c[i]>=zl
    return h[i]>=zl and c[i]<o[i] and c[i]<=zh

def trigger5(d,side):
    if len(d)<7:return False
    o,c,h,l=opens(d),closes(d),highs(d),lows(d); i=len(d)-2
    if side=="LONG":return c[i]>o[i] and c[i]>h[i-1] and l[i]<=l[i-1]
    return c[i]<o[i] and c[i]<l[i-1] and h[i]>=h[i-1]

def entry_zone(price,zone,fvg_zone):
    z=fvg_zone or (zone[1],zone[2]); zl,zh=sorted(z)
    maxw=price*MAX_ZONE_WIDTH_PCT
    if zh-zl>maxw:
        mid=(zl+zh)/2; zl,zh=mid-maxw/2,mid+maxw/2
    if price<zl or price>zh:
        half=maxw/2; zl,zh=price-half,price+half
    return min(zl,zh),max(zl,zh)

def evaluate(symbol,d1,d15,d10,d5,side):
    st=structure(d15)
    if side=="LONG" and st not in ("BULL_BOS","BULL_CHOCH","RANGE"):
        return None,f"15m={st} blocks LONG"
    if side=="SHORT" and st not in ("BEAR_BOS","BEAR_CHOCH","RANGE"):
        return None,f"15m={st} blocks SHORT"

    zone=choose_zone(d1,side)
    logging.info("%s | %s | HTF ZONE=%s",symbol,side,zone[0] if zone else "NONE")
    if not zone:return None,"no HTF zone"

    sw,liq=sweep_around_zone(d15,zone,side)
    logging.info("%s | %s | LIQUIDITY=%s",symbol,side,liq or "NONE")
    if not sw:return None,"no liquidity sweep"

    ch=choch_after(d15,side)
    logging.info("%s | %s | CHoCH/BOS=%s",symbol,side,ch)
    if not ch:return None,"no CHoCH/BOS"

    fv,fvz=fvg(d10,side); poi=poi_reaction(d10,zone,side)
    logging.info("%s | %s | 10m FVG=%s | POI=%s",symbol,side,fv,poi)
    if not (fv or poi):return None,"no 10m FVG/POI"

    conf=trigger5(d5,side)
    logging.info("%s | %s | 5m CONFIRM=%s",symbol,side,conf)
    if not conf:return None,"no 5m trigger"

    price=closes(d5)[-2]; a=atr(d5)
    if not math.isfinite(a) or a<=0:return None,"invalid ATR"
    el,eh=entry_zone(price,zone,fvz)
    h,l=highs(d5),lows(d5)
    ext=min(l[-20:]) if side=="LONG" else max(h[-20:])
    sl=ext-0.20*a if side=="LONG" else ext+0.20*a

    if side=="LONG":
        tp1,tp2=price*(1+TP1_PCT),price*(1+TP2_PCT)
        risk=max(price-sl,1e-12); rr=(tp1-price)/risk
    else:
        tp1,tp2=price*(1-TP1_PCT),price*(1-TP2_PCT)
        risk=max(sl-price,1e-12); rr=(price-tp1)/risk

    if rr<MIN_RR:
        logging.info("%s | %s | REJECT RR %.2f < %.2f",symbol,side,rr,MIN_RR)
        return None,f"RR {rr:.2f} < {MIN_RR:.2f}"

    return (side,symbol,el,eh,sl,tp1,tp2,rr,zone[0],liq,
            "FVG" if fv else "POI",st), "READY"

def signal(symbol):
    try:
        logging.info("%s | SCAN START",symbol)
        d1=fetch(symbol,"1h"); d15=fetch(symbol,"15m"); d5=fetch(symbol,"5m")
        d10=build_10m(d5)
        b= bias(d1); st=structure(d15)
        logging.info("%s | CONTEXT | 1H=%s | 15m=%s",symbol,b,st)

        sides=[b] if b in ("LONG","SHORT") else ["LONG","SHORT"]
        for side in sides:
            result,reason=evaluate(symbol,d1,d15,d10,d5,side)
            if result:return result

        if b in ("LONG","SHORT"):
            opposite="SHORT" if b=="LONG" else "LONG"
            result,reason=evaluate(symbol,d1,d15,d10,d5,opposite)
            if result:return result
            logging.info("%s | FINAL=NO SIGNAL | primary and opposite rejected",symbol)
        else:
            logging.info("%s | FINAL=NO SIGNAL | both directions rejected",symbol)
        return None
    except Exception as e:
        logging.exception("%s | ERROR | %s",symbol,e)
        return None

def send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram variables missing")
        return False
    try:
        r=requests.post(
            "https://api.telegram.org/bot"+TELEGRAM_TOKEN+"/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=8)
        if not r.ok:logging.warning("Telegram HTTP %s: %s",r.status_code,r.text[:300])
        return r.ok
    except Exception as e:
        logging.warning("Telegram error: %s",e); return False

def format_signal(x):
    side,sym,el,eh,sl,tp1,tp2,rr,zone,liq,poi,st=x
    icon="🟢 LONG" if side=="LONG" else "🔴 SHORT"
    return (f"{icon}\n\n{sym} Futures\n\n"
            f"Entry: {el:.8g} – {eh:.8g}\n"
            f"TP1: {tp1:.8g} ({TP1_PCT*100:.2f}%)\n"
            f"TP2: {tp2:.8g} ({TP2_PCT*100:.2f}%)\n"
            f"SL: {sl:.8g}\nRR: {rr:.2f}\nLeverage: {LEVERAGE}x\n\n"
            f"HTF Zone: {zone}\nLiquidity: {liq} sweep\n"
            f"Structure: {st}\nPOI: {poi}\n\nV7.1")

def main():
    logging.info("SCALP V7.1 started | 1H -> 15m -> 10m -> 5m")
    logging.info("symbols=%s | poll=%ss | leverage=%sx | TP1=%.2f%% | TP2=%.2f%%",
                 ",".join(SYMBOLS),POLL_SECONDS,LEVERAGE,TP1_PCT*100,TP2_PCT*100)
    last_sent={}
    while True:
        start=time.time()
        logging.info("========== V7.1 SCAN CYCLE START ==========")
        for symbol in SYMBOLS:
            x=signal(symbol)
            if x and time.time()-last_sent.get(symbol,0)>=COOLDOWN_SECONDS:
                if send(format_signal(x)):
                    last_sent[symbol]=time.time()
                    logging.info("%s | SIGNAL SENT | %s",symbol,x[0])
            elif x:
                logging.info("%s | SIGNAL BLOCKED BY COOLDOWN",symbol)
            time.sleep(0.25)
        elapsed=time.time()-start
        sleep_for=max(1,POLL_SECONDS-elapsed)
        logging.info("========== V7.1 SCAN CYCLE COMPLETE | %.1fs | sleeping %.1fs ==========",
                     elapsed,sleep_for)
        time.sleep(sleep_for)

if __name__=="__main__":
    main()
