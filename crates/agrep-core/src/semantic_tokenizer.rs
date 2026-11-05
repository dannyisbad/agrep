//! Tokenizers for the embedding model. Bulk embedding builds the library's BPE model
//! from JSON. Queries read the same BPE tables, written once by explicit provisioning,
//! without building the library's 50,000-entry maps in every process; the library still
//! normalizes, pre-tokenizes, handles added tokens and applies the template.
use anyhow::{anyhow, bail, Context, Result};
use std::{
    collections::{BinaryHeap, HashMap},
    fs,
    path::{Path, PathBuf},
};
use tokenizers::{
    models::bpe::{BpeTrainer, BPE},
    AddedToken, DecoderWrapper, Model, NormalizerWrapper, PaddingParams, PostProcessorWrapper,
    PreTokenizerWrapper, Token, Tokenizer, TokenizerBuilder, TokenizerImpl, TruncationParams,
};

/// Every field the library's tokenizer deserializer reads.
#[derive(serde::Deserialize)]
struct Parts<M> {
    version: Option<String>,
    truncation: Option<TruncationParams>,
    padding: Option<PaddingParams>,
    // Recorded ids only feed the library's mismatch warnings.
    #[serde(default)]
    added_tokens: Vec<AddedToken>,
    normalizer: Option<NormalizerWrapper>,
    pre_tokenizer: Option<PreTokenizerWrapper>,
    model: Option<M>,
    decoder: Option<DecoderWrapper>,
    post_processor: Option<PostProcessorWrapper>,
}

type Pipeline<M> =
    TokenizerImpl<M, NormalizerWrapper, PreTokenizerWrapper, PostProcessorWrapper, DecoderWrapper>;

/// The steps of the library's own visitor, around a model built separately.
fn build<M: Model, X>(parts: Parts<X>, model: M) -> Result<Pipeline<M>> {
    if parts.version.as_deref().is_some_and(|v| v != "1.0") {
        bail!("unknown tokenizer version");
    }
    let mut tokenizer = TokenizerBuilder::new()
        .with_truncation(parts.truncation)
        .with_padding(parts.padding)
        .with_normalizer(parts.normalizer)
        .with_pre_tokenizer(parts.pre_tokenizer)
        .with_model(model)
        .with_decoder(parts.decoder)
        .with_post_processor(parts.post_processor)
        .build()
        .map_err(|e| anyhow!("{e}"))?;
    tokenizer.add_tokens(&parts.added_tokens);
    Ok(tokenizer)
}

/// The tokenizer `Tokenizer::from_file` builds, without its buffered untagged pass
/// over the whole model: the BPE model goes straight through its own deserializer.
pub(crate) fn load(path: &Path) -> Result<Tokenizer> {
    let mut parts: Parts<BPE> = serde_json::from_slice(&fs::read(path)?)?;
    let model = parts.model.take().context("tokenizer has no model")?;
    Ok(Tokenizer::from(build(
        parts,
        tokenizers::ModelWrapper::BPE(model),
    )?))
}

/// Whether `load` rebuilds exactly the tokenizer `Tokenizer::from_file` reads from
/// `path`, compared through the library's own serialization.
#[doc(hidden)]
pub fn parity(path: &Path) -> Result<bool> {
    let reference = Tokenizer::from_file(path).map_err(|e| anyhow!("{e}"))?;
    let loaded = load(path)?;
    let text = |t: &Tokenizer| t.to_string(false).map_err(|e| anyhow!("{e}"));
    Ok(text(&reference)? == text(&loaded)?)
}

const MAGIC: &[u8; 8] = b"AGRPBPE1";

