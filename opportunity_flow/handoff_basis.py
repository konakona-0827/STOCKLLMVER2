"""Track user-supplied manual average cost across partial SELLs and further handoffs."""
from datetime import datetime
from decimal import Decimal
import json


def remaining_manual_basis(handoffs,attempts,held):
    if not handoffs or handoffs[0]['previous_ai_qty']!=0 or held<=0:return None
    first=datetime.fromisoformat(handoffs[0]['transferred_at'])
    events=[(datetime.fromisoformat(h['transferred_at']),'HANDOFF',h) for h in handoffs]
    for attempt in attempts:
        at=datetime.fromisoformat(attempt['started_at'])
        if at<first:continue
        detail=json.loads(attempt['detail_json'] or '{}')
        qty=int(detail.get('filled_quantity') or 0)
        if qty>0:
            if attempt['side']=='BUY':return None  # Use the existing mixed-acquisition basis instead.
            events.append((at,'SELL',qty))
    qty=0;cost=Decimal(0)
    for _,kind,value in sorted(events,key=lambda e:e[0]):
        if kind=='HANDOFF':
            if qty!=value['previous_ai_qty']:return None
            unit=Decimal(str(value['unit_cost_twd']))
            if not unit.is_finite() or unit<=0:return None
            qty+=value['quantity'];cost+=unit*value['quantity']
        else:
            if value>qty or qty<=0:return None
            cost-=cost*value/qty;qty-=value
    return str(cost/qty) if qty==held and qty>0 else None
