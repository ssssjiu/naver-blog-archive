from __future__ import annotations

import hashlib, html, json, mimetypes, re, shutil, subprocess, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, unquote_plus
import requests
from bs4 import BeautifulSoup
from PIL import Image

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
    """Discover public posts and retain Naver's category hierarchy fields."""
    out={}
    pages=int(CFG.get("initial_discovery_pages",30) if not (STATE/"initial_discovery_complete.json").exists() else CFG.get("daily_discovery_pages",5))
    delay=float(CFG.get("request_delay_seconds",.5))
    empty_rounds=0

    for page in range(1,pages+1):
        before=len(out)

        # Mobile list is a fallback source for IDs.
        mobile_url=f"https://m.blog.naver.com/PostList.naver?blogId={BLOG}&categoryNo=0&listStyle=style1&currentPage={page}"
        try:
            t=html.unescape(get(mobile_url,canonical("")).text)
            ids=set(re.findall(r'[?&]logNo[=:"\']+(\d{6,})',t))
            ids.update(re.findall(rf'/{re.escape(BLOG)}/(\d{{6,}})(?:[/?#"\'<]|$)',t))
            for pid in ids:
                out.setdefault(pid,{
                  "post_id":pid,"url":canonical(pid),"title":"","published":"",
                  "category_no":"","parent_category_no":""
                })
        except requests.RequestException:
            pass
        time.sleep(delay)

        # Naver's public async title list carries categoryNo and parentCategoryNo
        # per post. Prefer this over guessing from arbitrary links in PostView.
        async_url=(
          f"https://blog.naver.com/PostTitleListAsync.naver?blogId={BLOG}"
          f"&viewdate=&currentPage={page}&categoryNo=0&parentCategoryNo=0&countPerPage=30"
        )
        try:
            r=get(async_url,canonical(""))
            raw=r.text
            data=None
            try:
                data=r.json()
            except Exception:
                try:data=json.loads(raw)
                except Exception:data=None

            if isinstance(data,dict):
                for item in data.get("postList",[]) or []:
                    pid=str(item.get("logNo") or "").strip()
                    if not pid:continue
                    title=unquote_plus(str(item.get("title") or ""))
                    title=html.unescape(title)
                    cand=out.setdefault(pid,{
                      "post_id":pid,"url":canonical(pid),"title":"","published":"",
                      "category_no":"","parent_category_no":""
                    })
                    if title:cand["title"]=title
                    cand["category_no"]=str(item.get("categoryNo") or cand.get("category_no") or "")
                    cand["parent_category_no"]=str(item.get("parentCategoryNo") or cand.get("parent_category_no") or "")
                    add_date=str(item.get("addDate") or "")
                    if add_date and not cand.get("published"):cand["published"]=add_date
            else:
                # Resilient fallback if response stops being valid JSON.
                for m in re.finditer(r'"logNo"\s*:\s*"?(?P<id>\d{6,})"?.{0,800}?"categoryNo"\s*:\s*"?(?P<cat>\d*)"?(?:.{0,300}?"parentCategoryNo"\s*:\s*"?(?P<parent>\d*)")?',raw,re.S):
                    pid=m.group("id")
                    cand=out.setdefault(pid,{
                      "post_id":pid,"url":canonical(pid),"title":"","published":"",
                      "category_no":"","parent_category_no":""
                    })
                    cand["category_no"]=m.group("cat") or cand.get("category_no","")
                    cand["parent_category_no"]=m.group("parent") or cand.get("parent_category_no","")
        except requests.RequestException:
            pass
        time.sleep(delay)

        if len(out)==before:empty_rounds+=1
        else:empty_rounds=0
        if page>=3 and empty_rounds>=2:break

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

def script_number(text, names):
    for name in names:
        for pat in (
          rf'(?:var\s+)?{re.escape(name)}\s*=\s*["\']?(-?\d+)["\']?',
          rf'"{re.escape(name)}"\s*:\s*"?(-?\d+)"?'
        ):
            m=re.search(pat,text,re.I)
            if m:return m.group(1)
    return ""

