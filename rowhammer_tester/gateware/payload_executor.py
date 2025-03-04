from enum import IntEnum, unique
from functools import reduce
from operator import and_, or_

from litedram.core.refresher import Refresher
from litex.soc.integration.doc import AutoDoc, ModuleDoc
from litex.soc.interconnect.csr import CSR, AutoCSR, CSRField, CSRStatus, CSRStorage
from migen import (
    FSM,
    Cat,
    If,
    Module,
    NextState,
    NextValue,
    Replicate,
    ResetInserter,
    Signal,
    log2_int,
)
from migen.genlib.coding import Decoder as OneHotDecoder


@unique
class OpCode(IntEnum):
    NOOP = 0b000
    LOOP = 0b111
    # (ras, cas, we)
    ACT = 0b100
    PRE = 0b101
    REF = 0b110
    ZQC = 0b001
    READ = 0b010

    @classmethod
    def table(cls):
        div = "+------+-------+\n"
        t = ""
        t += div
        t += "+ Op   + Value +\n"
        t += div.replace("-", "=")
        for op_code in cls:
            t += f"+ {op_code.name:4} | 0b{op_code.value:03b} +\n"
            t += div
        t = t.rstrip()
        return t


class Decoder(Module):
    __doc__ = f"""
    **Instruction decoder**

    All instructions are 32-bit. The format of most instructions is the same,
    except for the LOOP instruction, which has a constant TIMESLICE of 1.

    NOOP with a TIMESLICE of 0 is a special case which is interpreted as
    STOP instruction. When this instruction is encountered execution gets
    finished immediately.

    **NOTE:** TIMESLICE is the number of cycles the instruction will take. This
    means that instructions other than NOOP that use TIMESLICE=0 are illegal
    (although will silently be executed as having TIMESLICE=1).

    **NOTE2:** LOOP instruction will *jump* COUNT times, meaning that the "code"
    inside the loop will effectively be executed COUNT+1 times.

    Op codes:

{OpCode.table()}

    Instruction format::

              LSB                       MSB
        dfi:  OP_CODE | TIMESLICE | ADDRESS
        noop: OP_CODE | TIMESLICE_NOOP
        loop: OP_CODE | COUNT     | JUMP
        stop: <NOOP>  | 0

    Where ADDRESS depends on the DFI command and is one of::

        LSB              MSB
        RANK | BANK | COLUMN
        RANK | BANK | ROW
    """

    # TODO: Load widths from .proto file
    INSTRUCTION = 32
    OP_CODE = 3
    TIMESLICE = 5
    ADDRESS = 24
    TIMESLICE_NOOP = TIMESLICE + ADDRESS
    LOOP_COUNT = 12
    LOOP_JUMP = 17

    def __init__(self, instruction, *, rankbits, bankbits, rowbits, colbits):
        assert len(instruction) == self.INSTRUCTION
        assert self.OP_CODE + self.TIMESLICE_NOOP == self.INSTRUCTION
        assert self.OP_CODE + self.TIMESLICE + self.ADDRESS == self.INSTRUCTION
        assert self.OP_CODE + self.LOOP_COUNT + self.LOOP_JUMP == self.INSTRUCTION
        assert rankbits + bankbits + max(rowbits, colbits) <= self.ADDRESS, (
            rankbits + bankbits + max(rowbits, colbits),
            self.ADDRESS,
        )

        self.op_code = Signal(self.OP_CODE)
        # DFI-mappable instructions
        self.timeslice = Signal(self.TIMESLICE_NOOP)
        self.address = Signal(self.ADDRESS)  # TODO: NOOP could reuse it as timeslice
        self.cas = Signal()
        self.ras = Signal()
        self.we = Signal()
        self.dfi_bank = Signal(bankbits)
        self.dfi_address = Signal(max(rowbits, colbits))
        # Loop instruction
        self.loop_count = Signal(self.LOOP_COUNT)  # max 32K loops
        self.loop_jump = Signal(self.LOOP_JUMP)  # max jump by 16K instructions
        # Stop instruction (NOOP with TIMESLICE=0)
        self.stop = Signal()

        tail = instruction[self.OP_CODE :]
        self.comb += [
            self.op_code.eq(instruction[: self.OP_CODE]),
            If(
                self.op_code == OpCode.NOOP,
                self.timeslice.eq(tail[: self.TIMESLICE_NOOP]),
            ).Else(
                self.timeslice.eq(tail[: self.TIMESLICE]),
            ),
            self.address.eq(tail[self.TIMESLICE :]),
            self.loop_count.eq(tail[: self.LOOP_COUNT]),
            self.loop_jump.eq(tail[self.LOOP_COUNT :]),
            self.stop.eq((self.op_code == OpCode.NOOP) & (self.timeslice == 0)),
            self.cas.eq(self.op_code[1]),
            self.ras.eq(self.op_code[2]),
            self.we.eq(self.op_code[0]),
            self.dfi_bank.eq(self.address[rankbits : rankbits + bankbits]),
            self.dfi_address.eq(self.address[rankbits + bankbits :]),
        ]

        if rankbits:
            self.dfi_rank = Signal(rankbits)
            self.comb += (self.dfi_rank.eq(self.address[:rankbits]),)


