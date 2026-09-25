// WaveQL paper. Every number is read from numbers.json, which
// scripts/run/render_paper.py computes from the measurement files.
#let N = json("numbers.json")
#let CH = json("/measurements/demo_chains.json")
#let trace = rgb("#2b5c8a")
#let grid-c = luma(215)
#let H = N.h1
#let C = N.cost
#let accent = rgb("#1a56a0")
#let hot = rgb("#b8235a")
#let fix(x, d) = { let s = str(calc.round(x, digits: d)).replace("−", "-"); let neg = s.starts-with("-"); s = s.trim("-"); let p = s.split("."); let f = if p.len() > 1 { p.at(1) } else { "" }; while f.len() < d { f += "0" }; (if neg { "−" } else { "" }) + p.at(0) + (if d > 0 { "." + f } else { "" }) }
#let pct(x) = fix(x * 100, 1) + "%"
#let mn(s) = str(s).replace("-", "−")
#let pts(x) = (if x >= 0 { "+" } else { "" }) + str(calc.round(x * 100, digits: 1))
#let r2(x) = fix(x, 2)
#let CO = N.corpus
#let TASKS = CO.draw1.tasks + CO.draw2.tasks + CO.draw3.tasks
#let SCREENED = CO.draw1.screened + CO.draw2.screened + CO.draw3.screened
#let T = N.tokens
#let TOKR = r2(T.ratio)
#let TOKLO = r2(T.lo)
#let TOKHI = r2(T.hi)
#let TOKPCT = str(calc.round((1 - T.ratio) * 100)) + "%"
#let HO = N.heldout.heldout
#let H1D = N.heldout.draw1
#let CL = if "total_usd" in N.cloud { N.cloud.total_usd } else { 0 }
#let BW = N.behaviour.waveql
#let BC = N.behaviour.control
#let FIXSAVE = str(calc.round((1 - N.tokens_per_fix.waveql / N.tokens_per_fix.control) * 100)) + "%"
#let conv(x) = { let p = x.split(" of "); if int(p.at(1)) == 0 { 0.0 } else { int(p.at(0)) / int(p.at(1)) } }
#let SAME = H.diff_ci.at(0) < 0 and H.diff_ci.at(1) > 0
#let KILLDIV = CO.draw1.divergence + CO.draw2.divergence + CO.draw3.divergence
#let KILLASR = CO.draw1.assertion + CO.draw2.assertion + CO.draw3.assertion
#let SPEND = str(calc.round(C.model_total_usd + CL))

#set document(title: "WaveQL: A Joined Debug Store for Agentic Bug Repair on an Out-of-Order RISC-V Core",
              author: "Jayaraj Jayakumar")
#let SHORT = "WaveQL: A Joined Debug Store for Agentic Bug Repair"
#set page(paper: "us-letter", margin: (top: 57pt, bottom: 60pt, x: 53pt), columns: 2,
  header: context {
    let n = counter(page).get().first()
    if n > 1 {
      set text(size: 7pt)
      if calc.even(n) [Jayaraj Jayakumar #h(1fr)] else [#h(1fr) #SHORT]
    }
  })
#set columns(gutter: 24pt)
#set text(font: "Libertinus Serif", size: 9pt)
#set par(justify: true, leading: 0.58em, spacing: 0.58em, first-line-indent: 1em)
#show raw: set text(font: "DejaVu Sans Mono", size: 7pt)
#show heading.where(level: 1): it => {
  v(0.45em)
  block(text(size: 10pt, weight: "bold")[#it.body])
  v(0.2em)
}
#show heading: set par(first-line-indent: 0em)
#set table(stroke: 0.4pt + luma(190), inset: (x: 3.2pt, y: 2.4pt))
#show table: set text(size: 8pt)
#show table: set par(first-line-indent: 0em)
#show figure.caption: set text(size: 8pt)
#show figure.caption: set par(first-line-indent: 0em)
#show figure.where(kind: table): set figure.caption(position: top)
#set figure(gap: 0.5em)
#set figure(placement: auto)
#set place(clearance: 1.1em)


#place(top + center, float: true, scope: "parent", clearance: 1.2em)[
  #set par(first-line-indent: 0em)
  #text(size: 14.4pt, weight: "bold")[WaveQL: A Joined Debug Store for Agentic Bug Repair \ on an Out-of-Order RISC-V Core]
  #v(0.5em)
  #text(size: 11pt)[Jayaraj Jayakumar] \
  #text(size: 9pt)[University of California, Santa Cruz \ Santa Cruz, CA, USA \ jj8\@ucsc.edu]
  #v(0.2em)
  #text(size: 8pt, style: "italic")[A#super[3] CHIA Hackathon · Track 1: discovery and resolution of bugs in open-source designs such as BOOM]
]

