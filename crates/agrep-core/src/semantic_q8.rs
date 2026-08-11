//! Generation-bound q8 cosine vectors derived from the committed f32 index.

use serde::{Deserialize, Serialize};
use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::fmt;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

pub const MAGIC: [u8; 4] = *b"AGQ8";
pub const VERSION: u32 = 1;
pub const FLAGS: u32 = 1;
pub const HEADER_LEN: usize = 64;
pub const SCORE_KIND: &str = "cosine-q8-v1";
pub const GROUP_MAGIC: [u8; 4] = *b"AGQG";
pub const GROUP_VERSION: u32 = 1;
pub const GROUP_HEADER_LEN: usize = 64;
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

#[derive(Debug)]
pub enum Q8Error {
    Io(io::Error),
    Format(String),
}

impl fmt::Display for Q8Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "{error}"),
            Self::Format(error) => f.write_str(error),
        }
    }
}

impl std::error::Error for Q8Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Format(_) => None,
        }
    }
}

impl From<io::Error> for Q8Error {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[derive(Deserialize)]
struct F32Meta {
    dim: u32,
    commit: F32Commit,
}

#[derive(Deserialize)]
struct F32Commit {
    version: u32,
    generation: String,
    rows: u64,
    matrix: F32Matrix,
}

#[derive(Deserialize)]
struct F32Matrix {
    size: u64,
}

#[derive(Debug, Serialize)]
pub struct BuildResult {
    pub version: u32,
    pub score_kind: &'static str,
    pub f32_generation: String,
    pub rows: u64,
    pub dim: u32,
    pub artifact: PathBuf,
    pub artifact_size: u64,
    pub checksum: String,
}

#[derive(Debug, Serialize)]
pub struct GroupBuildResult {
    pub version: u32,
    pub generation: String,
    pub rows: u64,
    pub groups: u32,
    pub artifact: PathBuf,
    pub artifact_size: u64,
    pub checksum: String,
}

enum Image {
    Owned(Vec<u8>),
    #[cfg(any(unix, windows))]
    Mapped(crate::mapped_file::MappedFile),
}

impl Image {
    fn as_slice(&self) -> &[u8] {
        match self {
            Self::Owned(bytes) => bytes,
            #[cfg(any(unix, windows))]
            Self::Mapped(mapping) => mapping.as_slice(),
        }
    }
}

pub struct Q8Matrix {
    image: Image,
    rows: usize,
    dim: usize,
    stride: usize,
    generation: [u8; 16],
    checksum: u64,
}

impl Q8Matrix {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, Q8Error> {
        let mut file = File::open(path)?;
        let len = usize::try_from(file.metadata()?.len())
            .map_err(|_| Q8Error::Format("q8 artifact is too large to address".into()))?;

        #[cfg(any(unix, windows))]
        if let Ok(mapping) = crate::mapped_file::MappedFile::map(&file, len) {
            return Self::parse_image(Image::Mapped(mapping), false);
        }

        let mut bytes = Vec::with_capacity(len);
        file.read_to_end(&mut bytes)?;
        Self::parse_image(Image::Owned(bytes), false)
    }

    pub fn parse(bytes: Vec<u8>) -> Result<Self, Q8Error> {
        Self::parse_image(Image::Owned(bytes), true)
    }

    fn parse_image(image: Image, verify_payload: bool) -> Result<Self, Q8Error> {
        let bytes = image.as_slice();
        if bytes.len() < HEADER_LEN {
            return Err(Q8Error::Format(
                "q8 artifact is shorter than its header".into(),
            ));
        }
        if bytes[..4] != MAGIC {
            return Err(Q8Error::Format("q8 artifact has bad magic".into()));
        }
        let u32_at =
            |offset: usize| u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
        let u64_at =
            |offset: usize| u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap());
        if u32_at(4) != VERSION {
            return Err(Q8Error::Format("q8 artifact version mismatch".into()));
        }
        if u32_at(12) != FLAGS {
            return Err(Q8Error::Format("q8 artifact flags are unsupported".into()));
        }
        let dim = usize::try_from(u32_at(8))
            .map_err(|_| Q8Error::Format("q8 dimension overflow".into()))?;
        let rows = usize::try_from(u64_at(16))
            .map_err(|_| Q8Error::Format("q8 row count overflow".into()))?;
        let stride = usize::try_from(u32_at(40))
            .map_err(|_| Q8Error::Format("q8 stride overflow".into()))?;
        if dim == 0 || dim > 16_384 || stride != dim + 4 || u32_at(44) != 0 {
            return Err(Q8Error::Format("q8 artifact has invalid dimensions".into()));
        }
        if bytes[56..HEADER_LEN].iter().any(|byte| *byte != 0) {
            return Err(Q8Error::Format(
                "q8 artifact reserved bytes are nonzero".into(),
            ));
        }
        let payload_len = rows
            .checked_mul(stride)
            .ok_or_else(|| Q8Error::Format("q8 payload length overflow".into()))?;
        if HEADER_LEN.checked_add(payload_len) != Some(bytes.len()) {
            return Err(Q8Error::Format("q8 artifact length mismatch".into()));
        }
        let checksum = u64_at(48);
        if verify_payload && fnv64(&bytes[HEADER_LEN..]) != checksum {
            return Err(Q8Error::Format("q8 artifact checksum mismatch".into()));
        }
        let mut generation = [0u8; 16];
        generation.copy_from_slice(&bytes[24..40]);
        Ok(Self {
            image,
            rows,
            dim,
            stride,
            generation,
            checksum,
        })
    }

    pub fn rows(&self) -> usize {
        self.rows
    }

    pub fn dim(&self) -> usize {
        self.dim
    }

    pub fn generation(&self) -> [u8; 16] {
        self.generation
    }

    pub fn checksum(&self) -> u64 {
        self.checksum
    }

    pub fn verify_checksum(&self) -> Result<(), Q8Error> {
        if fnv64(&self.image.as_slice()[HEADER_LEN..]) != self.checksum {
            return Err(Q8Error::Format("q8 artifact checksum mismatch".into()));
        }
        Ok(())
    }

    pub fn scores(&self, query: &[f32]) -> Result<Vec<f32>, Q8Error> {
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let payload = &self.image.as_slice()[HEADER_LEN..];
        let mut scores = Vec::with_capacity(self.rows);
        for row in payload.chunks_exact(self.stride) {
            let row_inv_norm = f32::from_le_bytes(row[..4].try_into().unwrap());
            let vector =
                unsafe { std::slice::from_raw_parts(row[4..].as_ptr().cast::<i8>(), self.dim) };
            let dot = dot_product(vector, &quantized);
            scores.push((dot as f32) * row_inv_norm * query_inv_norm);
        }
        Ok(scores)
    }

