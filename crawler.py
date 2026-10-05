from __future__ import annotations

import hashlib, html, json, mimetypes, re, shutil, subprocess, sys, time
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
BLOG_META_DIR=ARCH/"blog"; BLOG_META_PATH=BLOG_META_DIR/"metadata.json"
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


def js_value(text, keys):
    """Best-effort extractor for public values embedded in Naver page scripts."""
    for key in keys:
        patterns=[
            rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"',
            rf"'{re.escape(key)}'\s*:\s*'((?:\\.|[^'\\])*)'",
        ]
        for pat in patterns:
            m=re.search(pat,text,re.I)
            if m:
                raw=m.group(1)
                try:
                    return html.unescape(json.loads('"'+raw.replace('"','\\"')+'"')).strip()
                except Exception:
                    return html.unescape(raw.replace("\\/","/")).strip()
    return ""

def meta_content(soup, selectors):
    for sel in selectors:
        n=soup.select_one(sel)
        if n:
            v=(n.get("content") or n.get_text(" ",strip=True) or "").strip()
            if v:return v
    return ""

def normalize_blog_title(v):
    v=(v or "").strip()
    for suffix in (" : 네이버 블로그"," - 네이버 블로그"," | 네이버 블로그"," 네이버 블로그"):
        if v.endswith(suffix):v=v[:-len(suffix)].strip()
    return v

def extract_categories(soup, raw_text):
    found={}
    order=[]
    for a in soup.find_all("a",href=True):
        href=html.unescape(a.get("href",""))
        m=re.search(r"[?&]categoryNo=(\d+)",href)
        if not m:continue
        no=m.group(1)
        if no=="0":continue
        name=a.get_text(" ",strip=True)
        name=re.sub(r"\s+"," ",name).strip()
        if not name or len(name)>80:continue
        if no not in found:
            found[no]=name;order.append(no)

    # Script/JSON fallback.
    for m in re.finditer(r'"categoryNo"\s*:\s*"?(\d+)"?.{0,180}?"(?:categoryName|name)"\s*:\s*"((?:\\.|[^"\\])*)"',raw_text,re.S):
        no,name=m.group(1),html.unescape(m.group(2).replace("\\/","/")).strip()
        if no!="0" and name and no not in found:
            found[no]=name;order.append(no)

    return [{"category_no":no,"category_name":found[no]} for no in order]

def download_blog_profile(url, referer):
    if not url or not url.startswith(("http://","https://")):return ""
    try:
        r=get(url,referer,30)
        if not r.ok or not r.content:return ""
        ctype=r.headers.get("Content-Type","")
        if ctype and not ctype.startswith("image/"):return ""
        digest=hashlib.sha256(r.content).hexdigest()
        ext=ext_for(url,ctype)
        BLOG_META_DIR.mkdir(parents=True,exist_ok=True)
        target=BLOG_META_DIR/f"profile{ext}"
        current=next(iter(BLOG_META_DIR.glob("profile.*")),None)
        if current and current.read_bytes()==r.content:
            return current.name
        # Keep old profile versions before replacing.
        if current and current.exists():
            vd=BLOG_META_DIR/"profile_versions";vd.mkdir(exist_ok=True)
            shutil.copy2(current,vd/f"{now().strftime('%Y%m%dT%H%M%S%z')}{current.suffix}")
            current.unlink()
        target.write_bytes(r.content)
        return target.name
    except requests.RequestException:
        return ""

