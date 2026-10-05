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
