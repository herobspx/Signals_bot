from __future__ import annotations
import os, uuid
from pathlib import Path
from typing import Any, Dict
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

W, H = 1536, 864
OUT_DIR = Path(os.environ.get('CARD_OUT_DIR', '/tmp/bamsignals_cards'))
OUT_DIR.mkdir(parents=True, exist_ok=True)
GREEN=(47,214,82); RED=(255,70,83); WHITE=(245,245,245); LINE=(120,120,120,120)

def _font(size:int,bold:bool=False):
    paths=[('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
           ('/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf')]
    for p in paths:
        try:
            if Path(p).exists(): return ImageFont.truetype(p,size)
        except Exception: pass
    return ImageFont.load_default()

def _money(v, default='--'):
    try:
        if v is None or v=='': return default
        return f'{float(v):.2f}'
    except Exception:
        return str(v) if v is not None else default

def _cover(img):
    return ImageOps.fit(img.convert('RGB'), (W,H), method=Image.Resampling.LANCZOS, centering=(0.50,0.45))

def _contract(trade:Dict[str,Any]):
    symbol=str(trade.get('symbol','SPXW')).upper()
    strike=str(trade.get('strike','3900')).replace('.0','')
    expiry=str(trade.get('expiry','08 Mar 24'))
    typ=str(trade.get('type','CALL')).upper().title()
    return f'{symbol} {strike} {expiry} {typ}'

def generate_trade_card(trade:Dict[str,Any], current_price:Any=None, status:str='OPEN')->str:
    bg_path=Path(__file__).with_name('card_bg.png')
    bg=_cover(Image.open(bg_path)).filter(ImageFilter.GaussianBlur(1.0)) if bg_path.exists() else Image.new('RGB',(W,H),'black')
    img=Image.alpha_composite(bg.convert('RGBA'), Image.new('RGBA',(W,H),(0,0,0,74)))
    v=Image.new('RGBA',(W,H),(0,0,0,0)); vd=ImageDraw.Draw(v)
    vd.rectangle((0,0,430,H), fill=(0,0,0,86)); vd.rectangle((1120,0,W,H), fill=(0,0,0,66))
    img=Image.alpha_composite(img,v); d=ImageDraw.Draw(img)
    f_contract=_font(38,False); f_price=_font(128,True); f_change=_font(48,True)
    f_label=_font(32,False); f_val=_font(39,True); f_small=_font(31,False); f_wm=_font(96,True)
    # arrow
    d.line([(94,155),(58,190),(94,225)], fill=WHITE, width=7, joint='curve')
    d.text((145,168), _contract(trade), font=f_contract, fill=WHITE)
    entry=float(trade.get('entry',0) or 0); cur=float(current_price if current_price is not None else trade.get('last_price',entry) or 0)
    diff=cur-entry if entry else 0.0; pct=(diff/entry*100) if entry else 0.0; color=GREEN if diff>=0 else RED; sign='+' if diff>=0 else ''
    d.text((88,350), _money(cur), font=f_price, fill=color)
    d.text((92,515), f'▲ {sign}{diff:.2f}  {sign}{pct:.2f}%', font=f_change, fill=color)
    wm='BAM | SPX'; box=d.textbbox((0,0),wm,font=f_wm); d.text(((W-(box[2]-box[0]))//2,410), wm, font=f_wm, fill=(255,255,255,34))
    # stats no outer frame
    bid=trade.get('bid', max(cur-0.05,0)); ask=trade.get('ask', cur+0.05)
    openp=trade.get('open', entry or cur); high=trade.get('high', max(entry or cur,cur)); mid=trade.get('mid', cur); vol=trade.get('volume','--')
    x0,y0=1190,150; cg=190; rg=185
    def cell(c,r,label,value,fill=WHITE,sub=None):
        x=x0+c*cg; y=y0+r*rg
        d.text((x,y),label,font=f_label,fill=WHITE)
        d.text((x,y+54),value,font=f_val,fill=fill)
        if sub is not None: d.text((x,y+105),str(sub),font=f_small,fill=WHITE)
    cell(0,0,'Bid',_money(bid),GREEN,trade.get('bid_size','33')); cell(1,0,'Ask',_money(ask),RED,trade.get('ask_size','40'))
    cell(0,1,'Open',_money(openp)); cell(1,1,'High',_money(high)); cell(0,2,'Mid',_money(mid)); cell(1,2,'Volume',str(vol))
    for y in (y0+128,y0+128+rg): d.line((x0-10,y,x0+cg*2+105,y),fill=LINE,width=2)
    for y in (y0-10,y0+rg-10,y0+rg*2-10): d.line((x0+cg-45,y,x0+cg-45,y+125),fill=LINE,width=2)
    out=OUT_DIR/f'card_{uuid.uuid4().hex}.jpg'; img.convert('RGB').save(out,quality=94,optimize=True)
    return str(out)