/// Writes the query tokenizer for `source`: its JSON without the model, then the
/// vocabulary in id order, the ids in byte order, and the merge map the library's
/// `BpeBuilder` builds, in pair order. Only plain BPE options are supported.
pub(crate) fn write_query(source: &Path, dest: &Path) -> Result<()> {
    let mut json: serde_json::Value = serde_json::from_slice(&fs::read(source)?)?;
    let model = json
        .as_object_mut()
        .and_then(|o| o.remove("model"))
        .context("tokenizer has no model")?;
    let off = |key: &str| model[key].as_bool().is_none_or(|v| !v);
    if model["type"] != "BPE"
        || !model["dropout"].is_null()
        || !model["unk_token"].is_null()
        || !model["continuing_subword_prefix"].is_null()
        || !model["end_of_word_suffix"].is_null()
        || !off("fuse_unk")
        || !off("byte_fallback")
        || !off("ignore_merges")
    {
        bail!("unsupported tokenizer model options");
    }
    let vocab = model["vocab"].as_object().context("tokenizer vocabulary")?;
    let mut tokens = vec![None; vocab.len()];
    for (token, id) in vocab {
        let id = id.as_u64().context("vocabulary id")? as usize;
        if tokens
            .get_mut(id)
            .is_none_or(|slot| slot.replace(token).is_some())
        {
            bail!("vocabulary ids are not dense and unique");
        }
    }
    let tokens: Vec<&String> = tokens.into_iter().flatten().collect();
    let ids: HashMap<&str, u32> = tokens
        .iter()
        .enumerate()
        .map(|(id, t)| (t.as_str(), id as u32))
        .collect();
    let id = |t: &str| {
        ids.get(t)
            .copied()
            .with_context(|| format!("merge token {t} not in vocabulary"))
    };
    // As in `BpeBuilder::build`, a repeated pair keeps its last rank.
    let mut merges = HashMap::new();
    for (rank, merge) in model["merges"]
        .as_array()
        .context("merges")?
        .iter()
        .enumerate()
    {
        let (Some(a), Some(b)) = (merge[0].as_str(), merge[1].as_str()) else {
            bail!("merges must be token pairs");
        };
        merges.insert((id(a)?, id(b)?), (rank as u32, id(&format!("{a}{b}"))?));
    }
    let mut merges: Vec<_> = merges.into_iter().collect();
    merges.sort_unstable();
    let mut sorted: Vec<u32> = (0..tokens.len() as u32).collect();
    sorted.sort_unstable_by_key(|&id| tokens[id as usize].as_bytes());
    let header = serde_json::to_vec(&json)?;
    let mut out = Vec::new();
    let u32_of = |n: usize| u32::try_from(n).context("tokenizer table too large");
    out.extend_from_slice(MAGIC);
    for n in [header.len(), tokens.len(), merges.len()] {
        out.extend(u32_of(n)?.to_le_bytes());
    }
    out.extend(&header);
    let mut offset = 0;
    out.extend(0u32.to_le_bytes());
    for token in &tokens {
        offset += token.len();
        out.extend(u32_of(offset)?.to_le_bytes());
    }
    for id in sorted {
        out.extend(id.to_le_bytes());
    }
    for ((a, b), (rank, new)) in merges {
        for value in [a, b, rank, new] {
            out.extend(value.to_le_bytes());
        }
    }
    for token in tokens {
        out.extend(token.as_bytes());
    }
    let temp = dest.with_added_extension("partial");
    fs::write(&temp, &out)?;
    fs::OpenOptions::new().write(true).open(&temp)?.sync_all()?;
    fs::rename(temp, dest)?;
    Ok(())
}

/// The library's BPE model over tables read from `write_query`'s file: lookups are
/// binary searches instead of hash maps built at load.
pub struct QueryBpe {
    data: Vec<u8>,
    vocab: usize,
    merges: usize,
    offsets: usize,
    sorted: usize,
    pairs: usize,
    blob: usize,
}

pub(crate) type QueryTokenizer = Pipeline<QueryBpe>;