def discover_blog_metadata():
    """Archive only metadata that is publicly visible without authentication."""
    urls=[
      f"https://m.blog.naver.com/{BLOG}",
      f"https://m.blog.naver.com/PostList.naver?blogId={BLOG}&tab=1",
      f"https://blog.naver.com/PostList.naver?blogId={BLOG}&categoryNo=0&from=postList",
    ]
    soups=[];texts=[];source=""
    for u in urls:
        try:
            r=get(u,canonical(""))
            if r.ok and r.text:
                soups.append(BeautifulSoup(r.text,"html.parser"));texts.append(r.text)
                if not source:source=u
        except requests.RequestException:
            pass
        time.sleep(float(CFG.get("request_delay_seconds",.5)))

    if not soups:return load(BLOG_META_PATH,{})

    merged="\n".join(texts)
    title=""
    nickname=""
    intro=""
    profile_url=""
    categories=[]

    for soup in soups:
        if not title:
            title=normalize_blog_title(meta_content(soup,[
              'meta[property="og:title"]','meta[name="twitter:title"]','title',
              '.blog_name','.blogname','.blog_title'
            ]))
        if not intro:
            intro=meta_content(soup,[
              'meta[property="og:description"]','meta[name="description"]',
              '.profile_desc','.introduce','.blog_desc'
            ])
        if not profile_url:
            for sel in ('.profile img','.profile_image img','.profile-img img','img.profile'):
                n=soup.select_one(sel)
                if n:
                    profile_url=img_src(n) or ""
                    if profile_url:break
        if not categories:
            categories=extract_categories(soup,str(soup))

    title=js_value(merged,["blogName","blogTitle"]) or title
    nickname=js_value(merged,["nickName","nickname","userName"])
    intro=js_value(merged,["introduction","blogIntroduce","profileText","profileIntroduction"]) or intro
    profile_url=js_value(merged,["profileImageUrl","profileImagePath","profileImage"]) or profile_url

    if profile_url.startswith("//"):profile_url="https:"+profile_url
    if profile_url.startswith("/"):profile_url="https://blog.naver.com"+profile_url

    # Merge categories found across all fetched public pages.
    merged_categories={}
    cat_order=[]
    for soup,text in zip(soups,texts):
        for cat in extract_categories(soup,text):
            no=cat["category_no"]
            if no not in merged_categories:
                merged_categories[no]=cat["category_name"];cat_order.append(no)
    categories=[{"category_no":no,"category_name":merged_categories[no]} for no in cat_order]

    old=load(BLOG_META_PATH,{})
    profile_file=old.get("profile_image_file","")
    if profile_url:
        downloaded=download_blog_profile(profile_url,source or urls[0])
        if downloaded:profile_file=downloaded

    new={
      "blog_id":BLOG,
      "blog_name":title or old.get("blog_name",""),
      "nickname":nickname or old.get("nickname",""),
      "introduction":intro or old.get("introduction",""),
      "profile_image_source":profile_url or old.get("profile_image_source",""),
      "profile_image_file":profile_file,
      "categories":categories or old.get("categories",[]),
      "source_url":source or old.get("source_url",f"https://blog.naver.com/{BLOG}")
    }
    if new!=old:
        save(BLOG_META_PATH,new)
    return new

def category_from_post(soup,raw_text):
    # Prefer an actual visible category link on the public post page.
    for a in soup.find_all("a",href=True):
        href=html.unescape(a.get("href",""))
        m=re.search(r"[?&]categoryNo=(\d+)",href)
        if not m or m.group(1)=="0":continue
        name=re.sub(r"\s+"," ",a.get_text(" ",strip=True)).strip()
        if name and len(name)<=80:
            return m.group(1),name
    no=js_value(raw_text,["categoryNo"])
    name=js_value(raw_text,["categoryName"])
    return (no,name) if no and no!="0" else ("","")

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
    category_no,category_name=category_from_post(soup,r.text)
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
      "category_no":category_no or old.get("category_no",""),
      "category_name":category_name or old.get("category_name",""),
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

def esc(x): return html.escape(str(x or ""),quote=True)
def label(s): return {"active":"원본 확인됨","deleted":"원본 삭제 감지","private_or_unavailable":"원본 비공개/접근불가"}.get(s,s or "상태 미확인")

def shell(title,body,prefix="",blogmeta=None):
    blogmeta=blogmeta or load(BLOG_META_PATH,{})
    return f'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="{prefix}assets/style.css">