def script_string(text, names):
    for name in names:
        for pat in (
          rf'(?:var\s+)?{re.escape(name)}\s*=\s*["\']((?:\\.|[^"\'])*)["\']',
          rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"'
        ):
            m=re.search(pat,text,re.I)
            if m:return html.unescape(m.group(1).replace("\\/","/")).strip()
    return ""

def category_from_post(soup,raw_text,candidate=None):
    """Read the actual post category, not the first category-looking link."""
    candidate=candidate or {}
    no=str(candidate.get("category_no") or "")
    parent=str(candidate.get("parent_category_no") or "")
    name=""

    # SmartEditor 4 header. This is the category label shown above a post title.
    for sel in (
      ".se-documentTitle .blog2_series",
      ".se-documentTitle [class*='blog2_series']",
      ".blog2_series"
    ):
        n=soup.select_one(sel)
        if not n:continue
        name=re.sub(r"\s+"," ",n.get_text(" ",strip=True)).strip()
        link=n if n.name=="a" else (n.find("a",href=True) or n.find_parent("a",href=True))
        if link:
            href=html.unescape(link.get("href",""))
            m=re.search(r"[?&]categoryNo=(\d+)",href)
            if m:no=m.group(1)
            p=re.search(r"[?&]parentCategoryNo=(-?\d+)",href)
            if p:parent=p.group(1)
        if name:break

    # Classic editor / analytics variables expose the rendered category name.
    if not name:
        for pat in (
          r'baParams\.categoryName\s*=\s*["\']((?:\\.|[^"\'])*)["\']',
          r'nlog2Params\.categoryName\s*=\s*["\']((?:\\.|[^"\'])*)["\']',
          r'(?:var\s+)?categoryName\s*=\s*["\']((?:\\.|[^"\'])*)["\']'
        ):
            m=re.search(pat,raw_text,re.I)
            if m:
                name=html.unescape(m.group(1).replace("\\/","/")).strip()
                break

    if not no:
        no=script_number(raw_text,["currentCategoryNo","categoryNo"])
    if not parent:
        parent=script_number(raw_text,["parentCategoryNo"])

    # Last fallback only for the name; category number still comes from the
    # public title-list metadata when possible.
    if not name:
        name=script_string(raw_text,["categoryName"])

    if name in ("전체보기","블로그"):
        # "블로그" is often the top menu label, not the post category.
        # Keep it only when the title-list metadata really points there.
        if str(candidate.get("category_no") or "") and str(candidate.get("category_no"))!=no:
            name=""

    return no,name,parent

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

def clean_naver_chrome(fragment):
    """Remove Naver page controls accidentally captured as post content."""
    soup=BeautifulSoup(str(fragment),"html.parser")
    junk_phrases=(
      "URL 복사","이웃추가","본문 기타 기능","공유하기","신고하기",
      "공유하기 신고하기","공감","댓글 쓰기"
    )
    # Remove obvious control links/buttons by their visible label.
    for tag in list(soup.find_all(["a","button"])):
        txt=re.sub(r"\s+"," ",tag.get_text(" ",strip=True)).strip()
        if txt and any(p==txt or p in txt for p in junk_phrases):
            tag.decompose()
    # Remove now-empty control wrappers and standalone chrome text.
    for tag in list(soup.find_all(["div","span","p","li"])):
        txt=re.sub(r"\s+"," ",tag.get_text(" ",strip=True)).strip()
        if txt and len(txt)<80 and any(
            txt==p or txt.replace(" ","") in ("URL복사","이웃추가","본문기타기능","공유하기신고하기")
            for p in junk_phrases
        ):
            tag.decompose()
    return str(soup)

def localize(node,pdir,referer):
    soup=BeautifulSoup(clean_naver_chrome(node),"html.parser")
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

