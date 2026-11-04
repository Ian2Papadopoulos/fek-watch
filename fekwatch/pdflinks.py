"""Links to FEK PDFs: a small menu to preview the PDF in a new tab (at the right page) or to download it.

The Printing Office serves its PDFs as application/octet-stream, so a plain link always downloads the file and
the "#page=33" part is lost. Inside the local app the PDF is relayed by our own server (/pdf), labelled as a PDF;
anywhere else (a report opened from disk or from GitHub Pages) the page fetches the file itself (the Printing
Office allows cross-origin requests) and shows or saves it from memory.
"""
from __future__ import annotations

import re

PDF_HOST = "https://ia37rg02wpsa01.blob.core.windows.net/fek/"
PDF_PATH_RE = re.compile(r"^(\d{2})/(\d{4})/(\d{4})(\d{2})(\d{5})\.pdf$")
GROUP_LETTER = {"01": "Α", "02": "Β", "03": "Γ", "04": "Δ", "05": "Δ.Δ.Σ.", "14": "Υ.Ο.Δ.Δ.", "15": "Α.Α.Π."}
# file names in Latin letters: every browser and operating system keeps them as they are
GROUP_FILE = {"01": "A", "02": "B", "03": "G", "04": "D", "05": "DDS", "14": "YODD", "15": "AAP"}


def check_url(url: str) -> str | None:
    """The path under PDF_HOST if `url` is a FEK PDF of the Printing Office, else None."""
    if not url.startswith(PDF_HOST):
        return None
    path = url[len(PDF_HOST):].split("#", 1)[0].split("?", 1)[0]
    return path if PDF_PATH_RE.match(path) else None


def filename(path: str) -> str:
    m = PDF_PATH_RE.match(path)
    if not m:
        return "FEK.pdf"
    group, year, number = m.group(1), m.group(2), int(m.group(5))
    return f"FEK-{GROUP_FILE.get(group, group)}-{number}-{year}.pdf"


CSS = """
.pdfmenu{position:absolute;z-index:60;min-width:240px;padding:6px;background:var(--surface,#fff);color:var(--text,#131416);
border:1px solid var(--border,#e6e8eb);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.14);font:400 14px/1.4 inherit} /* taste-skill-ignore: a menu floats */
.pdfmenu .pm-title{padding:6px 12px 8px;color:var(--muted,#5b5e61);font-size:12.5px}
.pdfmenu button{display:block;width:100%;text-align:left;background:none;border:0;padding:9px 12px;border-radius:6px;cursor:pointer;font:inherit;color:inherit}
.pdfmenu button:hover,.pdfmenu button:focus-visible{background:var(--raised,#f4f6f8);outline:none}
.pdfmenu .pm-note{padding:4px 12px 6px;color:var(--muted,#5b5e61);font-size:12.5px}
"""

JS = r"""
(function(){
 if (window.__fekPdf) return; window.__fekPdf = true;
 const HOST = "https://ia37rg02wpsa01.blob.core.windows.net/fek/";
 const GROUP = {"01":"Α","02":"Β","03":"Γ","04":"Δ","05":"Δ.Δ.Σ.","14":"Υ.Ο.Δ.Δ.","15":"Α.Α.Π."};
 const FILE = {"01":"A","02":"B","03":"G","04":"D","05":"DDS","14":"YODD","15":"AAP"};
 const APP = /^(127\.0\.0\.1|localhost)(:\d+)?$/.test(location.host);  // served by the local app: use its /pdf relay
 let menu = null, opener = null;
 function info(href){
  const u = href.split("#")[0], m = u.slice(HOST.length).match(/^(\d{2})\/(\d{4})\/\d{6}(\d{5})\.pdf$/);
  const page = (href.match(/#page=(\d+)/) || [])[1];
  const label = m ? `ΦΕΚ ${GROUP[m[1]] || m[1]} ${parseInt(m[3], 10)}/${m[2]}` : "ΦΕΚ";
  const file = m ? `FEK-${FILE[m[1]] || m[1]}-${parseInt(m[3], 10)}-${m[2]}.pdf` : "FEK.pdf";
  return {url: u, page, label, file};
 }
 function close(){ if (menu){ menu.remove(); menu = null; if (opener) opener.focus(); opener = null; } }
 async function fetchPdf(url){
  const r = await fetch(url); if (!r.ok) throw new Error(r.status);
  return URL.createObjectURL(new Blob([await r.arrayBuffer()], {type: "application/pdf"}));
 }
 function preview(i){
  const frag = i.page ? "#page=" + i.page : "";
  if (APP) { window.open("/pdf?u=" + encodeURIComponent(i.url) + frag, "_blank", "noopener"); return; }
  const w = window.open("", "_blank");
  if (w) w.document.write(`<title>${i.label}</title><p style="font:16px system-ui;padding:24px">Φόρτωση του ${i.label}…</p>`);
  fetchPdf(i.url).then(b => { if (w) w.location.href = b + frag; else location.href = b + frag; })
   .catch(() => { if (w) w.location.href = i.url; });
 }
 function download(i){
  const save = href => { const a = document.createElement("a"); a.href = href; a.download = i.file;
   document.body.appendChild(a); a.click(); a.remove(); };
  if (APP) { save("/pdf?dl=1&u=" + encodeURIComponent(i.url)); return; }
  fetchPdf(i.url).then(b => { save(b); setTimeout(() => URL.revokeObjectURL(b), 60000); })
   .catch(() => { location.href = i.url; });
 }
 function open(a){
  close(); const i = info(a.href); opener = a;
  menu = document.createElement("div"); menu.className = "pdfmenu"; menu.setAttribute("role", "menu");
  menu.innerHTML = `<div class="pm-title">${i.label}${i.page ? " · σελ. " + i.page : ""}</div>
   <button role="menuitem" data-a="view">Προβολή σε νέα καρτέλα${i.page ? " (σελ. " + i.page + ")" : ""}</button>
   <button role="menuitem" data-a="dl">Λήψη PDF</button>`;
  document.body.appendChild(menu);
  const r = a.getBoundingClientRect(), mw = menu.offsetWidth;
  menu.style.left = Math.max(8, Math.min(r.left + scrollX, scrollX + document.documentElement.clientWidth - mw - 8)) + "px";
  menu.style.top = (r.bottom + scrollY + 6) + "px";
  menu.querySelector("[data-a=view]").onclick = () => { close(); preview(i); };
  menu.querySelector("[data-a=dl]").onclick = () => { close(); download(i); };
  menu.querySelector("button").focus();
 }
 document.addEventListener("click", e => {
  if (menu && !menu.contains(e.target)) close();
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a || !a.href.startsWith(HOST) || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
  e.preventDefault(); e.stopPropagation(); open(a);
 }, true);
 document.addEventListener("keydown", e => {
  if (!menu) return;
  if (e.key === "Escape") { e.preventDefault(); close(); }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault();
   const b = [...menu.querySelectorAll("button")], k = b.indexOf(document.activeElement);
   b[(k + (e.key === "ArrowDown" ? 1 : b.length - 1)) % b.length].focus(); }
 });
 addEventListener("resize", close);
})();
"""


def snippet() -> str:
    """<style> and <script> to put at the end of any page that links to FEK PDFs."""
    return f"<style>{CSS}</style><script>{JS}</script>"
