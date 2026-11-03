//! Shared storage and evidence representation of a tool event.
use crate::model::Event;
pub fn format(event: &Event) -> (String, Option<usize>) {
    let mut head = match event.kind {
        "subagent_start" => format!("subagent {}", event.name.trim()),
        "subagent_result" => format!("subagent result {}", event.name.trim()),
        _ => event.name.trim().to_owned(),
    };
    if event.ok == Some(false) {
        head.push_str(" [failed]");
    }
    let input = event.input.trim();
    let mut text = if head.is_empty() {
        input.to_owned()
    } else if input.is_empty() {
        head
    } else {
        format!("{head}: {input}")
    };
    let separator = (!event.output.is_empty()).then_some(text.chars().count());
    if !event.output.is_empty() {
        text.push('\n');
        text.push_str(&event.output);
    }
    (text.trim().to_owned(), separator)
}
pub fn split(
    text: &str,
    kind: &str,
    name: &str,
    ok: Option<bool>,
    input_end: Option<usize>,
    output_start: Option<usize>,
) -> (String, String) {
    // Offsets count characters; slicing at their byte positions copies once.
    let at = |chars: usize| {
        text.char_indices()
            .nth(chars)
            .map_or(text.len(), |(i, _)| i)
    };
    let head = &text[..input_end.map_or(text.len(), at)];
    let mut prefix = match kind {
        "subagent_start" => format!("subagent {}", name.trim()),
        "subagent_result" => format!("subagent result {}", name.trim()),
        _ => name.trim().to_owned(),
    };
    if ok == Some(false) {
        prefix.push_str(" [failed]");
    }
    let input = if head == prefix {
        String::new()
    } else {
        head.strip_prefix(&(prefix + ": "))
            .unwrap_or(head)
            .to_owned()
    };
    let output = output_start.map_or_else(String::new, |start| text[at(start)..].to_owned());
    (input, output)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn character_offsets_split_like_collected_characters() {
        fn collected(
            text: &str,
            kind: &str,
            name: &str,
            ok: Option<bool>,
            input_end: Option<usize>,
            output_start: Option<usize>,
        ) -> (String, String) {
            let head: String = text.chars().take(input_end.unwrap_or(usize::MAX)).collect();
            let mut prefix = match kind {
                "subagent_start" => format!("subagent {}", name.trim()),
                "subagent_result" => format!("subagent result {}", name.trim()),
                _ => name.trim().to_owned(),
            };
            if ok == Some(false) {
                prefix.push_str(" [failed]");
            }
            let input = if head == prefix {
                String::new()
            } else {
                head.strip_prefix(&(prefix + ": "))
                    .unwrap_or(&head)
                    .to_owned()
            };
            let output =
                output_start.map_or_else(String::new, |start| text.chars().skip(start).collect());
            (input, output)
        }
        let texts = [
            String::new(),
            "shell".into(),
            "shell [failed]".into(),
            "shell: ls -la café\n界 output 🙂 done".into(),
            "subagent review: plan\nresult é".into(),
            "é".repeat(40),
        ];
        for text in &texts {
            let length = text.chars().count();
            let offsets: Vec<_> = (0..=length + 2).map(Some).chain([None]).collect();
            for kind in ["tool", "subagent_start"] {
                for (name, ok) in [
                    ("shell", Some(true)),
                    ("shell", Some(false)),
                    (" review ", None),
                ] {
                    for input_end in &offsets {
                        for output_start in &offsets {
                            assert_eq!(
                                split(text, kind, name, ok, *input_end, *output_start),
                                collected(text, kind, name, ok, *input_end, *output_start),
                                "{text:?} {input_end:?} {output_start:?}"
                            );
                        }
                    }
                }
            }
        }
    }
}
