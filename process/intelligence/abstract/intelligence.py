"""
Intelligence Abstract — Core brain of LedgerClaw.

Two core systems:

A. LLM Providers — send prompts, receive responses
   1. Data Structures  — LLMMessage, ToolSchema, ToolCall, LLMResponse
   2. Provider          — base class for LLM providers (Ollama, Anthropic, etc.)
   3. IntelligenceManager — provider selection, fallback, setup

B. Knowledge Network — goal decomposition graph
   4. KnowledgeAtom    — one node: What, Why (→ right), How (← left)
   5. KnowledgeNetwork — load, navigate, query, modify, validate, visualize

The knowledge network is Intelligence's planning layer.
Every usecase has an address in this network.
LLM reasons by traversing it.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import yaml


# ═════════════════════════════════════════════════════════════════
# A. LLM PROVIDERS
# ═════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────
# 1. DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────

@dataclass
class LLMMessage:
    role: str          # "system", "user", "assistant", "tool_result"
    content: str
    tool_call_id: str = ""

@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_used: int = 0
    model: str = ""
    provider: str = ""


# ─────────────────────────────────────────────────────────────────
# 2. PROVIDER BASE CLASS
# ─────────────────────────────────────────────────────────────────

class Provider(ABC):

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec.get("name", "unknown")
        self.priority = spec.get("priority", 99)

    @abstractmethod
    def _platform_setup(self, user_dir: str) -> bool:
        """IMPLEMENT THIS — Validate provider config."""
        pass

    @abstractmethod
    def _platform_health(self) -> bool:
        """IMPLEMENT THIS — Check if provider is reachable."""
        pass

    @abstractmethod
    def _platform_complete(self, messages: list[LLMMessage], tools: list[ToolSchema] = None) -> LLMResponse:
        """IMPLEMENT THIS — Send messages + tools, return response."""
        pass


# ─────────────────────────────────────────────────────────────────
# 3. INTELLIGENCE MANAGER
# ─────────────────────────────────────────────────────────────────

class IntelligenceManager:

    def __init__(self, user_dir: str):
        self.user_dir = os.path.abspath(user_dir)
        self.config_path = os.path.join(self.user_dir, "config.json")
        self.providers: list[Provider] = []

    # — Config —

    def load_config(self) -> Optional[dict]:
        if not os.path.exists(self.config_path):
            return None
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def save_config(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    # — Setup —

    def load_providers(self, specs: list[dict], provider_classes: list[type]) -> None:
        self.providers = []
        for spec, cls in zip(specs, provider_classes):
            self.providers.append(cls(spec))
        self.providers.sort(key=lambda p: p.priority)

    def setup(self) -> bool:
        if not self.providers:
            print("No providers loaded.")
            return False

        config = self.load_config()
        if config and config.get("default_provider"):
            print(f"Intelligence configured. Default: {config['default_provider']}")
            return self._validate_providers()

        print("\nAvailable LLM providers:")
        for i, p in enumerate(self.providers):
            label = "local" if p.spec.get("local", False) else "cloud"
            print(f"  {i + 1}. {p.name} ({label}) — model: {p.spec.get('model', '?')}")

        choice = input("\nChoose default provider (number): ").strip()
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(self.providers):
                raise ValueError()
        except ValueError:
            print("Invalid choice.")
            return False

        chosen = self.providers[idx]
        if not chosen._platform_setup(self.user_dir):
            return False

        api_key_env = chosen.spec.get("api_key_env")
        if api_key_env:
            env_path = os.path.join(self.user_dir, ".env")
            if not self._read_env_key(env_path, api_key_env):
                key = input(f"Enter your {api_key_env}: ").strip()
                if not key:
                    print("API key required.")
                    return False
                self._write_env_key(env_path, api_key_env, key)

        self.save_config({"default_provider": chosen.name, "model": chosen.spec.get("model", "")})
        self.providers.sort(key=lambda p: 0 if p.name == chosen.name else p.priority)
        print(f"Default provider: {chosen.name}")
        return True

    def _validate_providers(self) -> bool:
        for p in self.providers:
            if p._platform_health():
                return True
        print("Warning: no providers reachable.")
        return False

    # — Connect —

    def connect(self) -> bool:
        healthy = [p.name for p in self.providers if p._platform_health()]
        if healthy:
            print(f"Providers available: {', '.join(healthy)}")
            return True
        print("No providers available.")
        return False

    def _select_provider(self) -> Optional[Provider]:
        config = self.load_config()
        default_name = config.get("default_provider") if config else None
        if default_name:
            for p in self.providers:
                if p.name == default_name and p._platform_health():
                    return p
        for p in self.providers:
            if p._platform_health():
                return p
        return None

    # — Complete —

    def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] = None) -> Optional[LLMResponse]:
        errors = []
        for provider in self.providers:
            if not provider._platform_health():
                continue
            try:
                response = provider._platform_complete(messages, tools)
                response.provider = provider.name
                return response
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                continue
        print(f"All providers failed: {'; '.join(errors)}" if errors else "No providers available.")
        return None

    # — Helpers —

    def _read_env_key(self, env_path: str, key_name: str) -> Optional[str]:
        if not os.path.exists(env_path):
            return None
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith(f"{key_name}="):
                    return line.split("=", 1)[1].strip()
        return None

    def _write_env_key(self, env_path: str, key_name: str, value: str) -> None:
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if line.strip().startswith(f"{key_name}="):
                        lines.append(f"{key_name}={value}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{key_name}={value}\n")
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.writelines(lines)


# ═════════════════════════════════════════════════════════════════
# B. KNOWLEDGE NETWORK
# ═════════════════════════════════════════════════════════════════
#
# A goal decomposition graph.
# Each atom: What (this goal), Why (→ serves which parent goals), How (← achieved by which sub-goals).
# Layers (left → right): task → agent → capability → pillar → objective
#
# Read rightward (Why):  "Why fetch Gmail?" → briefer → awareness → income mgmt → best finance
# Read leftward (How):   "How best finance?" → manage income → awareness → briefer → fetch Gmail
#
# New usecase = insert atom + connect it. Network grows. Nothing existing changes.


# ─────────────────────────────────────────────────────────────────
# 4. KNOWLEDGE ATOM
# ─────────────────────────────────────────────────────────────────

LAYER_ORDER = ["task", "agent", "capability", "pillar", "objective"]

@dataclass
class KnowledgeAtom:
    """One node in the knowledge network."""
    id: str                                        # unique identifier (snake_case)
    layer: str                                     # task / agent / capability / pillar / objective
    what: str                                      # what this goal is
    why: list[str] = field(default_factory=list)   # → IDs of parent goals (rightward)
    how: list[str] = field(default_factory=list)   # ← IDs of sub-goals (leftward)
    status: str = "active"                         # active / planned / idea


# ─────────────────────────────────────────────────────────────────
# 5. KNOWLEDGE NETWORK
# ─────────────────────────────────────────────────────────────────

class KnowledgeNetwork:
    """
    Goal decomposition graph.

    Usage:
      net = KnowledgeNetwork("path/to/network.yaml")
      net.load()                                     # parse YAML → in-memory graph
      atom = net.get("awareness")                    # get one atom
      path = net.path_to_objective("fetch_gmail")    # walk rightward to objective
      net.add(KnowledgeAtom(...))                    # grow the network
      net.save()                                     # write back to YAML
      net.visualize("network.html")                  # generate interactive graph
    """

    def __init__(self, yaml_path: str):
        self.yaml_path = os.path.abspath(yaml_path)
        self.atoms: dict[str, KnowledgeAtom] = {}

    # ─────────────────────────────────────────────────────────────
    # LOAD / SAVE
    # ─────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Parse network.yaml → in-memory graph."""
        if not os.path.exists(self.yaml_path):
            print(f"Network file not found: {self.yaml_path}")
            return

        with open(self.yaml_path, "r") as f:
            raw = yaml.safe_load(f) or []

        self.atoms = {}
        for entry in raw:
            atom = KnowledgeAtom(
                id=entry["id"],
                layer=entry.get("layer", "task"),
                what=entry.get("what", ""),
                why=entry.get("why", []),
                how=entry.get("how", []),
                status=entry.get("status", "active"),
            )
            self.atoms[atom.id] = atom

    def save(self) -> None:
        """Write graph back to network.yaml."""
        data = []
        for atom in sorted(self.atoms.values(),
                           key=lambda a: (-LAYER_ORDER.index(a.layer), a.id)):
            entry = {"id": atom.id, "layer": atom.layer, "what": atom.what}
            if atom.why:
                entry["why"] = atom.why
            if atom.how:
                entry["how"] = atom.how
            if atom.status != "active":
                entry["status"] = atom.status
            data.append(entry)

        os.makedirs(os.path.dirname(self.yaml_path) or ".", exist_ok=True)
        with open(self.yaml_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # ─────────────────────────────────────────────────────────────
    # NAVIGATE — walk the graph
    # ─────────────────────────────────────────────────────────────

    def get(self, atom_id: str) -> Optional[KnowledgeAtom]:
        """Get one atom by ID."""
        return self.atoms.get(atom_id)

    def get_why(self, atom_id: str) -> list[KnowledgeAtom]:
        """Walk RIGHT — what larger goals does this serve?"""
        atom = self.atoms.get(atom_id)
        if not atom:
            return []
        return [self.atoms[ref] for ref in atom.why if ref in self.atoms]

    def get_how(self, atom_id: str) -> list[KnowledgeAtom]:
        """Walk LEFT — what sub-goals achieve this?"""
        atom = self.atoms.get(atom_id)
        if not atom:
            return []
        return [self.atoms[ref] for ref in atom.how if ref in self.atoms]

    def path_to_objective(self, atom_id: str) -> list[KnowledgeAtom]:
        """Walk rightward from atom to the objective. Returns the path."""
        path = []
        visited = set()
        current = self.atoms.get(atom_id)

        while current and current.id not in visited:
            path.append(current)
            visited.add(current.id)
            if current.layer == "objective":
                break
            if current.why:
                current = self.atoms.get(current.why[0])
            else:
                break

        return path

    def explain_why(self, atom_id: str) -> str:
        """Human-readable why-chain from atom to objective."""
        path = self.path_to_objective(atom_id)
        if not path:
            return f"Atom '{atom_id}' not found."
        return " → ".join(a.what for a in path)

    # ─────────────────────────────────────────────────────────────
    # QUERY — find atoms
    # ─────────────────────────────────────────────────────────────

    def by_layer(self, layer: str) -> list[KnowledgeAtom]:
        return [a for a in self.atoms.values() if a.layer == layer]

    def by_status(self, status: str) -> list[KnowledgeAtom]:
        return [a for a in self.atoms.values() if a.status == status]

    def search(self, keyword: str) -> list[KnowledgeAtom]:
        kw = keyword.lower()
        return [a for a in self.atoms.values() if kw in a.what.lower() or kw in a.id.lower()]

    def leaves(self) -> list[KnowledgeAtom]:
        """Atoms with no 'how' — directly executable."""
        return [a for a in self.atoms.values() if not a.how]

    def roots(self) -> list[KnowledgeAtom]:
        """Atoms with no 'why' — top-level objectives."""
        return [a for a in self.atoms.values() if not a.why]

    # ─────────────────────────────────────────────────────────────
    # MODIFY — grow the network
    # ─────────────────────────────────────────────────────────────

    def add(self, atom: KnowledgeAtom) -> bool:
        """Add atom. Auto-updates bidirectional links."""
        if atom.id in self.atoms:
            print(f"Atom '{atom.id}' already exists.")
            return False
        if atom.layer not in LAYER_ORDER:
            print(f"Invalid layer '{atom.layer}'. Must be: {LAYER_ORDER}")
            return False

        self.atoms[atom.id] = atom

        # Bidirectional: if I serve X (why), X's how should include me
        for why_id in atom.why:
            if why_id in self.atoms and atom.id not in self.atoms[why_id].how:
                self.atoms[why_id].how.append(atom.id)

        # Bidirectional: if Y achieves me (how), Y's why should include me
        for how_id in atom.how:
            if how_id in self.atoms and atom.id not in self.atoms[how_id].why:
                self.atoms[how_id].why.append(atom.id)

        return True

    def remove(self, atom_id: str) -> bool:
        """Remove atom + clean all references."""
        if atom_id not in self.atoms:
            return False
        for other in self.atoms.values():
            if atom_id in other.why:
                other.why.remove(atom_id)
            if atom_id in other.how:
                other.how.remove(atom_id)
        del self.atoms[atom_id]
        return True

    def connect(self, child_id: str, parent_id: str) -> bool:
        """Connect child → parent. Child serves parent."""
        child = self.atoms.get(child_id)
        parent = self.atoms.get(parent_id)
        if not child or not parent:
            return False
        if parent_id not in child.why:
            child.why.append(parent_id)
        if child_id not in parent.how:
            parent.how.append(child_id)
        return True

    # ─────────────────────────────────────────────────────────────
    # VALIDATE — check graph integrity
    # ─────────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Returns list of warnings. Empty = healthy."""
        warnings = []
        for atom in self.atoms.values():
            # Dangling references
            for ref in atom.why:
                if ref not in self.atoms:
                    warnings.append(f"'{atom.id}' why → missing '{ref}'")
            for ref in atom.how:
                if ref not in self.atoms:
                    warnings.append(f"'{atom.id}' how → missing '{ref}'")
            # Layer ordering: why should point to same or higher layer
            idx = LAYER_ORDER.index(atom.layer)
            for ref in atom.why:
                if ref in self.atoms:
                    ref_idx = LAYER_ORDER.index(self.atoms[ref].layer)
                    if ref_idx < idx:
                        warnings.append(f"'{atom.id}' ({atom.layer}) why → '{ref}' ({self.atoms[ref].layer}) — wrong direction")
            # Orphan check
            if not atom.why and atom.layer != "objective":
                warnings.append(f"'{atom.id}' has no why — disconnected")
        return warnings

    # ─────────────────────────────────────────────────────────────
    # VISUALIZE — generate interactive HTML graph
    # ─────────────────────────────────────────────────────────────

    def visualize(self, output_path: str) -> None:
        """Generate standalone HTML with interactive left→right layered graph."""

        # Build nodes and edges
        nodes = []
        edges = []
        for atom in self.atoms.values():
            nodes.append({"id": atom.id, "layer": atom.layer, "what": atom.what, "status": atom.status})
            for how_id in atom.how:
                if how_id in self.atoms:
                    edges.append({"source": how_id, "target": atom.id})

        graph_json = json.dumps({"nodes": nodes, "edges": edges})
        html = _VIS_TEMPLATE.replace("/*__DATA__*/", f"const G = {graph_json};")

        out = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as f:
            f.write(html)
        print(f"Visualization saved: {out}")


# ─────────────────────────────────────────────────────────────────
# VISUALIZER HTML
# ─────────────────────────────────────────────────────────────────

_VIS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>LedgerClaw — Knowledge Network</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f172a;color:#e2e8f0;font-family:-apple-system,system-ui,sans-serif;overflow:hidden}
svg{width:100vw;height:100vh}
.edge{stroke:#475569;stroke-width:1.5;fill:none;marker-end:url(#arrow)}
.node rect{rx:10;ry:10;stroke-width:2;cursor:pointer;transition:filter .2s}
.node rect:hover{filter:brightness(1.3) drop-shadow(0 0 8px rgba(255,255,255,0.15))}
.node text{fill:#e2e8f0;pointer-events:none}
.node .name{font-weight:600;font-size:13px}
.node .tag{font-size:10px;fill:#94a3b8}
.l-objective rect{fill:#7c3aed;stroke:#a78bfa}
.l-pillar rect{fill:#2563eb;stroke:#60a5fa}
.l-capability rect{fill:#0891b2;stroke:#22d3ee}
.l-agent rect{fill:#059669;stroke:#34d399}
.l-task rect{fill:#d97706;stroke:#fbbf24}
.s-planned rect{opacity:.5;stroke-dasharray:6 3}
.s-idea rect{opacity:.3;stroke-dasharray:4 4}
.tip{position:fixed;background:#1e293b;border:1px solid #475569;border-radius:8px;padding:14px 18px;
  max-width:340px;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.5);z-index:100;display:none}
.tip .tw{color:#f1f5f9;font-weight:600;font-size:14px;margin-bottom:4px}
.tip .tl{color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:1px}
.tip .tc{margin-top:8px;font-size:12px;color:#cbd5e1;white-space:pre-line}
.hdr{position:fixed;top:20px;left:20px}
.hdr h1{font-size:18px;font-weight:700}
.hdr p{font-size:12px;color:#64748b;margin-top:4px}
.leg{position:fixed;bottom:20px;left:20px;display:flex;gap:16px;font-size:11px}
.leg span{display:flex;align-items:center;gap:5px}
.leg i{width:12px;height:12px;border-radius:3px;display:inline-block}
.hint{position:fixed;bottom:20px;right:20px;font-size:11px;color:#475569}
</style></head><body>
<div class="hdr"><h1>LedgerClaw Knowledge Network</h1>
<p>← How (achieved by) &nbsp;|&nbsp; Why (serves) →</p></div>
<div class="leg">
<span><i style="background:#d97706"></i>Task</span>
<span><i style="background:#059669"></i>Agent</span>
<span><i style="background:#0891b2"></i>Capability</span>
<span><i style="background:#2563eb"></i>Pillar</span>
<span><i style="background:#7c3aed"></i>Objective</span>
</div>
<div class="hint">Scroll to zoom · Drag to pan · Hover for details</div>
<div class="tip" id="tip"><div class="tl" id="tl"></div><div class="tw" id="tw"></div><div class="tc" id="tc"></div></div>
<svg id="svg"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
/*__DATA__*/
const layers=["task","agent","capability","pillar","objective"];
const W=190,H=52,LG=280,NG=66,PAD=60;

// Group by layer
const groups={};layers.forEach(l=>groups[l]=[]);
G.nodes.forEach(n=>{if(groups[n.layer])groups[n.layer].push(n)});

// Position: x by layer, y centered per group
const maxGroupH=Math.max(...layers.map(l=>groups[l].length));
layers.forEach((l,li)=>{
  const g=groups[l];
  const totalH=g.length*H+(g.length-1)*NG;
  const offsetY=(maxGroupH*(H+NG)-NG)/2-totalH/2;
  g.forEach((n,ni)=>{n.x=PAD+li*LG;n.y=PAD+offsetY+ni*(H+NG)})
});

const nmap={};G.nodes.forEach(n=>nmap[n.id]=n);

const svg=d3.select("#svg");
const g=svg.append("g");

// Zoom
const zm=d3.zoom().scaleExtent([.2,4]).on("zoom",e=>g.attr("transform",e.transform));
svg.call(zm);

// Center
const xs=G.nodes.map(n=>n.x),ys=G.nodes.map(n=>n.y);
const cx=(d3.min(xs)+d3.max(xs)+W)/2,cy=(d3.min(ys)+d3.max(ys)+H)/2;
svg.call(zm.transform,d3.zoomIdentity.translate(innerWidth/2-cx,innerHeight/2-cy));

// Arrow marker
svg.append("defs").append("marker").attr("id","arrow").attr("viewBox","0 0 10 10")
  .attr("refX",10).attr("refY",5).attr("markerWidth",6).attr("markerHeight",6)
  .attr("orient","auto").append("path").attr("d","M0 0L10 5L0 10z").attr("fill","#475569");

// Edges — smooth bezier curves
G.edges.forEach(e=>{
  const s=nmap[e.source],t=nmap[e.target];
  if(!s||!t)return;
  const x1=s.x+W,y1=s.y+H/2,x2=t.x,y2=t.y+H/2,mx=(x1+x2)/2;
  g.append("path").attr("class","edge")
    .attr("d",`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`)
});

// Tooltip
const tip=document.getElementById("tip");

// Nodes
const nodes=g.selectAll(".node").data(G.nodes).enter().append("g")
  .attr("class",d=>`node l-${d.layer} s-${d.status}`)
  .attr("transform",d=>`translate(${d.x},${d.y})`)
  .on("mouseenter",(ev,d)=>{
    document.getElementById("tl").textContent=d.layer;
    document.getElementById("tw").textContent=d.what;
    const w=G.edges.filter(e=>e.source===d.id).map(e=>nmap[e.target]?.what||e.target);
    const h=G.edges.filter(e=>e.target===d.id).map(e=>nmap[e.source]?.what||e.source);
    let c="";if(w.length)c+="Serves → "+w.join(", ")+"\n";if(h.length)c+="Done by ← "+h.join(", ");
    document.getElementById("tc").textContent=c||"Root";
    tip.style.display="block";tip.style.left=(ev.clientX+16)+"px";tip.style.top=(ev.clientY-10)+"px"
  })
  .on("mousemove",ev=>{tip.style.left=(ev.clientX+16)+"px";tip.style.top=(ev.clientY-10)+"px"})
  .on("mouseleave",()=>{tip.style.display="none"});

nodes.append("rect").attr("width",W).attr("height",H);

function trunc(s,n){return s.length>n?s.slice(0,n-1)+"…":s}
nodes.append("text").attr("class","name").attr("x",W/2).attr("y",H/2-4)
  .attr("text-anchor","middle").text(d=>trunc(d.id.replace(/_/g," "),24));
nodes.append("text").attr("class","tag").attr("x",W/2).attr("y",H/2+14)
  .attr("text-anchor","middle").text(d=>d.layer);
</script></body></html>"""