def archive_response(c,index,r):
    pid=c["post_id"]; pdir=POSTS/pid; pdir.mkdir(parents=True,exist_ok=True)
    mp=pdir/"metadata.json"; cp=pdir/"content.html"; old=load(mp,{})
    if deleted(r.text,r.status_code):
        if old and old.get("source_status")!="deleted":
            old["source_status"]="deleted";old["deleted_detected_at"]=iso();save(mp,old);index[pid]=old;return "deleted"
        return "unchanged"
    if private(r.text):
        if old and old.get("source_status")!="private_or_unavailable":
            old["source_status"]="private_or_unavailable";old["unavailable_detected_at"]=iso();save(mp,old);index[pid]=old;return "private"
        return "unchanged"
    soup=BeautifulSoup(r.text,"html.parser"); node=content_node(soup,pid)
    category_no,category_name,parent_category_no=category_from_post(soup,r.text,c)
    if not node:return "unparsed"
    title=title_of(soup,c.get("title","")); body=localize(node,pdir,c["url"])
    h=hashlib.sha256((title+"\n"+re.sub(r"\s+"," ",body)).encode()).hexdigest()
    if old.get("content_hash")==h and old.get("source_status")=="active":
        metadata_changed=False
        corrections={
          "category_no":category_no or c.get("category_no") or old.get("category_no",""),
          "category_name":category_name or old.get("category_name",""),
          "parent_category_no":parent_category_no or c.get("parent_category_no") or old.get("parent_category_no","")
        }
        for key,value in corrections.items():
            value=str(value or "")
            if value and str(old.get(key,""))!=value:
                old[key]=value;metadata_changed=True
        index[pid]=old
        if metadata_changed:
            save(mp,old)
            return "metadata"
        return "unchanged"
    if old and cp.exists() and old.get("content_hash")!=h:
        vd=pdir/"versions"/now().strftime("%Y%m%dT%H%M%S%z");vd.mkdir(parents=True,exist_ok=True)
        shutil.copy2(cp,vd/"content.html");shutil.copy2(mp,vd/"metadata.json")
    meta={
      "post_id":pid,"blog_id":BLOG,"source_type":"naver","title":title,"original_url":canonical(pid),
      "published":c.get("published") or old.get("published",""),
      "first_archived_at":old.get("first_archived_at",iso()),
      "last_archived_at":iso(),"source_status":"active","content_hash":h,
      "category_no":category_no or c.get("category_no") or old.get("category_no",""),
      "category_name":category_name or old.get("category_name",""),
      "parent_category_no":parent_category_no or c.get("parent_category_no") or old.get("parent_category_no",""),
      "version_count":(int(old.get("version_count",0))+1 if old.get("content_hash")!=h else int(old.get("version_count",1)))
    }
    cp.write_text(body,encoding="utf-8");save(mp,meta);index[pid]=meta
    return "new" if not old else "updated"


def archive_one(c,index):
    try:r=get(postview(c["post_id"]),c.get("url") or canonical(c["post_id"]))
    except requests.RequestException:return "error"
    return archive_response(c,index,r)

