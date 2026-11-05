# gemm-common 0.19.0: portable AArch64 builds

Source: crates.io gemm-common 0.19.0, checksum
`88027625910cc9b1085aaaa1c4bc46bb3a36aad323452b33c25b5e4e7c8e2a3e`.
The published Cargo.toml, README.md, LICENSE and src/ are copied verbatim
except for four `#[target_feature(enable = "neon,fp16")]` annotations in
src/simd.rs: vmulq_f16, vaddq_f16, vfmaq_f16 and vfmaq_laneq_f16.

These helpers emit FP16 arithmetic assembly. Their callers already select
NeonFp16 or the gemm-f16 neonfp16 kernel only after runtime FP16 detection;
the annotations let the assembler accept them without raising the target's
global CPU baseline. Other architectures and F32 kernels are unchanged.

Keep this patch until an upstream release in Candle's compatible range
carries the annotations. The 0.18.2 dependency is macOS-only and unchanged.
The vendored crate retains its upstream MIT license and generated notices.
