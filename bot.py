import os,time,math,logging,requests,ccxt

logging.basicConfig(level=logging.INFO,format='%(asctime)s | V7 | %(levelname)s | %(message)s')

SYMBOLS=os.getenv('SYMBOLS','BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT').split(',')
POLL_SECONDS=int(os.getenv('POLL_SECONDS','60'))
LEVERAGE=int(os.getenv('LEVERAGE','30'))
TP1_PCT=float(os.getenv('TP1_PCT','0.010'))
TP2_PCT=float(os.getenv('TP2_PCT','0.011'))
MIN_RR=float(os.getenv('MIN_RR','1.5'))
COOLDOWN_SECONDS=int(os.getenv('COOLDOWN_SECONDS','5400'))
ZONE_LOOKBACK=int(os.getenv('ZONE_LOOKBACK','80'))
MAX_ZONE_WIDTH_PCT=float(os.getenv('MAX_ZONE_WIDTH_PCT','0.012'))
TELEGRAM_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','')

exchange=ccxt.mexc({'enableRateLimit':True,'timeout':15000,'options':{'defaultType':'swap'}})

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

def fetch(s,tf,limit=100): return exchange.fetch_ohlcv(s,timeframe=tf,limit=limit)

def build_10m(d5):
    out=[]
    for i in range(0,len(d5)-1,2):
        a=d5[i:i+2]
        if len(a)<2: continue
        out.append([a[0][0],a[0][1],max(x[2] for x in a),min(x[3] for x in a),a[-1][4],sum(x[5] for x in a)])
    return out

def bias(d):
    c=closes(d)
    if len(c)<55:return 'NEUTRAL'
    e20,e50,last=ema(c[:-1],20),ema(c[:-1],50),c[-2]
    if last>e20>e50:return 'LONG'
    if last<e20<e50:return 'SHORT'
    return 'NEUTRAL'

def structure(d):
    if len(d)<15:return 'RANGE'
    c,h,l=closes(d),highs(d),lows(d); last=c[-2]
    hi,lo=max(h[-10:-2]),min(l[-10:-2])
    if last>hi:return 'BULL_BOS'
    if last<lo:return 'BEAR_BOS'
    hi2,lo2=max(h[-5:-2]),min(l[-5:-2])
    if last>hi2:return 'BULL_CHOCH'
    if last<lo2:return 'BEAR_CHOCH'
    return 'RANGE'

def htf_imbalance(d,side):
    h,l=highs(d),lows(d)
    end=len(d)-2
    for i in range(max(2,end-ZONE_LOOKBACK),end):
        if side=='LONG' and l[i] > h[i-2]:
            return ('HTF IMBALANCE',h[i-2],l[i])
        if side=='SHORT' and h[i] < l[i-2]:
            return ('HTF IMBALANCE',h[i],l[i-2])
    return None

def htf_order_block(d,side):
    o,c,h,l=opens(d),closes(d),highs(d),lows(d)
    end=len(d)-2
    for i in range(end-3,max(3,end-ZONE_LOOKBACK),-1):
        body=abs(c[i]-o[i]); rng=h[i]-l[i]
        if rng<=0 or body/rng<0.35: continue
        if side=='LONG' and c[i]<o[i] and c[i+1]>h[i] and c[i+2]>=c[i+1]:
            return ('HTF ORDER BLOCK',l[i],h[i])
        if side=='SHORT' and c[i]>o[i] and c[i+1]<l[i] and c[i+2]<=c[i+1]:
            return ('HTF ORDER BLOCK',l[i],h[i])
    return None

def zone_touch(d,zone,side):
    if not zone:return False
    _,zl,zh=zone
    h,l,c=highs(d),lows(d),closes(d)
    for i in range(max(0,len(d)-8),len(d)-1):
        if l[i]<=zh and h[i]>=zl:return True
    return False

def sweep_around_zone(d,zone,side):
    if not zone or len(d)<12:return False,None
    _,zl,zh=zone; h,l,c=highs(d),lows(d),closes(d)
    start=max(2,len(d)-36); end=len(d)-2
    for i in range(end,start,-1):
        if side=='LONG' and l[i]<zl and c[i]>zl:
            return True,'SSL'
        if side=='SHORT' and h[i]>zh and c[i]<zh:
            return True,'BSL'
    return False,None

def choch_after(d,side):
    if len(d)<15:return False
    c,h,l=closes(d),highs(d),lows(d); end=len(d)-2
    for i in range(max(3,end-12),end):
        if side=='LONG' and c[i]>max(h[max(0,i-5):i]): return True
        if side=='SHORT' and c[i]<min(l[max(0,i-5):i]): return True
    return False

def fvg(d,side):
    h,l=highs(d),lows(d); end=len(d)-2
    for i in range(max(2,end-8),end):
        if side=='LONG' and l[i]>h[i-2]: return True,(h[i-2],l[i])
        if side=='SHORT' and h[i]<l[i-2]: return True,(h[i],l[i-2])
    return False,None

def poi_reaction(d,zone,side):
    if not zone:return False
    _,zl,zh=zone; o,c,h,l=opens(d),closes(d),highs(d),lows(d); i=len(d)-2
    if side=='LONG': return l[i]<=zh and c[i]>o[i] and c[i]>=zl
    return h[i]>=zl and c[i]<o[i] and c[i]<=zh