def maintenance(index,seen):
    """Check a tiny rotating sample of known posts; reuse each GET to detect edits/status."""
    ids=sorted(pid for pid,m in index.items() if m.get("source_type","naver")=="naver" and str(pid).isdigit())
    n=int(CFG.get("deletion_checks_per_run",5))
    if not ids or n<=0:return
    sp=STATE/"cursor.json"; st=load(sp,{"cursor":0,"misses":{}})
    cur=int(st.get("cursor",0))%len(ids); misses=st.get("misses",{})
    checked=0;offset=0
    while checked<min(n,len(ids)) and offset<len(ids):
        pid=ids[(cur+offset)%len(ids)];offset+=1
        if pid in seen:continue
        m=index[pid]
        try:r=get(postview(pid),canonical(pid))
        except requests.RequestException:continue

        if deleted(r.text,r.status_code):
            misses[pid]=int(misses.get(pid,0))+1
            if misses[pid]>=2 and m.get("source_status")!="deleted":
                m["source_status"]="deleted";m.setdefault("deleted_detected_at",iso())
                save(POSTS/pid/"metadata.json",m)
            index[pid]=m
        elif private(r.text):
            misses.pop(pid,None)
            if m.get("source_status")!="private_or_unavailable":
                m["source_status"]="private_or_unavailable";m.setdefault("unavailable_detected_at",iso())
                save(POSTS/pid/"metadata.json",m)
            index[pid]=m
        else:
            misses.pop(pid,None)
            cand={
              "post_id":pid,"url":canonical(pid),"title":m.get("title",""),
              "published":m.get("published",""),
              "category_no":m.get("category_no",""),
              "parent_category_no":m.get("parent_category_no","")
            }
            archive_response(cand,index,r)
        checked+=1
        time.sleep(float(CFG.get("request_delay_seconds",.75)))
    st={"cursor":(cur+offset)%len(ids),"misses":misses};save(sp,st)

def esc(x): return html.escape(str(x or ""),quote=True)
def label(s): return {"active":"","deleted":"원본 삭제 감지","private_or_unavailable":"원본 비공개/접근불가"}.get(s,s or "")

def archive_badge(m):
    if m.get("source_type")=="screenshot_archive":
        return "삭제 전 스크린샷 보존본"
    status=m.get("source_status","active")
    if status=="active":
        return ""
    return label(status)

def published_text(m):
    return m.get("published_display") or m.get("published") or m.get("first_archived_at","")

def sort_timestamp(m):
    raw=m.get("published_iso") or m.get("published") or m.get("first_archived_at","")
    if not raw:return 0
    try:
        if isinstance(raw,str) and "T" in raw:
            return datetime.fromisoformat(raw.replace("Z","+00:00")).timestamp()
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        try:return datetime.fromisoformat(str(raw).replace("Z","+00:00")).timestamp()
        except Exception:return 0

def shell(title,body,prefix="",blogmeta=None,version="0"):
    blogmeta=blogmeta or load(BLOG_META_PATH,{})
    return f'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="archive-build" content="{esc(version)}">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="{prefix}assets/style.css?v={esc(version)}">
  <script>
  (()=>{{
    const current={json.dumps(version)};
    fetch("{prefix}build.json?t="+Date.now(),{{cache:"no-store"}})
      .then(r=>r.ok?r.json():null)
      .then(x=>{{
        if(!x||!x.version||x.version===current)return;
        const u=new URL(location.href);
        if(u.searchParams.get("_v")===x.version)return;
        u.searchParams.set("_v",x.version);
        location.replace(u.toString());
      }}).catch(()=>{{}});
  }})();
  </script>
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
  <script src="{prefix}assets/app.js?v={esc(version)}"></script>