    pub fn top_scores(&self, query: &[f32], k: usize) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_scores_with_eligibility(query, k, None)
    }

    pub fn top_scores_eligible(
        &self,
        query: &[f32],
        k: usize,
        eligibility: &[u8],
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_scores_with_eligibility(query, k, Some(eligibility))
    }

    fn top_scores_with_eligibility(
        &self,
        query: &[f32],
        k: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        if k == 0 {
            return Err(Q8Error::Format("q8 candidate count is zero".into()));
        }
        validate_eligibility(eligibility, self.rows)?;
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let payload = &self.image.as_slice()[HEADER_LEN..];
        let mut heap = BinaryHeap::with_capacity(k.min(self.rows));
        for (ordinal, row) in payload.chunks_exact(self.stride).enumerate() {
            if !is_eligible(eligibility, ordinal) {
                continue;
            }
            let row_inv_norm = f32::from_le_bytes(row[..4].try_into().unwrap());
            let vector =
                unsafe { std::slice::from_raw_parts(row[4..].as_ptr().cast::<i8>(), self.dim) };
            let score = (dot_product(vector, &quantized) as f32) * row_inv_norm * query_inv_norm;
            let candidate = Candidate {
                score,
                ordinal: ordinal as u64,
            };
            if heap.len() < k {
                heap.push(Reverse(candidate));
            } else if heap.peek().is_some_and(|worst| candidate > worst.0) {
                heap.pop();
                heap.push(Reverse(candidate));
            }
        }
        Ok(sorted_candidates(heap))
    }

    pub fn top_group_scores(
        &self,
        groups: &GroupMap,
        query: &[f32],
        k: usize,
        heads: usize,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_group_scores_with_eligibility(groups, query, k, heads, None)
    }

    pub fn top_group_scores_eligible(
        &self,
        groups: &GroupMap,
        query: &[f32],
        k: usize,
        heads: usize,
        eligibility: &[u8],
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_group_scores_with_eligibility(groups, query, k, heads, Some(eligibility))
    }

    fn top_group_scores_with_eligibility(
        &self,
        groups: &GroupMap,
        query: &[f32],
        k: usize,
        heads: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        if (groups.rows != self.rows || groups.generation != self.generation)
            || groups.group_count == 0
        {
            return Err(Q8Error::Format(
                "q8 group map does not match the matrix".into(),
            ));
        }
        if k == 0 || heads == 0 {
            return Err(Q8Error::Format("q8 candidate count is zero".into()));
        }
        validate_eligibility(eligibility, self.rows)?;
        let head_limit = u32::try_from(heads)
            .map_err(|_| Q8Error::Format("q8 candidate count exceeds u32".into()))?;
        let mut offsets = vec![0u32; groups.group_count + 1];
        for (ordinal, group) in groups.ids().enumerate() {
            if !is_eligible(eligibility, ordinal) {
                continue;
            }
            let index = group as usize;
            if index >= groups.group_count {
                return Err(Q8Error::Format("q8 group id is out of range".into()));
            }
            offsets[index] = offsets[index].saturating_add(1).min(head_limit);
        }
        let mut slots = 0u32;
        for count in &mut offsets[..groups.group_count] {
            let width = *count;
            *count = slots;
            slots = slots
                .checked_add(width)
                .ok_or_else(|| Q8Error::Format("q8 group candidate capacity overflow".into()))?;
        }
        offsets[groups.group_count] = slots;
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let payload = &self.image.as_slice()[HEADER_LEN..];
        let mut best_scores = vec![f32::NEG_INFINITY; slots as usize];
        let mut best_ordinals = vec![u64::MAX; slots as usize];
        for (ordinal, (row, group)) in payload
            .chunks_exact(self.stride)
            .zip(groups.ids())
            .enumerate()
        {
            if !is_eligible(eligibility, ordinal) {
                continue;
            }
            let row_inv_norm = f32::from_le_bytes(row[..4].try_into().unwrap());
            let vector =
                unsafe { std::slice::from_raw_parts(row[4..].as_ptr().cast::<i8>(), self.dim) };
            let score = (dot_product(vector, &quantized) as f32) * row_inv_norm * query_inv_norm;
            let start = offsets[group as usize] as usize;
            let stop = offsets[group as usize + 1] as usize;
            let candidate = Candidate {
                score,
                ordinal: ordinal as u64,
            };
            let mut insert = stop;
            for position in start..stop {
                let current = Candidate {
                    score: best_scores[position],
                    ordinal: best_ordinals[position],
                };
                if candidate > current {
                    insert = position;
                    break;
                }
            }
            if insert < stop {
                for position in (insert + 1..stop).rev() {
                    best_scores[position] = best_scores[position - 1];
                    best_ordinals[position] = best_ordinals[position - 1];
                }
                best_scores[insert] = score;
                best_ordinals[insert] = ordinal as u64;
            }
        }
        let mut heap = BinaryHeap::with_capacity(k.min(groups.group_count));
        for group in 0..groups.group_count {
            let start = offsets[group] as usize;
            if start == offsets[group + 1] as usize || best_ordinals[start] == u64::MAX {
                continue;
            }
            let candidate = GroupCandidate {
                head: Candidate {
                    score: best_scores[start],
                    ordinal: best_ordinals[start],
                },
                group: group as u32,
            };
            if heap.len() < k {
                heap.push(Reverse(candidate));
            } else if heap.peek().is_some_and(|worst| candidate > worst.0) {
                heap.pop();
                heap.push(Reverse(candidate));
            }
        }
        let mut selected: Vec<_> = heap.into_iter().map(|entry| entry.0).collect();
        selected.sort_unstable_by(|left, right| right.cmp(left));
        let mut output = Vec::with_capacity(selected.len() * heads);
        for candidate in selected {
            let start = offsets[candidate.group as usize] as usize;
            let stop = offsets[candidate.group as usize + 1] as usize;
            for position in start..stop {
                if best_ordinals[position] != u64::MAX {
                    output.push((best_ordinals[position], best_scores[position]));
                }
            }
        }
        output.sort_unstable_by(|left, right| {
            right
                .1
                .total_cmp(&left.1)
                .then_with(|| left.0.cmp(&right.0))
        });
        Ok(output)
    }

    fn quantized_query(&self, query: &[f32]) -> Result<(Vec<i8>, f32), Q8Error> {
        if query.len() != self.dim || query.iter().any(|value| !value.is_finite()) {
            return Err(Q8Error::Format("q8 query vector is invalid".into()));
        }
        quantize(query)
    }
}