#let cls(n) = text(font: "DejaVu Sans Mono", size: 5.4pt, n)
#let node(body, emph: false) = box(
  width: 100%, height: 40pt, inset: (x: 3pt, y: 3pt), radius: 2pt,
  fill: if emph { accent.lighten(88%) } else { luma(247) },
  stroke: if emph { 1pt + accent } else { 0.5pt + luma(170) },
  align(center + horizon, {set par(justify: false, leading: 0.45em); text(size: 7.4pt)[#body]}))
#let arr = align(center + horizon, text(fill: luma(120), size: 9pt)[→])
#place(top + center, float: true, scope: "parent", clearance: 1em)[
  #figure(
    grid(columns: (1fr, 8pt, 1.25fr, 8pt, 1.05fr, 8pt, 1.9fr, 8pt, 1fr, 8pt, 1fr),
      align: horizon,
      node[*Mutate*\ #cls("ChiselMutateNode")\ Chisel source], arr,
      node[*Screen*\ #cls("DetectabilityScreenNode")\ Spike + assertions], arr,
      node[*Capture*\ #cls("VerilatorRunNode")\ failure window], arr,
      node(emph: true)[*WaveQL store* · #cls("WaveQLIngestNode")\ waveform ⋈ commits ⋈ Spike trace\ ⋈ netlist ⋈ Chisel locators], arr,
      node[*Agent*\ #cls("WaveQLQueryTool")\ 12 queries, 1 edit], arr,
      node[*Verify*\ #cls("FixVerifyNode")\ rebuild + rerun],
    ),
    caption: [The loop as a CHIA graph. Each box is a CHIA block; CHIA's own Chipyard
      nodes build and run the core. The agent never sees a raw dump, and only a rebuilt
      core that passes every program in lockstep with Spike counts as repaired.],
    kind: image, supplement: [Figure], placement: none,
  ) <sys>
]


#block(text(size: 10pt, weight: "bold")[Abstract])
#par(first-line-indent: 0em)[When a processor fails a test, an AI agent is handed logs. WaveQL
  hands it what a hardware engineer would want: the waveform, the core's commit
  log and the golden Spike trace joined on cycle in one queryable store, with a
  causal walk over the generated netlist that answers _why_ a signal held a
  value --- built as CHIA blocks around BOOM, the out-of-order RISC-V core. To
  measure it we built BuggyBOOM, a verified-repair benchmark: #TASKS bugs in
  BOOM's Chisel source, from #SCREENED screened mutants, where a repair counts
  only if the rebuilt core passes all twelve programs in lockstep with Spike.
  Across #H.episodes scored episodes of `gemini-3.1-pro`, the joined view
  repairs #if SAME [a statistically indistinguishable share of bugs] else [#pct(H.waveql) of bugs]
  (#pct(H.waveql) against #pct(H.control) for the same evidence as text,
  difference #mn(N.DIFF) points, 95% CI \[#mn(N.DLO), #mn(N.DHI)\]) while reading
  #TOKR× the tokens per episode (95% CI \[#TOKLO, #TOKHI\]) and #FIXSAVE fewer
  per verified repair. The limit is navigation, not evidence: with the store,
  the agent spent its turns searching 29,183 signal names and proposed a patch in
  #pct(BW.proposed) of episodes against #pct(BC.proposed); once its patch was in
  the right file, it repaired at a similar rate. Scale changed the answer: on
  the first #N.PAIRED1 tasks the joined view led by #N.DIFF1 points, a lead that
  is gone at #H.paired. The whole study --- #N.patches_verified patches, each
  verified by a full rebuild of the core --- ran as one CHIA loop on four 96-core
  cloud VMs for about \$#SPEND.]

#block(text(size: 10pt, weight: "bold")[Keywords])
#par(first-line-indent: 0em)[agentic hardware debugging, waveforms, co-simulation, RISC-V, BOOM, CHIA]

= 1 Introduction

When a chip design fails a test, a person opens a waveform viewer and scrolls.
An AI agent has no screen; it gets raw logs and a first-mismatch report. For an
out-of-order core the gap is widest: the instruction that commits a wrong value
may have been corrupted dozens of cycles earlier, in another unit, by a signal
that never appears in any log. Debugging it means joining three records --- what
the golden model expected, what the core committed, what every signal held ---
on the one key they share, the cycle. CHIA's `CosimNode` computes the first
architectural divergence against Spike and then discards it into a text window;
agent-facing waveform tools compare waveform against waveform, which needs a
golden waveform that does not exist when the bug is real.

WaveQL puts the join in the loop, and BuggyBOOM measures what it is worth on
the hardest open-source target CHIA supports: BOOM v3, an out-of-order RISC-V
core, repaired at the Chisel source and verified by rebuilding the core.

- *A verified-repair benchmark on an out-of-order core.* #TASKS bugs in BOOM's
  Chisel source, observed failing by Spike lockstep (#KILLDIV) or by a fired
  Chisel assertion (#KILLASR), from #SCREENED mutants in eight operator classes;
  a repair counts only if the rebuilt core passes all twelve programs (§3).
- *A join that is exact, and a walk that explains.* Keyed on the core's own
  cycle counter, the store reproduces BOOM's commit log 235/235; the netlist
  evaluator reproduces simulator values at 97.7%, and `why` follows a wrong
  value back through the ROB's SRAM macro to the D-cache (§2, §4).
- *Comparable repairs for a third fewer tokens.* With the store the agent read
  #TOKR× the tokens per episode and #FIXSAVE fewer per verified repair, at a repair
  rate #if SAME [statistically indistinguishable from] else [different from]
  the text control's (§5).
- *Navigation, not evidence, is the bottleneck.* The agent with the store spent
  its budget finding its way around 29,183 signal names, and proposed a patch in
  #pct(BW.proposed) of episodes against #pct(BC.proposed) (§6).
- *Scale changes the answer.* An early #(N.DIFF1)-point lead on #N.PAIRED1 tasks
  vanished at #H.paired; the same CHIA loop running across four cloud VMs is
  what turned an anecdote into a measurement (§5, §8).

For CHIA this makes debugging collateral a first-class loop input.
`WaveQLIngestNode` and `WaveQLQueryTool` give any loop a joined, cycle-indexed
view of a failing run; `FixVerifyNode` makes "only a rebuilt, Spike-clean core
counts" a node; and BuggyBOOM gives the bug-resolution track a benchmark on BOOM
whose every score is machine-verified.

= 2 The loop and the store

The loop (@sys) mutates BOOM's Chisel, builds and screens each mutant with CHIA's
Chipyard nodes, records a waveform window around the failure, ingests it, hands
the agent a query surface, and verifies whatever it proposes. *WaveQL* streams
the Verilator VCD through `pywellen` into DuckDB, alongside the commit log, the
Spike trace, the divergence, a signal-to-Chisel map built from firtool's
locators, and a query log. Every row is keyed on the core's own cycle counter,
the key that makes the join exact. Answers are _asof_, since a waveform stores
changes and a range filter returns nothing for a signal holding its value;
every answer is size-capped and logged, so queries per repair is a measured
quantity. The surface is twelve operations (@ops); four answer what a recording
cannot: `stall_report` (why the pipeline stopped retiring), `source_of` and
`drivers` (which Chisel line and which signals produce a value), and `why`
(which operands _account for_ the value a signal held).

#figure(
  {
    set text(size: 7.4pt)
    set par(justify: false)
    show raw: set text(size: 6.8pt)
    table(columns: (auto, 1fr), align: (left, left), inset: (x: 3pt, y: 1.9pt),
      table.header([*operation*], [*answers*]),
      [`first_divergence`], [first mismatch with Spike, or the assertion],
      [`stall_report`], [why retirement stopped: the stalled chain],
      [`why`], [the operands that account for a value],
      [`drivers`], [the signals a value is computed from],
      [`source_of`], [the Chisel file and line that produce a signal],
      [`commits`], [instructions retired around a cycle, with values],
      [`inflight`], [ROB occupancy at a cycle: valid entries, head, tail],
      [`state_at`], [the value of any signals at a cycle],
      [`window`], [signal changes within a cycle range],
      [`trace_signal`], [one signal across a range, with entry value],
      [`find_signals`], [search among 29,183 signal names],
      [`sql`], [read-only SQL over the joined tables],
    )
  },
  kind: table, supplement: [Table],
  caption: [The WaveQL query surface. Every answer is size-capped and logged.],
) <ops>

#let chain = CH.find(c => c.id == "f27b9c07")
#let lo = 2436
#let hi = 2482
#let W = 100%
#let rowh = 10.2pt
#let links = chain.links.slice(0, 9)
#let ret = int(chain.facts.last_retired_cycle)
#let fx(v) = (v - lo) / (hi - lo)
#place(top + center, float: true, scope: "parent", clearance: 1em)[
#figure(
  block(width: 100%)[
    #set text(size: 6.7pt)
    #show raw: set text(size: 6.4pt)
    #grid(columns: (178pt, 1fr, 104pt), row-gutter: 0pt, column-gutter: 6pt,
      align: (right + horizon, left + horizon, left + horizon),
      text(fill: luma(90))[_link · signal_], text(fill: luma(90))[_cycle #lo to #hi · bar = frozen from that cycle on_], text(fill: luma(90))[_Chisel source_],
      ..links.map(l => {
        let st = int(l.stopped)
        (
          [#text(fill: luma(110))[d#l.depth] #raw(l.signal)],
          box(width: 100%, height: rowh, {
            place(dy: rowh / 2, line(length: 100%, stroke: 0.6pt + grid-c))
            place(dx: fx(st) * 100%, dy: 2.2pt,
              rect(width: (1 - fx(st)) * 100%, height: rowh - 4.4pt, fill: trace.lighten(10%)))
            place(dx: fx(ret) * 100%, dy: 0pt, line(end: (0pt, rowh), stroke: 1.1pt + hot))
            place(dx: fx(st) * 100% + 2pt, dy: 1.4pt, text(size: 6.4pt, fill: white)[#l.value])
          }),
          text(fill: trace)[#raw(l.chisel)],
        )
      }).flatten(),
      [], box(width: 100%, {
        for t in range(lo, hi + 1, step: 5) {
          place(dx: fx(t) * 100%, text(size: 6.2pt, fill: luma(100))[#t])
        }
      }), [],
    )
  ],
  kind: image, supplement: [Figure],
  caption: [`waveql-explain f27b9c07`: the D-cache mutant's hang, as the agent
    receives it. Every link froze at cycle 2450–2451; the ROB then drained what was
    in flight, the last instruction retired at #text(fill: hot)[cycle #ret (magenta)],
    and nothing retired for #chain.facts.stalled_cycles cycles after. Reading
    down: the ROB is empty because dispatch stalled because the store queue is
    full. The defect is in `dcache.scala`, behind the LSU.],
  placement: none,
) <chain>
]

= 3 BuggyBOOM: bugs an oracle sees

*Mutation.* A Scala lexer drives eight operator classes over BOOM v3's Chisel
source --- comparison and boundary flips, `&&`/`||` swaps, inverted selects,
handshake and pipeline-depth edits (a dropped `RegNext`), index arithmetic and
constant flips --- excluding sites inside assertions and the commit-log `printf`
the join reads, and performance-only sites a correct-but-mispredicting core
would hide. Three seeded, class-balanced draws sampled #SCREENED distinct mutants.

*Two oracles.* Each mutant runs twelve programs --- ISA tests, CSR and atomic
tests, and benchmarks --- under Spike lockstep and with BOOM's own Chisel
assertions live. The second oracle matters on an out-of-order core: a deadlocked
pipeline never commits anything wrong, so Spike alone scores a hang as a
survivor. #TASKS mutants are killed by a localizable oracle, #KILLASR of them by
an assertion.

*Evidence that contains the failure.* Each kill ships with its co-simulation
log, commit log and a waveform window. A hang is asserted 8,192 cycles after
the last commit, so its window is aimed there, and every window is checked
against the cycle of the failure: #N.windows.ok of #N.windows.n contain it (the
rest diverge within their first two commits, before the dump's first sample).

#figure(
  {
    let f(x) = if x == none { "–" } else { pct(x) }
    table(columns: (auto, auto, auto, auto, 1fr, 1fr),
      align: (left, right, right, right, right, right),
      table.header([*Class*], [*screened*], [*killed*], [*tasks*], [*waveql*], [*control*]),
      ..N.classes.map(c => (
        c.cls, str(c.screened), str(c.killed), str(c.paired), f(c.waveql), f(c.control),
      )).flatten()
    )
  },
  kind: table, supplement: [Table],
  caption: [BuggyBOOM by mutation class: mutants screened and killed, and the
    verified repair rate per task for each arm.],
) <classes>


= 4 From what to why

A waveform records what each signal was; why it took that value lives in the
netlist. WaveQL parses firtool's SystemVerilog into expressions with Chisel
locators attached, resolves ports across module boundaries in both directions,
and recomputes the anonymous `_GEN_*` temporaries --- 76% of a typical cone, never
traced by Verilator --- from the registers beneath them; the evaluator reproduces
the simulator's values at 97.7% over 1,027 signals. `why(signal, cycle)` follows
only the operands that account for a value, ranks links by how close to the
symptom they stopped moving, and crosses the ROB's SRAM macro by finding the
write that produced the value read (@chain, @vchain).

#let vchain = CH.find(c => c.id == "c80f8bde")
#let vroot = vchain.links.at(0).value
#let hexv(v) = {
  // exact 64-bit hex without floating point: parse the decimal string by digit
  let n = 0
  let out = ""
  if v == none or v == "None" { return "–" }
  let digits = v
  // big-integer decimal -> hex via repeated division on a digit array
  let a = digits.clusters().map(int)
  let hexs = "0123456789abcdef"
  while a.len() > 0 and not (a.len() == 1 and a.at(0) == 0) {
    let rem = 0
    let q = ()
    for d in a {
      let cur = rem * 10 + d
      q.push(calc.div-euclid(cur, 16))
      rem = calc.rem-euclid(cur, 16)
    }
    out = hexs.at(rem) + out
    while q.len() > 1 and q.at(0) == 0 { q = q.slice(1) }
    a = q
  }
  "0x" + if out == "" { "0" } else { out }
}
#figure(
  {
    set text(size: 6.6pt)
    show raw: set text(size: 5.9pt)
    let path = vchain.links.filter(l => "crossing" in l or l.value == vroot)
    table(columns: (auto, auto, auto), align: (left, left, left),
      inset: (x: 2.2pt, y: 2pt),
      table.header([*Block*], [*Signal holding the value*], [*Chisel file:line*]),
      ..path.map(l => if "crossing" in l {
        (table.cell(colspan: 3, fill: hot.lighten(90%), align: center,
          text(fill: hot)[#l.crossing · through the ROB's SRAM macro]),)
      } else {
        (text(fill: luma(90))[#l.block], raw(l.signal), text(fill: trace)[#raw(l.chisel.split(":").slice(0, calc.min(2, l.chisel.split(":").len())).join(":").replace(".scala", ""))])
      }).flatten()
    )
  },
  kind: image, supplement: [Figure],
  caption: [Mutant `c80f8bde` commits #raw(hexv(vroot)) to `x5`; Spike expects
    `0x12345678`. `why` follows that exact value back from the commit point,
    through the ROB's SRAM macro, the writeback arbiter, the execution unit and
    the LSU, to the D-cache response. Only links holding the value are shown. The
    defect is in `lsu.scala`.],
) <vchain>

*Localization is a prior in an out-of-order core.* Held out on #HO.n bugs, the
walk names the defect's file in #HO.file; a fixed list of the five files it
names most often (`core`, `rob`, `lsu`, ...) contains it #HO.static_file times,
and another bug's walk #pct(HO.perm_file) of the time. Every failure reaches the
same hub files within cycles, so what the waveform can add is the explanation
--- which signal held what, and on which Chisel line --- not the file name.

= 5 Does the join help an agent repair?

*Setup.* Two arms, identical except for the evidence surface: the WaveQL arm
queries the store; the control reads the same bytes the store was built from ---
co-simulation log, commit log, first mismatch --- through capped
`read`/`grep`/`tail`. Both get identical Chisel read access and one structured
edit, `propose_fix`, and the same task statement, which names no file, module
or cycle. Model `gemini-3.1-pro` at temperature 0, 30 turns and a 15-minute
wall-clock budget per episode, three seeds per task and arm; #H.episodes
episodes are scored, and every proposed patch that applies is verified by
rebuilding the core and re-running all twelve programs under Spike (@results).

#figure(
  table(columns: (auto, 1fr, 1fr),
    align: (left, right, right),
    table.header([], [*WaveQL*], [*text control*]),
    [verified repair rate (per task)], [*#pct(H.waveql)*], [#pct(H.control)],
    [#h(0.8em)95% bootstrap interval], [\[#pct(H.waveql_ci.at(0)), #pct(H.waveql_ci.at(1))\]], [\[#pct(H.control_ci.at(0)), #pct(H.control_ci.at(1))\]],
    [episodes repaired], [#N.NFW / #N.NW], [#N.NFC / #N.NC],
    [patch in the defect's file], [#N.LW], [#N.LC],
    [tokens per episode], [*#N.TEW*], [#N.TEC],
    [tokens per verified repair], [*#N.TFW*], [#N.TFC],
  ),
  caption: [#H.episodes scored episodes on #H.paired tasks, `gemini-3.1-pro`. Repair
    rates are per task, averaged over seeds, with 95% bootstrap intervals over tasks.],
) <results>

*Repairs.* Paired by task, the difference in verified repair rate is #mn(N.DIFF) points,
95% CI \[#mn(N.DLO), #mn(N.DHI)\]#if SAME [ --- statistically indistinguishable; the
interval excludes a gain of ten points or more]. *Cost.* Per task, the WaveQL arm
read #TOKR× the control's tokens per episode (95% CI \[#TOKLO, #TOKHI\], 10,000
resamples over tasks), and spent #FIXSAVE fewer tokens per verified repair.
The joined view read less on #T.below of #T.tasks tasks (@tok), and the saving is
largest where the bug is a wrong value: following a value back through the
netlist replaces searching the logs for it (@kinds).

#let KD = N.by_kind.divergence
#let KA = N.by_kind.assertion
#let kt(x) = str(calc.round(x / 1000)) + "k"
#figure(
  table(columns: (auto, auto, auto, auto, auto), align: (left, right, right, right, right),
    table.header([*failure*], [*tasks*], [*WaveQL*], [*control*], [*tokens / episode*]),
    [wrong value or PC], [#KD.tasks], [#pct(KD.waveql)], [#pct(KD.control)],
      [#kt(KD.tok_waveql) vs #kt(KD.tok_control) (#r2(KD.tok_waveql / KD.tok_control)×)],
    [hang or assertion], [#KA.tasks], [#pct(KA.waveql)], [#pct(KA.control)],
      [#kt(KA.tok_waveql) vs #kt(KA.tok_control) (#r2(KA.tok_waveql / KA.tok_control)×)],
  ),
  caption: [Verified repair rate per task and tokens per episode, by how the mutant
    failed: Spike saw a wrong committed value or PC, or a Chisel assertion fired.],
) <kinds>

#let tok-fig = {
  let P = T.per_task
  let m = calc.max(..P.map(p => calc.max(p.at(0), p.at(1)))) * 1.04
  let W = 190pt
  let Hh = 104pt
  let ox = 30pt
  set text(size: 6.2pt, fill: luma(80))
  box(width: 100%, height: Hh + 20pt, {
    place(dx: ox, line(start: (0pt, Hh), end: (W, Hh), stroke: 0.5pt + luma(120)))
    place(dx: ox, line(start: (0pt, 0pt), end: (0pt, Hh), stroke: 0.5pt + luma(120)))
    place(dx: ox, line(start: (0pt, Hh), end: (W, 0pt), stroke: (paint: luma(150), thickness: 0.5pt, dash: "dashed")))
    for p in P {
      let x = p.at(0) / m * W
      let y = Hh - p.at(1) / m * Hh
      place(dx: ox + x - 1.4pt, dy: y - 1.4pt,
        circle(radius: 1.4pt, fill: if p.at(1) < p.at(0) { trace } else { hot }))
    }
    for v in (0, 500000, 1000000, 1500000) {
      place(dx: ox + v / m * W - 6pt, dy: Hh + 3pt, [#if v == 0 [0] else [#(v / 1000000)M]])
      place(dx: 4pt, dy: Hh - v / m * Hh - 3pt, [#if v == 0 [0] else [#(v / 1000000)M]])
    }
    place(dx: ox + W / 2 - 38pt, dy: Hh + 11pt, [text control, tokens / episode])
    place(dx: ox + 6pt, dy: 0pt, [WaveQL, tokens / episode])
  })
}
#figure(tok-fig, caption: [Mean tokens per episode for each task, WaveQL against the
  text control. Below the dashed diagonal (blue) the joined view read less:
  #T.below of #T.tasks tasks.]) <tok>

#let ci-fig = {
  // Forest plot: the repair-rate difference at each stage of scaling, cumulative data.
  let lo = -0.4
  let hi = 0.5
  let fx(v) = (calc.max(lo, calc.min(hi, v)) - lo) / (hi - lo)
  let band = H.band
  let rows = N.trajectory
  let rh = 13pt
  let h = rows.len() * rh + 22pt
  set text(size: 6.6pt)
  grid(columns: (58pt, 1fr), column-gutter: 4pt,
    align(right, stack(spacing: 0pt, ..rows.map(r => box(height: rh, align(horizon + right)[#r.label (#r.paired)])))),
    box(width: 100%, height: h, {
      place(dx: fx(-band) * 100%, rect(width: (fx(band) - fx(-band)) * 100%, height: rows.len() * rh, fill: accent.lighten(88%)))
      place(dx: fx(0) * 100%, line(end: (0pt, rows.len() * rh), stroke: (paint: luma(120), thickness: 0.6pt, dash: "dashed")))
      for (i, r) in rows.enumerate() {
        let y = i * rh + rh / 2
        let last = i == rows.len() - 1
        let col = if last { hot } else { black }
        place(dx: fx(r.lo) * 100%, dy: y, line(length: (fx(r.hi) - fx(r.lo)) * 100%, stroke: 1.1pt + col))
        place(dx: fx(r.diff) * 100% - 2.6pt, dy: y - 2.6pt, circle(radius: 2.6pt, fill: col))
        place(dx: fx(r.hi) * 100% + 3pt, dy: y - 3.5pt, text(fill: col)[#mn(r.diff_s)])
      }
      place(dy: rows.len() * rh + 2pt, line(length: 100%, stroke: 0.5pt + luma(120)))
      for t in (-0.4, -0.2, 0.0, 0.2, 0.4) {
        place(dx: fx(t) * 100% - 5pt, dy: rows.len() * rh + 5pt, text(size: 6pt, fill: luma(90))[#pts(t)])
      }
      place(dx: fx(-band) * 100% + 1pt, dy: rows.len() * rh + 13pt, text(size: 6pt, fill: accent)[band ±#int(band * 100) pts])
    }))
}
#let bar(v, m, col) = box(width: 100%, height: 7pt,
  place(rect(width: v / m * 100%, height: 7pt, fill: col)))
#let twist-fig = {
  // A funnel per arm: every episode -> proposed a patch -> patch in the
  // defect's file -> verified fix. Where the waveform arm loses its episodes.
  let arms = (("WaveQL", N.behaviour.waveql, int(N.LW.split(" / ").at(0)), N.NFW, trace),
              ("control", N.behaviour.control, int(N.LC.split(" / ").at(0)), N.NFC, luma(110)))
  set text(size: 6.8pt)
  grid(columns: (34pt, 1fr, 64pt), row-gutter: 2.4pt, column-gutter: 4pt, align: horizon,
    ..arms.map(((name, b, rf, fx, col)) => {
      let n = b.n
      let prop = calc.round(b.proposed * n)
      let lv = (("episodes", n, 20%), ("proposed", prop, 45%), ("right file", rf, 70%), ("fixed", fx, 0%))
      lv.enumerate().map(((i, l)) => (
        if i == 0 { [*#name*] } else { [] },
        box(width: 100%, height: 6.5pt, place(rect(width: l.at(1) / n * 100%, height: 6.5pt, fill: col.lighten(l.at(2))))),
        [#l.at(1) #l.at(0)],
      )).flatten()
    }).flatten())
}


#figure(ci-fig, caption: [Scale changes the answer. WaveQL − control verified repair
  rate (points, 95% bootstrap interval) as the corpus grew, recomputed at each
  stage by the same analysis; paired tasks in brackets. Shaded: ±10 points.]) <ci>

*Scale.* On the first #N.PAIRED1 tasks the WaveQL arm led by #N.DIFF1 points (@ci).
Repeated runs of one cell do not always agree --- the same task, arm and seed
repaired the bug once and failed once --- so a dozen tasks cannot separate an
effect this size from noise; #H.paired can. Running the loop across four cloud
VMs is what made the second number affordable.

= 6 Where the budget goes

The two arms diverge before the edit, not at it (@twist). The WaveQL agent
proposed a patch in #pct(BW.proposed) of episodes against #pct(BC.proposed), and
made 30 or more tool calls in #pct(BW.capped) of them against #pct(BC.capped). Its
most-used operation was `find_signals` (#BW.top_tools.at(0).at(1) calls), a search
over 29,183 signal names; `why`, the operation built to make that search
unnecessary, was #pct(BW.why_share) of its calls. Once a patch was in the right
file, the two arms turned #pct(conv(N.CW)) and #pct(conv(N.CC)) of them into
verified repairs.

#figure(twist-fig, caption: [Where each arm's episodes went: every scored episode,
  those that proposed a patch, those whose patch was in the defect's file, and
  verified repairs.]) <twist>

When the agent does take the short path, the chain shows what a log cannot. On
a pipeline-depth mutant that dropped one `RegNext` from the exception-return
path, both arms reached `core.scala`. The control knew the redirect PC was wrong
and edited the line computing it; the chain showed the value arriving one cycle
out of step with the flush, and the WaveQL arm restored the missing stage --- the
core then passed all twelve programs. A log says where a failure surfaced; a
causal chain says what a signal held and when it stopped. The next surface
should lead with that chain rather than wait to be asked for it.

= 7 One repair, end to end

The mutant flipped one comparison in the D-cache's writeback unit, so it ended a
multi-beat release to L2 one beat early --- a bug that never commits a wrong
value, only violates the bus protocol, and that only the TileLink monitor on
the bus catches. From the assertion the agent
found the channel, asked `why` its valid signal held, followed the chain through
the release arbiter into the writeback unit, read that state machine, and
restored the comparison (@e2e). A log names the monitor; the chain names the
state machine behind it.


= 8 The loop at scale

The study ran as one CHIA loop across four 96-vCPU VMs, each running sixteen
Chipyard worker checkouts; screening a draw of about 100 mutants takes an hour and a half on one VM.
Agent episodes run one process per task, since the tools' netlist evaluation is
CPU-bound. Verification --- a full Chisel-to-Verilator rebuild per patch --- is
the expensive stage, so verifiers on every VM draw from one shared queue and
claim each cell by creating an object in Cloud Storage with
`ifGenerationMatch=0`, which succeeds for exactly one caller: every patch is
verified exactly once. Every recorded build failure is traced to the file its
own patch edited, and a stopped verifier restores both the mutation and the
agent's patch, so no verdict can be charged to another cell's residue (@scale).

#figure(
  {
    set text(size: 7.3pt)
    set par(justify: false)
    show raw: set text(size: 6.6pt)
    table(columns: (auto, 1fr), align: (left, left), inset: (x: 3pt, y: 2pt),
      table.header([*step*], [*what the agent asked, and what it learned*]),
      [`first_divergence`], [a TileLink monitor assertion at cycle 2469: _'C' channel opcode changed within a multibeat operation_],
      [10 queries], [`find_signals`, `window`, `trace_signal`, `state_at`: the D-cache's C channel, which carries releases to L2],
      [`why`], [`dcache.auto_out_c_valid` ← `nodeOut_c_valid` (`dcache.scala:431`) ← the release arbiter's state ← the writeback unit's `release.valid`],
      [`read_source`], [three reads of `dcache.scala`, around the writeback unit's state machine],
      [`propose_fix`], [`dcache.scala:132`: `data_req_cnt =/= (refillCycles-1).U` → `===`],
      [`FixVerifyNode`], [rebuilt core passes 12/12 programs in lockstep with Spike],
    )
  },
  kind: table, supplement: [Table],
  caption: [A WaveQL episode that repaired a BOOM bug: the injected edit flipped the
    comparison that ends the writeback unit's multibeat release, and the agent's
    patch is its exact inverse. 19 queries, from the transcripts in the artifact.],
) <e2e>

#figure(
  table(columns: (1fr, auto), align: (left, right),
    [Chisel mutants screened, three draws], [#SCREENED],
    [bugs killed by Spike lockstep or an assertion], [#TASKS],
    [agent episodes scored, two arms × three seeds], [#H.episodes],
    [patches verified by a full rebuild of the core], [#N.patches_verified],
    [waveform windows that contain their failure], [#N.windows.ok / #N.windows.n],
    [VM-hours (four 96-vCPU VMs)], [#str(calc.round(N.cloud.vm_hours))],
    [cost: model tokens + cloud compute], [\$#str(calc.round(C.model_total_usd)) + \$#str(calc.round(CL))],
  ),
  caption: [The study as one CHIA loop.],
) <scale>

= 9 What this gives CHIA

The track asks for the discovery and resolution of bugs in widely used
open-source designs such as BOOM. WaveQL does it on BOOM itself, end to end:
from a Chisel edit to a machine-verified repair, every step a CHIA block. Three
pieces carry over to any CHIA loop. `WaveQLIngestNode` and `WaveQLQueryTool` turn
a failing run into a joined, cycle-indexed store any agent can query, with the
divergence, the waveform and the source in one place. `FixVerifyNode` makes a
rebuilt, Spike-clean core the only evidence of a repair. BuggyBOOM gives the
community #TASKS verified tasks on an out-of-order core, per class, with the
evidence each kill ships with.

*Lessons for agentic debugging tools.* Put the causal answer first: the
operation that explains a value was #pct(BW.why_share) of the agent's calls, yet
it is the step that located the repair of §7. Budget the navigation: a richer
surface spent turns the text agent spent editing (§6). Measure at scale: a
dozen tasks showed a #(N.DIFF1)-point lead that #H.paired did not (§5).

= 10 Related work

`wave-mcp` [1] gives agents dozens of waveform tools, including
waveform-to-waveform first divergence, which needs a golden waveform. BluesFL [2]
localizes faults in an in-order RISC-V core from co-simulation divergence and
per-signal waveform reads. WaveQL differs in the query surface (a joined,
cycle-indexed store with a causal netlist walk), the target (an out-of-order
core) and the outcome (verified repair rather than localization). HWE-Bench [3]
contains repository-level hardware repair tasks; Encarsia [4] injects BOOM bugs at
the RTLIL level and proves their observability. BuggyBOOM contributes Chisel-source
mutants of BOOM with a rebuild-and-lockstep repair oracle. We build on BOOM [5],
Chipyard [6], CHIA [7] and Spike [8].

= 11 Scope

The results are on one core (BOOM v3, MediumBoom), one model family (Gemini;
`gemini-2.5-pro` repaired none of 8 episodes in either arm, so the study uses
`gemini-3.1-pro`) and injected bugs. Verilator is two-state, hiding
X-propagation. Six mutants were drawn in two of the three draws and count once.

= 12 Released as CHIA blocks; reproducibility

The loop is the five CHIA blocks of @sys, composed by `waveql-loop`; each is
usable alone. The artifact, at
#link("https://github.com/jyrj/WaveQL")[`github.com/jyrj/WaveQL`], contains the loop, the
store and query surface, the mutator, the screening and repair harness, the
cloud scale-out scripts, BuggyBOOM's manifests, every measurement file cited and
the transcripts of #N.transcripts.own of the #N.transcripts.scored scored
episodes; every number and figure in this paper is generated from those files by
a script. `waveql-explain f27b9c07` reproduces @chain.

#v(0.1em)
#text(size: 7.4pt)[*Acknowledgement of AI assistance.* Claude (Anthropic) was used
extensively throughout this work: writing and refactoring code, running and
analysing the measurement campaigns, and drafting and revising this paper.
Gemini models are the agents under test. All experimental design decisions, all
measurements reported here, and the final content and framing of this paper are
the responsibility of the human author.]

#v(0.3em)
#block(text(size: 9pt, weight: "bold")[References])
#{
  set text(size: 7pt)
  set par(first-line-indent: 0em, justify: true, leading: 0.4em, spacing: 0.4em)
  grid(columns: (12pt, 1fr), row-gutter: 3pt,
    ..(
      [Tencent. `wave-mcp`: waveform tools for agents over MCP. 2025.],
      [BluesFL: fault localization for RISC-V cores from co-simulation divergence. DAC 2026.],
      [HWE-Bench: repository-level hardware bug repair. 2025.],
      [Encarsia: provably observable bug injection for BOOM at RTLIL level. USENIX Security 2025.],
      [J. Zhao et al. SonicBOOM: the 3rd generation Berkeley out-of-order machine. CARRV 2020.],
      [A. Amid et al. Chipyard: integrated design, simulation and implementation framework for custom SoCs. IEEE Micro 2020.],
      [CHIA: an open framework for agentic HW/SW co-design flows. #link("https://chialoops.ai")[chialoops.ai].],
      [RISC-V International. Spike RISC-V ISA simulator.],
    ).enumerate().map(((i, r)) => ([\[#(i + 1)\]], r)).flatten())
}