</body>
</html>'''

def ordered_categories(blogmeta):
    cats=[dict(x) for x in blogmeta.get("categories",[]) if x.get("category_no") and x.get("category_name")]
    by_no={str(x.get("category_no")):x for x in cats}
    children={}
    roots=[]
    for cat in cats:
        no=str(cat.get("category_no"))
        parent=str(cat.get("parent_category_no") or "")
        if parent in ("","0","-1",no) or parent not in by_no:
            roots.append(cat)
        else:
            children.setdefault(parent,[]).append(cat)

    out=[];seen=set()
    def add(cat,depth=0):
        no=str(cat.get("category_no"))
        if no in seen:return
        seen.add(no)
        item=dict(cat);item["_depth"]=depth;out.append(item)
        for child in children.get(no,[]):
            add(child,depth+1)

    for cat in roots:add(cat,0)
    for cat in cats:
        if str(cat.get("category_no")) not in seen:add(cat,0)
    return out

def build_sidebar(total,active,deleted_n,priv,blogmeta,category_counts):
    pname=blogmeta.get("nickname") or blogmeta.get("blog_name") or BLOG
    intro=blogmeta.get("introduction") or "공개 게시물을 자동으로 보존하는 개인 아카이브입니다."
    pfile=blogmeta.get("profile_image_file","")
    avatar=(f'<img src="blog/{esc(pfile)}" alt="" class="profile-photo">' if pfile else f'<div class="avatar">{esc(BLOG[:1].upper())}</div>')
    cats=[]
    cats.append(f'<button class="side-btn active" type="button" data-filter="all"><span>전체글</span><span class="side-count">{total}</span></button>')
    for cat in ordered_categories(blogmeta):
        no=str(cat.get("category_no",""))
        name=cat.get("category_name","")
        if not no or not name:continue
        depth=int(cat.get("_depth",0) or 0)
        nested=" category-child" if depth>0 else ""
        cats.append(f'<a class="side-btn category-link{nested}" href="categories/{esc(no)}/index.html"><span>{esc(name)}</span><span class="side-count">{category_counts.get(no,0)}</span></a>')
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

def ensure_manual_media_crops():
    """Materialize real image files from the supplied source screenshots."""
    pid="manual-20250720-1500-first-date"
    idir=POSTS/pid/"images"
    s1=idir/"screenshot-01.webp"; s2=idir/"screenshot-02.webp"
    if not (s1.exists() and s2.exists()):return
    specs=[
      (s1,(180,1395,325,1533),idir/"sticker.webp",90),
      (s1,(175,1560,840,2004),idir/"photo-1.webp",82),
      (s2,(175,0,840,494),idir/"photo-2.webp",82),
      (s2,(175,710,840,1584),idir/"photo-3.webp",76),
    ]
    for src,box,dst,quality in specs:
        try:
            with Image.open(src) as im:
                crop=im.convert("RGB").crop(box)
                crop.save(dst,"WEBP",quality=quality,method=6)
        except Exception as e:
            print("Manual media crop warning:",dst.name,e)

def post_preview(pid, base_prefix=""):
    meta=load(POSTS/pid/"metadata.json",{})
    # Screenshot archives keep full-page screenshots as evidence files.
    # Do not misrepresent those source screenshots as the post's own photos.
    if meta.get("source_type")=="screenshot_archive":
        thumb=POSTS/pid/"images"/"photo-1.webp"
        if thumb.exists():
            return f'{base_prefix}posts/{pid}/images/photo-1.webp',int(meta.get("media_count",0) or 0)
        return "",int(meta.get("media_count",0) or 0)
    idir=POSTS/pid/"images"
    if not idir.exists():return "",0
    files=sorted([p for p in idir.iterdir() if p.is_file()])
    if not files:return "",0
    return f'{base_prefix}posts/{pid}/images/{files[0].name}',len(files)

def post_has_video(pid):
    cp=POSTS/pid/"content.html"
    if not cp.exists():return False
    try:
        t=cp.read_text(encoding="utf-8",errors="ignore").lower()
    except Exception:
        return False
    marks=("<video","<iframe","tv.naver.com","video.naver.com","serviceapi.nmv.naver.com","se-module-video")
    return any(x in t for x in marks)

def post_search_text(pid,title,category):
    cp=POSTS/pid/"content.html"
    text=""
    if cp.exists():
        try:
            soup=BeautifulSoup(cp.read_text(encoding="utf-8",errors="ignore"),"html.parser")
            text=re.sub(r"\s+"," ",soup.get_text(" ",strip=True))[:1200]
        except Exception:
            pass
    return " ".join([str(title or ""),str(category or ""),text]).strip()

def build_site(index):
    ensure_manual_media_crops()
    blogmeta=load(BLOG_META_PATH,{})
    DOCS.mkdir(parents=True,exist_ok=True)
    (DOCS/"assets").mkdir(exist_ok=True)
    blog_out=DOCS/"blog";blog_out.mkdir(exist_ok=True)
    pfile=blogmeta.get("profile_image_file","")
    if pfile and (BLOG_META_DIR/pfile).exists(): shutil.copy2(BLOG_META_DIR/pfile,blog_out/pfile)
    out=DOCS/"posts"; out.mkdir(exist_ok=True)

    rows=sorted(index.values(),key=sort_timestamp,reverse=True)
    active=sum(m.get("source_status")=="active" for m in rows)
    deleted_n=sum(m.get("source_status")=="deleted" for m in rows)
    priv=sum(m.get("source_status")=="private_or_unavailable" for m in rows)
    total=len(rows)
    version_material={
      "posts":[
        (m.get("post_id"),m.get("content_hash"),m.get("source_status"),
         m.get("category_no"),m.get("category_name"),m.get("parent_category_no"))
        for m in rows
      ],
      "blog":blogmeta
    }
    build_version=hashlib.sha256(
      json.dumps(version_material,ensure_ascii=False,sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    save(DOCS/"build.json",{"version":build_version})

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
        preview,preview_count=post_preview(pid)
        video_flag="1" if post_has_video(pid) else "0"
        search_blob=post_search_text(pid,m.get("title"),m.get("category_name"))
        preview_html=(
          f'<a class="card-thumb" href="posts/{pid}/index.html"><img src="{esc(preview)}" alt="" loading="lazy">'
          f'{f"<span>{preview_count}</span>" if preview_count>1 else ""}</a>'
          if preview else ""
        )
        badge=archive_badge(m)
        badge_html=(f'<span class="status status-{esc(status)}">{esc(badge)}</span>' if badge else "")
        cards.append(
          f'<article class="card" data-search="{esc(search_blob).lower()}" data-status="{esc(status)}" data-category="{esc(m.get("category_no",""))}" data-video="{video_flag}">'
          f'<div class="card-row"><div class="card-main">'
          f'<h2><a href="posts/{pid}/index.html">{esc(m.get("title"))}</a></h2>'
          f'<div class="meta">{esc(m.get("category_name") or "전체글")} · {esc(published_text(m))}</div>'
          f'</div>{preview_html}{badge_html}</div>'
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
        if body:
            cleaned=clean_naver_chrome(body)
            if cleaned!=body:
                body=cleaned
                (src/"content.html").write_text(body,encoding="utf-8")
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

        badge=archive_badge(m)
        badge_meta=(f'<span>{esc(badge)}</span>' if badge else "")
        if m.get("source_type")=="screenshot_archive":
            source_block='<div class="source archive-source">친구가 제공한 삭제 전 스크린샷을 OCR 복원한 보존본</div>'
        else:
            source_block=f'<div class="source"><a href="{esc(m.get("original_url"))}" target="_blank" rel="noopener noreferrer">네이버 원문 보기 ↗</a></div>'

        main=f'''