</head>
<body>
  <div class="archive-bar">
    <div class="archive-bar-inner">
      <a class="archive-mark" href="{prefix}index.html">NAVER BLOG ARCHIVE</a>
      <span class="archive-pill">보존본</span>
    </div>
  </div>
  <header class="blog-cover">
    <div class="blog-cover-inner">
      <p class="archive-kicker">개인 아카이브</p>
      <a class="blog-title" href="{prefix}index.html">{esc(blogmeta.get("blog_name") or CFG.get("archive_title", BLOG+" Archive"))}</a>
      <p class="blog-subtitle">{esc(blogmeta.get("introduction") or "네이버 블로그 공개 글을 자동 보존하는 미러 사이트")}</p>
    </div>
  </header>
  <nav class="blog-nav">
    <div class="blog-nav-inner">
      <a class="active" href="{prefix}index.html">전체글</a>
      <a href="https://blog.naver.com/{BLOG}" target="_blank" rel="noopener noreferrer">원본 블로그</a>
      <a href="{prefix}index.html#archive-info">아카이브 안내</a>
    </div>
  </nav>
  {body}
  <footer class="footer">이 사이트는 네이버 공식 서비스가 아닌 개인 보존용 아카이브입니다.</footer>
</body>
</html>'''

def build_sidebar(total,active,deleted_n,priv,blogmeta,category_counts):
    pname=blogmeta.get("nickname") or blogmeta.get("blog_name") or BLOG
    intro=blogmeta.get("introduction") or "공개 게시물을 자동으로 보존하는 개인 아카이브입니다."
    pfile=blogmeta.get("profile_image_file","")
    avatar=(f'<img src="blog/{esc(pfile)}" alt="" class="profile-photo">' if pfile else f'<div class="avatar">{esc(BLOG[:1].upper())}</div>')
    cats=[]
    cats.append(f'<button class="side-btn active" type="button" data-filter="all"><span>전체글</span><span class="side-count">{total}</span></button>')
    for cat in blogmeta.get("categories",[]):
        no=str(cat.get("category_no",""))
        name=cat.get("category_name","")
        if not no or not name:continue
        cats.append(f'<button class="side-btn" type="button" data-category="{esc(no)}"><span>{esc(name)}</span><span class="side-count">{category_counts.get(no,0)}</span></button>')
    cats.append(f'<button class="side-btn" type="button" data-filter="deleted"><span>원본 삭제됨</span><span class="side-count">{deleted_n}</span></button>')
    cats.append(f'<button class="side-btn" type="button" data-filter="private_or_unavailable"><span>접근불가</span><span class="side-count">{priv}</span></button>')
    return f'''
<aside class="sidebar">
  <section class="side-card profile-card">
    {avatar}
    <div class="profile-name">{esc(pname)}</div>
    <div class="profile-id">blog.naver.com/{esc(BLOG)}</div>
    <p class="profile-desc">{esc(intro)}</p>
    <a class="profile-link" href="https://blog.naver.com/{esc(BLOG)}" target="_blank" rel="noopener noreferrer">네이버 원본 블로그 ↗</a>
    <div class="stats-mini">
      <div><strong>{total}</strong><span>보존 글</span></div>
      <div><strong>{deleted_n}</strong><span>삭제 감지</span></div>
      <div><strong>{priv}</strong><span>접근불가</span></div>
    </div>
  </section>
  <section class="side-card">
    <div class="side-heading">카테고리</div>
    <div class="side-list">{''.join(cats)}</div>
    <input id="search" class="search-box" type="search" placeholder="이 블로그에서 검색">
  </section>