pub struct GroupMap {
    image: Image,
    rows: usize,
    group_count: usize,
    generation: [u8; 16],
    checksum: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SegmentSetRecord {
    version: u32,
    generation: String,
    dim: u32,
    row_high_water: u64,
    live_rows: u64,
    group_count: u32,
    segments: Vec<SegmentRecord>,
    shadows: Vec<ShadowRecord>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SegmentRecord {
    row_base: u64,
    rows: u64,
    artifact: String,
    groups: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ShadowRecord {
    path: String,
    rows: u64,
}

struct Q8Segment {
    row_base: u64,
    matrix: Q8Matrix,
    groups: GroupMap,
}

pub struct SegmentSet {
    segments: Vec<Q8Segment>,
    shadows: Vec<u64>,
    generation: [u8; 16],
    dim: usize,
    row_high_water: usize,
    live_rows: usize,
    group_count: usize,
}

impl SegmentSet {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, Q8Error> {
        let path = path.as_ref();
        let bytes = fs::read(path)?;
        let record: SegmentSetRecord = serde_json::from_slice(&bytes)
            .map_err(|error| Q8Error::Format(format!("invalid q8 segment set: {error}")))?;
        if record.version != 1 {
            return Err(Q8Error::Format("q8 segment set version mismatch".into()));
        }
        let generation = parse_generation(&record.generation)?;
        let dim = usize::try_from(record.dim)
            .map_err(|_| Q8Error::Format("q8 segment dimension overflow".into()))?;
        let row_high_water = usize::try_from(record.row_high_water)
            .map_err(|_| Q8Error::Format("q8 row high-water mark is too large".into()))?;
        let live_rows = usize::try_from(record.live_rows)
            .map_err(|_| Q8Error::Format("q8 live row count is too large".into()))?;
        let group_count = usize::try_from(record.group_count)
            .map_err(|_| Q8Error::Format("q8 global group count overflow".into()))?;
        if dim == 0
            || dim > 16_384
            || row_high_water == 0
            || live_rows == 0
            || group_count == 0
            || record.segments.is_empty()
        {
            return Err(Q8Error::Format("q8 segment set header is invalid".into()));
        }

        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        let mut segments = Vec::with_capacity(record.segments.len());
        let mut used_paths = HashSet::new();
        let mut physical_rows = 0usize;
        let mut previous_end = 0u64;
        for (index, descriptor) in record.segments.into_iter().enumerate() {
            let end = descriptor
                .row_base
                .checked_add(descriptor.rows)
                .ok_or_else(|| Q8Error::Format("q8 segment row range overflow".into()))?;
            if descriptor.rows == 0
                || end > record.row_high_water
                || (index > 0 && descriptor.row_base < previous_end)
            {
                return Err(Q8Error::Format(
                    "q8 segment ranges overlap or are out of order".into(),
                ));
            }
            previous_end = end;
            let artifact_path = regular_relative_path(parent, &descriptor.artifact)?;
            let groups_path = regular_relative_path(parent, &descriptor.groups)?;
            if !used_paths.insert(artifact_path.clone()) || !used_paths.insert(groups_path.clone())
            {
                return Err(Q8Error::Format(
                    "q8 segment set reuses an artifact path".into(),
                ));
            }
            let matrix = Q8Matrix::open(&artifact_path)?;
            let groups = GroupMap::open(&groups_path)?;
            let rows = usize::try_from(descriptor.rows)
                .map_err(|_| Q8Error::Format("q8 segment row count overflow".into()))?;
            if matrix.rows() != rows
                || groups.rows() != rows
                || matrix.dim() != dim
                || groups.generation() != matrix.generation()
                || groups.group_count() > group_count
                || groups.ids().any(|group| group as usize >= group_count)
            {
                return Err(Q8Error::Format(
                    "q8 segment artifacts do not match their descriptor".into(),
                ));
            }
            physical_rows = physical_rows
                .checked_add(rows)
                .ok_or_else(|| Q8Error::Format("q8 physical row count overflow".into()))?;
            segments.push(Q8Segment {
                row_base: descriptor.row_base,
                matrix,
                groups,
            });
        }

        let shadow_words = row_high_water
            .checked_add(63)
            .ok_or_else(|| Q8Error::Format("q8 shadow bitmap length overflow".into()))?
            / 64;
        let mut shadows = vec![0u64; shadow_words];
        let mut shadow_count = 0usize;
        for descriptor in record.shadows {
            let shadow_path = regular_relative_path(parent, &descriptor.path)?;
            if !used_paths.insert(shadow_path.clone()) {
                return Err(Q8Error::Format(
                    "q8 segment set reuses an artifact path".into(),
                ));
            }
            let expected_size = descriptor
                .rows
                .checked_mul(8)
                .ok_or_else(|| Q8Error::Format("q8 shadow length overflow".into()))?;
            if fs::metadata(&shadow_path)?.len() != expected_size {
                return Err(Q8Error::Format("q8 shadow length mismatch".into()));
            }
            let mut input = BufReader::with_capacity(1024 * 1024, File::open(&shadow_path)?);
            let mut previous = None;
            for _ in 0..descriptor.rows {
                let mut raw = [0u8; 8];
                input.read_exact(&mut raw)?;
                let row_ref = u64::from_le_bytes(raw);
                if previous.is_some_and(|value| row_ref <= value)
                    || !row_ref_in_segments(row_ref, &segments)
                {
                    return Err(Q8Error::Format(
                        "q8 shadows are not sorted unique live row references".into(),
                    ));
                }
                previous = Some(row_ref);
                let ordinal = usize::try_from(row_ref)
                    .map_err(|_| Q8Error::Format("q8 shadow row overflow".into()))?;
                let word = &mut shadows[ordinal / 64];
                let bit = 1u64 << (ordinal % 64);
                if *word & bit != 0 {
                    return Err(Q8Error::Format("q8 shadow row is duplicated".into()));
                }
                *word |= bit;
                shadow_count += 1;
            }
        }
        if physical_rows.checked_sub(shadow_count) != Some(live_rows) {
            return Err(Q8Error::Format(
                "q8 segment live row count does not match its shadows".into(),
            ));
        }
        Ok(Self {
            segments,
            shadows,
            generation,
            dim,
            row_high_water,
            live_rows,
            group_count,
        })
    }

    pub fn rows(&self) -> usize {
        self.row_high_water
    }

    pub fn live_rows(&self) -> usize {
        self.live_rows
    }

    pub fn dim(&self) -> usize {
        self.dim
    }

    pub fn generation(&self) -> [u8; 16] {
        self.generation
    }

    pub fn scores(&self, query: &[f32]) -> Result<Vec<f32>, Q8Error> {
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let mut scores = vec![f32::MIN; self.row_high_water];
        for segment in &self.segments {
            let payload = &segment.matrix.image.as_slice()[HEADER_LEN..];
            for (ordinal, row) in payload.chunks_exact(segment.matrix.stride).enumerate() {
                let row_ref = segment.row_base + ordinal as u64;
                if self.is_shadowed(row_ref) {
                    continue;
                }
                scores[row_ref as usize] = row_score(
                    row,
                    segment.matrix.dim,
                    &quantized,
                    query_inv_norm,
                    dot_product,
                );
            }
        }
        Ok(scores)
    }

    pub fn top_scores(&self, query: &[f32], k: usize) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_scores_with_eligibility(query, k, None)
    }

    pub fn top_scores_eligible(
        &self,
        query: &[f32],
        k: usize,
        eligibility: &[u8],
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_scores_with_eligibility(query, k, Some(eligibility))
    }

    fn top_scores_with_eligibility(
        &self,
        query: &[f32],
        k: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        if k == 0 {
            return Err(Q8Error::Format("q8 candidate count is zero".into()));
        }
        validate_eligibility(eligibility, self.row_high_water)?;
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let mut heap = BinaryHeap::with_capacity(k.min(self.live_rows));
        for segment in &self.segments {
            let payload = &segment.matrix.image.as_slice()[HEADER_LEN..];
            for (ordinal, row) in payload.chunks_exact(segment.matrix.stride).enumerate() {
                let row_ref = segment.row_base + ordinal as u64;
                if self.is_shadowed(row_ref) || !is_eligible(eligibility, row_ref as usize) {
                    continue;
                }
                let candidate = Candidate {
                    score: row_score(
                        row,
                        segment.matrix.dim,
                        &quantized,
                        query_inv_norm,
                        dot_product,
                    ),
                    ordinal: row_ref,
                };
                retain_candidate(&mut heap, candidate, k);
            }
        }
        Ok(sorted_candidates(heap))
    }

    pub fn top_group_scores(
        &self,
        query: &[f32],
        k: usize,
        heads: usize,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_group_scores_with_eligibility(query, k, heads, None)
    }

    pub fn top_group_scores_eligible(
        &self,
        query: &[f32],
        k: usize,
        heads: usize,
        eligibility: &[u8],
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        self.top_group_scores_with_eligibility(query, k, heads, Some(eligibility))
    }

    fn top_group_scores_with_eligibility(
        &self,
        query: &[f32],
        k: usize,
        heads: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, Q8Error> {
        if k == 0 || heads == 0 {
            return Err(Q8Error::Format("q8 candidate count is zero".into()));
        }
        validate_eligibility(eligibility, self.row_high_water)?;
        let mut offsets = vec![0usize; self.group_count + 1];
        for segment in &self.segments {
            for (ordinal, group) in segment.groups.ids().enumerate() {
                let row_ref = segment.row_base + ordinal as u64;
                if !self.is_shadowed(row_ref) && is_eligible(eligibility, row_ref as usize) {
                    offsets[group as usize] = (offsets[group as usize] + 1).min(heads);
                }
            }
        }
        let mut slots = 0usize;
        for count in &mut offsets[..self.group_count] {
            let width = *count;
            *count = slots;
            slots = slots
                .checked_add(width)
                .ok_or_else(|| Q8Error::Format("q8 group candidate capacity overflow".into()))?;
        }
        offsets[self.group_count] = slots;
        let (quantized, query_inv_norm) = self.quantized_query(query)?;
        let dot_product = selected_dot();
        let mut best_scores = vec![f32::NEG_INFINITY; slots];
        let mut best_ordinals = vec![u64::MAX; slots];
        for segment in &self.segments {
            let payload = &segment.matrix.image.as_slice()[HEADER_LEN..];
            for (ordinal, (row, group)) in payload
                .chunks_exact(segment.matrix.stride)
                .zip(segment.groups.ids())
                .enumerate()
            {
                let row_ref = segment.row_base + ordinal as u64;
                if self.is_shadowed(row_ref) || !is_eligible(eligibility, row_ref as usize) {
                    continue;
                }
                let score = row_score(
                    row,
                    segment.matrix.dim,
                    &quantized,
                    query_inv_norm,
                    dot_product,
                );
                insert_group_head(
                    &offsets,
                    &mut best_scores,
                    &mut best_ordinals,
                    group as usize,
                    Candidate {
                        score,
                        ordinal: row_ref,
                    },
                );
            }
        }
        select_group_candidates(&offsets, &best_scores, &best_ordinals, k, heads)
    }

    fn quantized_query(&self, query: &[f32]) -> Result<(Vec<i8>, f32), Q8Error> {
        if query.len() != self.dim || query.iter().any(|value| !value.is_finite()) {
            return Err(Q8Error::Format("q8 query vector is invalid".into()));
        }
        quantize(query)
    }

    fn is_shadowed(&self, row_ref: u64) -> bool {
        let ordinal = row_ref as usize;
        self.shadows[ordinal / 64] & (1u64 << (ordinal % 64)) != 0
    }
}

fn regular_relative_path(parent: &Path, value: &str) -> Result<PathBuf, Q8Error> {
    let relative = Path::new(value);
    if value.is_empty()
        || relative.is_absolute()
        || relative
            .components()
            .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err(Q8Error::Format(
            "q8 segment artifact path is not a normal relative path".into(),
        ));
    }
    let path = parent.join(relative);
    let metadata = fs::symlink_metadata(&path)?;
    if !metadata.file_type().is_file() {
        return Err(Q8Error::Format(
            "q8 segment artifact is not a regular file".into(),
        ));
    }
    Ok(path)
}

fn row_ref_in_segments(row_ref: u64, segments: &[Q8Segment]) -> bool {
    let index = segments.partition_point(|segment| segment.row_base <= row_ref);
    index > 0
        && row_ref
            < segments[index - 1].row_base + u64::try_from(segments[index - 1].matrix.rows).unwrap()
}

fn row_score(
    row: &[u8],
    dim: usize,
    quantized: &[i8],
    query_inv_norm: f32,
    dot_product: DotProduct,
) -> f32 {
    let row_inv_norm = f32::from_le_bytes(row[..4].try_into().unwrap());
    let vector = unsafe { std::slice::from_raw_parts(row[4..].as_ptr().cast::<i8>(), dim) };
    (dot_product(vector, quantized) as f32) * row_inv_norm * query_inv_norm
}

fn validate_eligibility(eligibility: Option<&[u8]>, rows: usize) -> Result<(), Q8Error> {
    let Some(bits) = eligibility else {
        return Ok(());
    };
    let expected = rows
        .checked_add(7)
        .ok_or_else(|| Q8Error::Format("q8 eligibility length overflow".into()))?
        / 8;
    if bits.len() != expected {
        return Err(Q8Error::Format("q8 eligibility length mismatch".into()));
    }
    let remainder = rows % 8;
    if remainder != 0 && bits.last().is_some_and(|byte| byte >> remainder != 0) {
        return Err(Q8Error::Format(
            "q8 eligibility contains out-of-range rows".into(),
        ));
    }
    Ok(())
}

#[inline]
fn is_eligible(eligibility: Option<&[u8]>, ordinal: usize) -> bool {
    eligibility.is_none_or(|bits| bits[ordinal / 8] & (1 << (ordinal % 8)) != 0)
}

fn retain_candidate(heap: &mut BinaryHeap<Reverse<Candidate>>, candidate: Candidate, k: usize) {
    if heap.len() < k {
        heap.push(Reverse(candidate));
    } else if heap.peek().is_some_and(|worst| candidate > worst.0) {
        heap.pop();
        heap.push(Reverse(candidate));
    }
}

fn insert_group_head(
    offsets: &[usize],
    best_scores: &mut [f32],
    best_ordinals: &mut [u64],
    group: usize,
    candidate: Candidate,
) {
    let start = offsets[group];
    let stop = offsets[group + 1];
    let mut insert = stop;
    for position in start..stop {
        let current = Candidate {
            score: best_scores[position],
            ordinal: best_ordinals[position],
        };
        if candidate > current {
            insert = position;
            break;
        }
    }
    if insert < stop {
        for position in (insert + 1..stop).rev() {
            best_scores[position] = best_scores[position - 1];
            best_ordinals[position] = best_ordinals[position - 1];
        }
        best_scores[insert] = candidate.score;
        best_ordinals[insert] = candidate.ordinal;
    }
}

fn select_group_candidates(
    offsets: &[usize],
    best_scores: &[f32],
    best_ordinals: &[u64],
    k: usize,
    heads: usize,
) -> Result<Vec<(u64, f32)>, Q8Error> {
    let group_count = offsets.len() - 1;
    let mut heap = BinaryHeap::with_capacity(k.min(group_count));
    for group in 0..group_count {
        let start = offsets[group];
        if start == offsets[group + 1] || best_ordinals[start] == u64::MAX {
            continue;
        }
        let candidate = GroupCandidate {
            head: Candidate {
                score: best_scores[start],
                ordinal: best_ordinals[start],
            },
            group: group as u32,
        };
        if heap.len() < k {
            heap.push(Reverse(candidate));
        } else if heap.peek().is_some_and(|worst| candidate > worst.0) {
            heap.pop();
            heap.push(Reverse(candidate));
        }
    }
    let mut selected: Vec<_> = heap.into_iter().map(|entry| entry.0).collect();
    selected.sort_unstable_by(|left, right| right.cmp(left));
    let mut output = Vec::with_capacity(selected.len() * heads);
    for candidate in selected {
        let start = offsets[candidate.group as usize];
        let stop = offsets[candidate.group as usize + 1];
        for position in start..stop {
            if best_ordinals[position] != u64::MAX {
                output.push((best_ordinals[position], best_scores[position]));
            }
        }
    }
    output.sort_unstable_by(|left, right| {
        right
            .1
            .total_cmp(&left.1)
            .then_with(|| left.0.cmp(&right.0))
    });
    Ok(output)
}

impl GroupMap {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, Q8Error> {
        let mut file = File::open(path)?;
        let len = usize::try_from(file.metadata()?.len())
            .map_err(|_| Q8Error::Format("q8 group map is too large to address".into()))?;
        #[cfg(any(unix, windows))]
        if let Ok(mapping) = crate::mapped_file::MappedFile::map(&file, len) {
            return Self::parse_image(Image::Mapped(mapping), false);
        }
        let mut bytes = Vec::with_capacity(len);
        file.read_to_end(&mut bytes)?;
        Self::parse_image(Image::Owned(bytes), false)
    }

    pub fn parse(bytes: Vec<u8>) -> Result<Self, Q8Error> {
        Self::parse_image(Image::Owned(bytes), true)
    }

    fn parse_image(image: Image, verify_payload: bool) -> Result<Self, Q8Error> {
        let bytes = image.as_slice();
        if bytes.len() < GROUP_HEADER_LEN {
            return Err(Q8Error::Format(
                "q8 group map is shorter than its header".into(),
            ));
        }
        if bytes[..4] != GROUP_MAGIC {
            return Err(Q8Error::Format("q8 group map has bad magic".into()));
        }
        let u32_at =
            |offset: usize| u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
        let u64_at =
            |offset: usize| u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap());
        if u32_at(4) != GROUP_VERSION || u32_at(8) != 0 || u32_at(40) != 4 {
            return Err(Q8Error::Format("q8 group map version mismatch".into()));
        }
        let group_count = usize::try_from(u32_at(12))
            .map_err(|_| Q8Error::Format("q8 group count overflow".into()))?;
        let rows = usize::try_from(u64_at(16))
            .map_err(|_| Q8Error::Format("q8 group row count overflow".into()))?;
        if group_count == 0 || u32_at(44) != 0 || bytes[56..64].iter().any(|byte| *byte != 0) {
            return Err(Q8Error::Format("q8 group map header is invalid".into()));
        }
        let payload_len = rows
            .checked_mul(4)
            .ok_or_else(|| Q8Error::Format("q8 group map length overflow".into()))?;
        if GROUP_HEADER_LEN.checked_add(payload_len) != Some(bytes.len()) {
            return Err(Q8Error::Format("q8 group map length mismatch".into()));
        }
        let checksum = u64_at(48);
        if verify_payload && fnv64(&bytes[GROUP_HEADER_LEN..]) != checksum {
            return Err(Q8Error::Format("q8 group map checksum mismatch".into()));
        }
        let mut generation = [0u8; 16];
        generation.copy_from_slice(&bytes[24..40]);
        let output = Self {
            image,
            rows,
            group_count,
            generation,
            checksum,
        };
        if verify_payload && output.ids().any(|group| group as usize >= group_count) {
            return Err(Q8Error::Format("q8 group id is out of range".into()));
        }
        Ok(output)
    }

    pub fn rows(&self) -> usize {
        self.rows
    }

    pub fn group_count(&self) -> usize {
        self.group_count
    }

    pub fn generation(&self) -> [u8; 16] {
        self.generation
    }

    pub fn checksum(&self) -> u64 {
        self.checksum
    }

    pub fn verify_checksum(&self) -> Result<(), Q8Error> {
        if fnv64(&self.image.as_slice()[GROUP_HEADER_LEN..]) != self.checksum {
            return Err(Q8Error::Format("q8 group map checksum mismatch".into()));
        }
        Ok(())
    }

    fn ids(&self) -> impl Iterator<Item = u32> + '_ {
        self.image.as_slice()[GROUP_HEADER_LEN..]
            .chunks_exact(4)
            .map(|bytes| u32::from_le_bytes(bytes.try_into().unwrap()))
    }
}

