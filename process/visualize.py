"""
Visualize — Scan agent + tool yaml files and generate a network graph.

Reads:
  process/agent/spec/*.yaml   (agent atoms — colored blue)
  process/tool/spec/*.yaml    (tool atoms — colored green)

Connections:
  Recursively scans all field values (including nested dicts/lists).
  If any leaf string matches another node's id → edge.
  "why" field = forward edge (solid arrow, left → right)
  All other fields = backward edge (dashed arrow, right → left)

Display:
  Nested fields with "value" key use that for display (e.g. what.value).

Graph:
  Drag nodes freely — they stay where you drop them. No physics.
  Scroll to zoom. Drag background to pan.

Usage:
  python3 process/visualize.py
  open network.html
"""

import glob
import json
import os

import yaml


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)


# ─────────────────────────────────────────────────────────────────
# 1. SCAN
# ─────────────────────────────────────────────────────────────────

def scan_yaml_files():
    nodes = []
    sources = [
        (os.path.join(PROJECT_ROOT, "process", "agent", "spec"), "agent"),
        (os.path.join(PROJECT_ROOT, "process", "tool", "spec"), "tool"),
    ]
    for folder, source_type in sources:
        for filepath in sorted(glob.glob(os.path.join(folder, "*.yaml"))):
            with open(filepath, "r") as f:
                data = yaml.safe_load(f) or {}
            node_id = data.get("id", os.path.splitext(os.path.basename(filepath))[0])
            nodes.append({"id": node_id, "source": source_type, "fields": data})

    return nodes, {n["id"]: n for n in nodes}


# ─────────────────────────────────────────────────────────────────
# 2. CONNECT
# ─────────────────────────────────────────────────────────────────

def extract_refs(value, all_ids):
    """Recursively walk nested dicts/lists, collect any leaf string matching a node id."""
    refs = []
    if isinstance(value, str):
        if value in all_ids:
            refs.append(value)
    elif isinstance(value, list):
        for item in value:
            refs.extend(extract_refs(item, all_ids))
    elif isinstance(value, dict):
        for item in value.values():
            refs.extend(extract_refs(item, all_ids))
    return refs


def find_edges(nodes, nodes_by_id):
    edges = []
    all_ids = set(nodes_by_id.keys())
    for node in nodes:
        for key, value in node["fields"].items():
            if key == "id":
                continue
            for ref in extract_refs(value, all_ids):
                if key == "why":
                    edges.append({"source": node["id"], "target": ref, "direction": "forward", "field": key})
                else:
                    edges.append({"source": ref, "target": node["id"], "direction": "backward", "field": key})

    seen = set()
    unique = []
    for e in edges:
        pair = (e["source"], e["target"])
        if pair not in seen:
            seen.add(pair)
            unique.append(e)
    return unique


# ─────────────────────────────────────────────────────────────────
# 3. DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────

def display_what(fields, fallback):
    """Prefer what.value (nested), then what (string), then fallback."""
    what = fields.get("what")
    if isinstance(what, dict):
        val = what.get("value")
        if isinstance(val, str) and val.strip():
            return val
    if isinstance(what, str) and what.strip():
        return what
    return fallback


def format_fields(fields):
    """Render nested yaml into readable tooltip text."""
    lines = []
    for k, v in fields.items():
        if k == "id":
            continue
        lines.append(f"{k}: {_fmt(v, 0)}")
    return "\\n".join(lines)


def _fmt(value, depth):
    if isinstance(value, dict):
        parts = [f"{'  ' * (depth + 1)}{k}: {_fmt(v, depth + 1)}" for k, v in value.items()]
        return "\\n" + "\\n".join(parts)
    elif isinstance(value, list):
        parts = [f"{'  ' * (depth + 1)}- {_fmt(i, depth + 1)}" for i in value]
        return "\\n" + "\\n".join(parts)
    return str(value)


