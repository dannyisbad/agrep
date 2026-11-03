//! Per-output-channel int8 projection probe, never selected by the product.
use anyhow::{Context, Result};
use candle_core::{
    quantized::{GgmlDType, QMatMul, QStorage, QTensor},
    Device, Module, Tensor,
};
use std::{borrow::Cow, time::Instant};

fn quantize(weight: &Tensor) -> Result<QMatMul> {
    let (rows, cols) = weight.dims2()?;
    anyhow::ensure!(cols.is_multiple_of(32), "Q8_0 block alignment");
    let mut bytes = Vec::with_capacity(rows * cols / 32 * 34);
    for row in weight.to_vec2::<f32>()? {
        let scale = row.iter().fold(0f32, |m, x| m.max(x.abs())) / 127.;
        let scale = half::f16::from_f32(scale);
        for block in row.chunks_exact(32) {
            bytes.extend_from_slice(&scale.to_le_bytes());
            bytes.extend(block.iter().map(|x| {
                if scale.to_f32() == 0. {
                    0
                } else {
                    (x / scale.to_f32()).round().clamp(-127., 127.) as i8 as u8
                }
            }));
        }
    }
    // Keep the backing bytes alive while Candle copies the typed block slice.
    let storage = QStorage::from_data(Cow::Borrowed(&bytes), &Device::Cpu, GgmlDType::Q8_0)?;
    Ok(QMatMul::from_qtensor(QTensor::new(storage, (rows, cols))?)?)
}
fn float_projection(input: &Tensor, weight: &Tensor) -> Result<Tensor> {
    use rayon::prelude::*;
    let rows = input.dim(0)?;
    let chunk = rows.div_ceil(rayon::current_num_threads()).max(128);
    if rows < 256 {
        return Ok(input.matmul(&weight.t()?)?);
    }
    let parts = (0..rows.div_ceil(chunk))
        .into_par_iter()
        .map(|i| {
            input
                .narrow(0, i * chunk, chunk.min(rows - i * chunk))?
                .matmul(&weight.t()?)
        })
        .collect::<candle_core::Result<Vec<_>>>()?;
    Ok(Tensor::cat(&parts, 0)?)
}
fn main() -> Result<()> {
    let path = std::env::args().nth(1).context("MODEL_F32.safetensors")?;
    let tensors = candle_core::safetensors::load(path, &Device::Cpu)?;
    let mut measurements = Vec::new();
    for name in ["attn.Wqkv", "attn.Wo", "mlp.Wi", "mlp.Wo"] {
        let weight = &tensors[&format!("layers.0.{name}.weight")];
        let quantized = quantize(weight)?;
        let (out, width) = weight.dims2()?;
        for rows in [16, 1000, 4000] {
            let input = Tensor::from_vec(
                (0..rows * width)
                    .map(|i| ((i * 17 % 4093) as f32 - 2046.) / 2046.)
                    .collect::<Vec<_>>(),
                (rows, width),
                &Device::Cpu,
            )?;
            let mut samples = Vec::new();
            for round in 0..4 {
                for quant in if round % 2 == 0 {
                    [false, true]
                } else {
                    [true, false]
                } {
                    let start = Instant::now();
                    let result = if quant {
                        quantized.forward(&input)?
                    } else {
                        float_projection(&input, weight)?
                    };
                    let ms = start.elapsed().as_secs_f64() * 1000.;
                    std::hint::black_box(result);
                    samples.push(serde_json::json!({"q8":quant,"round":round,"ms":ms}));
                }
            }
            measurements.push(serde_json::json!({"projection":name,"rows":rows,"in":width,"out":out,"samples":samples}));
        }
    }
    println!(
        "{}",
        serde_json::json!({"format":"Candle Q8_0 blocks sharing one F16 scale per output channel","measurements":measurements})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn per_channel_blocks_preserve_sign_scale_and_zero_rows() {
        let input = Tensor::from_vec(
            (0..3 * 64)
                .map(|i| if i < 64 { 0. } else { (i % 17) as f32 - 8. })
                .collect::<Vec<_>>(),
            (3, 64),
            &Device::Cpu,
        )
        .unwrap();
        let q = quantize(&input).unwrap();
        let QMatMul::QTensor(q) = q else {
            panic!("expected quantized storage")
        };
        let decoded = q
            .dequantize(&Device::Cpu)
            .unwrap()
            .to_vec2::<f32>()
            .unwrap();
        for (source, decoded) in input.to_vec2::<f32>().unwrap().iter().zip(decoded) {
            for (a, b) in source.iter().zip(decoded) {
                assert!((a - b).abs() < 0.033, "{a} != {b}");
            }
        }
    }
}
