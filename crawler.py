from __future__ import annotations

import hashlib, html, json, mimetypes, re, shutil, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
BLOG=CFG["blog_id"]
ARCH=ROOT/"archive"; POSTS=ARCH/"posts"; INDEX=ARCH/"index.json"
DOCS=ROOT/"docs"; STATE=ROOT/"state"
KST=timezone(timedelta(hours=9))
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
DEL_MARK=("삭제된 게시글입니다","삭제된 글입니다","존재하지 않는 게시글입니다","게시글이 삭제")
PRIVATE_MARK=("비공개 게시글입니다","비공개 포스트입니다","권한이 없습니다")

def now(): return datetime.now(KST)
def iso(): return now().isoformat(timespec="seconds")
def load(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default
def save(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")
def canonical(pid): return f"https://blog.naver.com/{BLOG}/{pid}"
def postview(pid): return f"https://blog.naver.com/PostView.naver?blogId={BLOG}&logNo={pid}&redirect=Dlog&widgetTypeCall=true&noTrackingCode=true&directAccess=false"
def pid_from(s):
    if not s:return None
    for pat in (r"[?&]logNo=(\d+)",rf"/{re.escape(BLOG)}/(\d+)(?:[/?#]|$)"):
        m=re.search(pat,s)
        if m:return m.group(1)
    return None

S=requests.Session()
S.headers.update({"User-Agent":UA,"Accept-Language":"ko-KR,ko;q=0.9,en;q=0.8"})

def get(url,referer=None,timeout=25):
    return S.get(url,headers={"Referer":referer} if referer else None,timeout=timeout)

def rss_candidates():
    r=get(f"https://rss.blog.naver.com/{BLOG}.xml"); r.raise_for_status()
    root=ET.fromstring(r.content); out={}
    for item in root.findall(".//item"):
        link=(item.findtext("link") or "").strip()
        pid=pid_from(link)
        if pid:
            out[pid]={"post_id":pid,"url":canonical(pid),"title":(item.findtext("title") or "").strip(),"published":(item.findtext("pubDate") or "").strip()}
    return out

def discover_history():
    out={}
    pages=int(CFG.get("initial_discovery_pages",30))
    delay=float(CFG.get("request_delay_seconds",.5))
    for page in range(1,pages+1):
        before=len(out)
        urls=[
          f"https://m.blog.naver.com/PostList.naver?blogId={BLOG}&categoryNo=0&listStyle=style1&currentPage={page}",
          f"https://blog.naver.com/PostTitleListAsync.naver?blogId={BLOG}&currentPage={page}&categoryNo=0&parentCategoryNo=0&countPerPage=30"
        ]
        for u in urls:
            try:
                t=html.unescape(get(u,canonical("")).text)
                ids=set(re.findall(r'[?&]logNo[=:"\']+(\d{6,})',t))
                ids.update(re.findall(rf'/{re.escape(BLOG)}/(\d{{6,}})(?:[/?#"\'<]|$)',t))
                ids.update(re.findall(r'"logNo"\s*:\s*"?(\d{6,})"?',t))
                for pid in ids:out.setdefault(pid,{"post_id":pid,"url":canonical(pid),"title":"","published":""})
            except requests.RequestException: pass
            time.sleep(delay)
        if page>=3 and len(out)==before: break
    return out

def backfill():
    out={}
    p=ROOT/"backfill_urls.txt"
    if not p.exists():return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#"):continue
        pid=pid_from(line) or (re.search(r"(\d{6,})",line).group(1) if re.search(r"(\d{6,})",line) else None)
        if pid:out[pid]={"post_id":pid,"url":canonical(pid),"title":"","published":""}
    return out

def deleted(text,status): return status in (404,410) or any(x in text for x in DEL_MARK)
def private(text): return any(x in text for x in PRIVATE_MARK)

def content_node(soup,pid):
    for sel in ("div.se-main-container",f"div#post-view{pid}","#postViewArea","div.post_ct","div.post-view"):
        n=soup.select_one(sel)
        if n:return n
    return None

def title_of(soup,fallback=""):
    for sel in ("div.se-title-text span","div.se-module-text.se-title-text","h3.se_textarea","h3.tit_h3","meta[property='og:title']","title"):
        n=soup.select_one(sel)
        if n:
            v=n.get("content","") if n.name=="meta" else n.get_text(" ",strip=True)
            if v:return v.strip()
    return fallback or "Untitled"

def img_src(img):
    for a in ("data-lazy-src","data-src","data-original","data-url","src"):
        v=img.get(a)
        if v:
            v=html.unescape(v.strip())
            if v.startswith("//"):v="https:"+v
            if v.startswith("http"):return v
    return None

def ext_for(url,ctype):
    e=Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}",e or ""):return e
    return mimetypes.guess_extension((ctype or "").split(";")[0].strip()) or ".bin"

def localize(node,pdir,referer):
    soup=BeautifulSoup(str(node),"html.parser")
    idir=pdir/"images"; idir.mkdir(parents=True,exist_ok=True)
    for tag in soup.find_all(["script","noscript"]):tag.decompose()
    for img in soup.find_all("img"):
        src=img_src(img)
        if not src:continue
        try:
            r=get(src,referer,30)
            if r.ok and r.content and (not r.headers.get("Content-Type") or r.headers.get("Content-Type","").startswith("image/")):
                name=hashlib.sha256(r.content).hexdigest()[:20]+ext_for(src,r.headers.get("Content-Type"))
                f=idir/name
                if not f.exists():f.write_bytes(r.content)
                img["src"]="images/"+name
                for a in ("data-lazy-src","data-src","data-original","data-url","srcset"):img.attrs.pop(a,None)
                img["loading"]="lazy"
        except requests.RequestException: pass
    for a in soup.find_all("a"):
        if a.get("href","").startswith("http"):
            a["target"]="_blank";a["rel"]="noopener noreferrer"
    return str(soup)

def archive_one(c,index):
    pid=c["post_id"]; pdir=POSTS/pid; pdir.mkdir(parents=True,exist_ok=True)
    mp=pdir/"metadata.json"; cp=pdir/"content.html"; old=load(mp,{})
    try:r=get(postview(pid),c["url"])
    except requests.RequestException:return "error"
    if deleted(r.text,r.status_code):
        if old and old.get("source_status")!="deleted":
            old["source_status"]="deleted";old["deleted_detected_at"]=iso();save(mp,old);index[pid]=old;return "deleted"
        return "unchanged"
    if private(r.text):
        if old and old.get("source_status")!="private_or_unavailable":
            old["source_status"]="private_or_unavailable";old["unavailable_detected_at"]=iso();save(mp,old);index[pid]=old;return "private"
        return "unchanged"
    soup=BeautifulSoup(r.text,"html.parser"); node=content_node(soup,pid)
    if not node:return "unparsed"
    title=title_of(soup,c.get("title","")); body=localize(node,pdir,c["url"])
    h=hashlib.sha256((title+"\n"+re.sub(r"\s+"," ",body)).encode()).hexdigest()
    if old.get("content_hash")==h and old.get("source_status")=="active":
        index[pid]=old;return "unchanged"
    if old and cp.exists() and old.get("content_hash")!=h:
        vd=pdir/"versions"/now().strftime("%Y%m%dT%H%M%S%z");vd.mkdir(parents=True,exist_ok=True)
        shutil.copy2(cp,vd/"content.html");shutil.copy2(mp,vd/"metadata.json")
    meta={
      "post_id":pid,"blog_id":BLOG,"title":title,"original_url":canonical(pid),
      "published":c.get("published") or old.get("published",""),
      "first_archived_at":old.get("first_archived_at",iso()),
      "last_archived_at":iso(),"source_status":"active","content_hash":h,
      "version_count":(int(old.get("version_count",0))+1 if old.get("content_hash")!=h else int(old.get("version_count",1)))
    }
    cp.write_text(body,encoding="utf-8");save(mp,meta);index[pid]=meta
    return "new" if not old else "updated"

def maintenance(index,seen):
    ids=sorted(index); n=int(CFG.get("deletion_checks_per_run",20))
    if not ids or n<=0:return
    sp=STATE/"cursor.json"; st=load(sp,{"cursor":0,"misses":{}})
    cur=int(st.get("cursor",0))%len(ids); misses=st.get("misses",{})
    for k in range(min(n,len(ids))):
        pid=ids[(cur+k)%len(ids)]
        if pid in seen:continue
        try:r=get(postview(pid),canonical(pid))
        except requests.RequestException:continue
        m=index[pid]; old_status=m.get("source_status","active")
        if deleted(r.text,r.status_code):
            misses[pid]=int(misses.get(pid,0))+1
            if misses[pid]>=2:m["source_status"]="deleted";m.setdefault("deleted_detected_at",iso())
        elif private(r.text):
            misses.pop(pid,None);m["source_status"]="private_or_unavailable";m.setdefault("unavailable_detected_at",iso())
        else:
            misses.pop(pid,None);m["source_status"]="active";m.pop("deleted_detected_at",None)
        if m.get("source_status")!=old_status:
            save(POSTS/pid/"metadata.json",m)
        index[pid]=m;time.sleep(float(CFG.get("request_delay_seconds",.5)))
    st={"cursor":(cur+min(n,len(ids)))%len(ids),"misses":misses};save(sp,st)

def esc(x):return html.escape(str(x or ""),quote=True)
def label(s):return {"active":"원본 확인됨","deleted":"원본 삭제 감지","private_or_unavailable":"원본 비공개/접근불가"}.get(s,s or "상태 미확인")
def shell(title,body,prefix=""):
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title><link rel="stylesheet" href="{prefix}assets/style.css"></head><body><header class="topbar"><a class="brand" href="{prefix}index.html">NAVER BLOG ARCHIVE</a></header><main class="container">{body}</main><footer class="footer">개인 아카이브 · 원본 출처는 각 게시물에 표시</footer></body></html>'''

def build_site(index):
    DOCS.mkdir(parents=True,exist_ok=True);(DOCS/"assets").mkdir(exist_ok=True);out=DOCS/"posts";out.mkdir(exist_ok=True)
    rows=sorted(index.values(),key=lambda m:m.get("published") or m.get("first_archived_at",""),reverse=True)
    cards=[]
    for m in rows:
        pid=m["post_id"];status=m.get("source_status","active")
        cards.append(f'<article class="card" data-search="{esc(m.get("title","")).lower()}"><div class="meta">{esc(m.get("published") or m.get("first_archived_at"))}</div><h2><a href="posts/{pid}/index.html">{esc(m.get("title"))}</a></h2><span class="status status-{esc(status)}">{esc(label(status))}</span></article>')
        src=POSTS/pid;dst=out/pid;dst.mkdir(parents=True,exist_ok=True)
        if (dst/"images").exists():shutil.rmtree(dst/"images")
        if (src/"images").exists():shutil.copytree(src/"images",dst/"images")
        body=(src/"content.html").read_text(encoding="utf-8") if (src/"content.html").exists() else ""
        notice='<div class="notice danger">원본 게시물의 삭제가 감지되었습니다. 아래 내용은 삭제 전에 저장된 사본입니다.</div>' if status=="deleted" else ('<div class="notice">현재 원본에 접근할 수 없습니다. 아래 내용은 이전에 저장된 사본입니다.</div>' if status=="private_or_unavailable" else "")
        page=f'<a class="back" href="../../index.html">← 전체 글</a><article class="post"><h1>{esc(m.get("title"))}</h1><div class="postmeta"><span>{esc(m.get("published") or m.get("first_archived_at"))}</span><span>{esc(label(status))}</span><span>보존 버전 {esc(m.get("version_count",1))}</span></div>{notice}<div class="source"><a href="{esc(m.get("original_url"))}" target="_blank" rel="noopener noreferrer">네이버 원본 열기 ↗</a></div><div class="content">{body}</div></article>'
        (dst/"index.html").write_text(shell(m.get("title","Archive"),page,"../../"),encoding="utf-8")
    active=sum(m.get("source_status")=="active" for m in rows);deleted_n=sum(m.get("source_status")=="deleted" for m in rows);priv=sum(m.get("source_status")=="private_or_unavailable" for m in rows)
    title=CFG.get("archive_title",BLOG+" Archive")
    home=f'<section class="hero"><p class="eyebrow">AUTOMATIC MIRROR + ARCHIVE</p><h1>{esc(title)}</h1><p>GitHub Actions가 보존하는 네이버 블로그 아카이브</p></section><section class="stats"><div><strong>{len(rows)}</strong><span>보존 글</span></div><div><strong>{active}</strong><span>원본 확인</span></div><div><strong>{deleted_n}</strong><span>삭제 감지</span></div><div><strong>{priv}</strong><span>접근불가</span></div></section><input id="search" type="search" placeholder="제목 검색"><section class="grid">{"".join(cards) if cards else "<p>아직 보존된 글이 없습니다.</p>"}</section><script src="assets/app.js"></script>'
    (DOCS/"index.html").write_text(shell(title,home),encoding="utf-8");(DOCS/".nojekyll").write_text("",encoding="utf-8")

def main():
    POSTS.mkdir(parents=True,exist_ok=True);STATE.mkdir(parents=True,exist_ok=True)
    index=load(INDEX,{})
    mode="fast" if "--fast" in sys.argv else ("discover" if "--discover" in sys.argv else "maintenance")
    c={}
    try:c.update(rss_candidates())
    except Exception as e:print("RSS warning:",e)
    if mode in ("discover","maintenance") and (mode=="discover" or not index):
        try:
            for k,v in discover_history().items():c.setdefault(k,v)
        except Exception as e:print("Discovery warning:",e)
    for k,v in backfill().items():c.setdefault(k,v)
    stats={}
    for i,x in enumerate(c.values(),1):
        result=archive_one(x,index);stats[result]=stats.get(result,0)+1
        print(f"[{i}/{len(c)}] {x['post_id']} {result}")
        time.sleep(float(CFG.get("request_delay_seconds",.5)))
    if mode=="maintenance":maintenance(index,set(c))
    save(INDEX,index);build_site(index)
    print("mode",mode,"stats",stats)

if __name__=="__main__":main()