<div class="blog-layout">
  {build_sidebar(total,active,deleted_n,priv,blogmeta,category_counts).replace('id="search"','').replace('href="categories/','href="../../categories/').replace('src="blog/','src="../../blog/')}
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
          <span>{esc(published_text(m))}</span>
          <span>{esc(m.get("category_name") or "전체글")}</span>
          {badge_meta}
          <span>보존 버전 {esc(m.get("version_count",1))}</span>
        </div>
        {notice}
        {source_block}
        <div class="content">{body}</div>
        {neighbors}
      </article>
    </div>
  </section>
</div>
'''
        (dst/"index.html").write_text(shell(m.get("title","Archive"),main,"../../",blogmeta,build_version),encoding="utf-8")

    # Persist category structure independently from the HTML view.
    category_meta={}
    for cat in blogmeta.get("categories",[]):
        no=str(cat.get("category_no") or "")
        if not no:continue
        category_meta[no]={
          "category_no":no,
          "category_name":cat.get("category_name") or no,
          "parent_category_no":str(cat.get("parent_category_no") or ""),
          "count":0
        }
    for m in rows:
        no=str(m.get("category_no") or "")
        if not no:continue
        entry=category_meta.setdefault(no,{
          "category_no":no,
          "category_name":m.get("category_name") or no,
          "parent_category_no":str(m.get("parent_category_no") or ""),
          "count":0
        })
        if m.get("category_name") and entry.get("category_name") in ("",no,"블로그"):
            entry["category_name"]=m.get("category_name")
        if m.get("parent_category_no"):
            entry["parent_category_no"]=str(m.get("parent_category_no"))
        entry["count"]+=1

    cat_root=ARCH/"categories";cat_root.mkdir(exist_ok=True)
    catalog=sorted(category_meta.values(),key=lambda x:(x.get("parent_category_no",""),x.get("category_name","")))
    save(cat_root/"index.json",catalog)
    cat_docs=DOCS/"categories";cat_docs.mkdir(exist_ok=True)

    for cat in catalog:
        no=cat["category_no"]
        cat_rows=[m for m in rows if str(m.get("category_no") or "")==no]
        save(cat_root/f"{no}.json",{
          "category":cat,
          "posts":[m.get("post_id") for m in cat_rows]
        })
        if not cat_rows:continue
        cat_cards=[]
        for m in cat_rows:
            status=m.get("source_status","active")
            preview,preview_count=post_preview(m["post_id"],"../../")
            video_flag="1" if post_has_video(m["post_id"]) else "0"
            search_blob=post_search_text(m["post_id"],m.get("title"),m.get("category_name"))
            preview_html=(
              f'<a class="card-thumb" href="../../posts/{m["post_id"]}/index.html"><img src="{esc(preview)}" alt="" loading="lazy">'
              f'{f"<span>{preview_count}</span>" if preview_count>1 else ""}</a>'
              if preview else ""
            )
            badge=archive_badge(m)
            badge_html=(f'<span class="status status-{esc(status)}">{esc(badge)}</span>' if badge else "")
            cat_cards.append(
              f'<article class="card" data-search="{esc(search_blob).lower()}" data-status="{esc(status)}" data-category="{esc(no)}" data-video="{video_flag}">'
              f'<div class="card-row"><div class="card-main"><h2><a href="../../posts/{m["post_id"]}/index.html">{esc(m.get("title"))}</a></h2>'
              f'<div class="meta">{esc(published_text(m))}</div></div>'
              f'{preview_html}{badge_html}</div></article>'
            )
        cat_body=f'''
