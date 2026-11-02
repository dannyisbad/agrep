"""Read executable headers; reject packaging the wrong OS or architecture."""
import struct


def validate_target(raw, target):
    expected_arch = target['arch']
    if target['os'] == 'linux':
        if raw[:6] != b'\x7fELF\x02\x01' or len(raw) < 20:
            raise ValueError('expected a 64-bit little-endian ELF binary')
        machine = struct.unpack_from('<H', raw, 18)[0]
        actual = {62: 'x64', 183: 'arm64'}.get(machine)
    elif target['os'] == 'darwin':
        if raw[:4] != b'\xcf\xfa\xed\xfe' or len(raw) < 8:
            raise ValueError('expected a 64-bit Mach-O binary')
        actual = {0x1000007: 'x64', 0x100000c: 'arm64'}.get(struct.unpack_from('<I', raw, 4)[0])
    else:
        if raw[:2] != b'MZ' or len(raw) < 64:
            raise ValueError('expected a PE executable')
        offset = struct.unpack_from('<I', raw, 60)[0]
        if offset + 6 > len(raw) or raw[offset:offset + 4] != b'PE\0\0':
            raise ValueError('invalid PE header')
        actual = {0x8664: 'x64', 0xaa64: 'arm64'}.get(struct.unpack_from('<H', raw, offset + 4)[0])
    if actual != expected_arch:
        raise ValueError(f'expected {expected_arch} executable, found {actual}')
