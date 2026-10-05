const search=document.querySelector("#search");
const cards=[...document.querySelectorAll(".card")];
const filters=[...document.querySelectorAll("[data-filter]")];
let activeFilter="all";

function apply(){
  const q=(search?.value||"").trim().toLowerCase();
  for(const card of cards){
    const matchText=!q||card.dataset.search.includes(q);
    const matchStatus=activeFilter==="all"||card.dataset.status===activeFilter;
    card.hidden=!(matchText&&matchStatus);
  }
}

if(search) search.addEventListener("input",apply);
for(const btn of filters){
  btn.addEventListener("click",()=>{
    activeFilter=btn.dataset.filter;
    for(const b of filters)b.classList.toggle("active",b===btn);
    apply();
  });
}