#[derive(Clone, Copy, Debug)]
struct Candidate {
    score: f32,
    ordinal: u64,
}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.score.to_bits() == other.score.to_bits() && self.ordinal == other.ordinal
    }
}

impl Eq for Candidate {}

impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> Ordering {
        self.score
            .total_cmp(&other.score)
            .then_with(|| other.ordinal.cmp(&self.ordinal))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct GroupCandidate {
    head: Candidate,
    group: u32,
}

impl PartialOrd for GroupCandidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for GroupCandidate {
    fn cmp(&self, other: &Self) -> Ordering {
        self.head
            .cmp(&other.head)
            .then_with(|| other.group.cmp(&self.group))
    }
}

fn sorted_candidates(heap: BinaryHeap<Reverse<Candidate>>) -> Vec<(u64, f32)> {
    let mut output: Vec<_> = heap
        .into_iter()
        .map(|entry| (entry.0.ordinal, entry.0.score))
        .collect();
    output.sort_unstable_by(|left, right| {
        right
            .1
            .total_cmp(&left.1)
            .then_with(|| left.0.cmp(&right.0))
    });
    output
}

fn artifact_candidates(
    output_dir: &Path,
    legacy_name: &str,
    prefix: &str,
    suffix: &str,
) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let Ok(entries) = fs::read_dir(output_dir) else {
        return paths;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        if name == legacy_name || (name.starts_with(prefix) && name.ends_with(suffix)) {
            paths.push(path);
        }
    }
    paths.sort_unstable();
    paths
}

fn reusable_matrix_artifact(
    output_dir: &Path,
    generation_hex: &str,
    generation: [u8; 16],
    rows: usize,
    dim: usize,
) -> Option<(PathBuf, Q8Matrix)> {
    let legacy = format!("embeddings.{generation_hex}.q8");
    let prefix = format!("embeddings.{generation_hex}.");
    for path in artifact_candidates(output_dir, &legacy, &prefix, ".q8") {
        let Ok(matrix) = Q8Matrix::open(&path) else {
            continue;
        };
        if matrix.generation() == generation
            && matrix.rows() == rows
            && matrix.dim() == dim
            && matrix.verify_checksum().is_ok()
        {
            return Some((path, matrix));
        }
    }
    None
}