class Encoder:
    """Helper for writing payloads"""

    def __init__(self, bankbits, nranks=1):
        self.nranks = nranks
        self.bankbits = bankbits
        # Allow to use Encoder.I as a backwards compatibility
        self.I = self.Instruction  # noqa E741

    class Instruction:
        """Instruction specification without encoding the value yet"""

        def __init__(self, op_code, **kwargs):
            self.op_code = op_code
            for k, v in kwargs.items():
                setattr(self, k, v)
            if op_code == OpCode.LOOP:
                count = kwargs["count"]
                assert (
                    count < 2**Decoder.LOOP_COUNT
                ), f"LOOP count value:{count} exceeded max value:{2**Decoder.LOOP_COUNT - 1}"
                self._parts = [
                    (Decoder.OP_CODE, op_code),
                    (Decoder.LOOP_COUNT, count),
                    (Decoder.LOOP_JUMP, kwargs["jump"]),
                ]
            elif op_code == OpCode.NOOP:
                timeslice = kwargs["timeslice"]
                assert (
                    timeslice < 2**Decoder.TIMESLICE_NOOP
                ), f"Timeslice value:{timeslice} exceeded max value:{2**Decoder.TIMESLICE_NOOP - 1}"
                self._parts = [
                    (Decoder.OP_CODE, op_code),
                    (Decoder.TIMESLICE_NOOP, timeslice),
                ]
            else:
                timeslice = kwargs["timeslice"]
                assert timeslice != 0, "Timeslice for instructions other than NOOP should be > 0"
                no_address = [OpCode.REF]  # PRE requires bank address
                assert (
                    "address" in kwargs or op_code in no_address
                ), f"{op_code.name} instruction requires `address`"

                base_timeslice = max(timeslice & (2**Decoder.TIMESLICE - 1), 1)
                self._parts = [
                    (Decoder.OP_CODE, op_code),
                    (Decoder.TIMESLICE, base_timeslice),
                    (Decoder.ADDRESS, kwargs.get("address", 0)),
                ]
                if base_timeslice != timeslice:
                    print(
                        f"WARNING: Timeslice value:{timeslice} exceeded max value"
                        f" intended for instruction: {2**Decoder.TIMESLICE - 1}"
                    )
                    print(
                        f"INFO: Instruction:{op_code.name} with timeslice:{timeslice}"
                        " will be encoded with extra NOOP(s) to grant a larger time delay"
                    )
                    remaining = timeslice - base_timeslice
                    self._noops = []
                    for i in range(0, remaining, Decoder.TIMESLICE_NOOP - 1):
                        wait = min(remaining - i, Decoder.TIMESLICE_NOOP - 1)
                        self._noops.append(
                            [
                                (Decoder.OP_CODE, OpCode.NOOP),
                                (Decoder.TIMESLICE_NOOP, wait),
                            ]
                        )

    def __call__(self, target, **kwargs):
        if isinstance(target, OpCode):
            return self.encode(target, **kwargs)
        elif isinstance(target, self.Instruction):
            assert len(kwargs) == 0, "No kwargs expected for Encoder.Instruction"
            return self.encode_spec(target)
        elif hasattr(target, "__iter__"):
            assert len(kwargs) == 0, "No kwargs expected for iterable"
            return self.encode_payload(target)
        raise TypeError(
            "One of the following is expected: OpCode+kwargs, "
            "Encoder.Instruction, list[Encoder.Instruction]"
        )

    def encode(self, op_code, **kwargs):
        return self.encode_spec(self.Instruction(op_code, **kwargs))

    def encode_spec(self, spec):
        assert isinstance(spec, self.Instruction)
        _instrs_to_enc = [spec._parts]
        if hasattr(spec, "_noops"):
            _instrs_to_enc.extend(spec._noops)
        instrs = []
        for _instr_to_enc in _instrs_to_enc:
            instr = 0
            n = 0
            for width, val in _instr_to_enc:
                mask = 2**width - 1
                instr |= (val & mask) << n
                n += width
            instrs.append(instr)
        return instrs

    def encode_payload(self, payload):
        ret = []
        for i in payload:
            ret.extend(self.encode_spec(i))
        return ret

    def address(self, *, rank=None, bank=0, row=None, col=None):
        assert not (row is not None and col is not None)
        if row is not None:
            rowcol = row
        elif col is not None:
            rowcol = col
        else:
            rowcol = 0
        address = bank & (2**self.bankbits - 1)
        address |= (rowcol) << self.bankbits
        if self.nranks > 1:
            address <<= log2_int(self.nranks)
            address |= rank
        return address


