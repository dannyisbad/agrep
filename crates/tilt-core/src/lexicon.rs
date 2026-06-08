//! The vocabulary of a frustrated developer. Pure data; the automaton is built in `scan`.
//!
//! Two signals come out of these tables:
//!   VULGARITY = severity-weighted profanity (exact; this is what regex/aho-corasick is good at)
//!   RAGE      = frustration x blame x intensity, amplified by profanity, knocked down by hype
//!
//! The lexical RAGE here is the tier-0 heuristic. The GPU sidecar (ModernBERT-GoEmotions gate
//! -> Qwen judge) replaces it on `--deep`; vulgarity stays lexical because it's a lexical fact.

/// Profanity, severity-weighted. Drives the vulgarity meter AND acts as the LLM-judge gate.
pub static SEVERITY: &[(&str, u8)] = &[
    ("motherfucker", 4), ("motherfucking", 4), ("motherfuckers", 4), ("clusterfuck", 4),
    ("fuck", 3), ("fucking", 3), ("fucked", 3), ("fuckin", 3), ("fucks", 3),
    ("fucker", 3), ("fuckers", 3), ("fuckup", 3), ("fuckups", 3), ("fuckery", 3),
    ("fuckall", 3), ("bullshit", 3), ("asshole", 3), ("assholes", 3), ("dumbass", 3),
    ("jackass", 3), ("dipshit", 3), ("dickhead", 3), ("dogshit", 3), ("goddammit", 3),
    ("jfc", 3), ("shitshow", 3), ("horseshit", 3),
    ("shit", 2), ("shitty", 2), ("shite", 2), ("bitch", 2), ("bitching", 2),
    ("bastard", 2), ("pissed", 2), ("pissing", 2), ("goddamn", 2), ("wtf", 2),
    ("ffs", 2), ("omfg", 2), ("stfu", 2), ("prick", 2), ("bollocks", 2), ("fml", 2),
    ("arse", 2), ("twat", 2), ("wanker", 2),
    ("damn", 1), ("dammit", 1), ("damnit", 1), ("hell", 1), ("crap", 1), ("crappy", 1),
    ("ass", 1), ("piss", 1), ("freaking", 1), ("frickin", 1), ("frick", 1),
    ("frigging", 1), ("darn", 1), ("heck", 1), ("bloody", 1),
];

/// Non-profane negativity. Feeds RAGE, not vulgarity. Weighted.
pub static FRUSTRATION: &[(&str, f32)] = &[
    ("broken", 1.5), ("broke", 1.2), ("wrong", 1.2), ("fail", 1.2), ("failed", 1.2),
    ("failing", 1.2), ("fails", 1.2), ("useless", 1.5), ("garbage", 1.5), ("trash", 1.3),
    ("nonsense", 1.4), ("ridiculous", 1.5), ("stupid", 1.3), ("dumb", 1.2), ("idiot", 1.5),
    ("idiotic", 1.5), ("terrible", 1.3), ("awful", 1.3), ("horrible", 1.3), ("mess", 1.1),
    ("messed", 1.2), ("hate", 1.5), ("annoying", 1.4), ("frustrating", 1.6),
    ("frustrated", 1.6), ("worse", 1.1), ("regression", 1.4), ("buggy", 1.0),
    ("stuck", 1.1), ("nope", 0.8), ("ugh", 1.2), ("argh", 1.4), ("smh", 1.0),
    ("sigh", 0.9), ("why", 0.5), ("again", 0.5), ("still", 0.5), ("keep", 0.5),
    ("keeps", 0.6), ("seriously", 0.8), ("redo", 0.7), ("revert", 0.6), ("undo", 0.6),
    ("cmon", 1.0), ("jesus", 0.8), ("christ", 0.9), ("disaster", 1.4), ("clueless", 1.4),
    ("lazy", 1.1), ("facepalm", 1.2),
];

/// Directed-at-the-agent blame phrases. "YOU did this." Multi-word; strong rage signal.
pub static BLAME: &[&str] = &[
    "you broke", "you keep", "you always", "you still", "you never", "you ruined",
    "you were supposed", "you said", "you literally", "you completely",
    "i told you", "i said", "i asked", "didnt ask", "did you even", "did you not",
    "didnt you", "why did you", "what did you", "stop doing", "stop changing",
    "stop adding", "stop removing", "you made it worse", "are you serious",
    "are you kidding", "pay attention", "read the", "i never said", "listen to me",
];

/// Positivity / hype. Knocks rage down so "sick as fuck" isn't logged as anger.
pub static POSITIVE: &[(&str, f32)] = &[
    ("awesome", 1.4), ("amazing", 1.4), ("perfect", 1.4), ("love", 1.3), ("lovely", 1.2),
    ("beautiful", 1.4), ("gorgeous", 1.4), ("clean", 1.0), ("great", 1.0), ("nice", 0.9),
    ("good", 0.7), ("brilliant", 1.4), ("genius", 1.4), ("elegant", 1.3), ("slick", 1.2),
    ("sick", 1.2), ("dope", 1.3), ("lit", 1.1), ("fire", 1.1), ("lfg", 1.6), ("yay", 1.2),
    ("woo", 1.2), ("gg", 1.2), ("goated", 1.5), ("banger", 1.4), ("based", 1.1),
    ("wonderful", 1.3), ("excellent", 1.3), ("fantastic", 1.4), ("smooth", 1.0),
    ("crisp", 1.0), ("thanks", 0.8), ("thank", 0.8), ("appreciate", 1.0), ("yess", 1.0),
    ("yesss", 1.3),
];

/// Hype phrases — profanity used as excitement, not anger. Treated as POSITIVE weight.
pub static POSITIVE_PHRASES: &[&str] = &[
    "fuck yeah", "fuck yes", "hell yeah", "hell yes", "fucking love", "fucking awesome",
    "fucking great", "fucking perfect", "fucking beautiful", "fucking sick",
    "fucking nice", "fucking works", "fucking brilliant", "fucking clean", "so good",
    "so clean", "lets go", "lets fucking go", "chefs kiss", "damn good", "damn nice",
    "sick as fuck", "love it", "love this", "nailed it", "good shit", "hot shit",
];