# ─────────────────────────────────────────────────────────────────
# 4. LAYOUT — initial positions
# ─────────────────────────────────────────────────────────────────

def compute_layout(nodes, edges):
    all_ids = {n["id"] for n in nodes}

    # Roots: no why refs inside graph
    roots = set()
    for node in nodes:
        refs = extract_refs(node["fields"].get("why"), all_ids)
        if not refs or not any(r in all_ids for r in refs):
            roots.add(node["id"])

    # Layer assignment via forward edges
    layers = {r: 0 for r in roots}
    changed = True
    while changed:
        changed = False
        for e in edges:
            if e["direction"] == "forward" and e["target"] in layers:
                nl = layers[e["target"]] + 1
                if e["source"] not in layers or layers[e["source"]] < nl:
                    layers[e["source"]] = nl
                    changed = True
    for n in nodes:
        if n["id"] not in layers:
            layers[n["id"]] = 0

    max_layer = max(layers.values()) if layers else 0
    GAP_X, GAP_Y, PAD = 300, 80, 100

    groups = {}
    for nid, layer in layers.items():
        groups.setdefault(layer, []).append(nid)
    for layer in groups:
        groups[layer].sort()

    max_group = max(len(g) for g in groups.values()) if groups else 1
    positions = {}
    for layer, nids in groups.items():
        x = PAD + (max_layer - layer) * GAP_X
        total_h = len(nids) * 56 + (len(nids) - 1) * GAP_Y
        off = (max_group * (56 + GAP_Y) - GAP_Y) / 2 - total_h / 2
        for i, nid in enumerate(nids):
            positions[nid] = (x, PAD + off + i * (56 + GAP_Y))
    return positions


# ─────────────────────────────────────────────────────────────────
# 5. GENERATE HTML
# ─────────────────────────────────────────────────────────────────

