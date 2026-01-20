function esc(s){
  return String(s ?? "").replace(/[&<>"']/g, (c)=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

async function getJSON(url){
  const res = await fetch(url, {headers:{"Accept":"application/json"}});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function postJSON(url, body){
  const res = await fetch(url, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function fmtTime(iso){
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}
function postCard(p){
  return `
    <article class="post" data-id="${p.id}" data-text="${esc((p.author||"")+" "+(p.content||""))}">
      <div class="post-top">
        <div>
          <div class="author"><i class="fa-solid fa-user"></i> ${esc(p.author||"Anonymous")}</div>
          <div class="time">${esc(fmtTime(p.created_at))}</div>
        </div>
        <div class="meta"></div>
      </div>
      <div class="content">${esc(p.content||"")}</div>
      <div class="post-actions">
        <span><i class="fa-regular fa-thumbs-up"></i> Like</span>
        <span><i class="fa-regular fa-comment"></i> Comment</span>
        <span><i class="fa-solid fa-share"></i> Share</span>
      </div>
    </article>
  `;
}

function upsert(feedEl, post){
  if (feedEl.querySelector(`.post[data-id="${post.id}"]`)) return;
  feedEl.insertAdjacentHTML("afterbegin", postCard(post));
}

function applySearch(feedEl, q){
  const query = (q||"").toLowerCase().trim();
  const cards = feedEl.querySelectorAll(".post");
  cards.forEach(c => {
    const t = (c.getAttribute("data-text")||"").toLowerCase();
    c.style.display = (!query || t.includes(query)) ? "block" : "none";
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  const meName = document.getElementById("me-name");
  const feedEl = document.getElementById("feed");
  const searchEl = document.getElementById("search");
  const logoutBtn = document.getElementById("logout-btn");

  const postText = document.getElementById("post-text");
  const postBtn = document.getElementById("post-btn");
  const postErr = document.getElementById("post-error");

  async function loadMe(){
    const me = await getJSON("/api/me");
    if (!me.role){
      window.location.href = "/user_login.html";
      return null;
    }
    meName.textContent = me.name || "User";
    return me;
  }

  async function loadInitial(){
    const data = await getJSON("/api/posts?include_filtered=1&limit=200");
    (data.posts||[]).forEach(p => {
      feedEl.insertAdjacentHTML("beforeend", postCard(p));
    });
  }

  searchEl.addEventListener("input", ()=>applySearch(feedEl, searchEl.value));

  logoutBtn.addEventListener("click", async ()=>{
    try { await postJSON("/api/auth/logout", {}); } catch {}
    window.location.href = "/";
  });

  postBtn.addEventListener("click", async ()=>{
    postErr.style.display = "none";
    const content = (postText.value||"").trim();
    if (!content){
      postErr.textContent = "Please type a report.";
      postErr.style.display = "block";
      return;
    }
    try {
      const res = await postJSON("/api/posts", {content});
      upsert(feedEl, res.post);
      postText.value = "";
      applySearch(feedEl, searchEl.value);
    } catch (err){
      postErr.textContent = err.message || "Failed to post.";
      postErr.style.display = "block";
    }
  });

  const me = await loadMe();
  if (!me) return;
  await loadInitial();

  const ev = new EventSource("/api/stream");
  ev.onmessage = (msg)=>{
    try {
      const data = JSON.parse(msg.data);
      if (data.type==="new_post" && data.post){
        upsert(feedEl, data.post);
        applySearch(feedEl, searchEl.value);
      }
    } catch {}
  };
});
