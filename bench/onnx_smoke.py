"""Dependency-free ONNX fixture proving this wheel's native CPU runtime executes.

The model is a one-element Identity graph encoded directly with protobuf's wire
format so release jobs do not need the large `onnx` authoring dependency.
"""

from __future__ import annotations


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _uint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _bytes(field: int, value: bytes | str) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return _varint((field << 3) | 2) + _varint(len(raw)) + raw


def identity_model() -> bytes:
    # Tensor(float)[1]
    dim = _uint(1, 1)
    shape = _bytes(1, dim)
    tensor_type = _uint(1, 1) + _bytes(2, shape)
    type_proto = _bytes(1, tensor_type)
    x = _bytes(1, "x") + _bytes(2, type_proto)
    y = _bytes(1, "y") + _bytes(2, type_proto)

    node = _bytes(1, "x") + _bytes(2, "y") + _bytes(4, "Identity")
    graph = (_bytes(1, node) + _bytes(2, "agrep-runtime-smoke")
             + _bytes(11, x) + _bytes(12, y))
    opset = _uint(2, 13)
    return _uint(1, 8) + _bytes(2, "agrep") + _bytes(7, graph) + _bytes(8, opset)


def main() -> int:
    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(identity_model(), providers=["CPUExecutionProvider"])
    result = session.run(["y"], {"x": np.asarray([3.25], dtype=np.float32)})[0]
    if result.shape != (1,) or float(result[0]) != 3.25:
        raise SystemExit(f"bad ONNX runtime output: {result!r}")
    print(f"onnxruntime {ort.__version__}: CPU Identity inference ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
