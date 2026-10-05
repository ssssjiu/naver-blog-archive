const q=document.querySelector("#search");
if(q)q.addEventListener("input",()=>{const s=q.value.trim().toLowerCase();document.querySelectorAll(".card").forEach(c=>c.hidden=s&&!c.dataset.search.includes(s));});