<div class="blog-layout">
  <section class="content-panel category-full">
    <div class="list-head"><h2>{esc(cat.get("category_name"))}</h2><span>총 {len(cat_rows)}개의 글</span></div>
    <section class="post-list">{''.join(cat_cards)}</section>
  </section>
</div>
'''
        cat_dir=cat_docs/no;cat_dir.mkdir(exist_ok=True)
        (cat_dir/"index.html").write_text(
          shell(cat.get("category_name") or "Category",cat_body,"../../",blogmeta,build_version),
          encoding="utf-8"
        )

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
'''
    (DOCS/"index.html").write_text(shell(CFG.get("archive_title",BLOG+" Archive"),home,"",blogmeta,build_version),encoding="utf-8")
    (DOCS/".nojekyll").write_text("",encoding="utf-8")

def merge_category_metadata_from_posts(index):
    blogmeta=load(BLOG_META_PATH,{})
    by_no={str(x.get("category_no") or ""):dict(x) for x in blogmeta.get("categories",[]) if x.get("category_no")}
    order=[str(x.get("category_no")) for x in blogmeta.get("categories",[]) if x.get("category_no")]
    for m in index.values():
        no=str(m.get("category_no") or "")
        if not no:continue
        if no not in by_no:
            by_no[no]={"category_no":no,"category_name":m.get("category_name") or no,"parent_category_no":str(m.get("parent_category_no") or "")}
            order.append(no)
        else:
            if m.get("category_name") and by_no[no].get("category_name") in ("","블로그",no):
                by_no[no]["category_name"]=m.get("category_name")
            if m.get("parent_category_no"):
                by_no[no]["parent_category_no"]=str(m.get("parent_category_no"))
    blogmeta["categories"]=[by_no[x] for x in order if x in by_no]
    save(BLOG_META_PATH,blogmeta)

