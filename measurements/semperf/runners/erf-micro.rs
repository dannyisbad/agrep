use std::{hint::black_box,time::Instant};
unsafe extern "C" { fn erff(x:f32)->f32; }
fn main(){ let xs:Vec<f32>=(0..1000000).map(|i|i as f32/100000.-5.).collect();
let mut different=0;let mut max=0f32;for &x in &xs {let a=candle_core::cpu::erf::erf_f32(x);let b=unsafe{erff(x)};different+=usize::from(a!=b);max=max.max((a-b).abs());}
for native in [false,true] {let t=Instant::now();let mut sum=0.;for _ in 0..10 {for &x in &xs {sum+=black_box(if native{unsafe{erff(black_box(x))}}else{candle_core::cpu::erf::erf_f32(black_box(x))});}}println!("native={native} seconds={} sum={sum} different={different} max_abs={max}",t.elapsed().as_secs_f64());}}