def trigger5(d,side):
    if len(d)<6:return False
    o,c,h,l=opens(d),closes(d),highs(d),lows(d); i=len(d)-2
    if side=='LONG': return c[i]>o[i] and c[i]>h[i-1] and l[i]<=l[i-1]
    return c[i]<o[i] and c[i]<l[i-1] and h[i]>=h[i-1]

def entry_zone(price,zone,fvg_zone,side):
    z=fvg_zone or (zone[1],zone[2])
    zl,zh=z
    maxw=price*MAX_ZONE_WIDTH_PCT
    if zh-zl>maxw:
        mid=(zl+zh)/2; zl,zh=mid-maxw/2,mid+maxw/2
    # Keep zone near market; otherwise use a clipped local reaction zone around price.
    if price<zl or price>zh:
        half=maxw/2
        zl,zh=price-half,price+half
    return min(zl,zh),max(zl,zh)

def signal(s):
    try:
        logging.info('%s | SCAN START',s)
        d1,d15,d5=fetch(s,'1h'),fetch(s,'15m'),fetch(s,'5m')
        d10=build_10m(d5)
        b,st=bias(d1),structure(d15)
        logging.info('%s | CONTEXT | 1H=%s | 15m=%s',s,b,st)
        if b=='LONG' and st not in ('BULL_BOS','BULL_CHOCH','RANGE'): return None
        if b=='SHORT' and st not in ('BEAR_BOS','BEAR_CHOCH','RANGE'): return None
        if b not in ('LONG','SHORT'): return None
        side=b
        zone=htf_imbalance(d1,side) or htf_order_block(d1,side)
        logging.info('%s | HTF ZONE | %s',s,zone[0] if zone else 'NONE')
        if not zone or not zone_touch(d1,zone,side): return None
        sw,liq=sweep_around_zone(d15,zone,side)
        logging.info('%s | LIQUIDITY | %s',s,liq or 'NONE')
        if not sw:return None
        ch=choch_after(d15,side)
        logging.info('%s | CHoCH/BOS | %s',s,ch)
        if not ch:return None
        fv,fvz=fvg(d10,side); poi=poi_reaction(d10,zone,side)
        logging.info('%s | 10m FVG=%s | POI=%s',s,fv,poi)
        if not (fv or poi):return None
        conf=trigger5(d5,side)
        logging.info('%s | 5m CONFIRM=%s',s,conf)
        if not conf:return None
        price=closes(d5)[-2]; a=atr(d5)
        if not math.isfinite(a) or a<=0:return None
        el,eh=entry_zone(price,zone,fvz,side)
        h,l=highs(d5),lows(d5)
        sweep_ext=min(l[-20:]) if side=='LONG' else max(h[-20:])
        sl=sweep_ext-0.20*a if side=='LONG' else sweep_ext+0.20*a
        if side=='LONG':
            tp1=price*(1+TP1_PCT); tp2=price*(1+TP2_PCT); risk=max(price-sl,1e-12)
            rr=(tp1-price)/risk
        else:
            tp1=price*(1-TP1_PCT); tp2=price*(1-TP2_PCT); risk=max(sl-price,1e-12)
            rr=(price-tp1)/risk
        if rr<MIN_RR:
            logging.info('%s | REJECT | RR %.2f < %.2f',s,rr,MIN_RR); return None
        return side,s,el,eh,sl,tp1,tp2,rr,zone[0],liq,('FVG' if fv else 'POI')
    except Exception as e:
        logging.exception('%s | ERROR | %s',s,e); return None

def send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning('Telegram variables missing'); return False
    try:
        r=requests.post('https://api.telegram.org/bot'+TELEGRAM_TOKEN+'/sendMessage',json={'chat_id':TELEGRAM_CHAT_ID,'text':msg},timeout=8)
        return r.ok
    except Exception as e:
        logging.warning('Telegram error: %s',e); return False

def main():
    logging.info('SCALP V7 started | HTF ZONE + SWEEP + CHoCH + POI/FVG')
    logging.info('V7 symbols=%s | poll=%ss | leverage=%sx | TP1=%.2f%% | TP2=%.2f%%',','.join(SYMBOLS),POLL_SECONDS,LEVERAGE,TP1_PCT*100,TP2_PCT*100)
    last={}
    while True:
        start=time.time()
        logging.info('========== V7 SCAN CYCLE START ==========')
        for s in SYMBOLS:
            s=s.strip()
            if not s: continue
            x=signal(s)
            if not x or time.time()-last.get(s,0)<COOLDOWN_SECONDS: continue
            side,sym,el,eh,sl,tp1,tp2,rr,zone,liq,poi=x
            icon='🟢 LONG' if side=='LONG' else '🔴 SHORT'
            msg=(f'{icon}\n\n{sym} Futures\n\n'
                 f'Entry: {el:.8g} – {eh:.8g}\n'
                 f'TP1: {tp1:.8g} (1.00%)\nTP2: {tp2:.8g} (1.10%)\n'
                 f'SL: {sl:.8g}\nLeverage: {LEVERAGE}x\n\n'
                 f'HTF Zone: {zone}\nLiquidity: {liq} sweep\nStructure: CHoCH/BOS\nPOI: {poi}\n\nV7')
            if send(msg):
                last[s]=time.time(); logging.info('%s | SIGNAL SENT | %s',s,side)
        logging.info('========== V7 SCAN CYCLE COMPLETE | %.1fs | sleeping %ss ==========',time.time()-start,POLL_SECONDS)
        time.sleep(POLL_SECONDS)

if __name__=='__main__': main()