@ResetInserter()
class Scratchpad(Module):
    """
    Scratchpad memory filled with data from subsequent READ commands
    """

    def __init__(self, mem, dfi):
        assert mem.width == len(dfi.p0.rddata) * len(dfi.phases)

        self.counter = Signal(max=mem.depth - 1)
        self.overflow = Signal()

        wr_port = mem.get_port(we_granularity=8, write_capable=True)
        we_replicate = len(dfi.p0.rddata) // 8
        self.specials += wr_port

        self.sync += [  # use sync for easier timing as we don't need comb here
            wr_port.adr.eq(self.counter),
            wr_port.dat_w.eq(Cat(*[p.rddata for p in dfi.phases])),
            wr_port.we.eq(Cat(*[Replicate(p.rddata_valid, we_replicate) for p in dfi.phases])),
        ]

        self.sync += [
            If(
                reduce(or_, wr_port.we),
                If(
                    self.counter == mem.depth - 1,
                    self.overflow.eq(1),
                    self.counter.eq(0),
                ).Else(self.counter.eq(self.counter + 1)),
            )
        ]


class DFIExecutor(Module):
    def __init__(self, dfi, decoder, rank_decoder):
        self.phase = Signal(max=max(len(dfi.phases) - 1, 2))
        self.exec = Signal()

        if len(dfi.phases) == 1:
            self.phase.eq(0)

        nranks = len(dfi.p0.cs_n)

        for i, phase in enumerate(dfi.phases):
            self.sync += [
                # constant signals
                phase.cke.eq(Replicate(1, nranks)),
                phase.odt.eq(
                    Replicate(1, nranks)
                ),  # FIXME: needs to be dynamically driven for multi-rank systems
                phase.reset_n.eq(Replicate(1, nranks)),
                # send the command on current phase
                If(
                    (self.phase == i) & self.exec,  # selected
                    phase.cas_n.eq(~decoder.cas),
                    phase.ras_n.eq(~decoder.ras),
                    phase.we_n.eq(~decoder.we),
                    phase.address.eq(decoder.dfi_address),
                    phase.bank.eq(decoder.dfi_bank),
                    phase.rddata_en.eq(decoder.op_code == OpCode.READ),
                    # chip select
                    If(
                        decoder.op_code == OpCode.NOOP,
                        phase.cs_n.eq(Replicate(1, nranks)),
                    )
                    .Elif(
                        decoder.op_code == OpCode.REF,  # select all ranks on refresh
                        phase.cs_n.eq(0),
                    )
                    .Else(
                        phase.cs_n.eq(~rank_decoder.o),
                    ),
                ).Else(  # inactive
                    phase.cs_n.eq(Replicate(1, nranks)),
                    phase.cas_n.eq(1),
                    phase.ras_n.eq(1),
                    phase.we_n.eq(1),
                    phase.rddata_en.eq(0),
                ),
            ]


class SyncableRefresher(Module):
    # Refresher that can be stopped
    def __init__(self, *args, **kwargs):
        refresher = ResetInserter()(Refresher(*args, **kwargs))
        self.submodules += refresher

        self.reset = refresher.reset
        self.cmd = refresher.cmd


