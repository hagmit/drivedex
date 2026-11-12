"""Export the index as one self-contained HTML file (search + keyboard browsing)."""
from __future__ import annotations

import json
from pathlib import Path

from .index import OTHER, Index, now_iso


def build_data(idx: Index) -> dict:
    sources, items = [], []
    for si, src in enumerate(idx.sources()):
        sources.append({"label": src.label, "provider": src.provider, "synced_at": src.synced_at})
        rows = idx.items(src.id)
        pos = {}
        for r in rows:
            pos[r["item_id"]] = len(items)
            items.append([si, None, r["name"], r["is_folder"], r["size"], r["mtime"], r["modified_by"], r["web_url"]])
        root = next((pos[r["item_id"]] for r in rows if r["parent_id"] is None), None)
        if root is None:  # never synced: show an empty root so the source is still listed
            root = len(items)
            items.append([si, -1, src.label, 1, None, None, None, None])
        other = None
        for r in rows:
            i = pos[r["item_id"]]
            if r["parent_id"] is None:
                items[i][1], items[i][2] = -1, src.label
            elif r["parent_id"] in pos:
                items[i][1] = pos[r["parent_id"]]
            else:
                if other is None:
                    other = len(items)
                    items.append([si, root, OTHER, 1, None, None, None, None])
                items[i][1] = other
    return {"generated": now_iso(), "sources": sources, "items": items}


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return TEMPLATE.replace("__DATA__", payload)


def export(idx: Index, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(build_data(idx)), "utf-8", newline="\n")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>drivedex</title>
<style>
:root{--bg:#fff;--fg:#1f2328;--mute:#656d76;--line:#d0d7de;--sel:#ddf4ff;--acc:#0969da;--star:#bf8700}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--line:#30363d;--sel:#1f3b5c;--acc:#4493f8;--star:#d29922}}
:root[data-theme=dark]{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--line:#30363d;--sel:#1f3b5c;--acc:#4493f8;--star:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Segoe UI","Yu Gothic UI",sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#q{flex:1;min-width:200px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
#info,#crumb,.mute{color:var(--mute);font-size:12px}#crumb{padding:6px 16px}#crumb a{color:var(--acc);cursor:pointer}
table{width:100%;border-collapse:collapse}td{padding:4px 16px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0}
tr.sel td{background:var(--sel)}td.n{width:55%}td.p{width:25%}td.m{width:12%}td.s{width:8%;text-align:right}
.star{color:var(--star)}.f{font-weight:600}tr{cursor:default}
@media (max-width:640px){td.p,td.m{display:none}td.n{width:80%}td.s{width:20%}}
</style></head><body>
<header><input id="q" placeholder="検索（スペース区切りで AND。/ で移動）" autocomplete="off">
<button id="starBtn" title="お気に入り (s で付け外し)">★</button><span id="info"></span></header>
<div id="crumb"></div><table><tbody id="list"></tbody></table>
<p class="mute" style="padding:8px 16px">キー: j/k 上下・Enter 開く・u/Backspace 上へ・s お気に入り・/ 検索・Esc 検索解除</p>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),I=D.items;
const kids=new Map();I.forEach((it,i)=>{const p=it[1];if(!kids.has(p))kids.set(p,[]);kids.get(p).push(i)});
const byName=(a,b)=>(I[b][3]-I[a][3])||I[a][2].localeCompare(I[b][2],'ja');kids.forEach(v=>v.sort(byName));
const pathCache=new Array(I.length);
function path(i){if(pathCache[i]!==undefined)return pathCache[i];const p=I[i][1];return pathCache[i]=p<0?'':path(p)+'/'+I[i][2]}
let lower=null;const KEY='drivedex.stars';
function loadStars(){try{return new Set(JSON.parse(localStorage.getItem(KEY)||'[]'))}catch(e){return new Set()}}
function saveStars(){try{localStorage.setItem(KEY,JSON.stringify([...stars]))}catch(e){}}
const stars=loadStars(),starKey=i=>I[i][7]||(D.sources[I[i][0]].label+path(i));
let cwd=-1,rows=[],sel=0,mode='browse';const q=document.getElementById('q'),list=document.getElementById('list');
const fmtSize=n=>n==null?'':n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const showPath=mode!=='browse';list.innerHTML=rows.slice(0,500).map((i,n)=>{const it=I[i];
    return `<tr data-n="${n}" class="${n===sel?'sel':''}"><td class="n ${it[3]?'f':''}">${stars.has(starKey(i))?'<span class=star>★</span> ':''}${it[3]?'📁':'📄'} ${esc(it[2])}</td>`+
    `<td class="p mute">${showPath?esc(D.sources[it[0]].label+path(i)):esc(it[6])}</td><td class="m mute">${esc((it[5]||'').slice(0,16).replace('T',' '))}</td><td class="s mute">${it[3]?'':fmtSize(it[4])}</td></tr>`}).join('');
  const c=document.getElementById('crumb');
  if(mode==='search')c.textContent=`検索結果 ${rows.length.toLocaleString()} 件`+(rows.length>500?'（先頭500件）':'');
  else if(mode==='stars')c.textContent=`お気に入り ${rows.length} 件`;
  else{const chain=[];for(let i=cwd;i>=0;i=I[i][1])chain.unshift(i);
    c.innerHTML='<a data-go="-1">すべて</a>'+chain.map(i=>` / <a data-go="${i}">${esc(I[i][2])}</a>`).join('')}
  const s=list.children[sel];if(s)s.scrollIntoView({block:'nearest'});
}
function browse(dir,focus){mode='browse';cwd=dir;rows=kids.get(dir)||[];sel=Math.max(0,rows.indexOf(focus));render()}
function search(){const t=q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);if(!t.length)return browse(cwd);
  if(!lower)lower=I.map((it,i)=>(D.sources[it[0]].label+path(i)).toLowerCase());
  const whole=t.join(' ');mode='search';rows=[];const score=new Map();
  for(let i=0;i<I.length;i++){if(I[i][1]<0)continue;const s=lower[i];if(!t.every(w=>s.includes(w)))continue;rows.push(i);
    const n=I[i][2].toLowerCase();score.set(i,n===whole?3:n.startsWith(t[0])&&t.every(w=>n.includes(w))?2:t.every(w=>n.includes(w))?1:0)}
  // exact name > prefix > name contains > path only (a folder otherwise drowns in its descendants), then newest
  rows.sort((a,b)=>(score.get(b)-score.get(a))||(I[b][5]||'').localeCompare(I[a][5]||''));sel=0;render()}
