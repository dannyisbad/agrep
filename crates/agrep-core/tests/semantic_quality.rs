//! The committed oracle comes from full precision ONNX, independent of Candle/q8.
use agrep_core::{semantic_model::DIM, semantic_q8};
#[test]
fn labeled_recall_at_five_and_ordered_neighbors_survive_q8() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/semantic-eval.json")).unwrap();
    let vectors: Vec<Vec<f32>> = serde_json::from_value(fixture["vectors"].clone()).unwrap();
    let quantize = |v: &[f32]| {
        let scale = v.iter().fold(0f32, |p, x| p.max(x.abs())) / 127.;
        (
            v.iter()
                .map(|x| (x / scale).round() as i8)
                .collect::<Vec<_>>(),
            scale,
        )
    };
    let mut rows = vec![];
    let mut scales = vec![];
    for v in &vectors[10..30] {
        let (q, s) = quantize(v);
        rows.extend(q);
        scales.push(s);
    }
    assert_eq!(rows.len(), 20 * DIM);
    let mut recall = 0;
    for query in fixture["queries"].as_array().unwrap() {
        let i = query["query"].as_u64().unwrap() as usize;
        let (q, s) = quantize(&vectors[i]);
        let mut hits = semantic_q8::scan(&rows, &scales, &q, s, 20);
        for hit in &mut hits {
            let f16: Vec<_> = vectors[10 + hit.0]
                .iter()
                .map(|v| half::f16::from_f32(*v).to_f32())
                .collect();
            let norm = f16.iter().map(|v| v * v).sum::<f32>().sqrt();
            hit.1 = vectors[i].iter().zip(f16).map(|(a, b)| a * b).sum::<f32>() / norm;
        }
        hits.sort_by(|a, b| b.1.total_cmp(&a.1));
        hits.truncate(5);
        let mut exact: Vec<_> = (0..20)
            .map(|d| {
                (
                    d,
                    vectors[i]
                        .iter()
                        .zip(&vectors[10 + d])
                        .map(|(a, b)| (*a as f64) * (*b as f64))
                        .sum::<f64>(),
                )
            })
            .collect();
        exact.sort_by(|a, b| b.1.total_cmp(&a.1));
        let actual: Vec<_> = hits.iter().map(|h| h.0).collect();
        let expected: Vec<_> = exact.iter().take(5).map(|h| h.0).collect();
        assert_eq!(
            actual, expected,
            "query {i}: q8 ranking differs from F32 oracle"
        );
        if hits.iter().any(|h| h.0 == i) {
            recall += 1;
        }
    }
    assert_eq!(recall, 10, "labeled recall@5 must be 1.0");
}

#[test]
fn chunks_match_the_reference_character_for_character() {
    let rows: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/semantic-chunks.json")).unwrap();
    for (i, row) in rows.as_array().unwrap().iter().enumerate() {
        let expected: Vec<String> = serde_json::from_value(row["chunks"].clone()).unwrap();
        let actual = agrep_core::semantic_model::chunks(
            row["agent"].as_str().unwrap(),
            row["who"].as_str().unwrap(),
            row["text"].as_str().unwrap(),
        );
        assert_eq!(actual, expected, "reference chunk case {i}");
    }
}