class RefreshCounter(Module):
    def __init__(self, dfi_phase, width=32, memtype=""):
        self.counter = Signal(width)
        self.refresh = Signal()

        if memtype == "DDR5":
            ref_cmd = 0b10011
            self.comb += self.refresh.eq(~dfi_phase.cs_n & (dfi_phase.address[:5] == ref_cmd))
        else:
            ref_cmd = dict(cs_n=0, cas_n=0, ras_n=0, we_n=1)
            self.comb += self.refresh.eq(
                reduce(and_, [getattr(dfi_phase, sig) == val for sig, val in ref_cmd.items()])
            )

        self.sync += If(self.refresh, self.counter.eq(self.counter + 1))


class Fetcher(Module):
    """
    Generates memory addresses for fetching instructions for PayloadExecutor. By default increases
    address by 1 on each cycle.
    When jump is asserted address gets decremented by jump_offset+pipeline_delay instead of
    getting increased by 1.
    When stall is asserted it stops the change of address. During stall it also outputs previous
    memory address so that address doesn't change in the cycle that stall is asserted.
    """

    def __init__(self, mem_addr, stall, jump, jump_offset, pipeline_delay=2):
        self.reset_pc = Signal()
        self.program_counter = Signal.like(mem_addr, reset=0)
        self.program_counter_old = Signal.like(mem_addr, reset=0)
        self.next_addr = Signal.like(mem_addr)

        self.comb += [
            If(
                ~jump,
                self.next_addr.eq(self.program_counter + 1),
            ).Else(
                self.next_addr.eq(self.program_counter - jump_offset - pipeline_delay),
            ),
            If(stall, mem_addr.eq(self.program_counter_old)).Else(
                mem_addr.eq(self.program_counter)
            ),
        ]

        self.sync += [
            If(
                ~stall,
                self.program_counter.eq(self.next_addr),
                self.program_counter_old.eq(self.program_counter),
            ).Elif(
                self.reset_pc,
                self.program_counter.eq(0),
                self.program_counter_old.eq(0),
            ),
        ]


class DFISwitch(Module, AutoCSR):
    # Synchronizes disconnection of the MC to last REF/ZQC command sent by MC
    # Refresher must provide `ce` signal
    def __init__(self, with_refresh, dfii, refresher_reset, memtype=""):
        self.wants_dfi = Signal()
        self.dfi_ready = Signal()
        self.dfi = dfii.ext_dfi

        # Refresh is issued always on phase 0. Count refresh commands on DFII
        # master (any refresh issued, both by MC and PayloadExecutor)
        if dfii.master.with_sub_channels:
            self.submodules.refresh_counter = RefreshCounter(
                dfii.master.phases[0].A_, width=64, memtype=memtype
            )
        else:
            self.submodules.refresh_counter = RefreshCounter(
                dfii.master.phases[0], width=64, memtype=memtype
            )

        # If non-zero, we must wait until exactly that refresh count
        # Refresh counter is updated 1 cycle after refresh, so add +1 in the test
        self.at_refresh = Signal.like(self.refresh_counter.counter, reset=0)
        refresh_matches = (self.at_refresh == 0) | (
            self.at_refresh == self.refresh_counter.counter + 1
        )

        self.submodules.fsm = fsm = FSM()
        fsm.act(
            "MEMORY-CONTROLLER",
            If(
                self.wants_dfi,
                If(
                    with_refresh,
                    # FIXME: sometimes ZQCS needs to be sent after refresh,
                    # currently it will be missed
                    If(
                        self.refresh_counter.refresh & refresh_matches,
                        NextState("PAYLOAD-EXECUTION"),
                    ),
                ).Else(NextState("PAYLOAD-EXECUTION")),
            ),
        )
        fsm.act(
            "PAYLOAD-EXECUTION",
            self.dfi_ready.eq(1),
            dfii.ext_dfi_sel.eq(1),
            If(
                ~self.wants_dfi,
                # Reset Refresher so that it starts counting tREFI from 0
                refresher_reset.eq(1),
                NextState("MEMORY-CONTROLLER"),
            ),
        )

    def add_csrs(self):
        self._refresh_count = CSRStatus(
            len(self.refresh_counter.counter),
            description="Count of all refresh commands issued "
            "(both by Memory Controller and the Payload Executor)."
            " Value is latched from internal counter on mode transition: MC -> PE "
            "or by writing to the `refresh_update` CSR.",
        )
        self._at_refresh = CSRStorage(
            len(self.at_refresh),
            reset=0,
            description="If set to a value different than 0 the mode transition MC -> PE"
            " will be performed only when the value of this register matches the current"
            " refresh commands count.",
        )
        self._refresh_update = CSR()
        self._refresh_update.description = "Force an update of the `refresh_count` CSR."

        self.comb += self.at_refresh.eq(self._at_refresh.storage)

        # detect mode transition
        pe_ongoing = self.fsm.ongoing("PAYLOAD-EXECUTION")
        mc_ongoing = self.fsm.ongoing("MEMORY-CONTROLLER")
        mc_ongoing_d = Signal()
        self.sync += mc_ongoing_d.eq(mc_ongoing)
        mc_to_pe = mc_ongoing_d & pe_ongoing

        self.sync += If(
            mc_to_pe | self._refresh_update.re,
            self._refresh_count.status.eq(self.refresh_counter.counter),
        )


