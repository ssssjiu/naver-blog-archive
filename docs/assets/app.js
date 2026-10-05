const search=document.querySelector("#search");
const cards=[...document.querySelectorAll(".card")];
const filters=[...document.querySelectorAll("[data-filter],[data-category]")];
let activeFilter="all";
let activeCategory="";

function apply(){
  const q=(search?.value||"").trim().toLowerCase();
  for(const card of cards){
    const matchText=!q||card.dataset.search.includes(q);
    const matchStatus=activeFilter==="all"||card.dataset.status===activeFilter;
    const matchCategory=!activeCategory||card.dataset.category===activeCategory;
    card.hidden=!(matchText&&matchStatus&&matchCategory);
  }
}
if(search) search.addEventListener("input",apply);
for(const btn of filters){
  btn.addEventListener("click",()=>{
    if(btn.dataset.category!==undefined){
      activeCategory=btn.dataset.category||"";
      activeFilter="all";
    }else{
      activeFilter=btn.dataset.filter||"all";
      activeCategory="";
    }
    for(const b of filters)b.classList.toggle("active",b===btn);
    apply();
  });
}

/* Naver-style post image viewer */
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
    '<button class="image-viewer-btn" data-action="actual">100%</button>',
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

  function render(){
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
      if(large.complete)setSize();
      else large.onload=setSize;
    }
  }
  function openViewer(i){
    current=i;scale=1;fit=true;
    viewer.classList.add("open");
    document.body.classList.add("viewer-open");
    render();
  }
  function closeViewer(){
    viewer.classList.remove("open");
    document.body.classList.remove("viewer-open");
  }
  function zoom(delta){
    fit=false;
    scale=Math.max(.25,Math.min(4,scale+delta));
    render();
  }
  function move(delta){
    current=(current+delta+articleImages.length)%articleImages.length;
    scale=1;fit=true;render();
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
    else if(action==="actual"){fit=false;scale=1;render();}
    else if(action==="fit"){fit=true;scale=1;render();}
    else if(e.target===viewer||e.target===stage)closeViewer();
  });

  stage.addEventListener("wheel",e=>{
    if(!viewer.classList.contains("open"))return;
    e.preventDefault();
    zoom(e.deltaY<0?.25:-.25);
  },{passive:false});

  large.addEventListener("dblclick",()=>{
    if(fit){fit=false;scale=1;}else{fit=true;scale=1;}
    render();
  });

  document.addEventListener("keydown",e=>{
    if(!viewer.classList.contains("open"))return;
    if(e.key==="Escape")closeViewer();
    else if(e.key==="ArrowLeft")move(-1);
    else if(e.key==="ArrowRight")move(1);
    else if(e.key==="+"||e.key==="=")zoom(.25);
    else if(e.key==="-")zoom(-.25);
    else if(e.key==="0"){fit=false;scale=1;render();}
  });
}