pub(crate) fn load_query(path: &Path) -> Result<QueryTokenizer> {
    let data = fs::read(path)?;
    let invalid = || anyhow!("invalid query tokenizer: {}", path.display());
    let word = |at: usize| -> Result<usize> {
        let bytes = data.get(at..at + 4).ok_or_else(invalid)?;
        Ok(u32::from_le_bytes(bytes.try_into().unwrap()) as usize)
    };
    if data.get(..8) != Some(MAGIC.as_slice()) {
        return Err(invalid());
    }
    let (header, vocab, merges) = (word(8)?, word(12)?, word(16)?);
    let offsets = 20 + header;
    let sorted = offsets + 4 * (vocab + 1);
    let pairs = sorted + 4 * vocab;
    let blob = pairs + 16 * merges;
    if blob > data.len() || word(offsets)? != 0 {
        return Err(invalid());
    }
    let mut last = 0;
    for i in 1..=vocab {
        let end = word(offsets + 4 * i)?;
        if end < last {
            return Err(invalid());
        }
        last = end;
    }
    if blob + last != data.len()
        || (0..vocab).any(|i| word(sorted + 4 * i).is_ok_and(|id| id >= vocab))
        || (0..merges).any(|i| {
            [0, 1, 3]
                .iter()
                .any(|k| word(pairs + 16 * i + 4 * k).is_ok_and(|id| id >= vocab))
        })
    {
        return Err(invalid());
    }
    let parts: Parts<serde::de::IgnoredAny> = serde_json::from_slice(&data[20..offsets])?;
    let model = QueryBpe {
        data,
        vocab,
        merges,
        offsets,
        sorted,
        pairs,
        blob,
    };
    build(parts, model)
}

impl QueryBpe {
    fn word(&self, at: usize) -> u32 {
        u32::from_le_bytes(self.data[at..at + 4].try_into().unwrap())
    }
    fn token(&self, id: u32) -> &[u8] {
        let at = self.offsets + 4 * id as usize;
        &self.data[self.blob + self.word(at) as usize..self.blob + self.word(at + 4) as usize]
    }
    fn id(&self, token: &[u8]) -> Option<u32> {
        let (mut low, mut high) = (0, self.vocab);
        while low < high {
            let mid = (low + high) / 2;
            let id = self.word(self.sorted + 4 * mid);
            match self.token(id).cmp(token) {
                std::cmp::Ordering::Less => low = mid + 1,
                std::cmp::Ordering::Greater => high = mid,
                std::cmp::Ordering::Equal => return Some(id),
            }
        }
        None
    }
    /// (rank, new id) of merging `a` with `b`.
    fn merge(&self, a: u32, b: u32) -> Option<(u32, u32)> {
        let (mut low, mut high) = (0, self.merges);
        while low < high {
            let mid = (low + high) / 2;
            let at = self.pairs + 16 * mid;
            match (self.word(at), self.word(at + 4)).cmp(&(a, b)) {
                std::cmp::Ordering::Less => low = mid + 1,
                std::cmp::Ordering::Greater => high = mid,
                std::cmp::Ordering::Equal => return Some((self.word(at + 8), self.word(at + 12))),
            }
        }
        None
    }
    fn string(&self, id: u32) -> tokenizers::Result<String> {
        Ok(String::from_utf8(self.token(id).to_vec())?)
    }
}

#[derive(Clone, Copy)]
struct Symbol {
    id: u32,
    prev: isize,
    next: isize,
    len: usize,
}