class PayloadExecutor(Module, AutoCSR, AutoDoc):
    PIPELINE_DELAY = 2

    def __init__(
        self,
        mem_payload,
        mem_scratchpad,
        dfi_switch,
        *,
        nranks,
        bankbits,
        rowbits,
        colbits,
        rdphase,
    ):
        self.description = ModuleDoc(
            f"""
        Executes the DRAM payload from memory

        {Decoder.__doc__}
        """
        )

        self.start = Signal()
        self.executing = Signal()
        self.ready = Signal()
        self.loop_counter = Signal(Decoder.LOOP_COUNT)
        self.idle_counter = Signal(Decoder.TIMESLICE_NOOP)

        self.exec_start = Signal(64, reset=0)
        self.exec_stop = Signal(64, reset=0)
        cycle_counter = Signal(64)

        # Payload Executor Status
        self.sync += [
            cycle_counter.eq(cycle_counter + 1),
            If(self.start, self.exec_start.eq(0), self.exec_stop.eq(0)),
            If(
                self.executing & (self.exec_start == 0),
                self.exec_start.eq(cycle_counter),
                self.exec_stop.eq(0),
            ),
            If(self.executing, self.exec_stop.eq(cycle_counter + 1)),
        ]

        # Scratchpad
        self.submodules.scratchpad = Scratchpad(mem_scratchpad, dfi_switch.dfi)

        # Fetcher
        # uses synchronous port, mem_data is ready 1 cycle after mem_addr is asserted
        assert (
            mem_payload.width == Decoder.INSTRUCTION
        ), f"Wrong payload memory word width: {mem_payload.width} vs {Decoder.INSTRUCTION}"

        self.instruction = Signal(Decoder.INSTRUCTION)

        self.mem_addr = Signal(max=mem_payload.depth)
        self.mem_data = Signal.like(self.instruction)
        self.stall = Signal()
        self.bubble = Signal()  # Helper signal for tests, doesn't get used in logic
        self.jump_offset = Signal(Decoder.LOOP_JUMP)
        self.jump = Signal()

        self.submodules.fetcher = Fetcher(
            self.mem_addr, self.stall, self.jump, self.jump_offset, self.PIPELINE_DELAY
        )

        payload_port = mem_payload.get_port(write_capable=False)
        self.specials += payload_port
        self.comb += [
            payload_port.adr.eq(self.mem_addr),
            self.mem_data.eq(payload_port.dat_r),
        ]

        self.sync += [If(~self.stall, self.instruction.eq(self.mem_data))]

        # Decoder
        rankbits = log2_int(nranks)
        self.submodules.decoder = decoder = Decoder(
            self.instruction, rankbits=rankbits, bankbits=bankbits, rowbits=rowbits, colbits=colbits
        )
        self.submodules.rank_decoder = OneHotDecoder(nranks)
        if rankbits:
            self.comb += self.rank_decoder.i.eq(self.decoder.dfi_rank)

        self.comb += self.jump_offset.eq(decoder.loop_jump)

        # Executor
        self.submodules.dfi_executor = DFIExecutor(dfi_switch.dfi, self.decoder, self.rank_decoder)
        self.submodules.fsm = FSM()
        self.fsm.act(
            "READY",
            self.stall.eq(1),
            self.ready.eq(1),
            self.fetcher.reset_pc.eq(1),
            If(
                self.start,
                NextValue(dfi_switch.wants_dfi, 1),
                NextState("WAIT-DFI"),
            ),
        )
        self.fsm.act(
            "WAIT-DFI",
            self.scratchpad.reset.eq(1),
            self.stall.eq(1),
            If(
                dfi_switch.dfi_ready,
                NextState("BUBBLE"),
                NextValue(self.idle_counter, self.PIPELINE_DELAY - 1),
            ),
        )
        self.fsm.act(
            "RUN",
            self.stall.eq(0),
            self.executing.eq(1),
            # Terminate after executing the whole program or when STOP instruction is encountered
            # Do not terminate when next instruction is LOOP with jump to previous address
            If(
                ((self.mem_addr == self.PIPELINE_DELAY - 1) | decoder.stop)
                & ((decoder.op_code != OpCode.LOOP) | (decoder.loop_count == self.loop_counter)),
                NextValue(dfi_switch.wants_dfi, 0),
                NextState("READY"),
            )
            .Elif(
                # Execute instruction
                decoder.op_code == OpCode.LOOP,
                # If a loop instruction with count=0 is found it will be a NOOP
                If(
                    self.loop_counter != decoder.loop_count,
                    # Continue the loop
                    self.jump.eq(1),
                    NextValue(self.loop_counter, self.loop_counter + 1),
                    NextState("BUBBLE"),
                    NextValue(self.idle_counter, self.PIPELINE_DELAY - 1),
                ).Else(
                    # Finish the loop
                    # Set loop_counter to 0 so that next loop instruction will start properly
                    NextValue(self.loop_counter, 0),
                ),
            )
            .Else(
                # DFI instruction
                # Timeslice=0 should be illegal but we still consider it as =1
                If(
                    (decoder.timeslice == 0) | (decoder.timeslice == 1),
                ).Else(
                    # Wait in idle loop after sending the command
                    NextValue(self.idle_counter, decoder.timeslice - 2),
                    NextState("IDLE"),
                ),
                # Send DFI command
                self.dfi_executor.exec.eq(1),
                If(
                    decoder.cas & ~decoder.ras & ~decoder.we,  # READ command
                    self.dfi_executor.phase.eq(rdphase),
                ).Else(
                    self.dfi_executor.phase.eq(0),
                ),
            ),
        )
        self.fsm.act(
            "IDLE",
            self.stall.eq(1),
            self.executing.eq(1),
            If(
                self.idle_counter == 0,
                NextState("RUN"),
            ).Else(
                NextValue(self.idle_counter, self.idle_counter - 1),
            ),
        )
        self.fsm.act(
            "BUBBLE",  # State that waits for pipeline to clear
            self.stall.eq(0),
            self.executing.eq(1),
            self.bubble.eq(1),
            If(
                self.idle_counter == 0,
                NextState("RUN"),
            ).Else(
                NextValue(self.idle_counter, self.idle_counter - 1),
            ),
        )

    def add_csrs(self):
        self._start = CSR()
        # CSR does not take a description parameter so we must set it manually
        self._start.description = "Writing to this register initializes payload execution"
        self._status = CSRStatus(
            fields=[
                CSRField("ready", description="Indicates that the executor is not running"),
                CSRField(
                    "overflow",
                    description="Indicates the scratchpad memory address counter"
                    " has overflown due to the number of READ commands sent during execution",
                ),
            ],
            description="Payload executor status register",
        )
        self._read_count = CSRStatus(
            len(self.scratchpad.counter),
            description="Number of data"
            " from READ commands that is stored in the scratchpad memory",
        )

        self._exec_start = CSRStatus(
            64,
            description="Number of cycles elapsed until the start of the payload execution.",
        )
        self._exec_stop = CSRStatus(
            64,
            description="Number of cycles elapsed until the end of the payload execution.",
        )

        self.comb += [
            self.start.eq(self._start.re),
            self._status.fields.ready.eq(self.ready),
            self._status.fields.overflow.eq(self.scratchpad.overflow),
            self._read_count.status.eq(self.scratchpad.counter),
            self._exec_start.status.eq(self.exec_start),
            self._exec_stop.status.eq(self.exec_stop),
        ]
