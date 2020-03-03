import argparse
from bitarray import bitarray

from .jesd3 import JESD3Parser
from .svf import SVFParser, SVFEventHandler
from .device import *


def read_jed(file):
    parser = JESD3Parser(file.read())
    parser.parse()
    return parser.fuse, parser.design_spec


def write_jed(file, jed_bits, *, comment):
    assert '*' not in comment
    file.write("\x02{}*\n".format(comment))
    file.write("QF{}* F0*\n".format(len(jed_bits)))
    chunk_size = 64
    for start in range(0, len(jed_bits), chunk_size):
        file.write("L{:05d} {}*\n".format(start, jed_bits[start:start+chunk_size].to01()))
    file.write("\x030000\n")


class ATFSVFEventHandler(SVFEventHandler):
    def ignored(self, *args, **kwargs):
        pass
    svf_frequency = ignored
    svf_trst = ignored
    svf_state = ignored
    svf_endir = ignored
    svf_enddr = ignored
    svf_hir = ignored
    svf_sir = ignored
    svf_tir = ignored
    svf_hdr = ignored
    svf_sdr = ignored
    svf_tdr = ignored
    svf_runtest = ignored
    svf_piomap = ignored
    svf_pio = ignored

    def __init__(self):
        self.ir = None
        self.erase = False
        self.addr = 0
        self.data = b''
        self.bits = {}

    def svf_sir(self, tdi, smask, tdo, mask):
        self.ir = int.from_bytes(tdi.tobytes(), 'little')
        if self.ir == ATF15xxInstr.ISC_LATCH_ERASE:
            self.erase = True
        if self.ir == ATF15xxInstr.ISC_DATA:
            self.erase = False

    def svf_sdr(self, tdi, smask, tdo, mask):
        if self.ir == ATF15xxInstr.ISC_ADDRESS:
            self.addr = int.from_bytes(tdi.tobytes(), 'little')
        if (self.ir & ~0x3) == ATF15xxInstr.ISC_DATA:
            self.data = tdi

    def svf_runtest(self, run_state, run_count, run_clock, min_time, max_time, end_state):
        if not self.erase and self.ir == ATF15xxInstr.ISC_PROGRAM_ERASE:
            self.bits[self.addr] = self.data


def read_svf(file):
    handler = ATFSVFEventHandler()
    parser = SVFParser(file.read(), handler)
    parser.parse_file()
    return handler.bits, ''


def _bitarray_to_hex(input_bits):
    bits = bitarray(input_bits, endian="little")
    bits.bytereverse()
    bits.reverse()
    return bits.tobytes().hex()


def write_svf(file, svf_bits, device, *, comment):
    # This code is kind of awful.
    def emit_header():
        for comment_line in comment.splitlines():
            file.write("// {}\n".format(comment_line))
        file.write("TRST ABSENT;\n")
        file.write("ENDIR IDLE;\n")
        file.write("ENDDR IDLE;\n")
        file.write("HDR 0;\n")
        file.write("HIR 0;\n")
        file.write("TDR 0;\n")
        file.write("TIR 0;\n")
        file.write("STATE RESET;\n")
    def emit_check_idcode(idcode):
        file.write("// Check IDCODE\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.IDCODE))
        file.write("SDR 32 TDI (ffffffff)\n\tTDO ({:08x})\n\tMASK (ffffffff);\n".format(idcode))
    def emit_enable():
        file.write("// ISC enable\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_CONFIG))
        file.write("SDR 10 TDI ({:03x});\n".format(0x1b9)) # magic constant?
        file.write("STATE IDLE;\n")
    def emit_disable():
        file.write("// ISC disable\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_CONFIG))
        file.write("SDR 10 TDI ({:03x});\n".format(0x000))
        file.write("STATE IDLE;\n")
    def emit_status():
        file.write("// ISC check status\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_STATUS))
        # actually check the status? ATMISP doesn't #yolo
    def emit_erase():
        file.write("// ISC erase\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_LATCH_ERASE))
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_PROGRAM_ERASE))
        file.write("RUNTEST IDLE 210E-3 SEC;\n")
        emit_status()
    def emit_program(address, data):
        file.write("// ISC program word\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_ADDRESS))
        file.write("SDR {} TDI ({:0{}x});\n".format(device.addr_width,
            address, (device.addr_width + 3) // 4))
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_DATA | (address >> 8)))
        file.write("SDR {} TDI ({:x});\n".format(len(data), int(data.to01()[::-1], 2)))
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_PROGRAM_ERASE))
        file.write("RUNTEST IDLE 30E-3 SEC;\n")
        emit_status()
    def emit_verify(address, data):
        file.write("// ISC verify word\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_ADDRESS))
        file.write("SDR {} TDI ({:0{}x});\n".format(device.addr_width,
            address, (device.addr_width + 3) // 4))
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_READ))
        file.write("RUNTEST IDLE 20E-3 SEC;\n")
        file.write("SIR 10 TDI ({:03x});\n".format(ATF15xxInstr.ISC_DATA | (address >> 8)))
        file.write("SDR {} TDI ({:x})\n\tTDO ({:x})\n\tMASK ({:x});\n".format(len(data),
            int(data.to01()[::-1], 2), int(data.to01()[::-1], 2), (1 << len(data)) - 1))

    emit_header()
    emit_check_idcode(device.idcode)
    emit_enable()
    emit_erase()
    for svf_row in svf_bits:
        emit_program(svf_row, svf_bits[svf_row])
    for svf_row in svf_bits:
        emit_verify(svf_row, svf_bits[svf_row])
    emit_disable()


class ATFFileType(argparse.FileType):
    def __call__(self, value):
        file = super().__call__(value)
        filename = file.name.lower()
        if not (filename.endswith('.jed') or filename.endswith('.svf')):
            raise argparse.ArgumentTypeError('{} is not a JED or SVF file'.format(filename))
        return file


def main():
    parser = argparse.ArgumentParser(description='Convert between ATF15xx JED and SVF files.')
    parser.add_argument(
        '-d', '--device', metavar='DEVICE', choices=('ATF1502',), default='ATF1502',
        help='device (one of: %(choices)s)')
    parser.add_argument(
        'input', metavar='INPUT', type=ATFFileType('r'),
        help='input file')
    parser.add_argument(
        'output', metavar='OUTPUT', type=ATFFileType('w'),
        help='output file')
    args = parser.parse_args()

    if args.device == 'ATF1502':
        device = ATF1502Device
    else:
        assert False

    jed_bits = svf_bits = None
    if args.input.name.lower().endswith('.jed'):
        jed_bits, comment = read_jed(args.input)
    elif args.input.name.lower().endswith('.svf'):
        svf_bits, comment = read_svf(args.input)
    else:
        assert False

    if args.output.name.lower().endswith('.jed'):
        if jed_bits is None:
            jed_bits = device.svf_to_jed(svf_bits)
        write_jed(args.output, jed_bits, comment=comment)
    elif args.output.name.lower().endswith('.svf'):
        if svf_bits is None:
            svf_bits = device.jed_to_svf(jed_bits)
        write_svf(args.output, svf_bits, device, comment=comment)
    else:
        assert False


if __name__ == '__main__':
    main()