fn reusable_group_artifact(
    output_dir: &Path,
    generation_hex: &str,
    generation: [u8; 16],
    rows: usize,
    group_count: usize,
    checksum: u64,
) -> Option<(PathBuf, GroupMap)> {
    let legacy = format!("groups.{generation_hex}.{checksum:016x}.q8g");
    let prefix = format!("groups.{generation_hex}.{checksum:016x}.");
    for path in artifact_candidates(output_dir, &legacy, &prefix, ".q8g") {
        let Ok(groups) = GroupMap::open(&path) else {
            continue;
        };
        if groups.generation() == generation
            && groups.rows() == rows
            && groups.group_count() == group_count
            && groups.checksum() == checksum
            && groups.verify_checksum().is_ok()
        {
            return Some((path, groups));
        }
    }
    None
}

pub fn build(
    embeddings_path: impl AsRef<Path>,
    meta_path: impl AsRef<Path>,
    output_dir: impl AsRef<Path>,
) -> Result<BuildResult, Q8Error> {
    let embeddings_path = embeddings_path.as_ref();
    let meta_path = meta_path.as_ref();
    let output_dir = output_dir.as_ref();
    let meta_before = fs::read(meta_path)?;
    let meta: F32Meta = serde_json::from_slice(&meta_before)
        .map_err(|error| Q8Error::Format(format!("invalid f32 metadata: {error}")))?;
    if meta.commit.version != 1 || meta.dim == 0 || meta.dim > 16_384 {
        return Err(Q8Error::Format("unsupported f32 embedding commit".into()));
    }
    let generation = parse_generation(&meta.commit.generation)?;
    let row_bytes = u64::from(meta.dim) * 4;
    let expected_bytes = meta
        .commit
        .rows
        .checked_mul(row_bytes)
        .ok_or_else(|| Q8Error::Format("f32 matrix length overflow".into()))?;
    let actual_bytes = embeddings_path.metadata()?.len();
    if actual_bytes != expected_bytes || meta.commit.matrix.size != expected_bytes {
        return Err(Q8Error::Format(
            "f32 matrix does not match its commit".into(),
        ));
    }

    fs::create_dir_all(output_dir)?;
    if let Some((artifact, existing)) = reusable_matrix_artifact(
        output_dir,
        &meta.commit.generation,
        generation,
        meta.commit.rows as usize,
        meta.dim as usize,
    ) {
        if fs::read(meta_path)? == meta_before {
            return Ok(BuildResult {
                version: VERSION,
                score_kind: SCORE_KIND,
                f32_generation: meta.commit.generation,
                rows: meta.commit.rows,
                dim: meta.dim,
                artifact_size: existing.image.as_slice().len() as u64,
                artifact,
                checksum: format!("{:016x}", existing.checksum()),
            });
        }
    }

    let nonce = format!("{}.{}", std::process::id(), monotonic_nonce());
    let temp = output_dir.join(format!(".embeddings.{}.tmp", nonce));
    let result = build_temp(
        embeddings_path,
        &temp,
        meta.dim as usize,
        meta.commit.rows,
        generation,
    );
    if let Err(error) = result {
        let _ = fs::remove_file(&temp);
        return Err(error);
    }
    if fs::read(meta_path)? != meta_before {
        let _ = fs::remove_file(&temp);
        return Err(Q8Error::Format(
            "f32 generation moved while deriving q8 vectors".into(),
        ));
    }
    let artifact = output_dir.join(format!(
        "embeddings.{}.{}.q8",
        meta.commit.generation, nonce
    ));
    fs::rename(&temp, &artifact)?;
    let matrix = Q8Matrix::open(&artifact)?;
    matrix.verify_checksum()?;
    Ok(BuildResult {
        version: VERSION,
        score_kind: SCORE_KIND,
        f32_generation: meta.commit.generation,
        rows: meta.commit.rows,
        dim: meta.dim,
        artifact_size: matrix.image.as_slice().len() as u64,
        checksum: format!("{:016x}", matrix.checksum()),
        artifact,
    })
}

pub fn build_group_map(
    group_ids_path: impl AsRef<Path>,
    output_dir: impl AsRef<Path>,
    generation_hex: &str,
    rows: u64,
) -> Result<GroupBuildResult, Q8Error> {
    build_group_map_with_mode(
        group_ids_path.as_ref(),
        output_dir.as_ref(),
        generation_hex,
        rows,
        GroupInput::Labels,
    )
}

pub fn build_numeric_group_map(
    group_ids_path: impl AsRef<Path>,
    output_dir: impl AsRef<Path>,
    generation_hex: &str,
    rows: u64,
) -> Result<GroupBuildResult, Q8Error> {
    build_group_map_with_mode(
        group_ids_path.as_ref(),
        output_dir.as_ref(),
        generation_hex,
        rows,
        GroupInput::Numeric,
    )
}

#[derive(Clone, Copy)]
enum GroupInput {
    Labels,
    Numeric,
}

fn build_group_map_with_mode(
    group_ids_path: &Path,
    output_dir: &Path,
    generation_hex: &str,
    rows: u64,
    mode: GroupInput,
) -> Result<GroupBuildResult, Q8Error> {
    let generation = parse_generation(generation_hex)?;
    fs::create_dir_all(output_dir)?;
    let nonce = format!("{}.{}", std::process::id(), monotonic_nonce());
    let payload_path = output_dir.join(format!(".groups.{nonce}.payload.tmp"));
    let temp_path = output_dir.join(format!(".groups.{nonce}.tmp"));
    let result = build_group_payload(group_ids_path, &payload_path, rows, mode);
    let (group_count, checksum) = match result {
        Ok(value) => value,
        Err(error) => {
            let _ = fs::remove_file(&payload_path);
            return Err(error);
        }
    };
    if let Some((artifact, existing)) = reusable_group_artifact(
        output_dir,
        generation_hex,
        generation,
        rows as usize,
        group_count as usize,
        checksum,
    ) {
        let _ = fs::remove_file(&payload_path);
        return Ok(GroupBuildResult {
            version: GROUP_VERSION,
            generation: generation_hex.to_owned(),
            rows,
            groups: group_count,
            artifact_size: existing.image.as_slice().len() as u64,
            artifact,
            checksum: format!("{checksum:016x}"),
        });
    }
    let write_result = write_group_artifact(
        &payload_path,
        &temp_path,
        generation,
        rows,
        group_count,
        checksum,
    );
    let _ = fs::remove_file(&payload_path);
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temp_path);
        return Err(error);
    }
    let artifact = output_dir.join(format!(
        "groups.{generation_hex}.{checksum:016x}.{nonce}.q8g"
    ));
    fs::rename(&temp_path, &artifact)?;
    let groups = GroupMap::open(&artifact)?;
    groups.verify_checksum()?;
    Ok(GroupBuildResult {
        version: GROUP_VERSION,
        generation: generation_hex.to_owned(),
        rows,
        groups: group_count,
        artifact_size: groups.image.as_slice().len() as u64,
        artifact,
        checksum: format!("{checksum:016x}"),
    })
}

fn build_group_payload(
    group_ids_path: &Path,
    payload_path: &Path,
    rows: u64,
    mode: GroupInput,
) -> Result<(u32, u64), Q8Error> {
    let mut input = BufReader::with_capacity(1024 * 1024, File::open(group_ids_path)?);
    let mut output = BufWriter::with_capacity(1024 * 1024, File::create(payload_path)?);
    let mut labels = HashMap::<Vec<u8>, u32>::new();
    let mut max_numeric = None;
    let mut line = Vec::new();
    let mut checksum = FNV_OFFSET;
    for _ in 0..rows {
        line.clear();
        if input.read_until(b'\n', &mut line)? == 0 {
            return Err(Q8Error::Format("q8 group map has too few rows".into()));
        }
        if line.ends_with(b"\n") {
            line.pop();
        }
        if line.ends_with(b"\r") {
            line.pop();
        }
        if line.is_empty() || line.len() > 1024 * 1024 {
            return Err(Q8Error::Format("q8 group label is invalid".into()));
        }
        let group = match mode {
            GroupInput::Labels => {
                let next = u32::try_from(labels.len())
                    .map_err(|_| Q8Error::Format("q8 group count exceeds u32".into()))?;
                *labels.entry(line.clone()).or_insert(next)
            }
            GroupInput::Numeric => {
                if line.len() > 10 || line.iter().any(|byte| !byte.is_ascii_digit()) {
                    return Err(Q8Error::Format("q8 numeric group id is invalid".into()));
                }
                let value = std::str::from_utf8(&line)
                    .ok()
                    .and_then(|text| text.parse::<u32>().ok())
                    .ok_or_else(|| Q8Error::Format("q8 numeric group id is invalid".into()))?;
                max_numeric = Some(max_numeric.map_or(value, |prior: u32| prior.max(value)));
                value
            }
        };
        let raw = group.to_le_bytes();
        output.write_all(&raw)?;
        checksum = fnv64_extend(checksum, &raw);
    }
    line.clear();
    if input.read_until(b'\n', &mut line)? != 0 {
        return Err(Q8Error::Format("q8 group map has too many rows".into()));
    }
    output.flush()?;
    output.get_ref().sync_all()?;
    let group_count = match mode {
        GroupInput::Labels => u32::try_from(labels.len())
            .map_err(|_| Q8Error::Format("q8 group count exceeds u32".into()))?,
        GroupInput::Numeric => max_numeric
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| Q8Error::Format("q8 numeric group count exceeds u32".into()))?,
    };
    Ok((group_count, checksum))
}