/// The library `Word`'s pending merge: lowest rank first, then leftmost.
#[derive(PartialEq, Eq)]
struct Merge {
    pos: usize,
    rank: u32,
    id: u32,
}
impl Ord for Merge {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        other.rank.cmp(&self.rank).then(other.pos.cmp(&self.pos))
    }
}
impl PartialOrd for Merge {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Model for QueryBpe {
    type Trainer = BpeTrainer;
    /// `BPE::tokenize` without dropout, prefixes, suffixes, unknown or byte-fallback
    /// tokens: characters outside the vocabulary are dropped, then `Word::merge_all`.
    fn tokenize(&self, sequence: &str) -> tokenizers::Result<Vec<Token>> {
        let mut symbols: Vec<Symbol> = Vec::with_capacity(sequence.len());
        for (at, c) in sequence.char_indices() {
            let text = &sequence[at..at + c.len_utf8()];
            if let Some(id) = self.id(text.as_bytes()) {
                let index = symbols.len() as isize;
                if let Some(last) = symbols.last_mut() {
                    last.next = index;
                }
                symbols.push(Symbol {
                    id,
                    prev: index - 1,
                    next: -1,
                    len: text.len(),
                });
            }
        }
        let mut queue: BinaryHeap<Merge> = symbols
            .windows(2)
            .enumerate()
            .filter_map(|(pos, pair)| {
                let (rank, id) = self.merge(pair[0].id, pair[1].id)?;
                Some(Merge { pos, rank, id })
            })
            .collect();
        while let Some(top) = queue.pop() {
            if symbols[top.pos].len == 0 || symbols[top.pos].next == -1 {
                continue;
            }
            let next_pos = symbols[top.pos].next as usize;
            let right = symbols[next_pos];
            // A merge queued before a neighbour changed is stale.
            if self
                .merge(symbols[top.pos].id, right.id)
                .is_none_or(|(_, id)| id != top.id)
            {
                continue;
            }
            let current = &mut symbols[top.pos];
            current.id = top.id;
            current.len += right.len;
            current.next = right.next;
            symbols[next_pos].len = 0;
            if right.next > -1 && (right.next as usize) < symbols.len() {
                symbols[right.next as usize].prev = top.pos as isize;
            }
            let current = symbols[top.pos];
            if current.prev >= 0 {
                let prev = current.prev as usize;
                if let Some((rank, id)) = self.merge(symbols[prev].id, current.id) {
                    queue.push(Merge {
                        pos: prev,
                        rank,
                        id,
                    });
                }
            }
            let next = current.next as usize;
            if next < symbols.len() {
                if let Some((rank, id)) = self.merge(current.id, symbols[next].id) {
                    queue.push(Merge {
                        pos: top.pos,
                        rank,
                        id,
                    });
                }
            }
        }
        let mut start = 0;
        symbols
            .into_iter()
            .filter(|s| s.len != 0)
            .map(|s| {
                let offsets = (start, start + s.len);
                start += s.len;
                Ok(Token::new(s.id, self.string(s.id)?, offsets))
            })
            .collect()
    }
    fn token_to_id(&self, token: &str) -> Option<u32> {
        self.id(token.as_bytes())
    }
    fn id_to_token(&self, id: u32) -> Option<String> {
        (id < self.vocab as u32)
            .then(|| self.string(id).ok())
            .flatten()
    }
    fn get_vocab(&self) -> HashMap<String, u32> {
        (0..self.vocab as u32)
            .filter_map(|id| Some((self.string(id).ok()?, id)))
            .collect()
    }
    fn get_vocab_size(&self) -> usize {
        self.vocab
    }
    fn save(&self, _: &Path, _: Option<&str>) -> tokenizers::Result<Vec<PathBuf>> {
        Err("the query tokenizer is read-only".into())
    }
    fn get_trainer(&self) -> BpeTrainer {
        BpeTrainer::default()
    }
}

/// Compares the query tokenizer in `model_dir` with the library tokenizer read from
/// its `tokenizer-compact.json`: vocabulary, and the ids and offsets of `texts` and of every
/// vocabulary entry's decoded text. Returns the number of compared encodings.
#[doc(hidden)]
pub fn query_parity(model_dir: &Path, texts: &[String]) -> Result<usize> {
    let reference = Tokenizer::from_file(model_dir.join("tokenizer-compact.json"))
        .map_err(|e| anyhow!("{e}"))?;
    let query = load_query(&model_dir.join("tokenizer-query.bin"))?;
    let size = reference.get_vocab_size(true);
    if query.get_vocab_size(true) != size {
        bail!("query tokenizer vocabulary size differs");
    }
    let mut inputs = texts.to_vec();
    for id in 0..size as u32 {
        let Some(token) = reference.id_to_token(id) else {
            continue;
        };
        if query.token_to_id(&token) != reference.token_to_id(&token) {
            bail!("query tokenizer id of vocabulary entry {id} differs");
        }
        inputs.push(reference.decode(&[id], false).map_err(|e| anyhow!("{e}"))?);
    }
    for (i, text) in inputs.iter().enumerate() {
        let (a, b) = (
            reference
                .encode(text.as_str(), true)
                .map_err(|e| anyhow!("{e}"))?,
            query
                .encode(text.as_str(), true)
                .map_err(|e| anyhow!("{e}"))?,
        );
        if a.get_ids() != b.get_ids() || a.get_offsets() != b.get_offsets() {
            bail!("query tokenizer differs on input {i}");
        }
    }
    Ok(inputs.len())
}

/// The tokenizer an embedder uses: the library's for bulk embedding, the query
/// tables for queries.
pub(crate) enum TextTokenizer {
    Library(Box<Tokenizer>),
    Query(Box<QueryTokenizer>),
}
impl TextTokenizer {
    pub(crate) fn ids(&self, text: &str) -> Result<Vec<u32>> {
        let encoding = match self {
            Self::Library(t) => t.encode(text, true),
            Self::Query(t) => t.encode(text, true),
        };
        Ok(encoding.map_err(|e| anyhow!("{e}"))?.get_ids().to_vec())
    }
    /// Right truncation at `max_length` tokens including CLS/SEP, no padding.
    pub(crate) fn window(&mut self, max_length: usize) -> Result<()> {
        let truncation = Some(TruncationParams {
            max_length,
            ..Default::default()
        });
        match self {
            Self::Library(t) => {
                t.with_truncation(truncation).map_err(|e| anyhow!("{e}"))?;
                t.with_padding(None);
            }
            Self::Query(t) => {
                t.with_truncation(truncation).map_err(|e| anyhow!("{e}"))?;
                t.with_padding(None);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A byte-level BPE in the pinned file's shape: every byte symbol, merges that
    /// chain, repeat a pair and reach one token through two pairs, special and
    /// normalized non-special added tokens, NFC and a CLS/SEP template.
    fn fixture() -> serde_json::Value {
        let mut alphabet: Vec<char> = tokenizers::pre_tokenizers::byte_level::ByteLevel::alphabet()
            .into_iter()
            .collect();
        alphabet.sort();
        let mut vocab = serde_json::Map::new();
        vocab.insert("[CLS]".into(), 0.into());
        vocab.insert("[SEP]".into(), 1.into());
        for c in &alphabet {
            vocab.insert(c.to_string(), (vocab.len() as u32).into());
        }
        let merges = [
            ("Ġ", "t"),
            ("h", "e"),
            ("Ġt", "he"),
            ("l", "o"),
            ("Ġ", "lo"),
            ("a", "b"),
            ("b", "c"),
            ("ab", "c"),
            ("a", "bc"),
            ("l", "o"),
            ("a", "a"),
            ("aa", "a"),
        ];
        for (a, b) in merges {
            let token = format!("{a}{b}");
            if !vocab.contains_key(&token) {
                vocab.insert(token, (vocab.len() as u32).into());
            }
        }
        let spaces = vocab.len() as u32;
        vocab.insert("ĠĠ".into(), spaces.into());
        let special = |id: u32, content: &str| serde_json::json!({"id":id,"content":content,"single_word":false,"lstrip":false,"rstrip":false,"normalized":false,"special":true});
        let byte_level = serde_json::json!({"type":"ByteLevel","add_prefix_space":false,"trim_offsets":true,"use_regex":true});
        serde_json::json!({
            "version":"1.0","truncation":null,"padding":null,
            "added_tokens":[special(0,"[CLS]"),special(1,"[SEP]"),
                {"id":spaces,"content":"  ","single_word":false,"lstrip":false,"rstrip":false,"normalized":true,"special":false}],
            "normalizer":{"type":"NFC"},
            "pre_tokenizer":byte_level,
            "post_processor":{"type":"TemplateProcessing",
                "single":[{"SpecialToken":{"id":"[CLS]","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"[SEP]","type_id":0}}],
                "pair":[{"SpecialToken":{"id":"[CLS]","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"[SEP]","type_id":0}},{"Sequence":{"id":"B","type_id":0}},{"SpecialToken":{"id":"[SEP]","type_id":0}}],
                "special_tokens":{"[CLS]":{"id":"[CLS]","ids":[0],"tokens":["[CLS]"]},"[SEP]":{"id":"[SEP]","ids":[1],"tokens":["[SEP]"]}}},
            "decoder":byte_level,
            "model":{"type":"BPE","dropout":null,"unk_token":null,"continuing_subword_prefix":null,"end_of_word_suffix":null,
                "fuse_unk":false,"byte_fallback":false,"ignore_merges":false,"vocab":vocab,
                "merges":merges.iter().map(|(a, b)| [a, b]).collect::<Vec<_>>()},
        })
    }

    #[test]
    fn direct_and_query_tokenizers_rebuild_the_library_tokenizer() {
        let t = tempfile::tempdir().unwrap();
        let file = fixture();
        fs::write(
            t.path().join("tokenizer-compact.json"),
            serde_json::to_vec(&file).unwrap(),
        )
        .unwrap();
        assert!(parity(&t.path().join("tokenizer-compact.json")).unwrap());
        write_query(
            &t.path().join("tokenizer-compact.json"),
            &t.path().join("tokenizer-query.bin"),
        )
        .unwrap();
        let mut texts: Vec<String> = [
            "the hello",
            "  two  spaces [SEP] inside",
            "e\u{301}t\u{e9} [CLS]",
            "",
            "abc aabc abcabc aaaa aaaaa lololo",
        ]
        .map(String::from)
        .to_vec();
        // Pseudo-random words over symbols the merges combine.
        let mut state = 0x9e37_79b9u32;
        for _ in 0..300 {
            let mut text = String::new();
            for _ in 0..(state % 12) as usize + 1 {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                text.push(
                    ['a', 'b', 'c', 'l', 'o', 'h', 'e', 't', ' ', 'é'][(state % 10) as usize],
                );
            }
            texts.push(text);
        }
        let compared = query_parity(t.path(), &texts).unwrap();
        assert!(compared > texts.len());
        let reference = Tokenizer::from_file(t.path().join("tokenizer-compact.json")).unwrap();
        let loaded = load(&t.path().join("tokenizer-compact.json")).unwrap();
        for text in &texts {
            let (a, b) = (
                reference.encode(text.as_str(), true).unwrap(),
                loaded.encode(text.as_str(), true).unwrap(),
            );
            assert_eq!(a.get_ids(), b.get_ids(), "{text:?}");
            assert_eq!(a.get_tokens(), b.get_tokens(), "{text:?}");
            assert_eq!(a.get_offsets(), b.get_offsets(), "{text:?}");
        }
        let mut other = file.clone();
        other["version"] = "2.0".into();
        fs::write(
            t.path().join("tokenizer-compact.json"),
            serde_json::to_vec(&other).unwrap(),
        )
        .unwrap();
        assert!(load(&t.path().join("tokenizer-compact.json")).is_err());
    }

    #[test]
    fn query_tables_refuse_unsupported_models_and_damaged_files() {
        let t = tempfile::tempdir().unwrap();
        let (source, dest) = (t.path().join("tokenizer.json"), t.path().join("q.bin"));
        let mut file = fixture();
        file["model"]["unk_token"] = "[CLS]".into();
        fs::write(&source, serde_json::to_vec(&file).unwrap()).unwrap();
        assert!(write_query(&source, &dest).is_err());
        fs::write(&source, serde_json::to_vec(&fixture()).unwrap()).unwrap();
        write_query(&source, &dest).unwrap();
        let bytes = fs::read(&dest).unwrap();
        assert!(load_query(&dest).is_ok());
        fs::write(&dest, &bytes[..bytes.len() - 1]).unwrap();
        assert!(load_query(&dest).is_err());
        let mut damaged = bytes.clone();
        damaged[0] ^= 1;
        fs::write(&dest, &damaged).unwrap();
        assert!(load_query(&dest).is_err());
    }
}