</aside>'''

def build_site(index):
    blogmeta=load(BLOG_META_PATH,{})
    DOCS.mkdir(parents=True,exist_ok=True)
    (DOCS/"assets").mkdir(exist_ok=True)
    blog_out=DOCS/"blog";blog_out.mkdir(exist_ok=True)
    pfile=blogmeta.get("profile_image_file","")
    if pfile and (BLOG_META_DIR/pfile).exists(): shutil.copy2(BLOG_META_DIR/pfile,blog_out/pfile)
    out=DOCS/"posts"; out.mkdir(exist_ok=True)

    rows=sorted(index.values(),key=lambda m:m.get("published") or m.get("first_archived_at",""),reverse=True)
    active=sum(m.get("source_status")=="active" for m in rows)
    deleted_n=sum(m.get("source_status")=="deleted" for m in rows)
    priv=sum(m.get("source_status")=="private_or_unavailable" for m in rows)
    total=len(rows)
    category_counts={}
    for m in rows:
        no=str(m.get("category_no",""))
        if no: category_counts[no]=category_counts.get(no,0)+1

    # Prepare stable neighbor relationships in displayed order.
    positions={m["post_id"]:i for i,m in enumerate(rows)}
    cards=[]

    for m in rows:
        pid=m["post_id"]
        status=m.get("source_status","active")
        cards.append(
          f'<article class="card" data-search="{esc(m.get("title","")).lower()}" data-status="{esc(status)}" data-category="{esc(m.get("category_no",""))}">'
          f'<div class="card-row"><div>'
          f'<h2><a href="posts/{pid}/index.html">{esc(m.get("title"))}</a></h2>'
          f'<div class="meta">{esc(m.get("published") or m.get("first_archived_at"))}</div>'
          f'</div><span class="status status-{esc(status)}">{esc(label(status))}</span></div>'
          f'</article>'
        )

        src=POSTS/pid
        dst=out/pid
        dst.mkdir(parents=True,exist_ok=True)
        if (dst/"images").exists():
            shutil.rmtree(dst/"images")
        if (src/"images").exists():
            shutil.copytree(src/"images",dst/"images")

        body=(src/"content.html").read_text(encoding="utf-8") if (src/"content.html").exists() else ""
        notice=""
        if status=="deleted":
            notice='<div class="notice danger">원본 게시물의 삭제가 감지되었습니다. 아래 내용은 삭제 전에 저장된 보존본입니다.</div>'
        elif status=="private_or_unavailable":
            notice='<div class="notice">현재 원본 게시물에 접근할 수 없습니다. 아래 내용은 이전에 저장된 보존본입니다.</div>'

        i=positions[pid]
        newer=rows[i-1] if i>0 else None
        older=rows[i+1] if i+1<len(rows) else None
        neighbors='<div class="post-neighbors">'
        if newer:
            neighbors+=f'<a class="neighbor" href="../{newer["post_id"]}/index.html"><span class="neighbor-label">다음글</span><span class="neighbor-title">{esc(newer.get("title"))}</span></a>'
        if older:
            neighbors+=f'<a class="neighbor" href="../{older["post_id"]}/index.html"><span class="neighbor-label">이전글</span><span class="neighbor-title">{esc(older.get("title"))}</span></a>'
        neighbors+='</div>'

        main=f'''
<div class="blog-layout">
  {build_sidebar(total,active,deleted_n,priv,blogmeta,category_counts).replace('id="search"','')}
  <section class="content-panel">
    <div class="mobile-profile">
      <div class="mobile-avatar">{esc(BLOG[:1].upper())}</div>
      <div><strong>{esc(BLOG)}</strong><span>네이버 블로그 보존본</span></div>
    </div>
    <div class="post-wrap">
      <div class="post-toolbar"><a class="back" href="../../index.html">← 전체글</a></div>
      <article class="post">
        <h1 class="post-title">{esc(m.get("title"))}</h1>
        <div class="postmeta">
          <span>{esc(m.get("published") or m.get("first_archived_at"))}</span>
          <span>{esc(m.get("category_name") or "전체글")}</span>
          <span>{esc(label(status))}</span>
          <span>보존 버전 {esc(m.get("version_count",1))}</span>
        </div>
        {notice}
        <div class="source"><a href="{esc(m.get("original_url"))}" target="_blank" rel="noopener noreferrer">네이버 원문 보기 ↗</a></div>
        <div class="content">{body}</div>
        {neighbors}
      </article>
    </div>
  </section>