fn write_group_artifact(
    payload_path: &Path,
    output_path: &Path,
    generation: [u8; 16],
    rows: u64,
    groups: u32,
    checksum: u64,
) -> Result<(), Q8Error> {
    let mut output = BufWriter::with_capacity(1024 * 1024, File::create(output_path)?);
    let mut header = [0u8; GROUP_HEADER_LEN];
    header[..4].copy_from_slice(&GROUP_MAGIC);
    header[4..8].copy_from_slice(&GROUP_VERSION.to_le_bytes());
    header[12..16].copy_from_slice(&groups.to_le_bytes());
    header[16..24].copy_from_slice(&rows.to_le_bytes());
    header[24..40].copy_from_slice(&generation);
    header[40..44].copy_from_slice(&4u32.to_le_bytes());
    header[48..56].copy_from_slice(&checksum.to_le_bytes());
    output.write_all(&header)?;
    let mut payload = BufReader::with_capacity(1024 * 1024, File::open(payload_path)?);
    io::copy(&mut payload, &mut output)?;
    output.flush()?;
    output.get_ref().sync_all()?;
    Ok(())
}

fn build_temp(
    embeddings_path: &Path,
    output_path: &Path,
    dim: usize,
    rows: u64,
    generation: [u8; 16],
) -> Result<(), Q8Error> {
    struct RemoveOnDrop(PathBuf);
    impl Drop for RemoveOnDrop {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }

    let mut input = BufReader::with_capacity(1024 * 1024, File::open(embeddings_path)?);
    let mut payload_path = output_path.to_path_buf();
    payload_path.set_extension("payload.tmp");
    let _payload_guard = RemoveOnDrop(payload_path.clone());
    let mut payload = BufWriter::with_capacity(1024 * 1024, File::create(&payload_path)?);
    let mut raw = vec![0u8; dim * 4];
    let mut vector = vec![0f32; dim];
    let mut checksum = FNV_OFFSET;
    for _ in 0..rows {
        input.read_exact(&mut raw)?;
        for (slot, bytes) in vector.iter_mut().zip(raw.chunks_exact(4)) {
            *slot = f32::from_le_bytes(bytes.try_into().unwrap());
        }
        let (quantized, inv_norm) = quantize(&vector)?;
        let norm_bytes = inv_norm.to_le_bytes();
        payload.write_all(&norm_bytes)?;
        checksum = fnv64_extend(checksum, &norm_bytes);
        let quantized_bytes =
            unsafe { std::slice::from_raw_parts(quantized.as_ptr().cast::<u8>(), quantized.len()) };
        payload.write_all(quantized_bytes)?;
        checksum = fnv64_extend(checksum, quantized_bytes);
    }
    if input.read(&mut [0u8; 1])? != 0 {
        return Err(Q8Error::Format("f32 matrix has trailing bytes".into()));
    }
    payload.flush()?;
    payload.get_ref().sync_all()?;

    let stride =
        u32::try_from(dim + 4).map_err(|_| Q8Error::Format("q8 row stride overflow".into()))?;
    let mut output = BufWriter::with_capacity(1024 * 1024, File::create(output_path)?);
    let mut header = [0u8; HEADER_LEN];
    header[..4].copy_from_slice(&MAGIC);
    header[4..8].copy_from_slice(&VERSION.to_le_bytes());
    header[8..12].copy_from_slice(&(dim as u32).to_le_bytes());
    header[12..16].copy_from_slice(&FLAGS.to_le_bytes());
    header[16..24].copy_from_slice(&rows.to_le_bytes());
    header[24..40].copy_from_slice(&generation);
    header[40..44].copy_from_slice(&stride.to_le_bytes());
    header[48..56].copy_from_slice(&checksum.to_le_bytes());
    output.write_all(&header)?;
    let mut payload = BufReader::with_capacity(1024 * 1024, File::open(&payload_path)?);
    io::copy(&mut payload, &mut output)?;
    output.flush()?;
    output.get_ref().sync_all()?;
    Ok(())
}

fn quantize(vector: &[f32]) -> Result<(Vec<i8>, f32), Q8Error> {
    let mut max_abs = 0.0f32;
    for value in vector {
        if !value.is_finite() {
            return Err(Q8Error::Format(
                "embedding contains a non-finite value".into(),
            ));
        }
        max_abs = max_abs.max(value.abs());
    }
    if max_abs == 0.0 {
        return Ok((vec![0; vector.len()], 0.0));
    }
    let scale = 127.0 / max_abs;
    let quantized: Vec<i8> = vector
        .iter()
        .map(|value| (value * scale).round().clamp(-127.0, 127.0) as i8)
        .collect();
    let norm_sq: i64 = quantized
        .iter()
        .map(|value| i64::from(*value) * i64::from(*value))
        .sum();
    Ok((quantized, 1.0 / (norm_sq as f32).sqrt()))
}

pub fn dot_scalar(left: &[i8], right: &[i8]) -> i32 {
    left.iter()
        .zip(right)
        .map(|(a, b)| i32::from(*a) * i32::from(*b))
        .sum()
}

pub fn dot(left: &[i8], right: &[i8]) -> i32 {
    debug_assert_eq!(left.len(), right.len());
    selected_dot()(left, right)
}

type DotProduct = fn(&[i8], &[i8]) -> i32;

fn selected_dot() -> DotProduct {
    #[cfg(target_arch = "x86_64")]
    if std::arch::is_x86_feature_detected!("avx2") {
        return dot_avx2_safe;
    }
    #[cfg(target_arch = "aarch64")]
    if std::arch::is_aarch64_feature_detected!("neon") {
        return dot_neon_safe;
    }
    dot_scalar
}

#[cfg(target_arch = "x86_64")]
fn dot_avx2_safe(left: &[i8], right: &[i8]) -> i32 {
    unsafe { dot_avx2(left, right) }
}

