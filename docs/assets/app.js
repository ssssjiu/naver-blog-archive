
const search=document.querySelector("#search");
const list=document.querySelector(".post-list");
const allCards=list?[...list.querySelectorAll(".card")]:[];
const filters=[...document.querySelectorAll("[data-filter]")];

let activeFilter="all";
let query="";
let currentPage=1;
const pageSizeByView={list:10,card:8,album:12,video:8};
const validViews=new Set(["list","card","album","video"]);
let view=localStorage.getItem("naverArchiveView")||"list";
if(!validViews.has(view))view="list";

function ensureControls(){
  if(!list)return;
  const head=document.querySelector(".list-head");
  if(head&&!head.querySelector(".view-controls")){
    const wrap=document.createElement("div");
    wrap.className="view-controls";
    wrap.setAttribute("aria-label","글 목록 보기 방식");
    wrap.innerHTML=[
      ["list","목록형","☰"],
      ["card","카드형","▤"],
      ["album","앨범형","▦"],
      ["video","동영상형","▶"]
    ].map(([v,label,icon])=>
      '<button type="button" class="view-btn" data-view="'+v+'" title="'+label+'" aria-label="'+label+'">'+
      '<span class="view-icon">'+icon+'</span><span class="view-label">'+label+'</span></button>'
    ).join("");
    head.appendChild(wrap);
  }
  if(!document.querySelector(".pager")){
    const pager=document.createElement("nav");
    pager.className="pager";
    pager.setAttribute("aria-label","글 목록 페이지");
    list.after(pager);
  }
  if(!document.querySelector(".list-empty")){
    const empty=document.createElement("div");
    empty.className="list-empty";
    empty.hidden=true;
    empty.textContent="조건에 맞는 글이 없습니다.";
    list.after(empty);
  }
  for(const card of allCards){
    if(!card.querySelector(".card-thumb"))card.classList.add("no-thumb");
  }
}

function matches(card){
  const text=(card.dataset.search||"").toLowerCase();
  const status=card.dataset.status||"";
  const isVideo=card.dataset.video==="1"||card.classList.contains("has-video");
  if(query&&!text.includes(query))return false;
  if(activeFilter!=="all"&&status!==activeFilter)return false;
  if(view==="video"&&!isVideo)return false;
  return true;
}

function renderPager(totalPages){
  const pager=document.querySelector(".pager");
  if(!pager)return;
  if(totalPages<=1){pager.innerHTML="";pager.hidden=true;return;}
  pager.hidden=false;

  const parts=[];
  parts.push('<button type="button" class="page-btn" data-page="prev"'+(currentPage===1?" disabled":"")+'>‹</button>');

  const start=Math.max(1,currentPage-2);
  const end=Math.min(totalPages,start+4);
  const realStart=Math.max(1,end-4);
  if(realStart>1){
    parts.push('<button type="button" class="page-btn" data-page="1">1</button>');
    if(realStart>2)parts.push('<span class="page-gap">…</span>');
  }
  for(let p=realStart;p<=end;p++){
    parts.push('<button type="button" class="page-btn'+(p===currentPage?" active":"")+'" data-page="'+p+'">'+p+'</button>');
  }
  if(end<totalPages){
    if(end<totalPages-1)parts.push('<span class="page-gap">…</span>');
    parts.push('<button type="button" class="page-btn" data-page="'+totalPages+'">'+totalPages+'</button>');
  }
  parts.push('<button type="button" class="page-btn" data-page="next"'+(currentPage===totalPages?" disabled":"")+'>›</button>');
  pager.innerHTML=parts.join("");
}

function renderList(){
  if(!list)return;
  list.dataset.view=view;
  document.querySelectorAll(".view-btn").forEach(btn=>{
    const active=btn.dataset.view===view;
    btn.classList.toggle("active",active);
    btn.setAttribute("aria-pressed",active?"true":"false");
  });

  const matched=allCards.filter(matches);
  const perPage=pageSizeByView[view]||10;
  const totalPages=Math.max(1,Math.ceil(matched.length/perPage));
  currentPage=Math.min(currentPage,totalPages);
  const start=(currentPage-1)*perPage;
  const visible=new Set(matched.slice(start,start+perPage));

  for(const card of allCards)card.hidden=!visible.has(card);

  const empty=document.querySelector(".list-empty");
  if(empty)empty.hidden=matched.length!==0;

  const count=document.querySelector(".list-head > span");
  if(count){
    const label=view==="video"?"동영상 글":"글";
    count.textContent="총 "+matched.length+"개의 "+label;
  }

  renderPager(totalPages);
}

ensureControls();
renderList();

if(search){
  search.addEventListener("input",()=>{
    query=search.value.trim().toLowerCase();
    currentPage=1;
    renderList();
  });
}

for(const btn of filters){
  btn.addEventListener("click",()=>{
    activeFilter=btn.dataset.filter||"all";
    currentPage=1;
    filters.forEach(b=>b.classList.toggle("active",b===btn));
    renderList();
  });
}