def generate_html(nodes, edges, positions):
    js_nodes = []
    for n in nodes:
        x, y = positions.get(n["id"], (100, 100))
        js_nodes.append({
            "id": n["id"], "source": n["source"], "x": x, "y": y,
            "what": display_what(n["fields"], n["id"]),
            "fields": format_fields(n["fields"]),
        })
    js_edges = [{"source": e["source"], "target": e["target"],
                 "direction": e["direction"], "field": e["field"]} for e in edges]
    return HTML.replace("/*__DATA__*/", f"const G = {json.dumps({'nodes': js_nodes, 'edges': js_edges})};")


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>LedgerClaw — Knowledge Network</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#fff;color:#1e293b;font-family:-apple-system,system-ui,sans-serif;overflow:hidden}
svg{width:100vw;height:100vh}
.edge{stroke-width:1.5;fill:none}
.ef{stroke:#94a3b8}
.eb{stroke:#cbd5e1;stroke-dasharray:6 3}
.node rect{rx:8;ry:8;stroke-width:2;cursor:grab}
.node rect:active{cursor:grabbing}
.node text{pointer-events:none}
.nm{font-weight:600;font-size:12px;fill:#fff}
.ns{font-size:10px;fill:rgba(255,255,255,.7)}
.sa rect{fill:#2563eb;stroke:#1d4ed8}
.st rect{fill:#059669;stroke:#047857}
.tip{position:fixed;background:#fff;border:1px solid #e2e8f0;border-radius:8px;
  padding:14px 18px;max-width:380px;pointer-events:none;display:none;
  box-shadow:0 4px 16px rgba(0,0,0,.08);z-index:100;font-size:13px}
.ti{font-weight:700;font-size:15px;color:#0f172a;margin-bottom:4px}
.ts{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.tf{white-space:pre-line;color:#334155;line-height:1.6}
.hdr{position:fixed;top:20px;left:24px}
.hdr h1{font-size:16px;font-weight:700}
.hdr p{font-size:11px;color:#94a3b8;margin-top:3px}
.leg{position:fixed;bottom:20px;left:24px;display:flex;gap:18px;font-size:11px;color:#64748b}
.leg span{display:flex;align-items:center;gap:5px}
.leg i{width:12px;height:12px;border-radius:3px;display:inline-block}
.ht{position:fixed;bottom:20px;right:24px;font-size:11px;color:#cbd5e1}
</style></head><body>
<div class="hdr"><h1>LedgerClaw — Knowledge Network</h1>
<p>← How (backward) | Why (forward) → · Drag nodes to rearrange</p></div>
<div class="leg">
<span><i style="background:#2563eb"></i>Agent</span>
<span><i style="background:#059669"></i>Tool</span>
<span>— why (forward)</span>
<span>┈ other (backward)</span>
</div>
<div class="ht">Scroll to zoom · Drag background to pan</div>
<div class="tip" id="tip"><div class="ts" id="ts"></div><div class="ti" id="ti"></div><div class="tf" id="tf"></div></div>
<svg id="svg"></svg>
<script>
/*__DATA__*/

const W=200,H=56,NS="http://www.w3.org/2000/svg";
const nmap={};G.nodes.forEach(n=>nmap[n.id]=n);
G.edges.forEach(e=>{e.s=nmap[e.source];e.t=nmap[e.target]});
const $=t=>document.createElementNS(NS,t);
const svg=document.getElementById("svg");
const tip=document.getElementById("tip");

// — View (pan + zoom) —
let vx=0,vy=0,vk=1;
const root=$("g");svg.appendChild(root);
function av(){root.setAttribute("transform",`translate(${vx},${vy})scale(${vk})`)}
function tw(cx,cy){const r=svg.getBoundingClientRect();return{x:(cx-r.left-vx)/vk,y:(cy-r.top-vy)/vk}}

// Center
(function(){
  if(!G.nodes.length)return;
  const mx=Math.min(...G.nodes.map(n=>n.x)),Mx=Math.max(...G.nodes.map(n=>n.x));
  const my=Math.min(...G.nodes.map(n=>n.y)),My=Math.max(...G.nodes.map(n=>n.y));
  vx=innerWidth/2-(mx+Mx+W)/2;vy=innerHeight/2-(my+My+H)/2;av();
})();

// — Arrows —
const defs=$("defs");svg.appendChild(defs);
function mkA(id,c){const m=$("marker");m.setAttribute("id",id);m.setAttribute("viewBox","0 0 10 10");
m.setAttribute("refX",10);m.setAttribute("refY",5);m.setAttribute("markerWidth",6);m.setAttribute("markerHeight",6);
m.setAttribute("orient","auto");const p=$("path");p.setAttribute("d","M0 0L10 5L0 10z");p.setAttribute("fill",c);
m.appendChild(p);defs.appendChild(m)}
mkA("af","#94a3b8");mkA("ab","#cbd5e1");

// — Edge path —
function ep(e){if(!e.s||!e.t)return"";
const x1=e.s.x+W,y1=e.s.y+H/2,x2=e.t.x,y2=e.t.y+H/2,mx=(x1+x2)/2;
return`M${x1},${y1}C${mx},${y1} ${mx},${y2} ${x2},${y2}`}

// — Draw edges —
const eg=$("g");root.appendChild(eg);
G.edges.forEach(e=>{const p=$("path");
p.setAttribute("class","edge "+(e.direction==="forward"?"ef":"eb"));
p.setAttribute("marker-end",e.direction==="forward"?"url(#af)":"url(#ab)");
p.setAttribute("d",ep(e));e.el=p;eg.appendChild(p)});

// — Draw nodes —
const ng=$("g");root.appendChild(ng);
function tr(s,n){return s.length>n?s.slice(0,n-1)+"…":s}

G.nodes.forEach(n=>{
  const g=$("g");g.setAttribute("class","node "+(n.source==="agent"?"sa":"st"));
  g.setAttribute("transform",`translate(${n.x},${n.y})`);
  const r=$("rect");r.setAttribute("width",W);r.setAttribute("height",H);g.appendChild(r);
  const t1=$("text");t1.setAttribute("class","nm");t1.setAttribute("x",W/2);t1.setAttribute("y",H/2-4);
  t1.setAttribute("text-anchor","middle");t1.textContent=tr(n.what,26);g.appendChild(t1);
  const t2=$("text");t2.setAttribute("class","ns");t2.setAttribute("x",W/2);t2.setAttribute("y",H/2+12);
  t2.setAttribute("text-anchor","middle");t2.textContent=tr(n.id.replace(/_/g," "),28);g.appendChild(t2);

  g.addEventListener("mouseenter",ev=>{
    document.getElementById("ts").textContent=n.source+" atom";
    document.getElementById("ti").textContent=n.id;
    document.getElementById("tf").textContent=n.fields;
    tip.style.display="block";tip.style.left=(ev.clientX+16)+"px";tip.style.top=(ev.clientY-10)+"px"});
  g.addEventListener("mousemove",ev=>{tip.style.left=(ev.clientX+16)+"px";tip.style.top=(ev.clientY-10)+"px"});
  g.addEventListener("mouseleave",()=>{tip.style.display="none"});
  n.el=g;ng.appendChild(g)
});

// — Drag nodes (no physics) —
let drag=null,pan=null;

ng.addEventListener("pointerdown",ev=>{
  const tgt=ev.target.closest(".node");if(!tgt)return;
  ev.stopPropagation();
  const n=G.nodes.find(n=>n.el===tgt);if(!n)return;
  const w=tw(ev.clientX,ev.clientY);
  drag={n,ox:w.x-n.x,oy:w.y-n.y};
  tgt.setPointerCapture(ev.pointerId)
});

svg.addEventListener("pointermove",ev=>{
  if(drag){
    const w=tw(ev.clientX,ev.clientY);
    drag.n.x=w.x-drag.ox;drag.n.y=w.y-drag.oy;
    drag.n.el.setAttribute("transform",`translate(${drag.n.x},${drag.n.y})`);
    G.edges.forEach(e=>{if(e.s===drag.n||e.t===drag.n)e.el.setAttribute("d",ep(e))});
    return}
  if(pan){vx=pan.vx+(ev.clientX-pan.sx);vy=pan.vy+(ev.clientY-pan.sy);av()}
});

svg.addEventListener("pointerup",()=>{drag=null;pan=null});
svg.addEventListener("pointercancel",()=>{drag=null;pan=null});

// — Pan background —
svg.addEventListener("pointerdown",ev=>{
  if(drag)return;
  pan={sx:ev.clientX,sy:ev.clientY,vx,vy};
  svg.setPointerCapture(ev.pointerId)
});

// — Zoom —
svg.addEventListener("wheel",ev=>{
  ev.preventDefault();
  const r=svg.getBoundingClientRect();
  const px=ev.clientX-r.left,py=ev.clientY-r.top;
  const wx=(px-vx)/vk,wy=(py-vy)/vk;
  vk=Math.min(4,Math.max(.15,vk*(ev.deltaY<0?1.1:.9)));
  vx=px-wx*vk;vy=py-wy*vk;av()
},{passive:false});
</script></body></html>"""


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    nodes, nodes_by_id = scan_yaml_files()
    print(f"Scanned {len(nodes)} atoms ({sum(1 for n in nodes if n['source']=='agent')} agent, {sum(1 for n in nodes if n['source']=='tool')} tool)")
    edges = find_edges(nodes, nodes_by_id)
    print(f"Found {len(edges)} connections ({sum(1 for e in edges if e['direction']=='forward')} forward, {sum(1 for e in edges if e['direction']=='backward')} backward)")
    positions = compute_layout(nodes, edges)
    html = generate_html(nodes, edges, positions)
    output_path = os.path.join(PROJECT_ROOT, "network.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"Generated: {output_path}")

if __name__ == "__main__":
    main()
