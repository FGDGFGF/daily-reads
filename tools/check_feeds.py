# One-off: check every feed in tools/feeds.opml (works? last post? posts/day?) -> tools/feed-check.json
import json, re, datetime as dt, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
import requests
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
def pdate(s):
    s=(s or "").strip()
    try: d=parsedate_to_datetime(s)
    except Exception:
        try: d=dt.datetime.fromisoformat(s.replace("Z","+00:00"))
        except Exception: return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
def check(o):
    url=o["xmlUrl"]; r={"title":o["title"],"folder":o["folder"],"url":url}
    try:
        resp=requests.get(url,timeout=25,headers={"User-Agent":UA})
        r["status"]=resp.status_code
        root=ET.fromstring(resp.content)
        items=[e for e in root.iter() if e.tag.split("}")[-1] in ("item","entry")]
        dates=[]
        for e in items:
            for c in e:
                if c.tag.split("}")[-1] in ("pubDate","published","updated","date"):
                    d=pdate(c.text); 
                    if d: dates.append(d); break
        r["items"]=len(items)
        if dates:
            dates.sort(); now=dt.datetime.now(dt.timezone.utc)
            r["newest_days_ago"]=round((now-dates[-1]).total_seconds()/86400,1)
            span=max((dates[-1]-dates[0]).total_seconds()/86400,0.04)
            r["per_day"]=round(len(dates)/span,1) if len(dates)>1 else None
        r["final_url"]=resp.url
    except Exception as e:
        r["error"]=f"{type(e).__name__}: {str(e)[:120]}"
    return r
tree=ET.parse("tools/feeds.opml"); out=[]
for folder in tree.getroot().find("body"):
    for o in folder.iter("outline"):
        if o.get("xmlUrl"): out.append({"title":o.get("title"),"xmlUrl":o.get("xmlUrl"),"folder":folder.get("title")})
with ThreadPoolExecutor(12) as ex: res=list(ex.map(check,out))
json.dump(res,open("tools/feed-check.json","w"),indent=1,ensure_ascii=False)
print(len(res),"checked")