#[cfg(target_arch = "aarch64")]
fn dot_neon_safe(left: &[i8], right: &[i8]) -> i32 {
    unsafe { dot_neon(left, right) }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn dot_avx2(left: &[i8], right: &[i8]) -> i32 {
    use std::arch::x86_64::*;
    let mut sum = _mm256_setzero_si256();
    let mut offset = 0usize;
    while offset + 32 <= left.len() {
        let a = _mm256_loadu_si256(left.as_ptr().add(offset).cast());
        let b = _mm256_loadu_si256(right.as_ptr().add(offset).cast());
        let a_low = _mm256_cvtepi8_epi16(_mm256_castsi256_si128(a));
        let b_low = _mm256_cvtepi8_epi16(_mm256_castsi256_si128(b));
        let a_high = _mm256_cvtepi8_epi16(_mm256_extracti128_si256(a, 1));
        let b_high = _mm256_cvtepi8_epi16(_mm256_extracti128_si256(b, 1));
        sum = _mm256_add_epi32(sum, _mm256_madd_epi16(a_low, b_low));
        sum = _mm256_add_epi32(sum, _mm256_madd_epi16(a_high, b_high));
        offset += 32;
    }
    let high = _mm256_extracti128_si256(sum, 1);
    let low = _mm256_castsi256_si128(sum);
    let mut total = _mm_add_epi32(low, high);
    total = _mm_hadd_epi32(total, total);
    total = _mm_hadd_epi32(total, total);
    _mm_cvtsi128_si32(total) + dot_scalar(&left[offset..], &right[offset..])
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn dot_neon(left: &[i8], right: &[i8]) -> i32 {
    use std::arch::aarch64::*;
    let mut sum0 = vdupq_n_s32(0);
    let mut sum1 = vdupq_n_s32(0);
    let mut sum2 = vdupq_n_s32(0);
    let mut sum3 = vdupq_n_s32(0);
    let mut offset = 0usize;
    while offset + 64 <= left.len() {
        for (lane, sum) in [
            (0usize, &mut sum0),
            (16, &mut sum1),
            (32, &mut sum2),
            (48, &mut sum3),
        ] {
            let a = vld1q_s8(left.as_ptr().add(offset + lane));
            let b = vld1q_s8(right.as_ptr().add(offset + lane));
            *sum = vpadalq_s16(*sum, vmull_s8(vget_low_s8(a), vget_low_s8(b)));
            *sum = vpadalq_s16(*sum, vmull_s8(vget_high_s8(a), vget_high_s8(b)));
        }
        offset += 64;
    }
    while offset + 16 <= left.len() {
        let a = vld1q_s8(left.as_ptr().add(offset));
        let b = vld1q_s8(right.as_ptr().add(offset));
        sum0 = vpadalq_s16(sum0, vmull_s8(vget_low_s8(a), vget_low_s8(b)));
        sum0 = vpadalq_s16(sum0, vmull_s8(vget_high_s8(a), vget_high_s8(b)));
        offset += 16;
    }
    let sum = vaddq_s32(vaddq_s32(sum0, sum1), vaddq_s32(sum2, sum3));
    vaddvq_s32(sum) + dot_scalar(&left[offset..], &right[offset..])
}

fn parse_generation(value: &str) -> Result<[u8; 16], Q8Error> {
    if value.len() != 32 {
        return Err(Q8Error::Format(
            "embedding generation is not 16-byte hex".into(),
        ));
    }
    let mut output = [0u8; 16];
    for (index, byte) in output.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .map_err(|_| Q8Error::Format("embedding generation is not hex".into()))?;
    }
    Ok(output)
}

fn fnv64(bytes: &[u8]) -> u64 {
    fnv64_extend(FNV_OFFSET, bytes)
}

fn fnv64_extend(mut hash: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

fn monotonic_nonce() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

    static TEMP_TREE_NONCE: AtomicU64 = AtomicU64::new(0);

    struct TempTree(PathBuf);

    impl TempTree {
        fn new() -> Self {
            loop {
                let nonce = TEMP_TREE_NONCE.fetch_add(1, AtomicOrdering::Relaxed);
                let path = std::env::temp_dir()
                    .join(format!("agrep-q8-segments-{}.{nonce}", std::process::id()));
                match fs::create_dir(&path) {
                    Ok(()) => return Self(path),
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                    Err(error) => panic!("failed to create {}: {error}", path.display()),
                }
            }
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn artifact(rows: &[Vec<f32>], generation: [u8; 16]) -> Vec<u8> {
        let dim = rows.first().map_or(3, Vec::len);
        let stride = dim + 4;
        let mut payload = Vec::new();
        for row in rows {
            let (quantized, inv_norm) = quantize(row).unwrap();
            payload.extend_from_slice(&inv_norm.to_le_bytes());
            payload.extend(quantized.iter().map(|value| *value as u8));
        }
        let mut bytes = vec![0u8; HEADER_LEN];
        bytes[..4].copy_from_slice(&MAGIC);
        bytes[4..8].copy_from_slice(&VERSION.to_le_bytes());
        bytes[8..12].copy_from_slice(&(dim as u32).to_le_bytes());
        bytes[12..16].copy_from_slice(&FLAGS.to_le_bytes());
        bytes[16..24].copy_from_slice(&(rows.len() as u64).to_le_bytes());
        bytes[24..40].copy_from_slice(&generation);
        bytes[40..44].copy_from_slice(&(stride as u32).to_le_bytes());
        bytes[48..56].copy_from_slice(&fnv64(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        bytes
    }

    fn group_artifact(groups: &[u32], group_count: u32, generation: [u8; 16]) -> Vec<u8> {
        let payload: Vec<u8> = groups
            .iter()
            .flat_map(|group| group.to_le_bytes())
            .collect();
        let mut bytes = vec![0u8; GROUP_HEADER_LEN];
        bytes[..4].copy_from_slice(&GROUP_MAGIC);
        bytes[4..8].copy_from_slice(&GROUP_VERSION.to_le_bytes());
        bytes[12..16].copy_from_slice(&group_count.to_le_bytes());
        bytes[16..24].copy_from_slice(&(groups.len() as u64).to_le_bytes());
        bytes[24..40].copy_from_slice(&generation);
        bytes[40..44].copy_from_slice(&4u32.to_le_bytes());
        bytes[48..56].copy_from_slice(&fnv64(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        bytes
    }

    fn write_segment(
        root: &Path,
        name: &str,
        rows: &[Vec<f32>],
        groups: &[u32],
        group_count: u32,
        generation: [u8; 16],
    ) -> (String, String) {
        let matrix_name = format!("{name}.q8");
        let group_name = format!("{name}.q8g");
        fs::write(root.join(&matrix_name), artifact(rows, generation)).unwrap();
        fs::write(
            root.join(&group_name),
            group_artifact(groups, group_count, generation),
        )
        .unwrap();
        (matrix_name, group_name)
    }

    fn write_set(root: &Path, record: serde_json::Value) -> PathBuf {
        let path = root.join("segments.json");
        fs::write(&path, serde_json::to_vec(&record).unwrap()).unwrap();
        path
    }

    fn generation_hex(byte: u8) -> String {
        format!("{byte:02x}").repeat(16)
    }

    #[test]
    fn scores_preserve_all_rows_and_generation() {
        let generation = [7u8; 16];
        let matrix = Q8Matrix::parse(artifact(
            &[
                vec![1.0, 0.0, 0.0],
                vec![0.0, 1.0, 0.0],
                vec![1.0, 1.0, 0.0],
            ],
            generation,
        ))
        .unwrap();
        let scores = matrix.scores(&[1.0, 0.0, 0.0]).unwrap();
        assert_eq!(matrix.generation(), generation);
        assert_eq!(scores.len(), 3);
        assert!((scores[0] - 1.0).abs() < 1e-6);
        assert!(scores[1].abs() < 1e-6);
        assert!((scores[2] - std::f32::consts::FRAC_1_SQRT_2).abs() < 0.01);
    }

    #[test]
    fn top_scores_match_full_scores_with_stable_ties() {
        let matrix = Q8Matrix::parse(artifact(
            &[
                vec![1.0, 0.0, 0.0],
                vec![0.0, 1.0, 0.0],
                vec![1.0, 1.0, 0.0],
                vec![1.0, 0.0, 0.0],
            ],
            [8u8; 16],
        ))
        .unwrap();
        let scores = matrix.scores(&[1.0, 0.0, 0.0]).unwrap();
        let top = matrix.top_scores(&[1.0, 0.0, 0.0], 3).unwrap();
        let mut expected: Vec<_> = scores.into_iter().enumerate().collect();
        expected.sort_unstable_by(|left, right| {
            right
                .1
                .total_cmp(&left.1)
                .then_with(|| left.0.cmp(&right.0))
        });
        assert_eq!(
            top,
            expected[..3]
                .iter()
                .map(|(ordinal, score)| (*ordinal as u64, *score))
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn eligibility_filters_flat_and_grouped_candidates_before_selection() {
        let generation = [81u8; 16];
        let matrix = Q8Matrix::parse(artifact(
            &[
                vec![1.0, 0.0, 0.0],
                vec![0.0, 1.0, 0.0],
                vec![1.0, 1.0, 0.0],
                vec![1.0, 0.0, 0.0],
            ],
            generation,
        ))
        .unwrap();
        let groups = GroupMap::parse(group_artifact(&[0, 0, 1, 2], 3, generation)).unwrap();
        let eligible = [0b0000_1010];
        assert_eq!(
            matrix
                .top_scores_eligible(&[1.0, 0.0, 0.0], 4, &eligible)
                .unwrap()
                .iter()
                .map(|row| row.0)
                .collect::<Vec<_>>(),
            [3, 1]
        );
        assert_eq!(
            matrix
                .top_group_scores_eligible(&groups, &[1.0, 0.0, 0.0], 3, 2, &eligible,)
                .unwrap()
                .iter()
                .map(|row| row.0)
                .collect::<Vec<_>>(),
            [3, 1]
        );
        assert!(matrix
            .top_scores_eligible(&[1.0, 0.0, 0.0], 1, &[0b0001_0000])
            .is_err());
    }

    #[test]
    fn grouped_scores_return_one_head_per_generic_group() {
        let generation = [9u8; 16];
        let matrix = Q8Matrix::parse(artifact(
            &[
                vec![1.0, 0.0, 0.0],
                vec![0.0, 1.0, 0.0],
                vec![1.0, 1.0, 0.0],
                vec![1.0, 2.0, 0.0],
            ],
            generation,
        ))
        .unwrap();
        let groups = GroupMap::parse(group_artifact(&[0, 0, 1, 2], 3, generation)).unwrap();
        let full = matrix.scores(&[1.0, 0.0, 0.0]).unwrap();
        let top = matrix
            .top_group_scores(&groups, &[1.0, 0.0, 0.0], 3, 1)
            .unwrap();
        assert_eq!(top.iter().map(|row| row.0).collect::<Vec<_>>(), [0, 2, 3]);
        for (ordinal, score) in top {
            assert_eq!(score, full[ordinal as usize]);
        }
    }

    #[test]
    fn grouped_scores_return_ragged_multiple_heads() {
        let generation = [10u8; 16];
        let matrix = Q8Matrix::parse(artifact(
            &[
                vec![1.0, 0.0, 0.0],
                vec![0.0, 1.0, 0.0],
                vec![1.0, 1.0, 0.0],
                vec![1.0, 2.0, 0.0],
            ],
            generation,
        ))
        .unwrap();
        let groups = GroupMap::parse(group_artifact(&[0, 0, 1, 2], 3, generation)).unwrap();
        let full = matrix.scores(&[1.0, 0.0, 0.0]).unwrap();
        let top = matrix
            .top_group_scores(&groups, &[1.0, 0.0, 0.0], 2, 2)
            .unwrap();
        assert_eq!(top.iter().map(|row| row.0).collect::<Vec<_>>(), [0, 2, 1]);
        for (ordinal, score) in top {
            assert_eq!(score, full[ordinal as usize]);
        }
    }

    #[test]
    fn segment_set_matches_monolith_for_flat_and_grouped_top_k() {
        let tree = TempTree::new();
        let first_rows = vec![vec![1.0, 0.0, 0.0], vec![0.0, 1.0, 0.0]];
        let second_rows = vec![vec![1.0, 1.0, 0.0], vec![1.0, 0.0, 0.0]];
        let (first_q8, first_groups) =
            write_segment(tree.path(), "first", &first_rows, &[0, 1], 2, [21; 16]);
        let (second_q8, second_groups) =
            write_segment(tree.path(), "second", &second_rows, &[0, 2], 3, [22; 16]);
        let path = write_set(
            tree.path(),
            serde_json::json!({
                "version": 1,
                "generation": generation_hex(42),
                "dim": 3,
                "row_high_water": 4,
                "live_rows": 4,
                "group_count": 3,
                "segments": [
                    {"row_base": 0, "rows": 2, "artifact": first_q8, "groups": first_groups},
                    {"row_base": 2, "rows": 2, "artifact": second_q8, "groups": second_groups}
                ],
                "shadows": []
            }),
        );
        let set = SegmentSet::open(path).unwrap();
        let all_rows: Vec<_> = first_rows.into_iter().chain(second_rows).collect();
        let monolith = Q8Matrix::parse(artifact(&all_rows, [23; 16])).unwrap();
        let monolith_groups = GroupMap::parse(group_artifact(&[0, 1, 0, 2], 3, [23; 16])).unwrap();
        let query = [1.0, 0.0, 0.0];
        assert_eq!(
            set.scores(&query).unwrap(),
            monolith.scores(&query).unwrap()
        );
        assert_eq!(
            set.top_scores(&query, 3).unwrap(),
            monolith.top_scores(&query, 3).unwrap()
        );
        assert_eq!(
            set.top_group_scores(&query, 3, 2).unwrap(),
            monolith
                .top_group_scores(&monolith_groups, &query, 3, 2)
                .unwrap()
        );
    }

    #[test]
    fn segment_shadows_remove_stale_high_rows_before_ranking() {
        let tree = TempTree::new();
        let (old_q8, old_groups) = write_segment(
            tree.path(),
            "old",
            &[vec![1.0, 0.0, 0.0]],
            &[0],
            1,
            [31; 16],
        );
        let (new_q8, new_groups) = write_segment(
            tree.path(),
            "new",
            &[vec![0.9, 0.1, 0.0], vec![0.0, 1.0, 0.0]],
            &[0, 1],
            2,
            [32; 16],
        );
        fs::write(tree.path().join("dead.rows"), 0u64.to_le_bytes()).unwrap();
        let path = write_set(
            tree.path(),
            serde_json::json!({
                "version": 1,
                "generation": generation_hex(43),
                "dim": 3,
                "row_high_water": 7,
                "live_rows": 2,
                "group_count": 2,
                "segments": [
                    {"row_base": 0, "rows": 1, "artifact": old_q8, "groups": old_groups},
                    {"row_base": 5, "rows": 2, "artifact": new_q8, "groups": new_groups}
                ],
                "shadows": [{"path": "dead.rows", "rows": 1}]
            }),
        );
        let set = SegmentSet::open(path).unwrap();
        let scores = set.scores(&[1.0, 0.0, 0.0]).unwrap();
        assert_eq!(scores.len(), 7);
        assert_eq!(scores[0], f32::MIN);
        assert_eq!(set.top_scores(&[1.0, 0.0, 0.0], 1).unwrap()[0].0, 5);
        let eligibility = [0b0100_0001];
        assert_eq!(
            set.top_scores_eligible(&[1.0, 0.0, 0.0], 2, &eligibility)
                .unwrap()
                .iter()
                .map(|row| row.0)
                .collect::<Vec<_>>(),
            [6]
        );
        assert_eq!(
            set.top_group_scores_eligible(&[1.0, 0.0, 0.0], 2, 2, &eligibility)
                .unwrap()
                .iter()
                .map(|row| row.0)
                .collect::<Vec<_>>(),
            [6]
        );
    }

    #[test]
    fn grouped_heads_merge_one_family_across_segments() {
        let tree = TempTree::new();
        let (first_q8, first_groups) = write_segment(
            tree.path(),
            "family-a",
            &[vec![1.0, 0.0, 0.0], vec![0.7, 0.3, 0.0]],
            &[7, 8],
            9,
            [51; 16],
        );
        let (second_q8, second_groups) = write_segment(
            tree.path(),
            "family-b",
            &[vec![0.9, 0.1, 0.0], vec![0.6, 0.4, 0.0]],
            &[7, 9],
            10,
            [52; 16],
        );
        let path = write_set(
            tree.path(),
            serde_json::json!({
                "version": 1,
                "generation": generation_hex(44),
                "dim": 3,
                "row_high_water": 12,
                "live_rows": 4,
                "group_count": 10,
                "segments": [
                    {"row_base": 0, "rows": 2, "artifact": first_q8, "groups": first_groups},
                    {"row_base": 10, "rows": 2, "artifact": second_q8, "groups": second_groups}
                ],
                "shadows": []
            }),
        );
        let set = SegmentSet::open(path).unwrap();
        let result = set.top_group_scores(&[1.0, 0.0, 0.0], 1, 2).unwrap();
        assert_eq!(
            result.iter().map(|value| value.0).collect::<Vec<_>>(),
            [0, 10]
        );
    }

    #[test]
    fn segment_set_rejects_overlaps_and_invalid_shadows() {
        let tree = TempTree::new();
        let (first_q8, first_groups) = write_segment(
            tree.path(),
            "invalid-a",
            &[vec![1.0, 0.0, 0.0], vec![0.0, 1.0, 0.0]],
            &[0, 1],
            2,
            [61; 16],
        );
        let (second_q8, second_groups) = write_segment(
            tree.path(),
            "invalid-b",
            &[vec![1.0, 1.0, 0.0], vec![0.0, 0.0, 1.0]],
            &[0, 1],
            2,
            [62; 16],
        );
        let overlap = write_set(
            tree.path(),
            serde_json::json!({
                "version": 1,
                "generation": generation_hex(45),
                "dim": 3,
                "row_high_water": 4,
                "live_rows": 4,
                "group_count": 2,
                "segments": [
                    {"row_base": 0, "rows": 2, "artifact": first_q8, "groups": first_groups},
                    {"row_base": 1, "rows": 2, "artifact": second_q8, "groups": second_groups}
                ],
                "shadows": []
            }),
        );
        assert!(SegmentSet::open(overlap).is_err());

        let mut shadows = Vec::new();
        shadows.extend_from_slice(&1u64.to_le_bytes());
        shadows.extend_from_slice(&0u64.to_le_bytes());
        fs::write(tree.path().join("unsorted.rows"), shadows).unwrap();
        let invalid_shadow = write_set(
            tree.path(),
            serde_json::json!({
                "version": 1,
                "generation": generation_hex(46),
                "dim": 3,
                "row_high_water": 4,
                "live_rows": 2,
                "group_count": 2,
                "segments": [
                    {"row_base": 0, "rows": 2, "artifact": "invalid-a.q8", "groups": "invalid-a.q8g"},
                    {"row_base": 2, "rows": 2, "artifact": "invalid-b.q8", "groups": "invalid-b.q8g"}
                ],
                "shadows": [{"path": "unsorted.rows", "rows": 2}]
            }),
        );
        assert!(SegmentSet::open(invalid_shadow).is_err());
    }

    #[test]
    fn numeric_group_builder_preserves_global_ids() {
        let tree = TempTree::new();
        let input = tree.path().join("groups.txt");
        fs::write(&input, b"7\n2\n7\n").unwrap();
        let result = build_numeric_group_map(&input, tree.path(), &generation_hex(47), 3).unwrap();
        assert_eq!(result.groups, 8);
        let groups = GroupMap::open(result.artifact).unwrap();
        assert_eq!(groups.ids().collect::<Vec<_>>(), [7, 2, 7]);
    }

    #[test]
    fn corruption_is_rejected() {
        let clean = artifact(&[vec![1.0, 2.0, 3.0]], [1u8; 16]);
        for mutate in [4usize, HEADER_LEN + 2] {
            let mut broken = clean.clone();
            broken[mutate] ^= 0x40;
            assert!(Q8Matrix::parse(broken).is_err());
        }
        let mut short = clean;
        short.pop();
        assert!(Q8Matrix::parse(short).is_err());
    }

    #[test]
    fn simd_matches_scalar_oracle() {
        let mut left = Vec::new();
        let mut right = Vec::new();
        let mut state = 0x1234_5678u32;
        for _ in 0..1027 {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            left.push(((state >> 24) as i8).clamp(-127, 127));
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            right.push(((state >> 24) as i8).clamp(-127, 127));
        }
        assert_eq!(dot(&left, &right), dot_scalar(&left, &right));
    }
}
