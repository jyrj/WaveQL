// SPDX-License-Identifier: BSD-3-Clause
// WaveQL addition to chipyard's harness binders.
package chipyard.harness

import chisel3._
import chisel3.util._

import org.chipsalliance.cde.config.{Config, Parameters}
import freechips.rocketchip.util._

import testchipip.cosim.{SpikeCosim}
import chipyard.iobinders._

/** Spike lockstep co-simulation AND PC-triggered waveform windows, together.
  *
  * WHY THIS EXISTS. Chipyard dispatches harness binders through a single
  * PartialFunction:
  *
  *   case HarnessBinders => fn orElse up(HarnessBinders)   // HarnessBinders.scala:42
  *   ports.foreach(port => p(HarnessBinders)(th, port, chipId))  // :34
  *
  * `orElse` means the FIRST matching case wins and the rest never run for that
  * port. Both `WithCospike` (:317) and `WithSelectiveWaveform` (:323) match the
  * byte-identical pattern
  *
  *   case (th: HasHarnessInstantiators, port: TracePort, chipId: Int)
  *
  * so a config composing both silently gets only the higher-precedence one. No
  * error, no warning -- the other binder's hardware is simply never elaborated.
  *
  * We hit this for real: building
  * `chipyard.harness.WithSelectiveWaveform_MediumBoomV3CosimConfig` produced a
  * simulator whose generated collateral contains 64 `wf_pc_` plusarg readers and
  * NO `SpikeCosim` instance at all. The run exited 0 and printed
  * `*** PASSED ***` -- because nothing was checking it against Spike. Every
  * mutant screened with that binary would have been recorded as "survived", and
  * the corpus would have been empty for a reason no downstream check could see.
  *
  * The fix cannot be a CONFIG string, because the conflict is in the dispatch,
  * not in the composition order. One case arm must do both jobs. The two bodies
  * below are copied verbatim from the upstream binders so that this stays a
  * faithful merge rather than a reimplementation.
  */
class WithCospikeAndSelectiveWaveform(val maxWindows: Int) extends HarnessBinder({
  case (th: HasHarnessInstantiators, port: TracePort, chipId: Int) => {

    // ---- verbatim from WithCospike (HarnessBinders.scala:317-321) ----------
    port.io.traces.zipWithIndex.map(t => SpikeCosim(t._1, t._2, port.cosimCfg))

    // ---- verbatim from WithSelectiveWaveform (HarnessBinders.scala:323-361) -
    // Use raw plusarg_reader with FORMAT="%h" for PC slots so users can pass
    // hex values like +wf_pc_0=80000000 (no 0x prefix). The PlusArg helper
    // hardcodes %d, which would parse "0x80000000" as 0 and disable the slot.
    val pcs     = Seq.tabulate(maxWindows)(i =>
      Module(new freechips.rocketchip.util.plusarg_reader(s"wf_pc_$i=%h", 0, s"selective-waveform PC slot $i (hex, no 0x prefix)", 64)).io.out)
    val num_seen      = Seq.tabulate(maxWindows)(i => PlusArg(s"wf_n_$i",   default=1, width=32))
    val cyc     = Seq.tabulate(maxWindows)(i => PlusArg(s"wf_cyc_$i", default=0, width=32))
    val dumpAll = PlusArg("wf_dump_all", default=0, width=1)(0)

    val firstTrace = port.io.traces.head
    val retired = port.io.traces.flatMap(_.trace.insns)

    val activeUnion = withClockAndReset(firstTrace.clock, firstTrace.reset) {
      val active = (0 until maxWindows).map { i =>
        val matched   = retired.map(t => t.valid && !t.exception && t.iaddr === pcs(i))
        val nMatched  = PopCount(matched)
        val matchCnt  = RegInit(0.U(32.W))
        val triggered = RegInit(false.B)
        val countdown = RegInit(0.U(32.W))
        val enabled   = pcs(i) =/= 0.U && cyc(i) =/= 0.U

        when (enabled && !triggered) {
          val newCnt = matchCnt + nMatched
          matchCnt := newCnt
          when (newCnt >= num_seen(i)) {
            triggered := true.B
            countdown := cyc(i)
          }
        }
        when (countdown > 0.U) { countdown := countdown - 1.U }
        countdown > 0.U
      }
      active.reduce(_ || _)
    }

    th.wf_active := dumpAll || activeUnion
  }
}) {
  // No-arg ctor for the underscore-CLI config composer, which instantiates via
  // `Class.forName(name).newInstance` (StageUtils.scala:14). Default 64 mirrors
  // WithSelectiveWaveform.
  def this() = this(64)
}