def checkpoint(index, reason):
    """Persist a partial archive and push it so GitHub Pages can update mid-run."""
    save(INDEX,index);merge_category_metadata_from_posts(index);build_site(index)
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

def load_manual_entries(index):
    """Keep manually supplied screenshot archives in the main index."""
    if not POSTS.exists(): return
    for mp in POSTS.glob("*/metadata.json"):
        m=load(mp,{})
        if m.get("source_type") and m.get("source_type")!="naver" and m.get("post_id"):
            index[str(m["post_id"])]=m

def main():
    POSTS.mkdir(parents=True,exist_ok=True);STATE.mkdir(parents=True,exist_ok=True)
    index=load(INDEX,{})
    load_manual_entries(index)
    mode="fast" if "--fast" in sys.argv else ("discover" if "--discover" in sys.argv else "maintenance")
    initial_done=STATE/"initial_discovery_complete.json"
    if mode=="maintenance" and not initial_done.exists():
        mode="discover"
        print("[INFO] Initial discovery is incomplete; resuming full discovery.")

    # Profile/category pages are not requested every 5 minutes.
    # Refresh them only during daily discovery or the very first run.
    if mode=="discover" or not BLOG_META_PATH.exists():
        try:discover_blog_metadata()
        except Exception as e:print("Blog metadata warning:",e)

    candidates={}
    rss={}
    try:
        rss=rss_candidates()
        candidates.update(rss)
    except Exception as e:
        print("RSS warning:",e)

    if mode=="discover":
        try:
            history=discover_history()
            for k,v in history.items():
                if k in candidates:
                    for key,val in v.items():
                        if val:candidates[k][key]=val
                else:
                    candidates[k]=v
        except Exception as e:
            print("Discovery warning:",e)

    for k,v in backfill().items():candidates.setdefault(k,v)

    # Critical analytics-minimization rule:
    # only open PostView for posts we have never archived before.
    # Existing RSS/list entries are metadata-only and cause no PostView request.
    todo=[]
    for pid,cand in candidates.items():
        if pid not in index:
            todo.append(cand)
            continue
        old=index[pid]
        changed=False
        # Safe metadata corrections from public list/RSS without opening the post.
        if cand.get("title") and cand.get("title")!=old.get("title"):
            old["title"]=cand["title"];changed=True
        for key in ("category_no","parent_category_no"):
            if cand.get(key) and str(cand.get(key))!=str(old.get(key,"")):
                old[key]=str(cand[key]);changed=True
        if changed:
            save(POSTS/pid/"metadata.json",old);index[pid]=old

    stats={}
    pending_changes=0
    last_checkpoint=time.monotonic()
    changed_results={"new","updated","metadata","deleted","private"}

    for i,x in enumerate(todo,1):
        result=archive_one(x,index);stats[result]=stats.get(result,0)+1
        print(f"[new {i}/{len(todo)}] {x['post_id']} {result}")
        if result in changed_results:pending_changes+=1
        if pending_changes and time.monotonic()-last_checkpoint>=90:
            checkpoint(index,f"{i}/{len(todo)} new posts")
            pending_changes=0;last_checkpoint=time.monotonic()
        time.sleep(float(CFG.get("request_delay_seconds",.75)))

    # Every 6 hours, inspect only a small rotating sample of old posts.
    # The same single request checks deletion/private status AND content edits.
    if mode=="maintenance":
        maintenance(index,set(x["post_id"] for x in todo))

    if mode=="discover":
        save(initial_done,{
          "completed_at":iso(),
          "candidate_count":len(candidates),
          "archived_count":len(index)
        })

    # Rebuild only when state/site needs it; checkpoint itself skips clean commits.
    checkpoint(index,"final")
    print("mode",mode,"new_posts",len(todo),"stats",stats)

if __name__=="__main__":main()
