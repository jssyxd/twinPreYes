#!/usr/bin/env python3
"""Read-only CLOB account read using the PAPER dir's live package (independent of the live deploy)."""
import json, sys
sys.path.insert(0, "/root/weatherbotPreYes0910")
from live import creds, v2_transport
env = creds.load_env_file("/root/weatherbotPreYes0910/.env")
c = creds.validate_creds(env)
client = v2_transport.build_client(c)
acc = v2_transport.read_account(client, address=c["funder_address"])
pos = acc.get("positions") or []
pv = sum(float(p.get("currentValue") or 0) for p in pos)
cash = float(acc.get("usdc_balance") or 0)
out = {
    "cash": round(cash, 6),
    "open_orders": acc.get("open_orders"),
    "positions_count": len(pos),
    "positions_value": round(pv, 4),
    "equity": round(cash + pv, 4),
    "positions": [
        {"title": p.get("title"), "size": p.get("size"), "avg": p.get("avgPrice"),
         "cur": p.get("curPrice"), "value": p.get("currentValue"),
         "redeemable": p.get("redeemable")} for p in pos
    ],
}
print(json.dumps(out, ensure_ascii=False))