function open(i){if(i==null)return;if(I[i][3]){q.value='';browse(i)}else if(I[i][7])window.open(I[i][7],'_blank','noopener')}
function up(){if(mode!=='browse'){q.value='';return browse(cwd)}if(cwd>=0)browse(I[cwd][1],cwd)}
q.addEventListener('input',()=>{clearTimeout(q.t);q.t=setTimeout(search,150)});
q.addEventListener('keydown',e=>{if(e.key==='Escape'){q.value='';browse(cwd);q.blur()}else if(e.key==='Enter'||e.key==='ArrowDown'){e.preventDefault();q.blur()}});
document.getElementById('starBtn').onclick=()=>{mode='stars';rows=I.map((_,i)=>i).filter(i=>stars.has(starKey(i)));sel=0;render()};
list.addEventListener('click',e=>{const tr=e.target.closest('tr');if(tr){sel=+tr.dataset.n;render()}});
list.addEventListener('dblclick',e=>{const tr=e.target.closest('tr');if(tr)open(rows[+tr.dataset.n])});
document.getElementById('crumb').addEventListener('click',e=>{const a=e.target.closest('a');if(a)browse(+a.dataset.go)});
document.addEventListener('keydown',e=>{if(e.target===q||e.ctrlKey||e.metaKey||e.altKey)return;const k=e.key;
  if(k==='j'||k==='ArrowDown'){sel=Math.min(sel+1,Math.min(rows.length,500)-1)}else if(k==='k'||k==='ArrowUp'){sel=Math.max(sel-1,0)}
  else if(k==='Enter'||k==='o'||k==='ArrowRight'){open(rows[sel]);return e.preventDefault()}
  else if(k==='u'||k==='Backspace'||k==='ArrowLeft'){up();return e.preventDefault()}
  else if(k==='/'){q.focus();return e.preventDefault()}else if(k==='Escape'){q.value='';return browse(cwd)}
  else if(k==='s'&&rows[sel]!=null){const key=starKey(rows[sel]);stars.has(key)?stars.delete(key):stars.add(key);saveStars()}
  else return;e.preventDefault();render()});
document.getElementById('info').textContent=`${D.sources.length} ソース・${I.length.toLocaleString()} 件・${D.generated.slice(0,16).replace('T',' ')} 作成`;
browse(-1);
</script></body></html>
"""
