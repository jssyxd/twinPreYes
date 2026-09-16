#!/bin/bash
python3 - <<'PY'
import json, os, subprocess, sys
from decimal import Decimal
def L(p):
    try: return json.load(open(p))
    except Exception: return {}

def leg_summary(pos):
    out=[]
    for l in pos.get("legs",[]):
        if float(l.get("shares") or 0)>0 or float(l.get("cost_usdc") or 0)>0:
            out.append((l.get("leg"), l.get("shares"), l.get("avg_price"), l.get("cost_usdc"),
                        l.get("closed_by") or ("WON" if l.get("leg_won") else ("LOST" if l.get("settled") else "OPEN")),
                        l.get("close_proceeds_usdc"), l.get("payout_credit_usdc"), l.get("bid_at_close")))
    return out

def track(name, d):
    st=L(d+"/data/yes2re_state.json"); h=L(d+"/data/yes2re_health.json")
    pos=st.get("positions",{})
    print(f"\n===== {name} =====")
    print(f"health: mode={h.get('mode')} ts={h.get('ts_utc')} capital_initial={h.get('capital_initial_usdc')} remaining={h.get('remaining_capital_usdc')} entry={h.get('entry_count')} open={h.get('open_positions')} {h.get('position_i18n_keys')}")
    realized=Decimal(0); open_cost=Decimal(0); settled_cost=Decimal(0); pay=Decimal(0); nopen=0; nset=0
    for k,p in sorted(pos.items()):
        legs=leg_summary(p)
        if not legs: continue
        r=Decimal(0); oc=Decimal(0)
        for (_,sh,avg,cost,cb,cp,payout,_b) in legs:
            c=Decimal(str(cost or 0)); sh_=Decimal(str(sh or 0))
            if cb in ("WON","LOST"):
                if cb=="WON": pay+=Decimal(str(payout or 0)); r+=Decimal(str(payout or 0))-c
                else: sat=Decimal(0); r-=c
                settled_cost+=c; nset+=1
            elif cb and cb.startswith("early") or cb in ("take_profit","paper_close") or cb=="early_stop_loss":
                pr=Decimal(str(cp or 0)); pay+=pr; r+=pr-c; settled_cost+=c; nset+=1
            else:
                oc+=c; nopen+=1
        realized+=r; open_cost+=oc
        print(f"  {k:34s} ch={p.get('entry_channel')} legs={len(legs)} realized={r:+.4f} open_cost={oc:.4f}")
        for (lg,sh,avg,cost,cb,cp,payout,bid) in legs:
            print(f"      {lg:13s} sh={sh:>8} avg={avg:>6} cost={cost:>9} -> {cb:16s} close={cp or '-'} payout={payout or '-'} bid@close={bid or '-'}")
    rp=Decimal(str(st.get("realized_pnl") or 0))
    print(f"  -- positions={len(pos)} settled_legs={nset} open_legs={nopen} settled_cost={settled_cost:.4f} payout={pay:.4f} leg_realized={realized:+.4f} state.realized_pnl(TP)={rp:+.4f} TOTAL_reported={realized+rp:+.4f} open_cost={open_cost:.4f}")
    le=d+"/data/live_events.jsonl"
    if os.path.exists(le):
        print("  live_events.jsonl:", subprocess.run(["wc","-l",le],capture_output=True,text=True).stdout.strip())
    return {"mode":h.get("mode"),"capital_initial":h.get("capital_initial_usdc"),"remaining":h.get("remaining_capital_usdc"),
            "entry_count":h.get("entry_count"),"open_positions":h.get("open_positions"),
            "leg_realized":str(realized),"realized_pnl_field":str(rp),"total_realized":str(realized+rp),"open_cost":str(open_cost)}

res={}
res["paper"]=track("PAPER 模拟盘 /root/weatherbotPreYes0910","/root/weatherbotPreYes0910")
res["live"]=track("LIVE 实盘 /root/weatherbotLive0915","/root/weatherbotLive0915")
# ---- live chain account: project reconcile if present, else independent data-api + RPC ----
import urllib.request
LIVE_DIR="/root/weatherbotLive0915"
res["live_account"]={}
acc=None
if os.path.exists(LIVE_DIR+"/live/reconcile.py"):
    try:
        out=subprocess.run(["/root/preyes-live/.venv/bin/python",LIVE_DIR+"/live/reconcile.py","--json","--out","/tmp/recon_cron.json"],capture_output=True,text=True,cwd=LIVE_DIR,timeout=150)
        acc=json.loads(out.stdout or "{}")
    except Exception as e:
        print("\nreconcile ERR", type(e).__name__, e)
else:
    print("\n!! LIVE_DIR missing project files (decommissioned?) - falling back to independent chain check")
if acc:
    res["live_account"]={"usdc_balance":acc.get("usdc_balance"),"open_orders":acc.get("open_orders"),
                         "positions":acc.get("positions"),"gate":(acc.get("risk_gate") or {}).get("detail"),
                         "limits":acc.get("limits")}
    print("\n===== 链上账户(project reconcile) =====", json.dumps(res["live_account"],ensure_ascii=False,indent=1))
# independent check: read-only chain read via project venv + paper dir live package
try:
    print("--- 独立链上核对(project venv,只读) ---")
    out3=subprocess.run(["/root/preyes-live/.venv/bin/python","/root/scripts/chain_read.py"],capture_output=True,text=True,timeout=120)
    a3=json.loads((out3.stdout or "").strip().splitlines()[-1] if (out3.stdout or "").strip() else "{}")
    print(f"  cash={a3.get('cash')} USDC  orders={a3.get('open_orders')}  positions={a3.get('positions_count')}  positions_value={a3.get('positions_value')}  equity={a3.get('equity')}")
    for p in (a3.get("positions") or []):
        print("   -", str(p.get("title"))[:70], "| sh", p.get("size"), "cur", p.get("cur"), "val", p.get("value"), "redeemable", p.get("redeemable"))
    res["live_account"]["independent"]=a3
except Exception as e3:
    print("  independent chain check ERR", type(e3).__name__, e3)
open("/tmp/dual_track_data.json","w").write(json.dumps(res,ensure_ascii=False,indent=1))
print("\n[saved] /tmp/dual_track_data.json")
PY