</div>
'''
        (dst/"index.html").write_text(shell(m.get("title","Archive"),main,"../../"),encoding="utf-8")

    listing="".join(cards) if cards else '<div class="empty">아직 보존된 글이 없습니다.</div>'
    home=f'''
<div class="blog-layout">
  {build_sidebar(total,active,deleted_n,priv,blogmeta,category_counts)}
  <section class="content-panel">
    <div class="mobile-profile">
      <div class="mobile-avatar">{esc(BLOG[:1].upper())}</div>
      <div><strong>{esc(BLOG)}</strong><span>네이버 블로그 보존본</span></div>
    </div>
    <div class="list-head">
      <h2>전체글</h2>
      <span>총 {total}개의 글</span>
    </div>
    <section class="post-list" id="post-list">{listing}</section>
    <section id="archive-info" class="side-card" style="margin-top:18px;padding:18px 20px;line-height:1.7;color:#777;font-size:12px">
      원본 게시물이 삭제되거나 비공개로 전환되어도 이미 저장된 보존본은 유지됩니다.
    </section>
  </section>
</div>
<script src="assets/app.js"></script>
'''
    (DOCS/"index.html").write_text(shell(CFG.get("archive_title",BLOG+" Archive"),home),encoding="utf-8")
    (DOCS/".nojekyll").write_text("",encoding="utf-8")

def checkpoint(index, reason):
    """Persist a partial archive and push it so GitHub Pages can update mid-run."""
    save(INDEX,index);build_site(index)
    subprocess.run(["git","config","user.name","github-actions[bot]"],check=True)
    subprocess.run(["git","config","user.email","41898282+github-actions[bot]@users.noreply.github.com"],check=True)
    subprocess.run(["git","add","archive","docs","state"],check=False)
    status=subprocess.run(["git","status","--porcelain"],capture_output=True,text=True,check=False)
    if not status.stdout.strip():
        return False
    stamp=now().strftime("%Y-%m-%d %H:%M KST")
    subprocess.run(["git","commit","-m",f"archive: checkpoint {reason} ({stamp})"],check=True)
    for attempt in range(3):
        pushed=subprocess.run(["git","push"],capture_output=True,text=True,check=False)
        if pushed.returncode==0:
            print(f"[CHECKPOINT] pushed: {reason}")
            return True
        print("[CHECKPOINT] push retry",attempt+1,pushed.stderr.strip())
        subprocess.run(["git","pull","--rebase","--autostash"],check=False)
        time.sleep(2)
    raise RuntimeError("Could not push progressive archive checkpoint")

def main():
    POSTS.mkdir(parents=True,exist_ok=True);STATE.mkdir(parents=True,exist_ok=True)
    index=load(INDEX,{})
    try:
        discover_blog_metadata()
    except Exception as e:
        print("Blog metadata warning:",e)
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
    pending_changes=0
    last_checkpoint=time.monotonic()
    changed_results={"new","updated","deleted","private"}

    for i,x in enumerate(c.values(),1):
        result=archive_one(x,index);stats[result]=stats.get(result,0)+1
        print(f"[{i}/{len(c)}] {x['post_id']} {result}")
        if result in changed_results:
            pending_changes+=1

        # Publish partial results during a large first import:
        # whichever happens first, 10 changed posts or 60 seconds.
        if pending_changes and (pending_changes>=10 or time.monotonic()-last_checkpoint>=60):
            checkpoint(index,f"{i}/{len(c)} posts")
            pending_changes=0
            last_checkpoint=time.monotonic()

        time.sleep(float(CFG.get("request_delay_seconds",.5)))

    if mode=="maintenance":
        maintenance(index,set(c))

    # Flush any remaining posts/status changes at the end.
    checkpoint(index,"final")
    print("mode",mode,"stats",stats)

if __name__=="__main__":main()
