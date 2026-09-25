// SPDX-License-Identifier: BSD-3-Clause
// WaveQL target configurations.
package chipyard

import org.chipsalliance.cde.config.{Config}

/** MediumBoom v3 with everything WaveQL's store needs to build the join.
  *
  * Modelled on `MediumBoomV3CosimConfig` (config/BoomConfigs.scala:91), with two
  * changes:
  *
  *   1. `WithCospike` is replaced by `WithCospikeAndSelectiveWaveform`, because
  *      the two upstream binders cannot coexist -- see the comment on that class.
  *
  *   2. `WithBoomHumanReadableCommitLog` is added. This is the join key, and the
  *      choice of variant is load-bearing.
  *
  * WHY THE HUMAN-READABLE VARIANT, against upstream's own advice. The plain
  * `WithBoomCommitLogPrintf` emits
  *
  *     printf("%d 0x%x ", priv, pc)      // core.scala:2098
  *
  * whose first field is the PRIVILEGE LEVEL, not a cycle -- we confirmed this by
  * running it: over 832 commits the field only ever took the values 0 and 3.
  * That log cannot be joined to a waveform at all, because nothing in it says
  * *when* anything happened.
  *
  * The human-readable variant additionally emits
  *
  *     printf("C%d: ", debug_tsc_reg)    // core.scala:2095
  *
  * and `debug_tsc_reg` is the core's cycle counter. That prefix is the only
  * cycle-stamped, per-instruction stream the DUT produces, so it is the only
  * thing that can carry an architectural event onto a microarchitectural
  * coordinate -- which is the entire thesis of this project.
  *
  * Upstream's comment warns the DASM tokens "break direct line-diff against
  * Spike's --log-commits output; use WithBoomCommitLogPrintf for cosim"
  * (boom v3 config-mixins.scala:35-38). That warning does not apply here:
  * cospike checks commits *in process* against its embedded Spike, it does not
  * line-diff two text files. We give up a text diff we never perform to gain the
  * cycle we cannot do without.
  */
class WaveQLMediumBoomV3Config extends Config(
  new chipyard.harness.WithCospikeAndSelectiveWaveform ++   // cosim AND PC-triggered windows
  new boom.v3.common.WithBoomHumanReadableCommitLog ++      // cycle-stamped DUT commit stream (C<cycle>:)
  new chipyard.config.WithTraceIO ++                        // TracePort, required by the binder
  new boom.v3.common.WithNMediumBooms(1) ++
  new chipyard.config.AbstractConfig)

/** The in-order control: same collateral, Rocket instead of BOOM.
  *
  * Rocket is the in-order comparison point: if the join only helps on an
  * out-of-order core, that is a finding about where the join matters, and it
  * needs an in-order core to be stated at all.
  */
class WaveQLRocketConfig extends Config(
  new chipyard.harness.WithCospikeAndSelectiveWaveform ++
  new chipyard.config.WithTraceIO ++
  new freechips.rocketchip.rocket.WithDebugROB ++           // cospike needs wdata from the debug ROM
  new freechips.rocketchip.rocket.WithNHugeCores(1) ++
  new chipyard.config.AbstractConfig)