document.addEventListener("click",e=>{
  const viewBtn=e.target.closest(".view-btn");
  if(viewBtn){
    view=viewBtn.dataset.view||"list";
    localStorage.setItem("naverArchiveView",view);
    currentPage=1;
    renderList();
    return;
  }
  const pageBtn=e.target.closest(".page-btn");
  if(pageBtn&&!pageBtn.disabled){
    const matched=allCards.filter(matches);
    const totalPages=Math.max(1,Math.ceil(matched.length/(pageSizeByView[view]||10)));
    const p=pageBtn.dataset.page;
    if(p==="prev")currentPage=Math.max(1,currentPage-1);
    else if(p==="next")currentPage=Math.min(totalPages,currentPage+1);
    else currentPage=Math.max(1,Math.min(totalPages,Number(p)||1));
    renderList();
    document.querySelector(".list-head")?.scrollIntoView({behavior:"smooth",block:"start"});
  }
});

/* Post image viewer */
const articleImages=[...document.querySelectorAll(".content img")]
  .filter(img=>img.getAttribute("src"));

if(articleImages.length){
  let current=0;
  let scale=1;
  let fit=true;

  const viewer=document.createElement("div");
  viewer.className="image-viewer";
  viewer.innerHTML=[
    '<div class="image-viewer-toolbar">',
    '<button class="image-viewer-btn" data-action="minus" aria-label="축소">−</button>',
    '<button class="image-viewer-btn" data-action="actual">원본크기</button>',
    '<button class="image-viewer-btn" data-action="plus" aria-label="확대">＋</button>',
    '<button class="image-viewer-btn" data-action="fit">화면맞춤</button>',
    '<span class="image-viewer-count"></span>',
    '<button class="image-viewer-btn image-viewer-close" data-action="close">닫기 ✕</button>',
    '</div>',
    '<button class="image-viewer-prev" data-action="prev" aria-label="이전 이미지">‹</button>',
    '<div class="image-viewer-stage fit"><img class="image-viewer-img" alt=""></div>',
    '<button class="image-viewer-next" data-action="next" aria-label="다음 이미지">›</button>'
  ].join("");
  document.body.appendChild(viewer);

  const stage=viewer.querySelector(".image-viewer-stage");
  const large=viewer.querySelector(".image-viewer-img");
  const count=viewer.querySelector(".image-viewer-count");

  function renderImage(){
    const src=articleImages[current].currentSrc||articleImages[current].src;
    large.src=src;
    count.textContent=(current+1)+" / "+articleImages.length;
    if(fit){
      stage.classList.add("fit");
      stage.classList.remove("actual");
      large.style.width="";
      large.style.height="";
    }else{
      stage.classList.remove("fit");
      stage.classList.add("actual");
      const setSize=()=>{
        const w=Math.max(1,large.naturalWidth||articleImages[current].naturalWidth||800);
        large.style.width=Math.round(w*scale)+"px";
        large.style.height="auto";
      };
      if(large.complete)setSize(); else large.onload=setSize;
    }
  }
  function openViewer(i){
    current=i;scale=1;fit=true;
    viewer.classList.add("open");
    document.body.classList.add("viewer-open");
    renderImage();
  }
  function closeViewer(){
    viewer.classList.remove("open");
    document.body.classList.remove("viewer-open");
  }
  function zoom(delta){
    fit=false;
    scale=Math.max(.25,Math.min(4,scale+delta));
    renderImage();
  }
  function move(delta){
    current=(current+delta+articleImages.length)%articleImages.length;
    scale=1;fit=true;renderImage();
  }

  articleImages.forEach((img,i)=>{
    img.classList.add("zoomable-image");
    img.tabIndex=0;
    img.setAttribute("role","button");
    img.setAttribute("aria-label","이미지 크게 보기");
    img.addEventListener("click",()=>openViewer(i));
    img.addEventListener("keydown",e=>{
      if(e.key==="Enter"||e.key===" "){e.preventDefault();openViewer(i);}
    });
  });

  viewer.addEventListener("click",e=>{
    const action=e.target.closest("[data-action]")?.dataset.action;
    if(action==="close")closeViewer();
    else if(action==="prev")move(-1);
    else if(action==="next")move(1);
    else if(action==="minus")zoom(-.25);
    else if(action==="plus")zoom(.25);
    else if(action==="actual"){fit=false;scale=1;renderImage();}
    else if(action==="fit"){fit=true;scale=1;renderImage();}
    else if(e.target===viewer||e.target===stage)closeViewer();
  });

  stage.addEventListener("wheel",e=>{
    if(!viewer.classList.contains("open"))return;
    e.preventDefault();
    zoom(e.deltaY<0?.25:-.25);
  },{passive:false});

  large.addEventListener("dblclick",()=>{
    fit=!fit;scale=1;renderImage();
  });

  document.addEventListener("keydown",e=>{
    if(!viewer.classList.contains("open"))return;
    if(e.key==="Escape")closeViewer();
    else if(e.key==="ArrowLeft")move(-1);
    else if(e.key==="ArrowRight")move(1);
    else if(e.key==="+"||e.key==="=")zoom(.25);
    else if(e.key==="-")zoom(-.25);
    else if(e.key==="0"){fit=false;scale=1;renderImage();}
  });
}
